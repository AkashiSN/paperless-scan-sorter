from __future__ import annotations

import errno
import logging
import socket
from datetime import date
from pathlib import Path

import pytest

from paperless_scan_sorter import __main__ as app
from paperless_scan_sorter.errors import MissingTagsError
from paperless_scan_sorter.originals import OriginalsStore
from paperless_scan_sorter.page_processor import PageCycleResult
from paperless_scan_sorter.processor import CycleResult
from paperless_scan_sorter.wake import DualStackError


@pytest.fixture(autouse=True)
def no_page_cycle(monkeypatch: pytest.MonkeyPatch) -> None:
    """ページ整形の周期は既定で何もしない（Paperless に繋がないため）。"""
    monkeypatch.setattr(app, "run_page_cycle", lambda *a, **k: PageCycleResult())


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for k in (
        "PAPERLESS_TOKEN_FILE",
        "INTERVAL",
        "CLAUDE_MODEL",
        "CLAUDE_TIMEOUT",
        "WEBHOOK_TOKEN",
        "WEBHOOK_PORT",
    ):
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


def test_httpxのリクエストログを出さない(monkeypatch: pytest.MonkeyPatch, env: None) -> None:
    monkeypatch.setattr(app, "run_cycle", lambda *a, **k: CycleResult())
    logging.getLogger("httpx").setLevel(logging.NOTSET)
    assert app.main(["--once"]) == 0
    assert logging.getLogger("httpx").level == logging.WARNING


