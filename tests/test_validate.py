from __future__ import annotations

import json
from datetime import date

import pytest

from paperless_scan_sorter.errors import DocumentFailure
from paperless_scan_sorter.validate import (
    PAGE_LABEL_MAX,
    PAGE_TEXT_MAX,
    Classification,
    PageText,
    parse_classification,
    parse_pages,
    sanitize_component,
)

TODAY = date(2026, 10, 27)
KNOWN = {"請求書", "保険", "銀行", "税"}


def _j(**over: object) -> str:
    base: dict[str, object] = {
        "title": "取引報告書 2026年9月",
        "correspondent": "○○銀行",
        "document_type": "取引報告書",
        "tags": ["銀行"],
        "created": "2026-10-20",
        "confident": True,
    }
    return json.dumps(base | over, ensure_ascii=False)


def test_前後に文章があっても最初のJSONを読む() -> None:
    c = parse_classification("以下です\n" + _j() + "\n以上", known_tags=KNOWN, today=TODAY)
    assert c == Classification(
        "取引報告書 2026年9月", "○○銀行", "取引報告書", ("銀行",), date(2026, 10, 20), True
    )


@pytest.mark.parametrize("text", ["", "JSON なし", "{壊れている", "[1, 2]"])
def test_JSONを読めなければ書類起因の失敗(text: str) -> None:
    with pytest.raises(DocumentFailure):
        parse_classification(text, known_tags=KNOWN, today=TODAY)


@pytest.mark.parametrize(
    "over",
    [
        {"confident": "true"},
        {"tags": "銀行"},
        {"tags": [1]},
        {"title": 1},
        {"correspondent": 1},
        {"document_type": 3},
        {"created": 20261020},
    ],
)
def test_型が違えば書類起因の失敗(over: dict[str, object]) -> None:
    with pytest.raises(DocumentFailure):
        parse_classification(_j(**over), known_tags=KNOWN, today=TODAY)


@pytest.mark.parametrize(
    "key", ["title", "correspondent", "document_type", "tags", "created", "confident"]
)
def test_キーが欠けていれば書類起因の失敗(key: str) -> None:
    data = json.loads(_j())
    del data[key]
    with pytest.raises(DocumentFailure):
        parse_classification(json.dumps(data), known_tags=KNOWN, today=TODAY)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("../../etc/passwd", "etcpasswd"),
        ("a/b\\c", "abc"),
        ("  請求書\n2026  ", "請求書2026"),
        ("...", None),
        ("..", None),
        ("   ", None),
        ("\x00\x1f", None),
        (".隠し.", "隠し"),
    ],
)
def test_パスに入る値を無害化する(raw: str, expected: str | None) -> None:
    assert sanitize_component(raw, 60) == expected


def test_切り詰めた末尾のピリオドも削る() -> None:
    assert sanitize_component("あ" * 29 + ".い", 30) == "あ" * 29


def test_titleは60文字で切りcorrespondentは切らない() -> None:
    c = parse_classification(
        _j(title="あ" * 61, correspondent="い" * 31), known_tags=KNOWN, today=TODAY
    )
    assert c.title == "あ" * 60
    # correspondent は既存の名前と照合するため、ここでは切り詰めない（新規作成時に無害化する）。
    assert c.correspondent == "い" * 31


def test_無害化で空になるtitleはNone() -> None:
    c = parse_classification(_j(title="..", correspondent="/"), known_tags=KNOWN, today=TODAY)
    assert c.title is None
    assert c.correspondent == "/"


def test_correspondentは制御文字と前後の空白だけ除く() -> None:
    c = parse_classification(_j(correspondent="  Foo\n Inc. "), known_tags=KNOWN, today=TODAY)
    assert c.correspondent == "Foo Inc."
    c = parse_classification(_j(correspondent=" \x00 "), known_tags=KNOWN, today=TODAY)
    assert c.correspondent is None


def test_発行元が分からないnullは失敗にせずNone() -> None:
    c = parse_classification(_j(correspondent=None), known_tags=KNOWN, today=TODAY)
    assert c.correspondent is None
    assert c.title == "取引報告書 2026年9月"


def test_document_typeは制御文字と前後の空白だけ除く() -> None:
    c = parse_classification(_j(document_type="  取引\n報告書/A. "), known_tags=KNOWN, today=TODAY)
    assert c.document_type == "取引報告書/A."
    c = parse_classification(_j(document_type=" \x00 "), known_tags=KNOWN, today=TODAY)
    assert c.document_type is None


def test_タグは既知のものだけ状態タグを除き3つまで() -> None:
    c = parse_classification(
        _j(tags=["ai-done", "未知", "銀行", "銀行", "保険", "税", "請求書"]),
        known_tags=KNOWN | {"ai-done"},
        today=TODAY,
    )
    assert c.tags == ("銀行", "保険", "税")


