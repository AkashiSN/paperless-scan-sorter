"""処理の失敗を、書類に起因するものとしないものに分ける例外。"""

from __future__ import annotations


class DocumentFailure(Exception):
    """書類に起因する失敗。書類ごとの失敗回数に数える。"""


class CliFailure(DocumentFailure):
    """claude CLI が応答の JSON を返さずに終わった失敗のうち、障害を示す語を含まないもの。

    書類起因として数えるが、同じ周期で連続する書類に続けて起きたら周期を打ち切る（D-010）。
    """


class ServiceUnavailable(Exception):
    """書類に起因しない失敗（利用枠・認証・ネットワーク・Paperless の 401/403/5xx）。

    失敗回数には数えず、その周期を打ち切る。
    """


class MissingTagsError(Exception):
    """Paperless に状態管理用のタグがそろっていない。設定の見直しが要るので常駐を止める。"""
