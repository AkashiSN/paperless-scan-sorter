"""元の PDF の退避のテスト。"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import pytest

from paperless_scan_sorter.errors import ServiceUnavailable
from paperless_scan_sorter.originals import RETENTION_DAYS, OriginalsStore

DAY = 24 * 60 * 60


def _age(path: Path, days: float, now: float) -> None:
    """ファイルの更新時刻を `days` 日前にする。"""
    t = now - days * DAY
    os.utime(path, (t, t))


def test_保存した中身が読める(tmp_path: Path) -> None:
    s = OriginalsStore(tmp_path / "originals")
    s.save(12, b"%PDF-1 original")
    assert (tmp_path / "originals" / "12.pdf").read_bytes() == b"%PDF-1 original"


def test_既にあれば上書きしない(tmp_path: Path) -> None:
    s = OriginalsStore(tmp_path)
    s.save(12, b"first")
    s.save(12, b"second")
    assert (tmp_path / "12.pdf").read_bytes() == b"first"


def test_ディレクトリが無ければ作る(tmp_path: Path) -> None:
    d = tmp_path / "a" / "b" / "originals"
    OriginalsStore(d).save(1, b"x")
    assert (d / "1.pdf").read_bytes() == b"x"


def test_保存後に一時ファイルが残らない(tmp_path: Path) -> None:
    OriginalsStore(tmp_path).save(1, b"x")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["1.pdf"]


def test_書き込めないと_ServiceUnavailable(tmp_path: Path) -> None:
    # 親が通常ファイルだと、ディレクトリを作れず OSError になる。root でも再現できる。
    blocker = tmp_path / "blocker"
    blocker.write_bytes(b"not a directory")
    with pytest.raises(ServiceUnavailable):
        OriginalsStore(blocker / "originals").save(1, b"x")


def test_古いファイルだけ消す(tmp_path: Path) -> None:
    now = time.time()
    s = OriginalsStore(tmp_path)
    s.save(1, b"old")
    s.save(2, b"recent")
    _age(tmp_path / "1.pdf", RETENTION_DAYS + 1, now)
    _age(tmp_path / "2.pdf", RETENTION_DAYS - 1, now)

    assert s.prune(now) == 1
    assert not (tmp_path / "1.pdf").exists()
    assert (tmp_path / "2.pdf").exists()


def test_90日ちょうどの前後の境界(tmp_path: Path) -> None:
    now = time.time()
    s = OriginalsStore(tmp_path)
    s.save(1, b"x")
    s.save(2, b"y")
    _age(tmp_path / "1.pdf", 91, now)
    _age(tmp_path / "2.pdf", 89, now)

    assert s.prune(now) == 1
    assert not (tmp_path / "1.pdf").exists()
    assert (tmp_path / "2.pdf").exists()


def test_pdf以外は消さない(tmp_path: Path) -> None:
    now = time.time()
    other = tmp_path / "notes.txt"
    other.write_text("keep")
    _age(other, 365, now)
    assert OriginalsStore(tmp_path).prune(now) == 0
    assert other.exists()


def test_ディレクトリが無ければ0(tmp_path: Path) -> None:
    assert OriginalsStore(tmp_path / "missing").prune(time.time()) == 0


def test_削除に失敗しても警告して続ける(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = time.time()
    s = OriginalsStore(tmp_path)
    s.save(1, b"x")
    s.save(2, b"y")
    _age(tmp_path / "1.pdf", 100, now)
    _age(tmp_path / "2.pdf", 100, now)

    real_unlink = Path.unlink

    def flaky_unlink(self: Path, missing_ok: bool = False) -> None:
        if self.name == "1.pdf":
            raise PermissionError("denied")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)
    with caplog.at_level(logging.WARNING):
        assert s.prune(now) == 1
    assert (tmp_path / "1.pdf").exists()
    assert not (tmp_path / "2.pdf").exists()
    assert any("1.pdf" in r.getMessage() for r in caplog.records)
    assert any("PermissionError" in r.getMessage() for r in caplog.records)


def test_hasは退避があるかを返す(tmp_path: Path) -> None:
    s = OriginalsStore(tmp_path / "originals")
    assert s.has(12) is False
    s.save(12, b"x")
    assert s.has(12) is True
    assert s.has(13) is False