@pytest.mark.parametrize(
    ("created", "expected"),
    [
        ("2026-10-27", date(2026, 10, 27)),
        ("2026-10-28", None),
        ("1996-10-28", date(1996, 10, 28)),
        ("1996-10-27", None),
        ("2026-13-01", None),
        (None, None),
    ],
)
def test_createdの範囲(created: str | None, expected: date | None) -> None:
    c = parse_classification(_j(created=created), known_tags=KNOWN, today=TODAY)
    assert c.created == expected


def test_閏日の30年前は2月28日() -> None:
    today = date(2024, 2, 29)
    c = parse_classification(_j(created="1994-02-28"), known_tags=KNOWN, today=today)
    assert c.created is None
    c = parse_classification(_j(created="1994-03-01"), known_tags=KNOWN, today=today)
    assert c.created == date(1994, 3, 1)


def test_入れ子が深すぎるJSONは書類起因の失敗() -> None:
    text = '{"a": ' + "[" * 100000
    with pytest.raises(DocumentFailure):
        parse_classification(text, known_tags=KNOWN, today=TODAY)


@pytest.mark.parametrize("ch", ["​", "‮", " ", " ", "﻿", "\x7f"])
def test_書式文字と行区切りも除く(ch: str) -> None:
    assert sanitize_component(f"a{ch}b", 60) == "ab"
    c = parse_classification(
        _j(correspondent=f"{ch}Foo{ch}", document_type=f"請{ch}求書"), known_tags=KNOWN, today=TODAY
    )
    assert c.correspondent == "Foo"
    assert c.document_type == "請求書"


def _pages(*rows: object) -> str:
    return json.dumps({"pages": list(rows)}, ensure_ascii=False)


def _row(page: object = 1, text: object = "本文", label: object = None) -> dict[str, object]:
    return {"page": page, "text": text, "label": label}


def test_parse_pages_ページ順に並べて返す() -> None:
    out = parse_pages(_pages(_row(2, "二", "2"), _row(1, "一", "1")), expected=2)
    assert out == (PageText(1, "一", "1"), PageText(2, "二", "2"))


def test_parse_pages_分類のJSONと同じ出力からも読める() -> None:
    text = json.dumps(json.loads(_j()) | {"pages": [_row()]}, ensure_ascii=False)
    assert parse_pages(text, expected=1) == (PageText(1, "本文", None),)


def test_parse_pages_改行とタブは残し_ほかの制御文字と書式文字は除く() -> None:
    raw = "一行目\r\n二行目\t列\x00\u200b\u2028終わり"
    (page,) = parse_pages(_pages(_row(text=raw)), expected=1)
    assert page.text == "一行目\n二行目\t列終わり"


def test_parse_pages_長い本文とラベルは切り詰める() -> None:
    (page,) = parse_pages(_pages(_row(text="あ" * (PAGE_TEXT_MAX + 5), label="1" * 50)), expected=1)
    assert len(page.text) == PAGE_TEXT_MAX
    assert page.label == "1" * PAGE_LABEL_MAX


def test_parse_pages_空のラベルはNone_空の本文は空文字列() -> None:
    (page,) = parse_pages(_pages(_row(text="", label=" \u200b ")), expected=1)
    assert page == PageText(1, "", None)


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [_row(1)],
        [_row(1), _row(1)],
        [_row(1), _row(3)],
        [_row(1), _row(2), _row(3)],
        [_row(0), _row(1)],
    ],
)
def test_parse_pages_ページが過不足なく揃わなければ書類起因の失敗(rows: list[object]) -> None:
    with pytest.raises(DocumentFailure):
        parse_pages(_pages(*rows), expected=2)


@pytest.mark.parametrize(
    "row",
    [
        _row(page="1"),
        _row(page=True),
        _row(page=1.0),
        _row(text=None),
        _row(text=1),
        _row(label=2),
        {"page": 1, "label": None},
        {"text": "本文", "label": None},
        "本文",
    ],
)
def test_parse_pages_型が違えば書類起因の失敗(row: object) -> None:
    with pytest.raises(DocumentFailure):
        parse_pages(_pages(row), expected=1)


@pytest.mark.parametrize("text", ["", "{壊れている", _j(), json.dumps({"pages": "本文"})])
def test_parse_pages_pagesが無いか配列でなければ書類起因の失敗(text: str) -> None:
    with pytest.raises(DocumentFailure):
        parse_pages(text, expected=1)


def test_parse_pages_labelのキーが無ければNoneとして扱う() -> None:
    (page,) = parse_pages(_pages({"page": 1, "text": "本文"}), expected=1)
    assert page == PageText(1, "本文", None)
