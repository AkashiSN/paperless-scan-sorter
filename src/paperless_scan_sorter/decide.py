"""判定結果から、書き戻す値と付け替えるタグを決める。"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from paperless_scan_sorter.paperless import Document, Named
from paperless_scan_sorter.validate import CORRESPONDENT_MAX, Classification, sanitize_component


@dataclass(frozen=True)
class TagIds:
    """状態管理用タグの ID。"""

    inbox: int
    ai_todo: int
    ai_done: int
    ai_review: int
    ai_failed: int


@dataclass(frozen=True)
class Decision:
    """書き戻す内容。None は変更しない。str は新規作成する名前、int は既存の ID。"""

    title: str | None
    correspondent: int | str | None
    document_type: int | str | None
    created: date | None
    tags: frozenset[int]
    review: bool
    reasons: tuple[str, ...]


def _choose(
    current: int | None,
    proposed: str | None,
    named: Sequence[Named],
    label: str,
    new_name: Callable[[str], str | None],
) -> tuple[int | str | None, str | None]:
    """発行元・文書タイプ 1 項目の書き戻し値と、review にする理由（無ければ None）を返す。

    既存の名前とは判定結果をそのまま照合する。一致が無く新しく作るときだけ
    new_name で作る名前に直し、None になれば作らずに採用できない扱いにする。
    """
    if current is not None:
        # 既に設定された値は上書きしない。判定が食い違うときだけ人に見てもらう。
        if proposed is None:
            return None, None
        existing = next((n.name for n in named if n.id == current), None)
        if existing != proposed:
            return None, f"{label}が自動マッチと食い違う"
        return None, None
    if proposed is None:
        return None, f"{label}を採用できない"
    for n in named:
        if n.name == proposed:
            return n.id, None
    name = new_name(proposed)
    if name is None:
        return None, f"{label}を採用できない"
    return name, f"{label}を新規作成"


def _correspondent_name(name: str) -> str | None:
    """発行元はパスに入るので、作る前に無害化と長さ制限を通す。"""
    return sanitize_component(name, CORRESPONDENT_MAX)


def _document_type_name(name: str) -> str | None:
    """文書タイプはパスに入らないので、検証済みの値をそのまま使う。"""
    return name


def decide(
    doc: Document,
    c: Classification,
    *,
    correspondents: Sequence[Named],
    document_types: Sequence[Named],
    tag_names: Mapping[str, int],
    ids: TagIds,
    extra_review_reasons: Sequence[str] = (),
) -> Decision:
    """書類と検証済みの判定結果から、書き戻す内容を決める。

    extra_review_reasons は判定結果の外から分かる確認待ちの理由で、他の理由の後ろに
    足し、1 つでもあれば確認待ちにする。
    """
    reasons: list[str] = []

    correspondent, reason = _choose(
        doc.correspondent, c.correspondent, correspondents, "発行元", _correspondent_name
    )
    if reason:
        reasons.append(reason)
    document_type, reason = _choose(
        doc.document_type, c.document_type, document_types, "文書タイプ", _document_type_name
    )
    if reason:
        reasons.append(reason)
    if c.title is None:
        reasons.append("タイトルを採用できない")
    if not c.confident:
        reasons.append("自信なし")
    reasons.extend(extra_review_reasons)

    review = bool(reasons)
    # PATCH の tags は置き換えなので、既存のタグに足した全体を返す。
    tags = set(doc.tags) | {tag_names[n] for n in c.tags if n in tag_names}
    tags.discard(ids.ai_todo)
    if review:
        tags.add(ids.ai_review)
    else:
        tags.add(ids.ai_done)
        tags.discard(ids.inbox)

    return Decision(
        title=c.title,
        correspondent=correspondent,
        document_type=document_type,
        created=c.created,
        tags=frozenset(tags),
        review=review,
        reasons=tuple(reasons),
    )


def failed_tags(doc: Document, ids: TagIds) -> frozenset[int]:
    """失敗時に送るタグ。ai-todo を外して ai-failed を足し、inbox は残す。"""
    return (doc.tags - {ids.ai_todo}) | {ids.ai_failed}
