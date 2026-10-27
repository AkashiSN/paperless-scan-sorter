from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from paperless_scan_sorter import __main__ as app
from paperless_scan_sorter.errors import MissingTagsError
from paperless_scan_sorter.processor import CycleResult


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for k in ("PAPERLESS_TOKEN_FILE", "INTERVAL", "CLAUDE_MODEL", "CLAUDE_TIMEOUT"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PAPERLESS_URL", "https://paperless.example.com")
    monkeypatch.setenv("PAPERLESS_TOKEN", "secret-token")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-secret")
    monkeypatch.setenv("STATE_DIR", str(tmp_path / "state"))


def test_onceは1周期で終わる(monkeypatch: pytest.MonkeyPatch, env: None) -> None:
    calls: list[date] = []

    def fake(paperless, claude, store, created, *, today):  # noqa: ANN001, ANN202
        calls.append(today)
        return CycleResult(done=1)

    monkeypatch.setattr(app, "run_cycle", fake)
    assert app.main(["--once"]) == 0
    assert calls == [date.today()]


def test_設定の不備は終了コード2(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    for k in (
        "PAPERLESS_URL",
        "PAPERLESS_TOKEN",
        "PAPERLESS_TOKEN_FILE",
        "CLAUDE_CODE_OAUTH_TOKEN",
    ):
        monkeypatch.delenv(k, raising=False)
    assert app.main(["--once"]) == 2
    assert "PAPERLESS_URL" in caplog.text


def test_必須タグが無ければ1で終わる(monkeypatch: pytest.MonkeyPatch, env: None) -> None:
    calls = 0

    def fake(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        nonlocal calls
        calls += 1
        raise MissingTagsError("Paperless に必要なタグが無い: ai-todo")

    monkeypatch.setattr(app, "run_cycle", fake)
    assert app.main([]) == 1
    assert calls == 1


def test_onceで想定外の例外なら1(monkeypatch: pytest.MonkeyPatch, env: None) -> None:
    def fake(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise ValueError("bug")

    monkeypatch.setattr(app, "run_cycle", fake)
    assert app.main(["--once"]) == 1


def test_常駐ループは例外でも次の周期へ進み停止要求で終わる(
    monkeypatch: pytest.MonkeyPatch, env: None
) -> None:
    monkeypatch.setenv("INTERVAL", "1")
    calls = 0

    def fake(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError("bug")
        app.request_stop()
        return CycleResult()

    monkeypatch.setattr(app, "run_cycle", fake)
    assert app.main([]) == 0
    assert calls == 2


def test_RuntimeErrorは必須タグ不足として扱わない(
    monkeypatch: pytest.MonkeyPatch, env: None
) -> None:
    monkeypatch.setenv("INTERVAL", "1")
    calls = 0

    def fake(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RecursionError("deep")
        app.request_stop()
        return CycleResult()

    monkeypatch.setattr(app, "run_cycle", fake)
    assert app.main([]) == 0
    assert calls == 2
