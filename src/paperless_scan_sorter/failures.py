"""書類ごとの失敗回数を JSON ファイルに残す。

再起動しても回数を引き継ぐ。ファイルが無い・壊れている場合は空として扱い、
処理を止めない。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


class FailureStore:
    """`{"<doc_id>": <回数>}` 形式のファイルで失敗回数を管理する。"""

    def __init__(self, path: Path) -> None:
        self._path = path

    def count(self, doc_id: int) -> int:
        """書類の失敗回数を返す。記録が無ければ 0。"""
        return self._load().get(str(doc_id), 0)

    def increment(self, doc_id: int) -> int:
        """失敗回数を 1 増やして保存し、増やした後の回数を返す。"""
        data = self._load()
        key = str(doc_id)
        data[key] = data.get(key, 0) + 1
        self._save(data)
        return data[key]

    def clear(self, doc_id: int) -> None:
        """書類の失敗回数の記録を消す。"""
        data = self._load()
        if data.pop(str(doc_id), None) is not None:
            self._save(data)

    def _load(self) -> dict[str, int]:
        """ファイルを読む。読めない・形が違うときは警告して空を返す。"""
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            logger.warning("失敗回数のファイルを読めないため空として扱います: %s", type(e).__name__)
            return {}
        if not isinstance(raw, dict) or not all(
            isinstance(v, int) and not isinstance(v, bool) for v in raw.values()
        ):
            logger.warning("失敗回数のファイルの形式が不正なため空として扱います")
            return {}
        return {str(k): v for k, v in raw.items()}

    def _save(self, data: dict[str, int]) -> None:
        """同じディレクトリの一時ファイルに書いてから置き換える。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, prefix=".failures-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, self._path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
