"""環境変数から設定を読む。

不備は ValueError にする。メッセージには変数名だけを入れ、値は入れない。
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_INTERVAL = 300
DEFAULT_STATE_DIR = "/state"
DEFAULT_MODEL = "sonnet"
DEFAULT_CLAUDE_TIMEOUT = 600.0
DEFAULT_WEBHOOK_PORT = 8080
# 推測されにくいよう、Webhook の合言葉には最低限の長さを求める。
WEBHOOK_TOKEN_MIN = 16


@dataclass(frozen=True)
class Settings:
    """実行時の設定。"""

    paperless_url: str
    paperless_token: str = field(repr=False)
    interval: int
    state_dir: Path
    model: str
    claude_timeout: float
    # None なら Webhook の受け口を開かず、INTERVAL ごとの周期だけで動く。
    webhook_token: str | None = field(default=None, repr=False)
    webhook_port: int = DEFAULT_WEBHOOK_PORT


def _get(env: Mapping[str, str], name: str) -> str | None:
    """空文字は未設定として扱う。"""
    value = env.get(name)
    return value if value else None


def _require(env: Mapping[str, str], name: str) -> str:
    value = _get(env, name)
    if value is None:
        raise ValueError(f"環境変数 {name} が未設定")
    return value


def _read_token(env: Mapping[str, str]) -> str:
    """PAPERLESS_TOKEN を優先し、無ければ PAPERLESS_TOKEN_FILE を読む。"""
    token = _get(env, "PAPERLESS_TOKEN")
    if token is not None:
        return token
    path = _get(env, "PAPERLESS_TOKEN_FILE")
    if path is None:
        raise ValueError("環境変数 PAPERLESS_TOKEN または PAPERLESS_TOKEN_FILE が必要")
    try:
        token = Path(path).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as e:
        raise ValueError("PAPERLESS_TOKEN_FILE を読めない") from e
    if not token:
        raise ValueError("PAPERLESS_TOKEN_FILE が空")
    return token


def _interval(env: Mapping[str, str]) -> int:
    raw = _get(env, "INTERVAL")
    if raw is None:
        return DEFAULT_INTERVAL
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value < 1:
        raise ValueError("INTERVAL は 1 以上の整数")
    return value


def _claude_timeout(env: Mapping[str, str]) -> float:
    raw = _get(env, "CLAUDE_TIMEOUT")
    if raw is None:
        return DEFAULT_CLAUDE_TIMEOUT
    try:
        value = float(raw)
    except ValueError:
        value = 0.0
    if not math.isfinite(value) or value <= 0:
        raise ValueError("CLAUDE_TIMEOUT は正の数")
    return value


def _webhook_token(env: Mapping[str, str]) -> str | None:
    token = _get(env, "WEBHOOK_TOKEN")
    if token is not None and len(token) < WEBHOOK_TOKEN_MIN:
        raise ValueError(f"WEBHOOK_TOKEN は {WEBHOOK_TOKEN_MIN} 文字以上")
    return token


def _webhook_port(env: Mapping[str, str]) -> int:
    raw = _get(env, "WEBHOOK_PORT")
    if raw is None:
        return DEFAULT_WEBHOOK_PORT
    try:
        value = int(raw)
    except ValueError:
        value = -1
    # 0 は空いているポートを OS に選ばせる（テスト用）。
    if not 0 <= value <= 65535:
        raise ValueError("WEBHOOK_PORT は 0〜65535 の整数")
    return value


def load_settings(env: Mapping[str, str]) -> Settings:
    """環境変数から Settings を作る。"""
    url = _require(env, "PAPERLESS_URL").rstrip("/")
    token = _read_token(env)
    # OAuth トークンは claude の子プロセスが環境から読む。ここでは有無だけ見る。
    _require(env, "CLAUDE_CODE_OAUTH_TOKEN")
    return Settings(
        paperless_url=url,
        paperless_token=token,
        interval=_interval(env),
        state_dir=Path(_get(env, "STATE_DIR") or DEFAULT_STATE_DIR),
        model=_get(env, "CLAUDE_MODEL") or DEFAULT_MODEL,
        claude_timeout=_claude_timeout(env),
        webhook_token=_webhook_token(env),
        webhook_port=_webhook_port(env),
    )
