"""書き戻す内容の決定のテスト。"""

from __future__ import annotations

from dataclasses import replace
from datetime import date

from paperless_scan_sorter.decide import Decision, TagIds, decide, failed_tags
from paperless_scan_sorter.paperless import Document, Named
from paperless_scan_sorter.validate import Classification

IDS = TagIds(inbox=1, ai_todo=2, ai_done=3, ai_review=4, ai_failed=5, page_todo=6, page_review=7)
CORR = [Named(10, "○○銀行")]
TYPES = [Named(20, "取引報告書")]
TAGN = {"銀行": 30, "保険": 31}
DOC = Document(1, "scan", "本文", None, None, frozenset({1, 2, 99}))


def cls(**over: object) -> Classification:
    base = Classification(
        "取引報告書 2026年9月", "○○銀行", "取引報告書", ("銀行",), date(2026, 10, 20), True
    )
    return replace(base, **over)


def run(doc: Document, c: Classification) -> Decision:
    return decide(doc, c, correspondents=CORR, document_types=TYPES, tag_names=TAGN, ids=IDS)


def test_自信があり既存の発行元と文書タイプならai_done() -> None:
    d = run(DOC, cls())
    assert d == Decision(
        "取引報告書 2026年9月", 10, 20, date(2026, 10, 20), frozenset({99, 30, 3}), False, ()
    )


def test_自信がなければai_reviewでinboxを残す() -> None:
    d = run(DOC, cls(confident=False))
    assert d.review and d.tags == frozenset({1, 99, 30, 4})
    assert d.reasons == ("自信なし",)


def test_新しい発行元は名前で返しai_review() -> None:
    d = run(DOC, cls(correspondent="△△市"))
    assert d.correspondent == "△△市" and d.review
    assert d.reasons == ("発行元を新規作成",)


def test_新しい文書タイプは名前で返しai_review() -> None:
    d = run(DOC, cls(document_type="通知書"))
    assert d.document_type == "通知書" and d.review
    assert d.reasons == ("文書タイプを新規作成",)


def test_既に設定された発行元は上書きしない() -> None:
    d = run(replace(DOC, correspondent=10), cls())
    assert d.correspondent is None and not d.review


def test_自動マッチと食い違えばai_review() -> None:
    d = run(replace(DOC, correspondent=10), cls(correspondent="△△市"))
    assert d.correspondent is None and d.review
    assert d.reasons == ("発行元が自動マッチと食い違う",)


def test_既に設定された文書タイプは上書きしない() -> None:
    d = run(replace(DOC, document_type=20), cls())
    assert d.document_type is None and not d.review


def test_文書タイプが自動マッチと食い違えばai_review() -> None:
    d = run(replace(DOC, document_type=20), cls(document_type="通知書"))
    assert d.document_type is None and d.review
    assert d.reasons == ("文書タイプが自動マッチと食い違う",)


def test_既存の発行元が一覧に無ければ食い違い扱い() -> None:
    d = run(replace(DOC, correspondent=777), cls())
    assert d.correspondent is None and d.review


def test_既存の発行元があり判定がNoneなら見直さない() -> None:
    d = run(replace(DOC, correspondent=10), cls(correspondent=None))
    assert d.correspondent is None and not d.review


def test_採用できないタイトルは変えずにai_review() -> None:
    d = run(DOC, cls(title=None))
    assert d.title is None and d.review
    assert d.reasons == ("タイトルを採用できない",)


def test_採用できない発行元はai_review() -> None:
    d = run(DOC, cls(correspondent=None))
    assert d.correspondent is None and d.review
    assert d.reasons == ("発行元を採用できない",)


def test_採用できない文書タイプはai_review() -> None:
    d = run(DOC, cls(document_type=None))
    assert d.document_type is None and d.review
    assert d.reasons == ("文書タイプを採用できない",)


def test_理由は発行元_文書タイプ_タイトル_自信なしの順() -> None:
    c = cls(correspondent="△△市", document_type=None, title=None, confident=False)
    assert run(DOC, c).reasons == (
        "発行元を新規作成",
        "文書タイプを採用できない",
        "タイトルを採用できない",
        "自信なし",
    )


