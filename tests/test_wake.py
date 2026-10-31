"""Webhook の受け口のテスト（実際に 127.0.0.1 の空きポートで待ち受ける）。"""

from __future__ import annotations

import errno
import logging
import socket
import threading
import time
from collections.abc import Iterator

import httpx
import pytest

from paperless_scan_sorter.wake import MAX_BODY_BYTES, WakeServer

TOKEN = "w" * 32


@pytest.fixture
def server() -> Iterator[tuple[WakeServer, threading.Event]]:
    event = threading.Event()
    srv = WakeServer(TOKEN, event, host="127.0.0.1", port=0)
    srv.start()
    try:
        yield srv, event
    finally:
        srv.close()


def _url(srv: WakeServer, path: str = "/wake") -> str:
    return f"http://127.0.0.1:{srv.port}{path}"


def test_合言葉が合えば起こして204(server: tuple[WakeServer, threading.Event]) -> None:
    srv, event = server
    r = httpx.post(_url(srv), headers={"Authorization": f"Bearer {TOKEN}"}, json={"doc": 1})
    assert r.status_code == 204
    assert event.is_set()


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN}, {"X-Token": TOKEN}],
)
def test_合言葉が無いか違えば401で起こさない(
    server: tuple[WakeServer, threading.Event], headers: dict[str, str]
) -> None:
    srv, event = server
    assert httpx.post(_url(srv), headers=headers).status_code == 401
    assert not event.is_set()


