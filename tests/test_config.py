from __future__ import annotations

from pathlib import Path

import pytest

from paperless_scan_sorter.config import load_settings

BASE = {
    "PAPERLESS_URL": "https://paperless.example.com/",
    "PAPERLESS_TOKEN": "secret-token",
    "CLAUDE_CODE_OAUTH_TOKEN": "oauth-secret",
}


def test_既定値() -> None:
    s = load_settings(BASE)
    assert (s.paperless_url, s.interval, s.state_dir, s.model, s.claude_timeout) == (
        "https://paperless.example.com",
        300,
        Path("/state"),
        "sonnet",
        600.0,
    )


def test_上書きできる() -> None:
    s = load_settings(
        BASE
        | {
            "INTERVAL": "60",
            "STATE_DIR": "/var/lib/x",
            "CLAUDE_MODEL": "opus",
            "CLAUDE_TIMEOUT": "12.5",
        }
    )
    assert (s.interval, s.state_dir, s.model, s.claude_timeout) == (
        60,
        Path("/var/lib/x"),
        "opus",
        12.5,
    )


def test_トークンをファイルから読む(tmp_path: Path) -> None:
    f = tmp_path / "token"
    f.write_text("t\n")
    env = {k: v for k, v in BASE.items() if k != "PAPERLESS_TOKEN"}
    s = load_settings(env | {"PAPERLESS_TOKEN_FILE": str(f)})
    assert s.paperless_token == "t"


def test_トークンの直接指定がファイルより優先される(tmp_path: Path) -> None:
    f = tmp_path / "token"
    f.write_text("from-file")
    s = load_settings(BASE | {"PAPERLESS_TOKEN_FILE": str(f)})
    assert s.paperless_token == "secret-token"


def test_読めないトークンファイルは名前だけ出して止める(tmp_path: Path) -> None:
    env = {k: v for k, v in BASE.items() if k != "PAPERLESS_TOKEN"}
    with pytest.raises(ValueError, match="PAPERLESS_TOKEN_FILE"):
        load_settings(env | {"PAPERLESS_TOKEN_FILE": str(tmp_path / "none")})


def test_空のトークンファイルは止める(tmp_path: Path) -> None:
    f = tmp_path / "token"
    f.write_text(" \n")
    env = {k: v for k, v in BASE.items() if k != "PAPERLESS_TOKEN"}
    with pytest.raises(ValueError, match="PAPERLESS_TOKEN_FILE"):
        load_settings(env | {"PAPERLESS_TOKEN_FILE": str(f)})


@pytest.mark.parametrize("missing", ["PAPERLESS_URL", "PAPERLESS_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"])
def test_必須の変数が無ければ止める(missing: str) -> None:
    env = {k: v for k, v in BASE.items() if k != missing}
    with pytest.raises(ValueError, match=missing):
        load_settings(env)


def test_空文字は未設定として扱う() -> None:
    with pytest.raises(ValueError, match="PAPERLESS_URL"):
        load_settings(BASE | {"PAPERLESS_URL": ""})


@pytest.mark.parametrize("v", ["0", "-1", "abc", "1.5"])
def test_INTERVALは1以上の整数(v: str) -> None:
    with pytest.raises(ValueError, match="INTERVAL"):
        load_settings(BASE | {"INTERVAL": v})


@pytest.mark.parametrize("v", ["0", "-3", "abc", "nan", "inf"])
def test_CLAUDE_TIMEOUTは正の数(v: str) -> None:
    with pytest.raises(ValueError, match="CLAUDE_TIMEOUT"):
        load_settings(BASE | {"CLAUDE_TIMEOUT": v})


def test_reprにトークンを出さない() -> None:
    s = load_settings(BASE)
    assert "secret-token" not in repr(s)
    assert "oauth-secret" not in repr(s)
