"""Paperless のワークフローの Webhook を受けて、待機中のループを起こす。

受け取るのは「今すぐ周期を回してほしい」という合図だけで、本文の中身（書類 ID など）は
信頼せずに読み捨てる。処理する書類は、起こされた周期がいつもどおりタグで探す。
"""

from __future__ import annotations

import hmac
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logger = logging.getLogger(__name__)

PATH = "/wake"
# Paperless の Webhook は書類の情報を本文に付けうる。読み捨てるだけなので小さく制限する。
MAX_BODY_BYTES = 64 * 1024


class WakeServer:
    """`POST /wake` を受けて event を立てる HTTP サーバー。別スレッドで待ち受ける。"""

    def __init__(self, token: str, event: threading.Event, *, host: str, port: int) -> None:
        expected = f"Bearer {token}".encode()

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - http.server が決める名前
                if self.path != PATH:
                    self._reply(404)
                    return
                given = self.headers.get("Authorization", "").encode()
                if not hmac.compare_digest(given, expected):
                    logger.warning("Webhook の合言葉が合わないので捨てる")
                    self._reply(401)
                    return
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = -1
                if not 0 <= length <= MAX_BODY_BYTES:
                    self._reply(413)
                    return
                self.rfile.read(length)
                logger.info("Webhook を受けたので次の周期を早める")
                event.set()
                self._reply(204)

            def do_GET(self) -> None:  # noqa: N802
                self._reply(405)

            do_PUT = do_DELETE = do_PATCH = do_GET  # noqa: N815

            def _reply(self, status: int) -> None:
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                # 既定は stderr に要求行を出す。合言葉は要求行に無いが、ログの書式を揃えるため
                # 状態コードだけを DEBUG で残す。
                logger.debug("Webhook の要求: %s", args[1] if len(args) > 1 else "")

        self._httpd = ThreadingHTTPServer((host, port), Handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    def start(self) -> None:
        self._thread.start()
        logger.info("Webhook の受け口をポート %d で開いた", self.port)

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)
