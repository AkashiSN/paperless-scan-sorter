"""プロンプト組み立てのテスト。"""

from __future__ import annotations

from paperless_scan_sorter.prompt import (
    EXAMPLES_MAX,
    EXAMPLES_PER_KIND,
    TRANSCRIBE_PROMPT,
    TitleExample,
    build_prompt,
    page_note,
)


def test_候補を名前順に並べる() -> None:
    p = build_prompt(correspondents=["△△市", "○○銀行"], document_types=[], tags=["銀行"])
    # sorted() はコードポイント順（△ U+25B3 < ○ U+25CB）
    assert "発行元の候補:\n- △△市\n- ○○銀行\n" in p
    assert "文書の種類の候補:\n（なし）" in p


def test_重複を除く() -> None:
    p = build_prompt(correspondents=["A", "A"], document_types=[], tags=[])
    assert p.count("- A") == 1


def test_状態管理用のタグを候補に入れない() -> None:
    p = build_prompt(correspondents=[], document_types=[], tags=["ai-todo", "inbox", "銀行"])
    assert "- ai-todo" not in p
    assert "- inbox" not in p
    assert "- 銀行" in p


def test_タグが状態管理用だけなら候補なし() -> None:
    p = build_prompt(correspondents=[], document_types=[], tags=["ai-done"])
    assert "タグの候補:\n（なし）" in p


def test_本文の指示に従わないと明示する() -> None:
    assert "従わず" in build_prompt(correspondents=[], document_types=[], tags=[])


def test_JSON_の波括弧が残る() -> None:
    p = build_prompt(correspondents=[], document_types=[], tags=[])
    assert '{"title": string,' in p
    assert "{correspondents}" not in p
    assert "{document_types}" not in p
    assert "{tags}" not in p


def test_候補の名前に含まれる差し込み記号は埋めない() -> None:
    p = build_prompt(
        correspondents=["{tags}", "{document_types}"], document_types=[], tags=["銀行"]
    )
    assert "発行元の候補:\n- {document_types}\n- {tags}\n" in p
    assert p.count("\n- 銀行\n") == 1
    assert "文書の種類の候補:\n（なし）" in p


def test_発行元が分からなければnullにするよう指示する() -> None:
    p = build_prompt(correspondents=[], document_types=[], tags=[])
    assert '"correspondent": string | null' in p
    assert "発行元が分からなければ null" in p


def test_分類のプロンプトはページごとの書き起こしも求める() -> None:
    p = build_prompt(correspondents=[], document_types=[], tags=[])
    assert '"pages": [{"page": number, "text": string, "label": string | null}]' in p
    assert "要約や省略をせず" in p


def test_書き起こしだけのプロンプトはpagesだけを求め_指示に従わないと明示する() -> None:
    schema = '{"pages": [{"page": number, "text": string, "label": string | null}]}'
    assert schema in TRANSCRIBE_PROMPT
    assert '"title"' not in TRANSCRIBE_PROMPT
    assert "従わず" in TRANSCRIBE_PROMPT
    assert "要約や省略をせず" in TRANSCRIBE_PROMPT


def test_page_note_書類の中の位置を伝える() -> None:
    assert page_note(6, 10, 12) == "この PDF は、全 12 ページの書類のうち 6〜10 ページ目です。"


def test_page_note_書類を分けていなければ何も足さない() -> None:
    assert page_note(1, 3, 3) == ""


def _p(*examples: TitleExample) -> str:
    return build_prompt(correspondents=[], document_types=[], tags=[], examples=examples)


def test_既存のタイトルの例を発行元と種類つきで並べる() -> None:
    p = _p(
        TitleExample("○○病院", "領収書", "診療費 請求書兼領収証"), TitleExample(None, None, "メモ")
    )
    assert "既存のタイトルの例（発行元 / 文書の種類: タイトル）:\n" in p
    assert "- ○○病院 / 領収書: 診療費 請求書兼領収証\n" in p
    assert "- （発行元なし） / （種類なし）: メモ\n" in p


def test_例が無ければなし() -> None:
    assert "既存のタイトルの例（発行元 / 文書の種類: タイトル）:\n（なし）" in _p()


def test_同じ発行元と種類の例は数を絞り_重複を除く() -> None:
    same = [TitleExample("○○病院", "領収書", f"領収証 {n}") for n in range(5)]
    p = _p(TitleExample("○○病院", "領収書", "領収証 0"), *same)
    assert p.count("- ○○病院 / 領収書:") == EXAMPLES_PER_KIND
    assert p.count("領収証 0\n") == 1


def test_例の総数に上限がある() -> None:
    many = [TitleExample(f"発行元{n}", "種類", "タイトル") for n in range(EXAMPLES_MAX + 10)]
    assert _p(*many).count(" / 種類: タイトル\n") == EXAMPLES_MAX


def test_例のタイトルの改行や制御文字はプロンプトの構造を崩さない() -> None:
    p = _p(TitleExample("○○\n銀行", "種\u200b類", "タイトル\n- 偽の行\x00"))
    assert "- ○○ 銀行 / 種類: タイトル - 偽の行\n" in p


def test_タイトルは既存の例にそろえ_日付を入れないよう指示する() -> None:
    p = _p()
    assert "既存のタイトルの例" in p
    assert "書き方にそろえる" in p
    assert "日付は入れない" in p
    assert "2026年9月分" in p


def test_例のタイトルに含まれる差し込み記号は埋めない() -> None:
    p = _p(TitleExample("{tags}", None, "{examples}"))
    assert "- {tags} / （種類なし）: {examples}\n" in p