def test_ページ整形の周期を分類の周期より先に回す(
    monkeypatch: pytest.MonkeyPatch, env: None, tmp_path: Path
) -> None:
    calls: list[str] = []
    stores: list[OriginalsStore] = []

    def fake_page(paperless, originals):  # noqa: ANN001, ANN202
        calls.append("page")
        stores.append(originals)
        return PageCycleResult()

    def fake(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        calls.append("classify")
        return CycleResult()

    monkeypatch.setattr(app, "run_page_cycle", fake_page)
    monkeypatch.setattr(app, "run_cycle", fake)
    assert app.main(["--once"]) == 0
    assert calls == ["page", "classify"]
    assert isinstance(stores[0], OriginalsStore)
    assert stores[0]._dir == tmp_path / "state" / "originals"


def test_ページ整形の周期が打ち切られても分類の周期は回す(
    monkeypatch: pytest.MonkeyPatch, env: None, caplog: pytest.LogCaptureFixture
) -> None:
    calls = 0

    def fake(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        nonlocal calls
        calls += 1
        return CycleResult()

    monkeypatch.setattr(
        app,
        "run_page_cycle",
        lambda *a, **k: PageCycleResult(fixed=1, unchanged=2, review=3, aborted=True),
    )
    monkeypatch.setattr(app, "run_cycle", fake)
    caplog.set_level(logging.INFO)
    assert app.main(["--once"]) == 0
    assert calls == 1
    assert "ページ整形: fixed=1 unchanged=2 review=3 aborted=True" in caplog.text


def test_ページ整形で必須タグが無ければ1で終わる(
    monkeypatch: pytest.MonkeyPatch, env: None
) -> None:
    calls = 0

    def fake_page(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise MissingTagsError("Paperless に必要なタグが無い: page-todo")

    def fake(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        nonlocal calls
        calls += 1
        return CycleResult()

    monkeypatch.setattr(app, "run_page_cycle", fake_page)
    monkeypatch.setattr(app, "run_cycle", fake)
    assert app.main([]) == 1
    assert calls == 0


def test_onceでページ整形が想定外の例外なら分類を回して1(
    monkeypatch: pytest.MonkeyPatch, env: None
) -> None:
    calls = 0

    def fake_page(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise ValueError("bug")

    def fake(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        nonlocal calls
        calls += 1
        return CycleResult()

    monkeypatch.setattr(app, "run_page_cycle", fake_page)
    monkeypatch.setattr(app, "run_cycle", fake)
    assert app.main(["--once"]) == 1
    assert calls == 1


def test_常駐ループはページ整形の例外でも分類を回し次の周期へ進む(
    monkeypatch: pytest.MonkeyPatch, env: None
) -> None:
    monkeypatch.setenv("INTERVAL", "1")
    pages = 0
    calls = 0

    def fake_page(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        nonlocal pages
        pages += 1
        if pages == 1:
            raise ValueError("bug")
        return PageCycleResult()

    def fake(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        nonlocal calls
        calls += 1
        if calls == 2:
            app.request_stop()
        return CycleResult()

    monkeypatch.setattr(app, "run_page_cycle", fake_page)
    monkeypatch.setattr(app, "run_cycle", fake)
    assert app.main([]) == 0
    assert (pages, calls) == (2, 2)


def test_起こされたら待たずに次の周期を回す(monkeypatch: pytest.MonkeyPatch, env: None) -> None:
    monkeypatch.setenv("INTERVAL", "3600")
    calls = 0

    def fake(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        nonlocal calls
        calls += 1
        if calls == 1:
            app.request_wake()
        else:
            app.request_stop()
        return CycleResult()

    monkeypatch.setattr(app, "run_cycle", fake)
    assert app.main([]) == 0
    assert calls == 2


def test_WEBHOOK_TOKENがあれば受け口を開き終了時に閉じる(
    monkeypatch: pytest.MonkeyPatch, env: None
) -> None:
    monkeypatch.setenv("WEBHOOK_TOKEN", "w" * 32)
    monkeypatch.setenv("WEBHOOK_PORT", "0")
    events: list[str] = []

    class FakeServer:
        def __init__(self, token, event, *, host, port):  # noqa: ANN001
            events.append(f"init {host} {port} {token == 'w' * 32}")

        def start(self) -> None:
            events.append("start")

        def close(self) -> None:
            events.append("close")

    def fake(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        app.request_stop()
        return CycleResult()

    monkeypatch.setattr(app, "WakeServer", FakeServer)
    monkeypatch.setattr(app, "run_cycle", fake)
    assert app.main([]) == 0
    assert events == ["init :: 0 True", "start", "close"]


def test_onceでは受け口を開かない(monkeypatch: pytest.MonkeyPatch, env: None) -> None:
    monkeypatch.setenv("WEBHOOK_TOKEN", "w" * 32)

    def boom(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("開いてはいけない")

    monkeypatch.setattr(app, "WakeServer", boom)
    monkeypatch.setattr(app, "run_cycle", lambda *a, **k: CycleResult())
    assert app.main(["--once"]) == 0


def test_受け口を開けなくてもPaperlessのクライアントを閉じる(
    monkeypatch: pytest.MonkeyPatch, env: None
) -> None:
    monkeypatch.setenv("WEBHOOK_TOKEN", "w" * 32)
    closed: list[str] = []

    class FakePaperless:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
            pass

        def close(self) -> None:
            closed.append("paperless")

    def boom(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise OSError("address in use")

    monkeypatch.setattr(app, "PaperlessClient", FakePaperless)
    monkeypatch.setattr(app, "WakeServer", boom)
    with pytest.raises(OSError):
        app.main([])
    assert closed == ["paperless"]


def test_受け口を閉じる例外が出てもPaperlessのクライアントを閉じる(
    monkeypatch: pytest.MonkeyPatch, env: None
) -> None:
    monkeypatch.setenv("WEBHOOK_TOKEN", "w" * 32)
    closed: list[str] = []

    class FakePaperless:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
            pass

        def close(self) -> None:
            closed.append("paperless")

    class FakeServer:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
            pass

        def start(self) -> None:
            pass

        def close(self) -> None:
            raise OSError("close failed")

    def fake(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        app.request_stop()
        return CycleResult()

    monkeypatch.setattr(app, "PaperlessClient", FakePaperless)
    monkeypatch.setattr(app, "WakeServer", FakeServer)
    monkeypatch.setattr(app, "run_cycle", fake)
    with pytest.raises(OSError):
        app.main([])
    assert closed == ["paperless"]


def test_IPv6で受け口を開けなければIPv4だけで開く(
    monkeypatch: pytest.MonkeyPatch, env: None
) -> None:
    monkeypatch.setenv("WEBHOOK_TOKEN", "w" * 32)
    hosts: list[str] = []

    class FakeServer:
        def __init__(self, token, event, *, host, port):  # noqa: ANN001
            hosts.append(host)
            if host == "::":
                raise OSError(errno.EAFNOSUPPORT, "Address family not supported by protocol")

        def start(self) -> None:
            pass

        def close(self) -> None:
            pass

    def fake(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        app.request_stop()
        return CycleResult()

    monkeypatch.setattr(app, "WakeServer", FakeServer)
    monkeypatch.setattr(app, "run_cycle", fake)
    assert app.main([]) == 0
    assert hosts == [app.WEBHOOK_HOST, app.WEBHOOK_HOST_IPV4]
    assert app.WEBHOOK_HOST == "::"


@pytest.mark.parametrize("code", [errno.EADDRINUSE, errno.EACCES])
def test_IPv6が使えないこと以外のエラーではIPv4で開き直さない(
    monkeypatch: pytest.MonkeyPatch, env: None, code: int
) -> None:
    monkeypatch.setenv("WEBHOOK_TOKEN", "w" * 32)
    hosts: list[str] = []

    class FakeServer:
        def __init__(self, token, event, *, host, port):  # noqa: ANN001
            hosts.append(host)
            raise OSError(code, "bind failed")

    monkeypatch.setattr(app, "WakeServer", FakeServer)
    with pytest.raises(OSError) as e:
        app.main([])
    assert e.value.errno == code
    assert hosts == [app.WEBHOOK_HOST]


def _ipv6_available() -> bool:
    try:
        with socket.socket(socket.AF_INET6) as s:
            s.bind(("::1", 0))
    except OSError:
        return False
    return True


@pytest.mark.skipif(not _ipv6_available(), reason="IPv6 が使えない")
@pytest.mark.parametrize("code", [errno.EPROTONOSUPPORT, errno.EAFNOSUPPORT, errno.ENOPROTOOPT])
def test_IPV6_V6ONLYを外せなければIPv4で開き直さず起動を失敗させる(
    monkeypatch: pytest.MonkeyPatch, env: None, code: int
) -> None:
    monkeypatch.setenv("WEBHOOK_TOKEN", "w" * 32)
    monkeypatch.setenv("WEBHOOK_PORT", "0")
    real = socket.socket.setsockopt
    hosts: list[str] = []
    real_server = app.WakeServer

    def setsockopt(self, level, option, value):  # noqa: ANN001, ANN202
        if level == socket.IPPROTO_IPV6 and option == socket.IPV6_V6ONLY:
            raise OSError(code, "cannot clear IPV6_V6ONLY")
        return real(self, level, option, value)

    def spy(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        hosts.append(kwargs["host"])
        return real_server(*args, **kwargs)

    monkeypatch.setattr(socket.socket, "setsockopt", setsockopt)
    monkeypatch.setattr(app, "WakeServer", spy)
    with pytest.raises(DualStackError):
        app.main([])
    assert hosts == [app.WEBHOOK_HOST]
