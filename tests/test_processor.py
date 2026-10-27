"""1 周期の処理のテスト（偽の Paperless サーバーと偽の claude runner を使う）。"""

from __future__ import annotations

import json
import logging
import subprocess
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path

import httpx
import pytest

from paperless_scan_sorter.claude import ClaudeClient
from paperless_scan_sorter.errors import MissingTagsError
from paperless_scan_sorter.failures import CreatedStore, FailureStore
from paperless_scan_sorter.paperless import Named, PaperlessClient
from paperless_scan_sorter.processor import (
    CONTENT_LIMIT,
    MAX_PDF_BYTES,
    CycleResult,
    resolve_tag_ids,
    run_cycle,
)

TODAY = date(2026, 10, 27)
INBOX, AI_TODO, AI_DONE, AI_REVIEW, AI_FAILED = 1, 2, 3, 4, 5
EXISTING_TAG, CHOSEN_TAG = 99, 30
DOC_ID = 100
LONG_TEXT = "請求書の本文です。" * 20


def _doc(doc_id: int = DOC_ID, content: str = LONG_TEXT, **over: object) -> dict:
    row = {
        "id": doc_id,
        "title": "scan",
        "content": content,
        "correspondent": None,
        "document_type": None,
        "tags": [INBOX, AI_TODO, EXISTING_TAG],
    }
    row.update(over)
    return row


class FakePaperless:
    """MockTransport のハンドラ。PATCH・POST を記録し、作成には連番 ID を返す。"""

    def __init__(self, docs: list[dict]) -> None:
        self.docs = docs
        self.patches: dict[int, dict] = {}
        self.posts: list[tuple[str, dict]] = []
        self.downloads: list[int] = []
        self.pdf = b"%PDF-fake"
        self.patch_status: dict[int, int] = {}
        self.post_status: dict[str, int] = {}
        self.list_status: int | None = None
        self._next_id = 500
        self.tag_rows = [
            {"id": INBOX, "name": "inbox"},
            {"id": AI_TODO, "name": "ai-todo"},
            {"id": AI_DONE, "name": "ai-done"},
            {"id": AI_REVIEW, "name": "ai-review"},
            {"id": AI_FAILED, "name": "ai-failed"},
            {"id": EXISTING_TAG, "name": "家計"},
            {"id": CHOSEN_TAG, "name": "請求"},
        ]
        self.correspondent_rows = [{"id": 10, "name": "○○銀行"}]
        self.type_rows = [{"id": 20, "name": "請求書"}]

    def client(self) -> PaperlessClient:
        return PaperlessClient(
            "https://paperless.example.com", "tok", transport=httpx.MockTransport(self)
        )

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        if method == "GET" and path == "/api/documents/":
            if self.list_status:
                return httpx.Response(self.list_status)
            return self._page(self.docs)
        if method == "GET" and path == "/api/tags/":
            return self._page(self.tag_rows)
        if method == "GET" and path == "/api/correspondents/":
            return self._page(self.correspondent_rows)
        if method == "GET" and path == "/api/document_types/":
            return self._page(self.type_rows)
        if method == "GET" and path.endswith("/download/"):
            self.downloads.append(int(path.split("/")[3]))
            return httpx.Response(200, content=self.pdf)
        if method == "PATCH":
            doc_id = int(path.split("/")[3])
            if doc_id in self.patch_status:
                return httpx.Response(self.patch_status[doc_id])
            self.patches[doc_id] = json.loads(request.content)
            return httpx.Response(200, json={})
        if method == "POST":
            body = json.loads(request.content)
            if path in self.post_status:
                return httpx.Response(self.post_status[path])
            self.posts.append((path, body))
            self._next_id += 1
            return httpx.Response(201, json={"id": self._next_id, "name": body["name"]})
        return httpx.Response(404)

    @staticmethod
    def _page(rows: list[dict]) -> httpx.Response:
        return httpx.Response(200, json={"results": rows, "next": None})

    def posts_to(self, path: str) -> list[dict]:
        return [b for p, b in self.posts if p == path]


def _answer(**over: object) -> dict:
    base = {
        "title": "10月分の請求書",
        "correspondent": "○○銀行",
        "document_type": "請求書",
        "tags": ["請求"],
        "created": "2026-10-20",
        "confident": True,
    }
    base.update(over)
    return base


class FakeClaude:
    """runner として注入する偽の claude。応答を順に返し、呼び出しを記録する。"""

    def __init__(self, *outputs: str) -> None:
        self.outputs = list(outputs)
        self.calls: list[dict] = []

    def client(self) -> ClaudeClient:
        return ClaudeClient(model="m", runner=self, env={})

    def __call__(
        self,
        argv: Sequence[str],
        stdin: str | None,
        cwd: Path,
        env: Mapping[str, str],
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append({"argv": list(argv), "stdin": stdin})
        out = self.outputs.pop(0) if len(self.outputs) > 1 else self.outputs[0]
        return subprocess.CompletedProcess(list(argv), 0, out, "")


def ok(answer: dict | str) -> str:
    result = answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False)
    return json.dumps({"is_error": False, "subtype": "success", "result": result})


