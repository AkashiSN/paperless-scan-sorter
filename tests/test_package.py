from __future__ import annotations


def test_パッケージを読み込める() -> None:
    import paperless_scan_sorter

    assert paperless_scan_sorter.__doc__
