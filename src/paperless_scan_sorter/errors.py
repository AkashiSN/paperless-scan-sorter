"""処理の失敗を、書類に起因するものとしないものに分ける例外。"""

from __future__ import annotations


class DocumentFailure(Exception):
    """書類に起因する失敗。書類ごとの失敗回数に数える。"""


class ServiceUnavailable(Exception):
    """書類に起因しない失敗（利用枠・認証・ネットワーク・Paperless の 401/403/5xx）。

    失敗回数には数えず、その周期を打ち切る。
    """
