"""ページごとの測定値から、白紙・重複・向きの直し方を決める（純粋関数）。

I/O は行わない。測定は呼び出し側が済ませ、ここは `edit_pdf` に渡す operations と、
ログに残す判定理由だけを返す。自動で直すのは確かなものに限り、曖昧なものは `review`
にして人の確認に回す。
"""

from __future__ import annotations

import difflib
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class PageInfo:
    """1 ページ分の測定値。"""

    number: int  # 1 始まり
    ink_ratio: float  # 0.0〜1.0
    text: str  # そのページの pdftotext の結果
    dhash: int  # 64 ビット
    rotate: int | None  # 正立にするため時計回りに回す角度。判定できなければ None
    rotate_confidence: float


@dataclass(frozen=True)
class Thresholds:
    """判定のしきい値。実際のスキャンで調整する前提で、ここ 1 か所にまとめる。"""

    blank_ink_max: float = 0.003
    blank_ink_review_max: float = 0.01
    blank_text_max: int = 5  # 空白を除いた文字数
    blank_text_review_max: int = 30
    dup_hash_max: int = 4  # dHash のハミング距離
    dup_hash_review_max: int = 10
    dup_text_ratio_min: float = 0.95  # SequenceMatcher の ratio（空白を除いた文字列）
    dup_text_min_chars: int = 20  # 両ページともこれ以上の文字があるときだけ自動で削除
    rotate_confidence_min: float = 5.0
    rotate_confidence_review_min: float = 1.0
    max_deletions: int = 2


@dataclass(frozen=True)
class PageAction:
    page: int
    action: str  # "delete" | "rotate" | "review"
    reason: str  # 例 "白紙", "重複（3 ページ目と）", "向き"
    detail: str  # 判定値。例 "ink=0.0012 text=0"


@dataclass(frozen=True)
class PagePlan:
    # 変更が無ければ None。あれば edit_pdf にそのまま渡す
    operations: list[dict[str, int]] | None
    actions: tuple[PageAction, ...]
    review: bool


def _squash(text: str) -> str:
    """空白をすべて取り除く。"""
    return "".join(text.split())


DEFAULT_THRESHOLDS = Thresholds()


def plan_pages(pages: Sequence[PageInfo], t: Thresholds = DEFAULT_THRESHOLDS) -> PagePlan:
    """各ページの処置を決め、残すページの operations と判定理由を返す。"""
    texts = {p.number: _squash(p.text) for p in pages}
    # ページ番号 -> 処置の並び。最後にページ順へ並べて出す
    per_page: dict[int, list[PageAction]] = {p.number: [] for p in pages}
    candidates: dict[int, PageAction] = {}  # 削除候補（理由は安全策で取り消すときも使う）
    kept: PageInfo | None = None  # 直前の残すページ

    for p in pages:
        chars = len(texts[p.number])
        blank_detail = f"ink={p.ink_ratio:.4f} text={chars}"

        if p.ink_ratio <= t.blank_ink_max and chars <= t.blank_text_max:
            candidates[p.number] = PageAction(p.number, "delete", "白紙", blank_detail)
            continue
        if p.ink_ratio <= t.blank_ink_review_max and chars <= t.blank_text_review_max:
            per_page[p.number].append(PageAction(p.number, "review", "白紙", blank_detail))
        elif kept is not None:
            dist = (p.dhash ^ kept.dhash).bit_count()
            if dist <= t.dup_hash_review_max:
                reason = f"重複（{kept.number} ページ目と）"
                a, b = texts[kept.number], texts[p.number]
                ratio = difflib.SequenceMatcher(None, a, b).ratio() if a and b else 0.0
                detail = f"dist={dist} ratio={ratio:.2f}"
                if (
                    dist <= t.dup_hash_max
                    and len(a) >= t.dup_text_min_chars
                    and len(b) >= t.dup_text_min_chars
                    and ratio >= t.dup_text_ratio_min
                ):
                    candidates[p.number] = PageAction(p.number, "delete", reason, detail)
                    continue
                per_page[p.number].append(PageAction(p.number, "review", reason, detail))

        kept = p

    # 安全策: 1 ページの書類、上限超え、全ページ削除では削除せず確認に回す
    if candidates and (
        len(pages) == 1 or len(candidates) > t.max_deletions or len(candidates) == len(pages)
    ):
        for n, a in candidates.items():
            per_page[n].append(PageAction(n, "review", a.reason, a.detail))
        candidates = {}
    else:
        for n, a in candidates.items():
            per_page[n].append(a)

    rotations: dict[int, int] = {}
    for p in pages:
        if p.number in candidates or not p.rotate:
            continue
        detail = f"rotate={p.rotate} conf={p.rotate_confidence:.1f}"
        if p.rotate_confidence >= t.rotate_confidence_min:
            rotations[p.number] = p.rotate
            per_page[p.number].append(PageAction(p.number, "rotate", "向き", detail))
        elif p.rotate_confidence >= t.rotate_confidence_review_min:
            per_page[p.number].append(PageAction(p.number, "review", "向き", detail))

    actions = tuple(a for n in sorted(per_page) for a in per_page[n])
    operations: list[dict[str, int]] | None = None
    if candidates or rotations:
        operations = []
        for p in pages:
            if p.number in candidates:
                continue
            op = {"page": p.number}
            if p.number in rotations:
                op["rotate"] = rotations[p.number]
            operations.append(op)
    return PagePlan(operations, actions, any(a.action == "review" for a in actions))
