"""Paperless のワークフローの Webhook を受けて、待機中のループを起こす。

受け取るのは「今すぐ周期を回してほしい」という合図だけで、本文の中身（書類 ID など）は
信頼せずに読み捨てる。処理する書類は、起こされた周期がいつもどおりタグで探す。
"""

from __future__ import annotations

import contextlib
import hmac
import logging
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logger = logging.getLogger(__name__)

PATH = "/wake"
# Paperless の Webhook は書類の情報を本文に付けうる。読み捨てるだけなので小さく制限する。
MAX_BODY_BYTES = 64 * 1024
# 1 接続の期限と、同時に扱う接続の上限。少しずつ送り続けたり途中で止まったりする接続で
# スレッドが溜まらないようにする。Webhook は 1 回の要求で終わるので、どちらも小さくてよい。
DEFAULT_READ_TIMEOUT = 5.0
DEFAULT_MAX_CONNECTIONS = 8


class _Server(ThreadingHTTPServer):
    """同時接続数を slots で制限し、終了時に処理中のハンドラを待つ HTTP サーバー。

    各接続は受け付けてから deadline 秒でソケットを閉じる。ソケットの timeout は無通信の
    時間にしか効かず、少しずつ送り続ける接続を止められないため。
    """

    daemon_threads = False
    block_on_close = True

    def __init__(
        self, address: tuple[str, int], handler: type, max_connections: int, deadline: float
    ) -> None:
        super().__init__(address, handler)
        self._slots = threading.BoundedSemaphore(max_connections)
        self._deadline = deadline

    def process_request(self, request: socket.socket, client_address: object) -> None:
        if not self._slots.acquire(blocking=False):
            # 上限を超えた接続は、読まずにすぐ閉じる。
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request: socket.socket, client_address: object) -> None:
        try:
            timer = threading.Timer(self._deadline, _cut, args=(request,))
            timer.daemon = True
            try:
                timer.start()
            except RuntimeError:
                # スレッドを作れないときは、期限を付けられないので読まずに閉じる。
                logger.warning("Webhook の接続に期限を付けられないので閉じる")
                self.shutdown_request(request)
                return
            try:
                super().process_request_thread(request, client_address)
            finally:
                timer.cancel()
        finally:
            self._slots.release()


def _cut(request: socket.socket) -> None:
    """期限を過ぎた接続のソケットを閉じ、読み書きで止まっているハンドラを終わらせる。"""
    # 既に閉じた接続なら何もしない。
    with contextlib.suppress(OSError):
        request.shutdown(socket.SHUT_RDWR)


class WakeServer:
    """`POST /wake` を受けて event を立てる HTTP サーバー。別スレッドで待ち受ける。"""

    def __init__(
        self,
        token: str,
        event: threading.Event,
        *,
        host: str,
        port: int,
        read_timeout: float = DEFAULT_READ_TIMEOUT,
        max_connections: int = DEFAULT_MAX_CONNECTIONS,
    ) -> None:
        expected = f"Bearer {token}".encode()

        class Handler(BaseHTTPRequestHandler):
            # 無通信の期限（StreamRequestHandler が settimeout する）。接続全体の期限は _Server。
            timeout = read_timeout

            def do_POST(self) -> None:  # noqa: N802 - http.server が決める名前
                if self.path != PATH:
                    self._reply(404)
                    return
                given = self.headers.get("Authorization", "").encode()
                if not hmac.compare_digest(given, expected):
                    logger.warning("Webhook の合言葉が合わないので捨てる")
                    self._reply(401)
                    return
                # 本文の長さは Content-Length だけで受ける。chunked などを読まずに通すと、
                # 本文の上限が効かなくなる。
                if "Transfer-Encoding" in self.headers:
                    self._reply(400)
                    return
                raw = self.headers.get("Content-Length")
                try:
                    length = 0 if raw is None else int(raw)
                except ValueError:
                    length = -1
                if length < 0:
                    self._reply(400)
                    return
                if length > MAX_BODY_BYTES:
                    self._reply(413)
                    return
                body = self.rfile.read(length)
                if len(body) != length:
                    # 期限までに本文が揃わなかった。
                    return
                logger.info("Webhook を受けたので次の周期を早める")
                event.set()
                self._reply(204)

            def do_GET(self) -> None:  # noqa: N802
                self._reply(405)

            do_PUT = do_DELETE = do_PATCH = do_GET  # noqa: N815

            def _reply(self, status: int) -> None:
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                # 既定は stderr に要求行を出す。書式を揃えるため DEBUG で残す。
                logger.debug("Webhook の要求: " + format, *args)

        self._httpd = _Server((host, port), Handler, max_connections, read_timeout)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    def start(self) -> None:
        self._thread.start()
        logger.info("Webhook の受け口をポート %d で開いた", self.port)

    def close(self) -> None:
        """待ち受けをやめ、処理中の接続が終わる（遅くとも接続の期限）まで待つ。"""
        if self._thread.is_alive():
            self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)
