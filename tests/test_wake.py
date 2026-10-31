"""Webhook の受け口のテスト（実際に 127.0.0.1 の空きポートで待ち受ける）。"""

from __future__ import annotations

import logging
import threading
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
