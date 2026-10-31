"""Claude の出力の検証と、パスに入る値の無害化。

書類の本文も Claude の出力も信頼できない入力として扱う。ここを通った値だけを
Paperless に書き戻す。
"""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Collection
from dataclasses import dataclass
from datetime import date

from paperless_scan_sorter.errors import DocumentFailure
from paperless_scan_sorter.tags import STATE_TAGS

TITLE_MAX = 60
CORRESPONDENT_MAX = 30
TAGS_MAX = 3
# 1 ページの書き起こしと、印字されたページ番号の長さの上限。超えた分は切り捨てる。
PAGE_TEXT_MAX = 20000
PAGE_LABEL_MAX = 20
# created として採用する範囲。これ以前（同日を含む）は捨てる。
_CREATED_YEARS_BACK = 30
# 名前から除く Unicode の一般カテゴリ。制御文字（Cc）、ゼロ幅文字や双方向制御などの
# 書式文字（Cf）、行区切り（Zl）、段落区切り（Zp）は見た目に出ず、名前を偽装できる。
_REMOVED_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})
# 書き起こしの本文では、改行とタブだけは制御文字でも残す。
_KEPT_IN_TEXT = frozenset("\n\t")


@dataclass(frozen=True)
class Classification:
    """検証を通した分類結果。

    title・correspondent・document_type が None のときは「不採用」を表し、
    呼び出し側は確認待ちに回す。correspondent は書類から分からないときも None になる。

    title はパス用に無害化済み。correspondent と document_type は既存の名前と
    そのまま照合できるよう制御文字と前後の空白だけを除いてある。correspondent を
    新しく作るときは、呼び出し側が sanitize_component で無害化する。
    """

    title: str | None
    correspondent: str | None
    document_type: str | None
    tags: tuple[str, ...]
    created: date | None
    confident: bool


@dataclass(frozen=True)
class PageText:
    """検証を通した 1 ページの書き起こし。

    page は読ませた PDF の中でのページ番号（1 から）。label はページに印字された
    ページ番号で、無ければ None。
    """

    page: int
    text: str
    label: str | None


def _strip_edges(value: str) -> str:
    """前後の空白とピリオドを削る。"""
    start, end = 0, len(value)
    while start < end and (value[start].isspace() or value[start] == "."):
        start += 1
    while end > start and (value[end - 1].isspace() or value[end - 1] == "."):
        end -= 1
    return value[start:end]


def _remove_controls(value: str) -> str:
    return "".join(ch for ch in value if unicodedata.category(ch) not in _REMOVED_CATEGORIES)


def _clean_name(value: str) -> str | None:
    """制御文字と前後の空白を除く。空になれば None。"""
    return _remove_controls(value).strip() or None


def _clean_text(value: str) -> str:
    """書き起こしの本文から、改行とタブ以外の制御文字・書式文字・区切りを除く。"""
    value = value.replace("\r\n", "\n")
    return "".join(
        ch
        for ch in value
        if ch in _KEPT_IN_TEXT or unicodedata.category(ch) not in _REMOVED_CATEGORIES
    )


def sanitize_component(value: str, max_len: int) -> str | None:
    """パスの一部になる値から危険な文字を除き、長さを制限する。

    スラッシュ・バックスラッシュ・制御文字・書式文字・行と段落の区切りを除き、前後の空白とピリオドを削って
    max_len 文字に切り詰める。切り詰めで新しく末尾に出た空白やピリオドも削る。
    空、または ".." だけになるときは None を返す。
    """
    cleaned = "".join(ch for ch in _remove_controls(value) if ch not in "/\\")
    result = _strip_edges(_strip_edges(cleaned)[:max_len])
    if not result or set(result) == {"."}:
        return None
    return result


def _earliest_exclusive(today: date) -> date:
    """30 年前の同日。2 月 29 日は 2 月 28 日にそろえる。"""
    year = today.year - _CREATED_YEARS_BACK
    if today.month == 2 and today.day == 29:
        return date(year, 2, 28)
    return today.replace(year=year)


