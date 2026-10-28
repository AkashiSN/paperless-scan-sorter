from __future__ import annotations

from paperless_scan_sorter import tags


def test_状態管理用のタグは7つ() -> None:
    expected = {
        "inbox",
        "page-todo",
        "page-review",
        "ai-todo",
        "ai-done",
        "ai-review",
        "ai-failed",
    }
    actual = tags.STATE_TAGS
    assert actual == expected


def test_必須タグは状態管理用のタグに含まれる() -> None:
    assert set(tags.REQUIRED_TAGS) <= tags.STATE_TAGS


def test_必須タグは状態管理用のタグ7つすべて() -> None:
    assert set(tags.REQUIRED_TAGS) == tags.STATE_TAGS
    assert len(tags.REQUIRED_TAGS) == 7
