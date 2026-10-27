"""常駐ループの入口。"""

from __future__ import annotations

import argparse
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
from paperless_scan_sorter.paperless import PaperlessClient
from paperless_scan_sorter.processor import CycleResult, run_cycle

logger = logging.getLogger(__name__)

# 停止要求。待機中なら wait が即座に返り、処理中ならその周期の終わりで止まる。
_stop = threading.Event()


def request_stop() -> None:
    """ループに停止を要求する。"""
    _stop.set()


def _on_signal(signum: int, frame: FrameType | None) -> None:
    logger.info("シグナル %s を受けたので停止する", signum)
    request_stop()


def _summary(r: CycleResult) -> str:
    return (
        f"周期完了: done={r.done} review={r.review} retried={r.retried} "
        f"failed={r.failed} aborted={r.aborted}"
    )


def main(argv: Sequence[str] | None = None) -> int:
    """設定を読み、`--once` なら 1 周期、そうでなければ停止要求まで周期を回す。"""
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
    if not args.once and threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, _on_signal)
        signal.signal(signal.SIGINT, _on_signal)

    settings.state_dir.mkdir(parents=True, exist_ok=True)
    store = FailureStore(settings.state_dir / "failures.json")
    created = CreatedStore(settings.state_dir / "created.json")
    claude = ClaudeClient(model=settings.model, timeout=settings.claude_timeout, env=os.environ)
    paperless = PaperlessClient(settings.paperless_url, settings.paperless_token)
    try:
        while True:
            try:
                result = run_cycle(paperless, claude, store, created, today=date.today())
                logger.info(_summary(result))
            except MissingTagsError as e:
                logger.error("設定の見直しが必要: %s", e)
                return 1
            except Exception:
                logger.exception("周期の処理で想定外の例外")
                if args.once:
                    return 1
            if args.once or _stop.wait(settings.interval):
                return 0
    finally:
        paperless.close()


if __name__ == "__main__":
    raise SystemExit(main())
