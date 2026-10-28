"""状態管理用のタグ名。"""

from __future__ import annotations

INBOX = "inbox"
PAGE_TODO = "page-todo"
PAGE_REVIEW = "page-review"
AI_TODO = "ai-todo"
AI_DONE = "ai-done"
AI_REVIEW = "ai-review"
AI_FAILED = "ai-failed"

# Claude に候補として渡さず、出力に含まれていても採用しないタグ。
STATE_TAGS: frozenset[str] = frozenset(
    {INBOX, PAGE_TODO, PAGE_REVIEW, AI_TODO, AI_DONE, AI_REVIEW, AI_FAILED}
)

# ai-classifier が付け外しするタグ。起動時に Paperless に存在することを確かめる。
REQUIRED_TAGS: tuple[str, ...] = (INBOX, AI_TODO, AI_DONE, AI_REVIEW, AI_FAILED)
