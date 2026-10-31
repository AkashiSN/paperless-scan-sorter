"""Claude Code CLI（`claude -p`）を非対話で起動し、応答の `result` を取り出す。"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from paperless_scan_sorter.errors import CliFailure, DocumentFailure, ServiceUnavailable

logger = logging.getLogger(__name__)

# 子プロセスに渡さない環境変数。API キーが残ると従量課金になり、Paperless のトークンは
# claude が使わないうえ、書類の本文に誘導されて漏れる経路を残さないため。
_DROPPED_ENV = frozenset({"ANTHROPIC_API_KEY", "PAPERLESS_TOKEN", "PAPERLESS_TOKEN_FILE"})

# 起動のたびに付ける引数。
# - --no-session-persistence: 本文・PDF の読み取り結果・応答を ~/.claude の履歴に残さない
# - dontAsk: 事前に許可していない操作は確認せずに拒否する
# - --setting-sources "": ユーザー・プロジェクト・ローカルの設定ファイルを読まない
# - --strict-mcp-config: --mcp-config を渡さないので、MCP サーバーを 1 つも使わない
# - --restricted: ファイル系ツールを作業ディレクトリ内に閉じ、bypassPermissions を拒む
# 後ろ 4 つは書類からの指示で許可外のツールが動かないようにするためのもの。
# 使えるツールの集合そのものは --tools で決める（--allowedTools は許可ルールで、
# 集合を狭めない）。
_LOCKDOWN_ARGS = (
    "--no-session-persistence", "--permission-mode", "dontAsk",
    "--setting-sources", "", "--strict-mcp-config", "--restricted",
)  # fmt: skip

# 応答の JSON が無いとき、stderr（小文字にしたもの）にこれらを含めば、認証・利用枠・
# ネットワークの障害とみなして周期を打ち切る。含まなければ書類起因の CLI の失敗とする（D-010）。
_OUTAGE_MARKERS = (
    "auth", "login", "token", "401", "403", "rate limit", "usage limit", "quota",
    "network", "econnrefused", "econnreset", "enotfound", "etimedout", "getaddrinfo",
    "socket",
)  # fmt: skip

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
    """`claude -p` に PDF を読ませ、応答の文字列を返す。"""

    def __init__(
        self,
        *,
        model: str,
        timeout: float = 600.0,
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

    def read_pdf(self, prompt: str, pdf: bytes) -> str:
        """PDF を一時ディレクトリへ置き、そのファイルだけ読ませて応答の result を返す。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp).resolve() / "doc.pdf"
            path.write_bytes(pdf)
            # 権限ルールの `/path` は設定ディレクトリからの相対になる。絶対パスは
            # `//path` と書くので、絶対パスの前にもう 1 つ `/` を付ける。使えるツールは
            # --tools で Read だけにし、--allowedTools でそのファイルの読み取りだけを許可する。
            # stdin は使わないので、読むファイルの場所はプロンプトの末尾で伝える。
            prompt = f"{prompt}\n書類の PDF は {path} にあります。Read ツールで読んでください。"
            argv = [
                self._executable, "-p", "--model", self._model,
                "--max-turns", "3", "--output-format", "json", *_LOCKDOWN_ARGS,
                "--tools", "Read", "--allowedTools", f"Read(/{path})", "--", prompt,
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
            stderr = proc.stderr or ""
            logger.error(
                "claude の出力を JSON として読めない: returncode=%s stderr=%s",
                proc.returncode,
                stderr[:200],
            )
            lowered = stderr.lower()
            if any(marker in lowered for marker in _OUTAGE_MARKERS):
                raise ServiceUnavailable("claude が認証・利用枠・ネットワークの障害で終わった")
            raise CliFailure("claude の出力を JSON として読めない")

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