def quota() -> str:
    return json.dumps({"is_error": True, "subtype": "error_during_execution", "result": "limit"})


@pytest.fixture
def store(tmp_path: Path) -> FailureStore:
    return FailureStore(tmp_path / "failures.json")


@pytest.fixture
def created(tmp_path: Path) -> CreatedStore:
    return CreatedStore(tmp_path / "created.json")


def test_通常の書類をai_doneにする(store: FailureStore, created: CreatedStore) -> None:
    server = FakePaperless([_doc()])
    claude = FakeClaude(ok(_answer()))
    r = run_cycle(server.client(), claude.client(), store, created, today=TODAY)
    assert r == CycleResult(done=1)
    assert server.patches[DOC_ID] == {
        "tags": sorted([EXISTING_TAG, CHOSEN_TAG, AI_DONE]),
        "title": "10月分の請求書",
        "correspondent": 10,
        "document_type": 20,
        "created": "2026-10-20",
    }
    assert server.posts == []


def test_新しい発行元を作ってai_reviewにする(store: FailureStore, created: CreatedStore) -> None:
    server = FakePaperless([_doc()])
    claude = FakeClaude(ok(_answer(correspondent="△△商事")))
    r = run_cycle(server.client(), claude.client(), store, created, today=TODAY)
    assert r == CycleResult(review=1)
    assert server.posts_to("/api/correspondents/") == [{"name": "△△商事"}]
    patch = server.patches[DOC_ID]
    assert patch["correspondent"] == 501
    assert AI_REVIEW in patch["tags"]
    assert AI_DONE not in patch["tags"]
    assert INBOX in patch["tags"]


def test_新しい文書タイプを作る(store: FailureStore, created: CreatedStore) -> None:
    server = FakePaperless([_doc()])
    claude = FakeClaude(ok(_answer(document_type="領収書")))
    r = run_cycle(server.client(), claude.client(), store, created, today=TODAY)
    assert r.review == 1
    assert server.posts_to("/api/document_types/") == [{"name": "領収書"}]
    assert server.patches[DOC_ID]["document_type"] == 501


def test_同じ周期で同じ新しい発行元を2度作らない(
    store: FailureStore, created: CreatedStore
) -> None:
    server = FakePaperless([_doc(100), _doc(101)])
    claude = FakeClaude(ok(_answer(correspondent="△△商事")))
    r = run_cycle(server.client(), claude.client(), store, created, today=TODAY)
    assert len(server.posts_to("/api/correspondents/")) == 1
    # 2 件目は既存として使われるため、decide の規則どおり done になる。
    assert r == CycleResult(done=1, review=1)
    assert server.patches[100]["correspondent"] == server.patches[101]["correspondent"] == 501


def test_本文が短ければPDFを読ませる(store: FailureStore, created: CreatedStore) -> None:
    server = FakePaperless([_doc(content="  \n")])
    claude = FakeClaude(ok(_answer()))
    r = run_cycle(server.client(), claude.client(), store, created, today=TODAY)
    assert r.done == 1
    assert server.downloads == [DOC_ID]
    assert "--allowedTools" in claude.calls[0]["argv"]
    assert claude.calls[0]["stdin"] is None


def test_本文は8000文字で切る(store: FailureStore, created: CreatedStore) -> None:
    server = FakePaperless([_doc(content="あ" * 9000)])
    claude = FakeClaude(ok(_answer()))
    run_cycle(server.client(), claude.client(), store, created, today=TODAY)
    assert CONTENT_LIMIT == 8000
    assert len(claude.calls[0]["stdin"]) == 8000
    assert server.downloads == []


def test_壊れた出力は再試行に回す(store: FailureStore, created: CreatedStore) -> None:
    server = FakePaperless([_doc()])
    claude = FakeClaude(ok("壊れ"))
    r = run_cycle(server.client(), claude.client(), store, created, today=TODAY)
    assert r == CycleResult(retried=1)
    assert server.patches == {}
    assert store.count(DOC_ID) == 1


def test_3回目の失敗でai_failedにする(store: FailureStore, created: CreatedStore) -> None:
    store.increment(DOC_ID)
    store.increment(DOC_ID)
    server = FakePaperless([_doc()])
    claude = FakeClaude(ok("壊れ"))
    r = run_cycle(server.client(), claude.client(), store, created, today=TODAY)
    assert r == CycleResult(failed=1)
    assert server.patches[DOC_ID] == {"tags": sorted([INBOX, EXISTING_TAG, AI_FAILED])}
    assert store.count(DOC_ID) == 0


