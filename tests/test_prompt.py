"""プロンプト組み立てのテスト。"""

from __future__ import annotations

from paperless_scan_sorter.prompt import build_prompt


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
