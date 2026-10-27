"""Claude Code CLI（`claude -p`）を非対話で起動し、応答の `result` を取り出す。"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from paperless_scan_sorter.errors import DocumentFailure, ServiceUnavailable

logger = logging.getLogger(__name__)

# 子プロセスに渡さない環境変数。API キーが残ると従量課金になり、Paperless のトークンは
# claude が使わないうえ、書類の本文に誘導されて漏れる経路を残さないため。
_DROPPED_ENV = frozenset({"ANTHROPIC_API_KEY", "PAPERLESS_TOKEN", "PAPERLESS_TOKEN_FILE"})

# (argv, stdin_text, cwd, env, timeout)
type Runner = Callable[
    [Sequence[str], str | None, Path, Mapping[str, str], float],
    subprocess.CompletedProcess[str],
]


def run_subprocess(
    argv: Sequence[str],
    stdin_text: str | None,
    cwd: Path,
    env: Mapping[str, str],
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    """既定の実行関数。引数配列のまま起動し、シェルは経由しない。"""
    return subprocess.run(  # noqa: S603 - 引数配列で起動し、シェルを使わない
        argv,
        input=stdin_text,
        cwd=cwd,
        env=env,
        timeout=timeout,
        capture_output=True,
        text=True,
        check=False,
    )


class ClaudeClient:
    """`claude -p` を呼び、分類結果の文字列を返す。"""

    def __init__(
        self,
        *,
        model: str,
        timeout: float = 300.0,
        executable: str = "claude",
        runner: Runner = run_subprocess,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._model = model
        self._timeout = timeout
        self._executable = executable
        self._runner = runner
        source = os.environ if env is None else env
        self._env = {k: v for k, v in source.items() if k not in _DROPPED_ENV}

    def classify_text(self, prompt: str, content: str) -> str:
        """本文を stdin で渡す。ツールは許可しない。"""
        with tempfile.TemporaryDirectory() as tmp:
            argv = [
                self._executable, "-p", "--model", self._model,
                "--max-turns", "1", "--output-format", "json", prompt,
            ]  # fmt: skip
            return self._run(argv, content, Path(tmp))

    def classify_pdf(self, prompt: str, pdf: bytes) -> str:
        """PDF を一時ディレクトリへ置き、そのファイルだけ読ませる。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp).resolve() / "doc.pdf"
            path.write_bytes(pdf)
            # 権限ルールの `/path` は設定ディレクトリからの相対になる。絶対パスは
            # `//path` と書くので、絶対パスの前にもう 1 つ `/` を付ける。
            argv = [
                self._executable, "-p", "--model", self._model,
                "--max-turns", "3", "--output-format", "json",
                "--allowedTools", f"Read(/{path})", prompt,
            ]  # fmt: skip
            return self._run(argv, None, path.parent)

    def _run(self, argv: Sequence[str], stdin: str | None, cwd: Path) -> str:
        try:
            proc = self._runner(argv, stdin, cwd, self._env, self._timeout)
        except subprocess.TimeoutExpired as e:
            raise DocumentFailure("claude がタイムアウトした") from e
        except FileNotFoundError as e:
            raise ServiceUnavailable("claude コマンドが見つからない") from e

        try:
            data = json.loads(proc.stdout)
        except ValueError:
            data = None
        if not isinstance(data, dict):
            logger.error(
                "claude の出力を JSON として読めない: returncode=%s stderr=%s",
                proc.returncode,
                (proc.stderr or "")[:200],
            )
            raise ServiceUnavailable("claude の出力を JSON として読めない")

        if data.get("is_error"):
            if data.get("subtype") == "error_max_turns":
                raise DocumentFailure("claude がターン上限に達した")
            # is_error のときの result は CLI のエラーメッセージで、原因の切り分けに要る。
            message = data.get("result")
            message = message if isinstance(message, str) else ""
            logger.error(
                "claude がエラーを返した: subtype=%s result=%s",
                data.get("subtype"),
                message[:200],
            )
            # 書類が長すぎるエラーは書類を変えない限り繰り返すので、書類起因として数える。
            # それ以外（利用枠・認証・未知のエラー）は数えずに周期を打ち切る（D-008）。
            if "prompt is too long" in message.lower():
                raise DocumentFailure("claude の入力が長すぎる")
            raise ServiceUnavailable("claude がエラーを返した（利用枠・認証など）")

        result = data.get("result")
        if not isinstance(result, str):
            raise DocumentFailure("claude の result が文字列でない")
        return result