def test_ai_failedへのPATCHが失敗しても次の書類へ進む(
    store: FailureStore, created: CreatedStore, caplog: pytest.LogCaptureFixture
) -> None:
    store.increment(100)
    store.increment(100)
    server = FakePaperless([_doc(100), _doc(101)])
    server.patch_status[100] = 400
    claude = FakeClaude(ok("壊れ"), ok(_answer()))
    with caplog.at_level(logging.WARNING):
        r = run_cycle(server.client(), claude.client(), store, created, today=TODAY)
    assert r.done == 1
    assert 101 in server.patches
    assert store.count(100) == 3
    assert any(rec.levelno == logging.WARNING for rec in caplog.records)


def test_利用枠の上限で周期を打ち切り数えない(store: FailureStore, created: CreatedStore) -> None:
    server = FakePaperless([_doc(100), _doc(101)])
    claude = FakeClaude(quota())
    r = run_cycle(server.client(), claude.client(), store, created, today=TODAY)
    assert r.aborted
    assert len(claude.calls) == 1
    assert store.count(100) == 0
    assert server.patches == {}


def test_一覧の取得で利用不可なら打ち切る(store: FailureStore, created: CreatedStore) -> None:
    server = FakePaperless([_doc()])
    server.list_status = 503
    claude = FakeClaude(ok(_answer()))
    r = run_cycle(server.client(), claude.client(), store, created, today=TODAY)
    assert r == CycleResult(aborted=True)
    assert claude.calls == []


def test_1件のPATCHが400でも次の書類を処理する(store: FailureStore, created: CreatedStore) -> None:
    server = FakePaperless([_doc(100), _doc(101)])
    server.patch_status[100] = 400
    claude = FakeClaude(ok(_answer()))
    r = run_cycle(server.client(), claude.client(), store, created, today=TODAY)
    assert r == CycleResult(done=1, retried=1)
    assert store.count(100) == 1
    assert 101 in server.patches


def test_成功したら失敗回数を消す(store: FailureStore, created: CreatedStore) -> None:
    store.increment(DOC_ID)
    server = FakePaperless([_doc()])
    run_cycle(server.client(), FakeClaude(ok(_answer())).client(), store, created, today=TODAY)
    assert store.count(DOC_ID) == 0


def test_ログに書類IDと結果を出し本文とタイトルは出さない(
    store: FailureStore, created: CreatedStore, caplog: pytest.LogCaptureFixture
) -> None:
    server = FakePaperless([_doc()])
    with caplog.at_level(logging.INFO):
        run_cycle(server.client(), FakeClaude(ok(_answer())).client(), store, created, today=TODAY)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert str(DOC_ID) in text
    assert "done" in text
    assert "請求書の本文" not in text
    assert "10月分の請求書" not in text


def test_必須タグが無ければ止める() -> None:
    tags = [Named(1, "inbox"), Named(2, "ai-todo"), Named(3, "ai-done")]
    with pytest.raises(MissingTagsError) as e:
        resolve_tag_ids(tags)
    assert "ai-failed" in str(e.value)
    assert "ai-review" in str(e.value)


def test_必須タグがそろえばIDを引く() -> None:
    tags = [
        Named(i, n)
        for i, n in enumerate(["inbox", "ai-todo", "ai-done", "ai-review", "ai-failed"], 1)
    ]
    ids = resolve_tag_ids(tags)
    assert (ids.inbox, ids.ai_todo, ids.ai_done, ids.ai_review, ids.ai_failed) == (1, 2, 3, 4, 5)


def test_ai_failedへのPATCHで利用不可なら周期を打ち切る(
    store: FailureStore, created: CreatedStore
) -> None:
    store.increment(100)
    store.increment(100)
    server = FakePaperless([_doc(100), _doc(101)])
    server.patch_status[100] = 503
    claude = FakeClaude(ok("壊れ"))
    r = run_cycle(server.client(), claude.client(), store, created, today=TODAY)
    assert r.aborted
    assert len(claude.calls) == 1


def test_発行元の作成が400でも次の書類を処理する(
    store: FailureStore, created: CreatedStore
) -> None:
    server = FakePaperless([_doc(100), _doc(101)])
    server.post_status["/api/correspondents/"] = 400
    claude = FakeClaude(ok(_answer(correspondent="△△商事")), ok(_answer()))
    r = run_cycle(server.client(), claude.client(), store, created, today=TODAY)
    assert r == CycleResult(done=1, retried=1)
    assert not r.aborted
    assert store.count(100) == 1
    assert 100 not in server.patches
    assert 101 in server.patches