def test_createdがNoneなら変えない() -> None:
    assert run(DOC, cls(created=None)).created is None


def test_一覧に無いタグ名は読み飛ばす() -> None:
    d = run(DOC, cls(tags=("銀行", "存在しない")))
    assert d.tags == frozenset({99, 30, 3})


def test_失敗時はai_todoを外しai_failedを足す() -> None:
    assert failed_tags(DOC, IDS) == frozenset({1, 99, 5})


def test_追加の理由があればai_reviewにして理由の最後に足す() -> None:
    d = decide(
        DOC,
        cls(confident=False),
        correspondents=CORR,
        document_types=TYPES,
        tag_names=TAGN,
        ids=IDS,
        extra_review_reasons=("前回の試行で発行元・文書タイプを新規作成",),
    )
    assert d.review
    assert d.reasons == ("自信なし", "前回の試行で発行元・文書タイプを新規作成")
    assert 4 in d.tags and 3 not in d.tags and 1 in d.tags


def test_追加の理由だけでもai_reviewにする() -> None:
    d = decide(
        DOC,
        cls(),
        correspondents=CORR,
        document_types=TYPES,
        tag_names=TAGN,
        ids=IDS,
        extra_review_reasons=("理由",),
    )
    assert d.review and d.reasons == ("理由",)
    assert d.correspondent == 10


def test_ピリオドで終わる既存の発行元にそのまま一致する() -> None:
    corr = [*CORR, Named(11, "Foo Inc.")]
    d = decide(
        DOC,
        cls(correspondent="Foo Inc."),
        correspondents=corr,
        document_types=TYPES,
        tag_names=TAGN,
        ids=IDS,
    )
    assert d.correspondent == 11 and not d.review


def test_長い既存の発行元にもそのまま一致する() -> None:
    corr = [Named(12, "い" * 31)]
    d = decide(
        DOC, cls(correspondent="い" * 31), correspondents=corr,
        document_types=TYPES, tag_names=TAGN, ids=IDS,
    )  # fmt: skip
    assert d.correspondent == 12 and not d.review


def test_新しい発行元は無害化してから作る() -> None:
    d = run(DOC, cls(correspondent="a/b"))
    assert d.correspondent == "ab" and d.review
    assert d.reasons == ("発行元を新規作成",)
    d = run(DOC, cls(correspondent="い" * 31))
    assert d.correspondent == "い" * 30


def test_無害化した名前が既存にあれば作らずにその発行元を使う() -> None:
    corr = [Named(10, "○○銀行"), Named(11, "い" * 30)]
    for proposed, expected in [("○○銀行.", 10), ("い" * 31, 11)]:
        d = decide(
            DOC,
            cls(correspondent=proposed),
            correspondents=corr,
            document_types=TYPES,
            tag_names=TAGN,
            ids=IDS,
        )
        assert d.correspondent == expected and not d.review


def test_自動マッチの発行元と無害化した名前が一致すれば食い違いにしない() -> None:
    d = run(replace(DOC, correspondent=10), cls(correspondent="○○銀行."))
    assert d.correspondent is None and not d.review


def test_無害化で空になる新しい発行元は作らずai_review() -> None:
    d = run(DOC, cls(correspondent="/.."))
    assert d.correspondent is None and d.review
    assert d.reasons == ("発行元を採用できない",)


def test_文書タイプは新規作成でも無害化しない() -> None:
    d = run(DOC, cls(document_type="A/B."))
    assert d.document_type == "A/B."


STALE = replace(DOC, tags=frozenset({1, 2, 99, 3, 4, 5}))


def test_done_にするとき古い状態タグを外す() -> None:
    assert run(STALE, cls()).tags == frozenset({99, 30, 3})


def test_review_にするとき古い状態タグを外す() -> None:
    assert run(STALE, cls(confident=False)).tags == frozenset({1, 99, 30, 4})


def test_失敗時も古い状態タグを外す() -> None:
    assert failed_tags(STALE, IDS) == frozenset({1, 99, 5})
