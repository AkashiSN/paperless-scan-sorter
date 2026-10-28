"""書類のページを直す前に、元の PDF を状態ディレクトリへ退避する。

退避したファイルは `{書類ID}.pdf` の名前で置く。同じ書類の原本は最初の 1 つだけを残し、
上書きしない。退避に失敗したときは書き換えてはいけないため、呼び出し側へ
ServiceUnavailable を送って周期を止める。古い退避ファイルは周期の始めに消す。
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

from paperless_scan_sorter.errors import ServiceUnavailable

logger = logging.getLogger(__name__)

RETENTION_DAYS = 90
_DAY_SECONDS = 24 * 60 * 60


class OriginalsStore:
    """元の PDF を書類 ID ごとに保存し、保存期間を過ぎたものを消す。"""

    def __init__(self, directory: Path) -> None:
        self._dir = directory

    def save(self, doc_id: int, pdf: bytes) -> None:
        """書類の元の PDF を保存する。既に保存されていれば何もしない。

        書けなければ ServiceUnavailable を送出する。一時ファイルは同じディレクトリに作り、
        置き換えに失敗したら消す。
        """
        target = self._dir / f"{doc_id}.pdf"
        if target.exists():
            return
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self._dir, prefix=f".{doc_id}-", suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(pdf)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, target)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
        except OSError as e:
            logger.error("書類 %s の元の PDF を退避できない: %s", doc_id, type(e).__name__)
            raise ServiceUnavailable("元の PDF を退避できない") from e

    def prune(self, now: float) -> int:
        """更新時刻が保存期間より古い `*.pdf` を消し、消した数を返す。

        ディレクトリが無ければ 0 を返す。個々の削除に失敗したら警告して続ける。
        """
        if not self._dir.is_dir():
            return 0
        cutoff = now - RETENTION_DAYS * _DAY_SECONDS
        removed = 0
        for path in self._dir.glob("*.pdf"):
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError as e:
                logger.warning("退避ファイル %s を消せない: %s", path.name, type(e).__name__)
        return removed