def _parse_created(value: str | None, today: date) -> date | None:
    if value is None:
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    if parsed > today or parsed <= _earliest_exclusive(today):
        return None
    return parsed


def _extract_object(text: str) -> dict[str, object]:
    start = text.find("{")
    if start < 0:
        raise DocumentFailure("出力に JSON オブジェクトがない")
    try:
        obj, _ = json.JSONDecoder().raw_decode(text, start)
    except ValueError as exc:
        raise DocumentFailure("出力の JSON を読めない") from exc
    except RecursionError:
        # 入れ子が深すぎる出力。周期ごと落ちないよう書類起因の失敗として扱う。
        raise DocumentFailure("出力の JSON の入れ子が深すぎる") from None
    if not isinstance(obj, dict):
        raise DocumentFailure("出力の JSON がオブジェクトではない")
    return obj


def parse_classification(
    result_text: str, *, known_tags: Collection[str], today: date
) -> Classification:
    """Claude の出力から分類結果を取り出して検証する。

    JSON が読めない、キーが欠ける、型が違うときは DocumentFailure を送出する。
    値の範囲外（日付、未知のタグなど）は失敗にせず、その項目だけ捨てる。
    """
    obj = _extract_object(result_text)
    try:
        title = obj["title"]
        correspondent = obj["correspondent"]
        document_type = obj["document_type"]
        raw_tags = obj["tags"]
        created = obj["created"]
        confident = obj["confident"]
    except KeyError as exc:
        raise DocumentFailure(f"出力にキーがない: {exc.args[0]}") from exc

    if not (
        isinstance(title, str)
        and (correspondent is None or isinstance(correspondent, str))
        and isinstance(document_type, str)
        and isinstance(raw_tags, list)
        and all(isinstance(t, str) for t in raw_tags)
        and (created is None or isinstance(created, str))
        and isinstance(confident, bool)
    ):
        raise DocumentFailure("出力の型が仕様と違う")

    picked: list[str] = []
    for tag in raw_tags:
        if tag in known_tags and tag not in STATE_TAGS and tag not in picked:
            picked.append(tag)

    return Classification(
        title=sanitize_component(title, TITLE_MAX),
        correspondent=None if correspondent is None else _clean_name(correspondent),
        document_type=_clean_name(document_type),
        tags=tuple(picked[:TAGS_MAX]),
        created=_parse_created(created, today),
        confident=confident,
    )


def parse_pages(result_text: str, *, expected: int) -> tuple[PageText, ...]:
    """Claude の出力から、ページごとの書き起こし（`pages`）を取り出して検証する。

    1〜expected のページがちょうど 1 回ずつ揃わない、page か text のキーが欠ける、型が違う
    ときは DocumentFailure を送出する。label のキーが無ければ None とする。長すぎる本文と
    ページ番号は切り詰める。
    """
    obj = _extract_object(result_text)
    rows = obj.get("pages")
    if not isinstance(rows, list):
        raise DocumentFailure("出力の pages が配列でない")
    pages: dict[int, PageText] = {}
    for row in rows:
        if not isinstance(row, dict) or not {"page", "text"} <= row.keys():
            raise DocumentFailure("出力の pages の要素にキーがない")
        # label は印字されたページ番号の手がかりで、無いページでは省かれることがある。
        # 省かれていれば無い（None）として扱い、書類の失敗には数えない。
        number, text, label = row["page"], row["text"], row.get("label")
        if not (
            type(number) is int
            and isinstance(text, str)
            and (label is None or isinstance(label, str))
        ):
            raise DocumentFailure("出力の pages の型が仕様と違う")
        if not 1 <= number <= expected or number in pages:
            raise DocumentFailure("出力の pages のページ番号が範囲外か重複している")
        cleaned_label = None if label is None else _clean_name(label)
        pages[number] = PageText(
            page=number,
            text=_clean_text(text)[:PAGE_TEXT_MAX],
            label=None if cleaned_label is None else cleaned_label[:PAGE_LABEL_MAX],
        )
    if len(pages) != expected:
        raise DocumentFailure("出力の pages にページが揃っていない")
    return tuple(pages[n] for n in range(1, expected + 1))