class RaisingFirstClaude(FakeClaude):
    """1 回目の呼び出しだけ例外を送出する偽の claude。"""

    def __init__(self, error: Exception, *outputs: str) -> None:
        super().__init__(*outputs)
        self.error = error

    def __call__(
        self,
        argv: Sequence[str],
        stdin: str | None,
        cwd: Path,
        env: Mapping[str, str],
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        if not self.calls:
            self.calls.append({"argv": list(argv), "stdin": stdin})
            raise self.error
        return super().__call__(argv, stdin, cwd, env, timeout)


def test_想定外の例外は書類の失敗として数え次の書類へ進む(
    store: FailureStore, created: CreatedStore, caplog: pytest.LogCaptureFixture
) -> None:
    server = FakePaperless([_doc(100), _doc(101)])
    error = UnicodeEncodeError("utf-8", "秘密の本文", 0, 1, "surrogates not allowed")
    claude = RaisingFirstClaude(error, ok(_answer()))
    with caplog.at_level(logging.INFO):
        r = run_cycle(server.client(), claude.client(), store, created, today=TODAY)
    assert r == CycleResult(done=1, retried=1)
    assert store.count(100) == 1
    assert 101 in server.patches
    text = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "UnicodeEncodeError" in text
    assert "秘密の本文" not in text
    assert all(rec.exc_info is None for rec in caplog.records)


def test_大きすぎるPDFはclaudeに渡さず書類の失敗にする(
    store: FailureStore, created: CreatedStore
) -> None:
    assert MAX_PDF_BYTES == 20 * 1024 * 1024
    server = FakePaperless([_doc(100, content=""), _doc(101)])
    server.pdf = b"%" * (MAX_PDF_BYTES + 1)
    claude = FakeClaude(ok(_answer()))
    r = run_cycle(server.client(), claude.client(), store, created, today=TODAY)
    assert r == CycleResult(done=1, retried=1)
    assert store.count(100) == 1
    assert len(claude.calls) == 1
    assert "--allowedTools" not in claude.calls[0]["argv"]


def test_新規作成後にPATCHが失敗したら次の周期はai_reviewにする(
    store: FailureStore, created: CreatedStore, caplog: pytest.LogCaptureFixture
) -> None:
    server = FakePaperless([_doc()])
    server.patch_status[DOC_ID] = 400
    claude = FakeClaude(ok(_answer(correspondent="△△商事")))
    r = run_cycle(server.client(), claude.client(), store, created, today=TODAY)
    assert r == CycleResult(retried=1)
    assert created.contains(DOC_ID)

    # 次の周期では作った発行元が既存として見えるが、ai-done にはしない。
    server.correspondent_rows.append({"id": 501, "name": "△△商事"})
    del server.patch_status[DOC_ID]
    with caplog.at_level(logging.INFO):
        r = run_cycle(server.client(), claude.client(), store, created, today=TODAY)
    assert r == CycleResult(review=1)
    patch = server.patches[DOC_ID]
    assert patch["correspondent"] == 501
    assert AI_REVIEW in patch["tags"] and AI_DONE not in patch["tags"]
    assert "前回の試行で発行元・文書タイプを新規作成" in caplog.text
    assert len(server.posts_to("/api/correspondents/")) == 1
    assert not created.contains(DOC_ID)


def test_文書タイプの作成が失敗しても発行元の作成は記録する(
    store: FailureStore, created: CreatedStore
) -> None:
    server = FakePaperless([_doc()])
    server.post_status["/api/document_types/"] = 400
    claude = FakeClaude(ok(_answer(correspondent="△△商事", document_type="通知書")))
    r = run_cycle(server.client(), claude.client(), store, created, today=TODAY)
    assert r == CycleResult(retried=1)
    assert created.contains(DOC_ID)


def test_ai_failedに送ったら新規作成の記録を消す(
    store: FailureStore, created: CreatedStore
) -> None:
    store.increment(DOC_ID)
    store.increment(DOC_ID)
    created.mark(DOC_ID)
    server = FakePaperless([_doc()])
    r = run_cycle(server.client(), FakeClaude(ok("壊れ")).client(), store, created, today=TODAY)
    assert r == CycleResult(failed=1)
    assert not created.contains(DOC_ID)


def test_既存の発行元はそのまま照合し新しい名前は無害化して作る(
    store: FailureStore, created: CreatedStore
) -> None:
    server = FakePaperless([_doc(100), _doc(101)])
    server.correspondent_rows.append({"id": 11, "name": "Foo Inc."})
    claude = FakeClaude(ok(_answer(correspondent="Foo Inc.")), ok(_answer(correspondent="a/b")))
    r = run_cycle(server.client(), claude.client(), store, created, today=TODAY)
    assert r == CycleResult(done=1, review=1)
    assert server.patches[100]["correspondent"] == 11
    assert server.posts_to("/api/correspondents/") == [{"name": "ab"}]
    assert server.patches[101]["correspondent"] == 501
