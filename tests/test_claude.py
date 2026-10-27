"""claude -p 起動のテスト（偽の runner のみ使い、実物の claude は呼ばない）。"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import pytest

from paperless_scan_sorter.claude import ClaudeClient
from paperless_scan_sorter.errors import DocumentFailure, ServiceUnavailable


def _ok(result: str = "{}") -> str:
    return json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": result})


class Recorder:
    """呼び出しを記録し、決められた応答を返す偽の runner。"""

    def __init__(
        self,
        stdout: str | None = None,
        returncode: int = 0,
        stderr: str = "",
        raises: Exception | None = None,
    ) -> None:
        self.stdout = _ok() if stdout is None else stdout
        self.returncode = returncode
        self.stderr = stderr
        self.raises = raises
        self.calls: list[dict[str, object]] = []

    @property
    def argv(self) -> list[str]:
        return list(self.calls[-1]["argv"])  # type: ignore[arg-type]

    @property
    def stdin(self) -> str | None:
        return self.calls[-1]["stdin"]  # type: ignore[return-value]

    @property
    def cwd(self) -> Path:
        return self.calls[-1]["cwd"]  # type: ignore[return-value]

    @property
    def env(self) -> Mapping[str, str]:
        return self.calls[-1]["env"]  # type: ignore[return-value]

    @property
    def pdf_bytes(self) -> bytes | None:
        return self.calls[-1]["pdf_bytes"]  # type: ignore[return-value]

    def __call__(
        self,
        argv: Sequence[str],
        stdin: str | None,
        cwd: Path,
        env: Mapping[str, str],
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        pdf = cwd / "doc.pdf"
        self.calls.append(
            {
                "argv": list(argv),
                "stdin": stdin,
                "cwd": cwd,
                "env": dict(env),
                "timeout": timeout,
                "listing": sorted(p.name for p in cwd.iterdir()),
                "pdf_bytes": pdf.read_bytes() if pdf.exists() else None,
            }
        )
        if self.raises is not None:
            raise self.raises
        return subprocess.CompletedProcess(list(argv), self.returncode, self.stdout, self.stderr)


def make(rec: Callable[..., object], **kw: object) -> ClaudeClient:
    return ClaudeClient(model="sonnet", runner=rec, env={"PATH": "/bin"}, **kw)  # type: ignore[arg-type]


# 両経路に共通する、ツール・設定・MCP を閉じる引数。
LOCKDOWN = [
    "--permission-mode", "dontAsk", "--setting-sources", "", "--strict-mcp-config",
    "--restricted",
]  # fmt: skip


def test_本文はstdinで渡しツールを許可しない() -> None:
    rec = Recorder()
    out = make(rec).classify_text("プロンプト", "本文")
    assert out == "{}"
    assert rec.argv == [
        "claude", "-p", "--model", "sonnet", "--max-turns", "1",
        "--output-format", "json", *LOCKDOWN, "--tools", "", "--", "プロンプト",
    ]  # fmt: skip
    assert rec.stdin == "本文"


def test_プロンプトは区切りの後に置き可変長の引数に飲まれない() -> None:
    rec = Recorder()
    make(rec).classify_text("-p から始まる文", "c")
    assert rec.argv[-2:] == ["--", "-p から始まる文"]
    make(rec).classify_pdf("-p から始まる文", b"%PDF")
    assert rec.argv[-2:] == ["--", "-p から始まる文"]


def test_作業ディレクトリは毎回空の一時ディレクトリ() -> None:
    rec = Recorder()
    client = make(rec)
    client.classify_text("p", "c")
    client.classify_text("p", "c")
    assert [c["listing"] for c in rec.calls] == [[], []]
    assert rec.calls[0]["cwd"] != rec.calls[1]["cwd"]
    assert not Path(str(rec.calls[0]["cwd"])).exists()


def test_APIキーを渡さない() -> None:
    rec = Recorder()
    client = ClaudeClient(
        model="sonnet",
        runner=rec,
        env={"ANTHROPIC_API_KEY": "k", "CLAUDE_CODE_OAUTH_TOKEN": "t", "PATH": "/bin"},
    )
    client.classify_text("p", "c")
    assert "ANTHROPIC_API_KEY" not in rec.env
    assert rec.env["CLAUDE_CODE_OAUTH_TOKEN"] == "t"


def test_envを省略するとos_environを使う(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("PSS_MARKER", "v")
    rec = Recorder()
    ClaudeClient(model="sonnet", runner=rec).classify_text("p", "c")
    assert rec.env["PSS_MARKER"] == "v"
    assert "ANTHROPIC_API_KEY" not in rec.env
    assert os.environ["ANTHROPIC_API_KEY"] == "k"


def test_PDFはそのファイルだけ読ませる() -> None:
    rec = Recorder()
    make(rec).classify_pdf("p", b"%PDF-1.7")
    pdf = rec.cwd / "doc.pdf"
    assert rec.argv == [
        "claude", "-p", "--model", "sonnet", "--max-turns", "3",
        "--output-format", "json", *LOCKDOWN, "--tools", "Read",
        "--allowedTools", f"Read(/{pdf})", "--", "p",
    ]  # fmt: skip
    # 権限ルールの `/path` は設定ディレクトリからの相対で、絶対パスは `//path` と書く。
    rule = rec.argv[rec.argv.index("--allowedTools") + 1]
    assert rule == f"Read(/{pdf})"
    assert rule.startswith("Read(//")
    assert rec.argv[rec.argv.index("--max-turns") + 1] == "3"
    assert rec.pdf_bytes == b"%PDF-1.7"
    assert rec.stdin is None


def test_タイムアウトは書類起因の失敗() -> None:
    rec = Recorder(raises=subprocess.TimeoutExpired(["claude"], 1.0))
    with pytest.raises(DocumentFailure):
        make(rec).classify_text("p", "c")


def test_ターン上限は書類起因の失敗() -> None:
    out = json.dumps({"is_error": True, "subtype": "error_max_turns", "result": "x"})
    with pytest.raises(DocumentFailure):
        make(Recorder(stdout=out)).classify_text("p", "c")


def test_利用枠や認証のエラーは周期を止める() -> None:
    out = json.dumps({"is_error": True, "subtype": "success", "result": "usage limit"})
    with pytest.raises(ServiceUnavailable):
        make(Recorder(stdout=out)).classify_text("p", "c")


def test_JSONでない出力は周期を止める() -> None:
    with pytest.raises(ServiceUnavailable):
        make(Recorder(stdout="", returncode=1, stderr="boom")).classify_text("p", "c")


def test_オブジェクトでないJSONは周期を止める() -> None:
    with pytest.raises(ServiceUnavailable):
        make(Recorder(stdout="[1, 2]")).classify_text("p", "c")


def test_CLIが無ければ周期を止める() -> None:
    rec = Recorder(raises=FileNotFoundError("claude"))
    with pytest.raises(ServiceUnavailable):
        make(rec).classify_text("p", "c")


def test_resultが文字列でなければ書類起因の失敗() -> None:
    out = json.dumps({"is_error": False, "subtype": "success", "result": None})
    with pytest.raises(DocumentFailure):
        make(Recorder(stdout=out)).classify_text("p", "c")


def test_resultは任意の文字列を許す() -> None:
    assert make(Recorder(stdout=_ok(""))).classify_text("p", "c") == ""


def test_実行ファイルとタイムアウトを渡す() -> None:
    rec = Recorder()
    ClaudeClient(
        model="m", timeout=12.5, executable="/opt/claude", runner=rec, env={}
    ).classify_text("p", "c")
    assert rec.argv[0] == "/opt/claude"
    assert rec.calls[-1]["timeout"] == 12.5


def _error(result: object, subtype: str = "success") -> str:
    return json.dumps({"is_error": True, "subtype": subtype, "result": result})


@pytest.mark.parametrize("message", ["Prompt is too long", "API Error: prompt is too long: 1 > 2"])
def test_プロンプトが長すぎるエラーは書類起因の失敗(message: str) -> None:
    with pytest.raises(DocumentFailure):
        make(Recorder(stdout=_error(message))).classify_text("p", "c")


def test_PDFでもプロンプトが長すぎるエラーは書類起因の失敗() -> None:
    with pytest.raises(DocumentFailure):
        make(Recorder(stdout=_error("Prompt is too long"))).classify_pdf("p", b"%PDF")


def test_PDFでも利用枠のエラーは周期を止める() -> None:
    with pytest.raises(ServiceUnavailable):
        make(Recorder(stdout=_error("usage limit reached"))).classify_pdf("p", b"%PDF")


def test_エラーのresultは先頭200文字だけログに出す(caplog: pytest.LogCaptureFixture) -> None:
    message = "Invalid API key" + "x" * 300
    with pytest.raises(ServiceUnavailable):
        make(Recorder(stdout=_error(message))).classify_text("p", "c")
    assert message[:200] in caplog.text
    assert message[:201] not in caplog.text


def test_エラーのresultが文字列でなくても周期を止める() -> None:
    with pytest.raises(ServiceUnavailable):
        make(Recorder(stdout=_error(None))).classify_text("p", "c")


def test_Paperlessのトークンを渡さない() -> None:
    rec = Recorder()
    ClaudeClient(
        model="sonnet",
        runner=rec,
        env={
            "PAPERLESS_TOKEN": "p",
            "PAPERLESS_TOKEN_FILE": "/run/secrets/x",
            "CLAUDE_CODE_OAUTH_TOKEN": "t",
            "PATH": "/bin",
        },
    ).classify_text("p", "c")
    assert "PAPERLESS_TOKEN" not in rec.env
    assert "PAPERLESS_TOKEN_FILE" not in rec.env
    assert rec.env["CLAUDE_CODE_OAUTH_TOKEN"] == "t"
