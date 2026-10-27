"""失敗回数の記録のテスト。"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from paperless_scan_sorter.failures import CreatedStore, FailureStore


def test_増やして消せる(tmp_path: Path) -> None:
    s = FailureStore(tmp_path / "failures.json")
    assert s.increment(7) == 1
    assert s.increment(7) == 2
    assert s.increment(8) == 1
    s.clear(7)
    assert s.count(7) == 0
    assert s.count(8) == 1


def test_別のインスタンスからも読める(tmp_path: Path) -> None:
    p = tmp_path / "failures.json"
    FailureStore(p).increment(3)
    FailureStore(p).increment(3)
    assert FailureStore(p).count(3) == 2


def test_壊れたファイルは空として扱う(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    p = tmp_path / "failures.json"
    p.write_text("{壊れ")
    with caplog.at_level(logging.WARNING):
        assert FailureStore(p).increment(1) == 1
    assert caplog.records
    assert json.loads(p.read_text()) == {"1": 1}


def test_辞書でないファイルは空として扱う(tmp_path: Path) -> None:
    p = tmp_path / "failures.json"
    p.write_text("[1, 2]")
    assert FailureStore(p).count(1) == 0


def test_値が整数でないファイルは全体を空として扱う(tmp_path: Path) -> None:
    p = tmp_path / "failures.json"
    p.write_text('{"1": 2, "2": "x"}')
    assert FailureStore(p).count(1) == 0


def test_ファイルが無ければ0(tmp_path: Path) -> None:
    assert FailureStore(tmp_path / "failures.json").count(1) == 0


def test_親ディレクトリを作る(tmp_path: Path) -> None:
    p = tmp_path / "a" / "b" / "failures.json"
    assert FailureStore(p).increment(1) == 1
    assert p.exists()


def test_一時ファイルを残さない(tmp_path: Path) -> None:
    s = FailureStore(tmp_path / "failures.json")
    s.increment(1)
    s.increment(2)
    s.clear(1)
    assert [x.name for x in tmp_path.iterdir()] == ["failures.json"]


def test_新規作成した書類を記録して消せる(tmp_path: Path) -> None:
    p = tmp_path / "created.json"
    s = CreatedStore(p)
    assert not s.contains(7)
    s.mark(7)
    s.mark(7)
    s.mark(8)
    assert CreatedStore(p).contains(7)
    assert json.loads(p.read_text(encoding="utf-8")) == [7, 8]
    s.clear(7)
    assert not s.contains(7)
    assert s.contains(8)


@pytest.mark.parametrize("content", ["{壊れ", '{"7": true}', '["7"]', "[true]"])
def test_新規作成の記録が読めなければ空として扱う(
    tmp_path: Path, content: str, caplog: pytest.LogCaptureFixture
) -> None:
    p = tmp_path / "created.json"
    p.write_text(content, encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert not CreatedStore(p).contains(7)
    assert caplog.records


def test_新規作成の記録は一時ファイルを残さない(tmp_path: Path) -> None:
    s = CreatedStore(tmp_path / "sub" / "created.json")
    s.mark(1)
    assert sorted(x.name for x in (tmp_path / "sub").iterdir()) == ["created.json"]