def test_ほかのパスは404(server: tuple[WakeServer, threading.Event]) -> None:
    srv, event = server
    r = httpx.post(_url(srv, "/other"), headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 404
    assert not event.is_set()


def test_POST以外は405(server: tuple[WakeServer, threading.Event]) -> None:
    srv, event = server
    r = httpx.get(_url(srv), headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 405
    assert not event.is_set()


def test_大きすぎる本文は413で起こさない(server: tuple[WakeServer, threading.Event]) -> None:
    srv, event = server
    r = httpx.post(
        _url(srv),
        headers={"Authorization": f"Bearer {TOKEN}"},
        content=b"x" * (MAX_BODY_BYTES + 1),
    )
    assert r.status_code == 413
    assert not event.is_set()


def test_合言葉と本文をログに出さない(
    server: tuple[WakeServer, threading.Event], caplog: pytest.LogCaptureFixture
) -> None:
    srv, _ = server
    with caplog.at_level(logging.DEBUG):
        httpx.post(_url(srv), headers={"Authorization": f"Bearer {TOKEN}"}, content=b"secret-body")
        httpx.post(_url(srv), headers={"Authorization": "Bearer guess"})
    assert TOKEN not in caplog.text
    assert "guess" not in caplog.text
    assert "secret-body" not in caplog.text


def test_closeすると待ち受けをやめる() -> None:
    srv = WakeServer(TOKEN, threading.Event(), host="127.0.0.1", port=0)
    srv.start()
    port = srv.port
    srv.close()
    with pytest.raises(httpx.TransportError):
        httpx.post(f"http://127.0.0.1:{port}/wake", timeout=1)


def _raw(srv: WakeServer) -> socket.socket:
    return socket.create_connection(("127.0.0.1", srv.port), timeout=5)


def _status(sock: socket.socket) -> int:
    return int(sock.recv(64).split()[1])


def test_途中で止まった接続は読み取りの期限で切る() -> None:
    srv = WakeServer(TOKEN, threading.Event(), host="127.0.0.1", port=0, read_timeout=0.2)
    srv.start()
    try:
        with _raw(srv) as sock:
            sock.sendall(b"POST /wake HTTP/1.1\r\nAuthor")
            start = time.monotonic()
            sock.recv(64)  # 期限で切られれば空か 408 が返る
            assert time.monotonic() - start < 3
    finally:
        srv.close()


def test_本文が届かなければ期限で切り起こさない() -> None:
    event = threading.Event()
    srv = WakeServer(TOKEN, event, host="127.0.0.1", port=0, read_timeout=0.2)
    srv.start()
    try:
        with _raw(srv) as sock:
            sock.sendall(
                f"POST /wake HTTP/1.1\r\nAuthorization: Bearer {TOKEN}\r\n"
                "Content-Length: 100\r\n\r\nabc".encode()
            )
            start = time.monotonic()
            sock.recv(64)
            assert time.monotonic() - start < 3
        assert not event.is_set()
    finally:
        srv.close()


def test_同時接続の上限を超えた接続はすぐ閉じる() -> None:
    srv = WakeServer(
        TOKEN, threading.Event(), host="127.0.0.1", port=0, read_timeout=2.0, max_connections=2
    )
    srv.start()
    held = [_raw(srv), _raw(srv)]
    try:
        time.sleep(0.2)
        with _raw(srv) as extra:
            start = time.monotonic()
            assert extra.recv(64) == b""
            assert time.monotonic() - start < 1.5
    finally:
        for s in held:
            s.close()
        srv.close()


def test_処理中の接続があってもcloseは期限内に終わる() -> None:
    srv = WakeServer(TOKEN, threading.Event(), host="127.0.0.1", port=0, read_timeout=0.3)
    srv.start()
    sock = _raw(srv)
    try:
        sock.sendall(b"POST /wake HTTP/1.1\r\n")
        time.sleep(0.1)
        start = time.monotonic()
        srv.close()
        assert time.monotonic() - start < 3
    finally:
        sock.close()


@pytest.mark.parametrize(
    ("extra", "status"),
    [
        ("Transfer-Encoding: chunked\r\n", 400),
        ("Transfer-Encoding: chunked\r\nContent-Length: 3\r\n", 400),
        ("Content-Length: -1\r\n", 400),
        ("Content-Length: abc\r\n", 400),
        ("", 204),
    ],
)
def test_本文の長さの扱い(extra: str, status: int) -> None:
    event = threading.Event()
    srv = WakeServer(TOKEN, event, host="127.0.0.1", port=0, read_timeout=1.0)
    srv.start()
    try:
        with _raw(srv) as sock:
            sock.sendall(
                f"POST /wake HTTP/1.1\r\nAuthorization: Bearer {TOKEN}\r\n{extra}\r\n".encode()
            )
            assert _status(sock) == status
        assert event.is_set() is (status == 204)
    finally:
        srv.close()


def test_少しずつ送り続ける接続も接続からの期限で切る() -> None:
    srv = WakeServer(TOKEN, threading.Event(), host="127.0.0.1", port=0, read_timeout=0.3)
    srv.start()
    sock = _raw(srv)
    start = time.monotonic()
    cut = False
    try:
        sock.sendall(b"POST /wake HTTP/1.1\r\n")
        # 無通信の期限（0.3 秒）より短い間隔でヘッダーを 1 バイトずつ送り続ける。
        while time.monotonic() - start < 5:
            try:
                sock.sendall(b"X")
            except OSError:
                cut = True
                break
            sock.settimeout(0.05)
            try:
                if sock.recv(64) == b"":
                    cut = True
                    break
            except TimeoutError:
                pass
        assert cut
        assert time.monotonic() - start < 2
    finally:
        sock.close()
        close_start = time.monotonic()
        srv.close()
        assert time.monotonic() - close_start < 2


def test_期限のタイマーを開始できなくても接続枠を返し接続を閉じる(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import paperless_scan_sorter.wake as wake

    class BrokenTimer:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
            self.daemon = False

        def start(self) -> None:
            raise RuntimeError("can't start new thread")

        def cancel(self) -> None:
            pass

    srv = WakeServer(
        TOKEN, threading.Event(), host="127.0.0.1", port=0, read_timeout=1.0, max_connections=1
    )
    srv.start()
    try:
        monkeypatch.setattr(wake.threading, "Timer", BrokenTimer)
        with _raw(srv) as sock:
            assert sock.recv(64) == b""
        monkeypatch.undo()
        # 枠が 1 つだけでも、返っていれば次の要求を受けられる。
        r = httpx.post(_url(srv), headers={"Authorization": f"Bearer {TOKEN}"})
        assert r.status_code == 204
    finally:
        srv.close()


def _ipv6_available() -> bool:
    try:
        with socket.socket(socket.AF_INET6) as s:
            s.bind(("::1", 0))
    except OSError:
        return False
    return True


needs_ipv6 = pytest.mark.skipif(not _ipv6_available(), reason="IPv6 が使えない")


@needs_ipv6
def test_コロンを含むホストではIPv6とIPv4の両方で受ける() -> None:
    event = threading.Event()
    srv = WakeServer(TOKEN, event, host="::", port=0)
    srv.start()
    try:
        headers = {"Authorization": f"Bearer {TOKEN}"}
        r6 = httpx.post(f"http://[::1]:{srv.port}/wake", headers=headers)
        r4 = httpx.post(f"http://127.0.0.1:{srv.port}/wake", headers=headers)
        assert (r6.status_code, r4.status_code) == (204, 204)
        assert event.is_set()
    finally:
        srv.close()


@needs_ipv6
def test_IPV6_V6ONLYを外せなければ開かない(monkeypatch: pytest.MonkeyPatch) -> None:
    real = socket.socket.setsockopt

    def setsockopt(self, level, option, value):  # noqa: ANN001, ANN202
        if level == socket.IPPROTO_IPV6 and option == socket.IPV6_V6ONLY:
            raise OSError(errno.ENOPROTOOPT, "Protocol not available")
        return real(self, level, option, value)

    monkeypatch.setattr(socket.socket, "setsockopt", setsockopt)
    with pytest.raises(OSError) as e:
        WakeServer(TOKEN, threading.Event(), host="::", port=0)
    assert e.value.errno == errno.ENOPROTOOPT
