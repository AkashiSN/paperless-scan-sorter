"""書類ごとの処理の状態（失敗回数・新規作成の有無）を JSON ファイルに残す。

再起動しても回数を引き継ぐ。ファイルが無い場合はどちらも空として扱う。壊れている場合、
失敗回数は空として扱って処理を止めないが、新規作成の記録は利用不可として周期を止める
（記録を失うと、AI が作った名前が人の確認を経ずに通るため。D-009）。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

from paperless_scan_sorter.errors import ServiceUnavailable

logger = logging.getLogger(__name__)


def _read_json(path: Path) -> object | None:
    """ファイルを読む。無ければ None。読めない・JSON でなければ OSError か ValueError。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def _save_json(path: Path, data: object) -> None:
    """同じディレクトリの一時ファイルに書いてから置き換える。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


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
            raw = _read_json(self._path)
        except (OSError, ValueError) as e:
            logger.warning("失敗回数のファイルを読めないため空として扱います: %s", type(e).__name__)
            return {}
        if raw is None:
            return {}
        if not isinstance(raw, dict) or not all(
            isinstance(v, int) and not isinstance(v, bool) for v in raw.values()
        ):
            logger.warning("失敗回数のファイルの形式が不正なため空として扱います")
            return {}
        return {str(k): v for k, v in raw.items()}

    def _save(self, data: dict[str, int]) -> None:
        _save_json(self._path, data)


class CreatedStore:
    """この分類器が発行元・文書タイプを新規作成する書類の ID を `[<doc_id>, …]` 形式で残す。

    作成の後に PATCH が失敗すると、次の試行では作った名前が既存として見え、確認待ちを
    経ずに ai-done に進んでしまう。それを防ぐため、作成を試みる前に記録し、書き戻しが
    済むか ai-failed に送るまで残す（D-009）。ファイルが読めない・形が違うときは
    ServiceUnavailable を送出し、中身を空とみなして上書きすることはしない。
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    def check(self) -> None:
        """ファイルを読めるか確かめる。読めなければ ServiceUnavailable。"""
        self._load()

    def contains(self, doc_id: int) -> bool:
        return doc_id in self._load()

    def mark(self, doc_id: int) -> None:
        """書類を記録する。既に記録があれば何もしない。"""
        ids = self._load()
        if doc_id not in ids:
            ids.add(doc_id)
            _save_json(self._path, sorted(ids))

    def clear(self, doc_id: int) -> None:
        ids = self._load()
        if doc_id in ids:
            ids.discard(doc_id)
            _save_json(self._path, sorted(ids))

    def _load(self) -> set[int]:
        """ファイルを読む。無ければ空。読めない・形が違うときは ServiceUnavailable。"""
        try:
            raw = _read_json(self._path)
        except (OSError, ValueError) as e:
            raw = e
        if raw is None:
            return set()
        if isinstance(raw, list) and all(
            isinstance(v, int) and not isinstance(v, bool) for v in raw
        ):
            return set(raw)
        # 中身は書類の ID だけだが、壊れ方によっては何が入っているか分からないので出さない。
        reason = type(raw).__name__ if isinstance(raw, Exception) else "形式が不正"
        logger.error("新規作成の記録 %s を読めない: %s", self._path, reason)
        raise ServiceUnavailable("新規作成の記録を読めない")
