"""常駐ループの入口。"""

from __future__ import annotations

import argparse
import contextlib
import errno
import logging
import os
import signal
import threading
from collections.abc import Sequence
from datetime import date
from types import FrameType

from paperless_scan_sorter.claude import ClaudeClient
from paperless_scan_sorter.config import load_settings
from paperless_scan_sorter.errors import MissingTagsError
from paperless_scan_sorter.failures import CreatedStore, FailureStore
from paperless_scan_sorter.originals import OriginalsStore
from paperless_scan_sorter.page_processor import PageCycleResult, run_page_cycle
from paperless_scan_sorter.paperless import PaperlessClient
from paperless_scan_sorter.processor import CycleResult, run_cycle
from paperless_scan_sorter.wake import WakeServer

logger = logging.getLogger(__name__)

# 停止要求。処理中ならその周期の終わりで止まる。
_stop = threading.Event()
# 待機を打ち切る合図。停止要求と Webhook の両方が立てる。周期の途中で立てば、
# 周期が終わった直後にもう 1 周期回す（その間に取り込まれた書類を拾うため）。
_wake = threading.Event()

# Webhook の受け口はコンテナの中のネットワークで待ち受ける。ホストには公開しない。
# IPv6 と IPv4 の両方で受け、コンテナで IPv6 が使えなければ IPv4 だけで受ける。
WEBHOOK_HOST = "::"
WEBHOOK_HOST_IPV4 = "0.0.0.0"  # noqa: S104 - Compose の内部ネットワークからの要求を受けるため
# IPv4 だけで開き直すのは、IPv6 そのものが使えないときだけ。ポートの使用中や権限の不足などを
# 隠して IPv4 だけで動くと、IPv6 のアドレスに接続する Paperless の Webhook が届かない。
_IPV6_UNAVAILABLE = frozenset({errno.EAFNOSUPPORT, errno.EPROTONOSUPPORT, errno.EADDRNOTAVAIL})


def request_stop() -> None:
    """ループに停止を要求する。"""
    _stop.set()
    _wake.set()


def request_wake() -> None:
    """待機を打ち切り、すぐに次の周期を回させる。"""
    _wake.set()


def _wait(timeout: float) -> bool:
    """次の周期まで待つ。停止を要求されていれば True。"""
    _wake.wait(timeout)
    _wake.clear()
    return _stop.is_set()


def _on_signal(signum: int, frame: FrameType | None) -> None:
    logger.info("シグナル %s を受けたので停止する", signum)
    request_stop()


def _summary(r: CycleResult) -> str:
    return (
        f"周期完了: done={r.done} review={r.review} retried={r.retried} "
        f"failed={r.failed} aborted={r.aborted}"
    )


def _page_summary(r: PageCycleResult) -> str:
    return (
        f"ページ整形: fixed={r.fixed} unchanged={r.unchanged} review={r.review} aborted={r.aborted}"
    )


def main(argv: Sequence[str] | None = None) -> int:
    """設定を読み、`--once` なら 1 周期、そうでなければ停止要求まで周期を回す。

    常駐するときは INTERVAL 秒ごとに周期を回す。WEBHOOK_TOKEN があれば Webhook の受け口を
    開き、受けたら待たずに次の周期を回す。

    1 周期ではページ整形を先に回し、直した書類をその回の分類で処理する。片方が
    想定外の例外で止まっても、もう片方は回す。
    """
    parser = argparse.ArgumentParser(prog="paperless-scan-sorter")
    parser.add_argument("--once", action="store_true", help="1 周期だけ回して終わる")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # httpx は INFO で要求ごとの URL を出し、周期ごとにログが埋まるので警告以上に絞る。
    logging.getLogger("httpx").setLevel(logging.WARNING)

    try:
        settings = load_settings(os.environ)
    except ValueError as e:
        logger.error("設定の不備: %s", e)
        return 2

    _stop.clear()
    _wake.clear()
    if not args.once and threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, _on_signal)
        signal.signal(signal.SIGINT, _on_signal)

    settings.state_dir.mkdir(parents=True, exist_ok=True)
    store = FailureStore(settings.state_dir / "failures.json")
    created = CreatedStore(settings.state_dir / "created.json")
    originals = OriginalsStore(settings.state_dir / "originals")
    claude = ClaudeClient(model=settings.model, timeout=settings.claude_timeout, env=os.environ)
    with contextlib.ExitStack() as cleanup:
        paperless = PaperlessClient(settings.paperless_url, settings.paperless_token)
        cleanup.callback(paperless.close)
        if not args.once and settings.webhook_token is not None:
            try:
                server = WakeServer(
                    settings.webhook_token, _wake, host=WEBHOOK_HOST, port=settings.webhook_port
                )
            except OSError as e:
                if e.errno not in _IPV6_UNAVAILABLE:
                    raise
                logger.warning("IPv6 が使えないので Webhook の受け口を IPv4 だけで開く: %s", e)
                server = WakeServer(
                    settings.webhook_token,
                    _wake,
                    host=WEBHOOK_HOST_IPV4,
                    port=settings.webhook_port,
                )
            cleanup.callback(server.close)
            server.start()
        while True:
            failed = False
            try:
                logger.info(_page_summary(run_page_cycle(paperless, originals)))
            except MissingTagsError as e:
                logger.error("設定の見直しが必要: %s", e)
                return 1
            except Exception:
                logger.exception("ページ整形の周期で想定外の例外")
                failed = True
            try:
                result = run_cycle(paperless, claude, store, created, today=date.today())
                logger.info(_summary(result))
            except MissingTagsError as e:
                logger.error("設定の見直しが必要: %s", e)
                return 1
            except Exception:
                logger.exception("周期の処理で想定外の例外")
                failed = True
            if args.once:
                return 1 if failed else 0
            if _wait(settings.interval):
                return 0


if __name__ == "__main__":
    raise SystemExit(main())
