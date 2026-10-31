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
from paperless_scan_sorter.errors import DocumentFailure, MissingTagsError, ServiceUnavailable
from paperless_scan_sorter.failures import CreatedStore, FailureStore
from paperless_scan_sorter.paperless import Named, PaperlessClient
from paperless_scan_sorter.pdfpages import PdfChunk
from paperless_scan_sorter.processor import (
    EXAMPLE_DOCS,
    MAX_PDF_BYTES,
    PAGES_PER_CHUNK,
    CycleResult,
    resolve_tag_ids,
)
from paperless_scan_sorter.processor import run_cycle as _run_cycle
from paperless_scan_sorter.prompt import TRANSCRIBE_PROMPT

TODAY = date(2026, 10, 27)
PAGE_TEXT = "請求書の本文"
INBOX, AI_TODO, AI_DONE, AI_REVIEW, AI_FAILED = 1, 2, 3, 4, 5
PAGE_TODO, PAGE_REVIEW = 6, 7
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
        self.example_rows: list[dict] = []
        self.example_status: int | None = None
        self.example_params: list[dict[str, str]] = []
        self._next_id = 500
        self.tag_rows = [
            {"id": INBOX, "name": "inbox"},
            {"id": PAGE_TODO, "name": "page-todo"},
            {"id": PAGE_REVIEW, "name": "page-review"},
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
        if method == "GET" and path == "/api/documents/" and "ordering" in request.url.params:
            self.example_params.append(dict(request.url.params))
            if self.example_status:
                return httpx.Response(self.example_status)
            return self._page(self.example_rows)
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
        "pages": [{"page": 1, "text": PAGE_TEXT, "label": None}],
    }
    base.update(over)
    return base


def _one_chunk(pdf: bytes) -> list[PdfChunk]:
    """PDF を分けずに 1 ページの書類として返す偽の分割。"""
    return [PdfChunk(1, 1, pdf)]


def run_cycle(*args: object, split: object = _one_chunk, **kw: object) -> CycleResult:
    """テストでは poppler を呼ばないよう、既定で偽の分割を渡す。"""
    return _run_cycle(*args, split=split, **kw)  # type: ignore[arg-type]


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
        "content": PAGE_TEXT,
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


def _pages(*texts: str) -> list[dict[str, object]]:
    return [{"page": n, "text": t, "label": str(n)} for n, t in enumerate(texts, start=1)]


def _chunks(*sizes: int) -> object:
    """sizes ページずつに分けた体の偽の分割。data には何番目の PDF かを入れる。"""

    def split(pdf: bytes) -> list[PdfChunk]:
        chunks, first = [], 1
        for i, size in enumerate(sizes):
            chunks.append(PdfChunk(first, first + size - 1, f"part{i}".encode()))
            first += size
        return chunks

    return split


def test_本文が長くても原本のPDFを読ませる(store: FailureStore, created: CreatedStore) -> None:
    server = FakePaperless([_doc(content="あ" * 9000)])
    claude = FakeClaude(ok(_answer()))
    r = run_cycle(server.client(), claude.client(), store, created, today=TODAY)
    assert r.done == 1
    assert server.downloads == [DOC_ID]
    assert "--allowedTools" in claude.calls[0]["argv"]
    assert claude.calls[0]["stdin"] is None
    assert server.patches[DOC_ID]["content"] == PAGE_TEXT


def test_既定の分割は5ページずつ_20MBまで() -> None:
    assert PAGES_PER_CHUNK == 5
    assert MAX_PDF_BYTES == 20 * 1024 * 1024


def test_分けたPDFごとに読ませ_本文をページ順につなぐ(
    store: FailureStore, created: CreatedStore
) -> None:
    server = FakePaperless([_doc()])
    first = _answer(pages=_pages("一", "二", "三", "四", "五"))
    claude = FakeClaude(
        ok(first),
        ok({"pages": list(reversed(_pages("六", "七", "八", "九", "十")))}),
        ok({"pages": _pages("", "十二")}),
    )
    r = run_cycle(
        server.client(), claude.client(), store, created, today=TODAY, split=_chunks(5, 5, 2)
    )
    assert r == CycleResult(done=1)
    assert len(claude.calls) == 3
    prompts = [c["argv"][-1] for c in claude.calls]
    assert "全 12 ページの書類のうち 1〜5 ページ目" in prompts[0]
    assert '"title"' in prompts[0]
    assert prompts[1].startswith(TRANSCRIBE_PROMPT)
    assert "全 12 ページの書類のうち 6〜10 ページ目" in prompts[1]
    assert "全 12 ページの書類のうち 11〜12 ページ目" in prompts[2]
    texts = ["一", "二", "三", "四", "五", "六", "七", "八", "九", "十", "", "十二"]
    assert server.patches[DOC_ID]["content"] == "\n\n".join(texts)
    assert server.patches[DOC_ID]["title"] == "10月分の請求書"


def test_分けていなければページの位置を伝えない(store: FailureStore, created: CreatedStore) -> None:
    server = FakePaperless([_doc()])
    claude = FakeClaude(ok(_answer()))
    run_cycle(server.client(), claude.client(), store, created, today=TODAY)
    assert "ページの書類のうち" not in claude.calls[0]["argv"][-1]


def test_最初の分類が壊れていれば残りを読まずに失敗として数える(
    store: FailureStore, created: CreatedStore
) -> None:
    server = FakePaperless([_doc()])
    claude = FakeClaude(ok(_answer(title=1)), ok({"pages": _pages("六")}))
    r = run_cycle(
        server.client(), claude.client(), store, created, today=TODAY, split=_chunks(5, 1)
    )
    assert r == CycleResult(retried=1)
    assert len(claude.calls) == 1
    assert server.patches == {}


def test_書き起こしのページが欠ければ失敗として数え何も書かない(
    store: FailureStore, created: CreatedStore
) -> None:
    server = FakePaperless([_doc()])
    first = _answer(pages=_pages("一", "二", "三", "四", "五"), correspondent="△△商事")
    claude = FakeClaude(ok(first), ok({"pages": _pages("六")}))
    r = run_cycle(
        server.client(), claude.client(), store, created, today=TODAY, split=_chunks(5, 2)
    )
    assert r == CycleResult(retried=1)
    assert store.count(DOC_ID) == 1
    assert server.patches == {}
    # 発行元は書き起こしがすべて揃ってから作る。
    assert server.posts == []


def test_2つ目以降で利用枠の上限なら打ち切り数えない(
    store: FailureStore, created: CreatedStore
) -> None:
    server = FakePaperless([_doc(100), _doc(101)])
    claude = FakeClaude(ok(_answer(pages=_pages("一", "二", "三", "四", "五"))), quota())
    r = run_cycle(
        server.client(), claude.client(), store, created, today=TODAY, split=_chunks(5, 1)
    )
    assert r.aborted
    assert store.count(100) == 0
    assert server.patches == {}


def test_書き起こしが空なら本文を置き換えない(store: FailureStore, created: CreatedStore) -> None:
    server = FakePaperless([_doc()])
    claude = FakeClaude(ok(_answer(pages=[{"page": 1, "text": " \n ", "label": None}])))
    r = run_cycle(server.client(), claude.client(), store, created, today=TODAY)
    assert r.done == 1
    assert "content" not in server.patches[DOC_ID]


def test_分けられないPDFはclaudeに渡さず書類の失敗にする(
    store: FailureStore, created: CreatedStore
) -> None:
    def split(pdf: bytes) -> list[PdfChunk]:
        raise DocumentFailure("1 ページで PDF の上限を超える")

    server = FakePaperless([_doc()])
    claude = FakeClaude(ok(_answer()))
    r = run_cycle(server.client(), claude.client(), store, created, today=TODAY, split=split)
    assert r == CycleResult(retried=1)
    assert claude.calls == []


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


def test_page_todoが無ければ止める() -> None:
    tags = [
        Named(i, n)
        for i, n in enumerate(
            ["inbox", "page-review", "ai-todo", "ai-done", "ai-review", "ai-failed"], 1
        )
    ]
    with pytest.raises(MissingTagsError) as e:
        resolve_tag_ids(tags)
    assert "page-todo" in str(e.value)


def test_必須タグがそろえばIDを引く() -> None:
    tags = [
        Named(i, n)
        for i, n in enumerate(
            [
                "inbox",
                "page-todo",
                "page-review",
                "ai-todo",
                "ai-done",
                "ai-review",
                "ai-failed",
            ],
            1,
        )
    ]
    ids = resolve_tag_ids(tags)
    assert (ids.inbox, ids.ai_todo, ids.ai_done, ids.ai_review, ids.ai_failed) == (1, 4, 5, 6, 7)
    assert (ids.page_todo, ids.page_review) == (2, 3)


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


def test_失敗が上限に達した書類は判定せずにai_failedへ送る(
    store: FailureStore, created: CreatedStore
) -> None:
    for _ in range(3):
        store.increment(DOC_ID)
    created.mark(DOC_ID)
    server = FakePaperless([_doc()])
    claude = FakeClaude(ok(_answer()))
    r = run_cycle(server.client(), claude.client(), store, created, today=TODAY)
    assert r == CycleResult(failed=1)
    assert claude.calls == []
    assert server.downloads == []
    assert server.patches[DOC_ID] == {"tags": sorted([INBOX, EXISTING_TAG, AI_FAILED])}
    assert store.count(DOC_ID) == 0
    assert not created.contains(DOC_ID)


def test_上限に達した書類のai_failedへのPATCHが400なら回数を残し次へ進む(
    store: FailureStore, created: CreatedStore
) -> None:
    for _ in range(3):
        store.increment(100)
    server = FakePaperless([_doc(100), _doc(101)])
    server.patch_status[100] = 400
    claude = FakeClaude(ok(_answer()))
    r = run_cycle(server.client(), claude.client(), store, created, today=TODAY)
    assert r == CycleResult(done=1, failed=1)
    assert len(claude.calls) == 1
    assert store.count(100) == 3
    assert 101 in server.patches


def test_発行元の作成で利用不可になっても作成前に記録を残す(
    store: FailureStore, created: CreatedStore
) -> None:
    # 作成が Paperless で確定したのに応答だけ失われた場合と区別できないので、記録は残す。
    server = FakePaperless([_doc(100), _doc(101)])
    server.post_status["/api/correspondents/"] = 503
    claude = FakeClaude(ok(_answer(correspondent="△△商事")))
    r = run_cycle(server.client(), claude.client(), store, created, today=TODAY)
    assert r == CycleResult(aborted=True)
    assert created.contains(100)
    assert store.count(100) == 0
    assert server.patches == {}


def test_文書タイプの作成で利用不可になっても作成前に記録を残す(
    store: FailureStore, created: CreatedStore
) -> None:
    server = FakePaperless([_doc()])
    server.post_status["/api/document_types/"] = 503
    claude = FakeClaude(ok(_answer(document_type="通知書")))
    r = run_cycle(server.client(), claude.client(), store, created, today=TODAY)
    assert r == CycleResult(aborted=True)
    assert created.contains(DOC_ID)


def test_作成が利用不可で終わった書類は次の周期でai_reviewにする(
    store: FailureStore, created: CreatedStore
) -> None:
    server = FakePaperless([_doc()])
    server.post_status["/api/correspondents/"] = 503
    claude = FakeClaude(ok(_answer(correspondent="△△商事")))
    run_cycle(server.client(), claude.client(), store, created, today=TODAY)
    # Paperless 側では作成が確定していて、次の周期では既存として見える。
    del server.post_status["/api/correspondents/"]
    server.correspondent_rows.append({"id": 501, "name": "△△商事"})
    r = run_cycle(server.client(), claude.client(), store, created, today=TODAY)
    assert r == CycleResult(review=1)
    assert AI_REVIEW in server.patches[DOC_ID]["tags"]
    assert not created.contains(DOC_ID)


def test_新規作成の記録が壊れていたら周期を打ち切る(
    tmp_path: Path, store: FailureStore, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "created.json"
    path.write_text("{壊れ", encoding="utf-8")
    created = CreatedStore(path)
    server = FakePaperless([_doc(100), _doc(101)])
    claude = FakeClaude(ok(_answer()))
    with caplog.at_level(logging.ERROR):
        r = run_cycle(server.client(), claude.client(), store, created, today=TODAY)
    assert r == CycleResult(aborted=True)
    assert claude.calls == []
    assert server.patches == {}
    assert server.posts == []
    assert store.count(100) == 0
    assert path.read_text(encoding="utf-8") == "{壊れ"
    assert str(path) in caplog.text


def test_上限に達した書類でも新規作成の記録が壊れていたら周期を打ち切る(
    tmp_path: Path, store: FailureStore
) -> None:
    for _ in range(3):
        store.increment(DOC_ID)
    path = tmp_path / "created.json"
    path.write_text("{壊れ", encoding="utf-8")
    server = FakePaperless([_doc()])
    r = run_cycle(
        server.client(), FakeClaude(ok(_answer())).client(), store, CreatedStore(path), today=TODAY
    )
    assert r.aborted
    assert store.count(DOC_ID) == 3


class ClearFailsCreated(CreatedStore):
    """記録の消去だけが利用不可になる新規作成の記録。"""

    def clear(self, doc_id: int) -> None:
        raise ServiceUnavailable("新規作成の記録を読めない")


def test_書き戻し後の記録の消去で利用不可なら集計してから打ち切る(
    tmp_path: Path, store: FailureStore
) -> None:
    server = FakePaperless([_doc(100), _doc(101)])
    claude = FakeClaude(ok(_answer()))
    created = ClearFailsCreated(tmp_path / "created.json")
    r = run_cycle(server.client(), claude.client(), store, created, today=TODAY)
    assert r == CycleResult(done=1, aborted=True)
    assert list(server.patches) == [100]


class CrashingClaude(FakeClaude):
    """指定した回の呼び出しだけ、応答の JSON を返さずに終わる偽の claude。"""

    def __init__(self, crash_on: set[int], *outputs: str) -> None:
        super().__init__(*outputs)
        self.crash_on = crash_on

    def __call__(
        self,
        argv: Sequence[str],
        stdin: str | None,
        cwd: Path,
        env: Mapping[str, str],
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        n = len(self.calls)
        proc = super().__call__(argv, stdin, cwd, env, timeout)
        if n in self.crash_on:
            return subprocess.CompletedProcess(list(argv), 139, "", "Segmentation fault")
        return proc


def test_CLIの失敗が1件なら数えて次の書類へ進む(store: FailureStore, created: CreatedStore) -> None:
    server = FakePaperless([_doc(100), _doc(101)])
    claude = CrashingClaude({0}, ok(_answer()))
    r = run_cycle(server.client(), claude.client(), store, created, today=TODAY)
    assert r == CycleResult(done=1, retried=1)
    assert store.count(100) == 1
    assert 101 in server.patches


def test_連続する書類でCLIの失敗が続いたら2件目は数えずに打ち切る(
    store: FailureStore, created: CreatedStore
) -> None:
    server = FakePaperless([_doc(100), _doc(101), _doc(102)])
    claude = CrashingClaude({0, 1}, ok(_answer()))
    r = run_cycle(server.client(), claude.client(), store, created, today=TODAY)
    assert r == CycleResult(retried=1, aborted=True)
    assert store.count(100) == 1
    assert store.count(101) == 0
    assert len(claude.calls) == 2
    assert server.patches == {}


def test_間に成功を挟んだCLIの失敗はそれぞれ数える(
    store: FailureStore, created: CreatedStore
) -> None:
    server = FakePaperless([_doc(100), _doc(101), _doc(102)])
    claude = CrashingClaude({0, 2}, ok(_answer()))
    r = run_cycle(server.client(), claude.client(), store, created, today=TODAY)
    assert r == CycleResult(done=1, retried=2)
    assert store.count(100) == 1
    assert store.count(102) == 1


def test_間に別の書類の失敗を挟んだCLIの失敗はそれぞれ数える(
    store: FailureStore, created: CreatedStore
) -> None:
    server = FakePaperless([_doc(100), _doc(101), _doc(102)])
    claude = CrashingClaude({0, 2}, ok("壊れ"))
    r = run_cycle(server.client(), claude.client(), store, created, today=TODAY)
    assert r == CycleResult(retried=3)


def test_前の周期のCLIの失敗は持ち越さない(store: FailureStore, created: CreatedStore) -> None:
    server = FakePaperless([_doc(100)])
    claude = CrashingClaude({0, 1}, ok(_answer()))
    run_cycle(server.client(), claude.client(), store, created, today=TODAY)
    r = run_cycle(server.client(), claude.client(), store, created, today=TODAY)
    assert r == CycleResult(retried=1)
    assert store.count(100) == 2


def test_ai_doneの書類のタイトルを例としてプロンプトに入れる(
    store: FailureStore, created: CreatedStore
) -> None:
    server = FakePaperless([_doc()])
    server.example_rows = [
        {"id": 7, "title": "取引報告書 2026年9月分", "correspondent": 10, "document_type": 20},
        {"id": 8, "title": "メモ", "correspondent": None, "document_type": None},
    ]
    claude = FakeClaude(ok(_answer()))
    run_cycle(server.client(), claude.client(), store, created, today=TODAY)
    prompt = claude.calls[0]["argv"][-1]
    assert (
        '- {"発行元": "○○銀行", "種類": "請求書", "タイトル": "取引報告書 2026年9月分"}\n' in prompt
    )
    assert '- {"発行元": null, "種類": null, "タイトル": "メモ"}\n' in prompt
    assert server.example_params[0]["tags__id__all"] == str(AI_DONE)
    assert server.example_params[0]["page_size"] == str(EXAMPLE_DOCS)


def test_同じ周期で先に決めたタイトルも次の書類の例にする(
    store: FailureStore, created: CreatedStore
) -> None:
    server = FakePaperless([_doc(100), _doc(101)])
    claude = FakeClaude(ok(_answer(title="診療費 請求書兼領収証")), ok(_answer()))
    run_cycle(server.client(), claude.client(), store, created, today=TODAY)
    assert "診療費 請求書兼領収証" not in claude.calls[0]["argv"][-1]
    assert '"タイトル": "診療費 請求書兼領収証"' in claude.calls[1]["argv"][-1]


def test_同じ周期で作った発行元は次の書類の候補に出るが_確認待ちなので例には出ない(
    store: FailureStore, created: CreatedStore
) -> None:
    server = FakePaperless([_doc(100), _doc(101)])
    claude = FakeClaude(ok(_answer(correspondent="△△病院", title="領収証")), ok(_answer()))
    run_cycle(server.client(), claude.client(), store, created, today=TODAY)
    second = claude.calls[1]["argv"][-1]
    assert "\n- △△病院\n" in second
    assert '"タイトル": "領収証"' not in second


def test_タイトルの例を取れなければ周期を打ち切る(
    store: FailureStore, created: CreatedStore
) -> None:
    server = FakePaperless([_doc()])
    server.example_status = 503
    claude = FakeClaude(ok(_answer()))
    r = run_cycle(server.client(), claude.client(), store, created, today=TODAY)
    assert r == CycleResult(aborted=True)
    assert claude.calls == []


def test_確認待ちになった書類のタイトルは同じ周期の例にしない(
    store: FailureStore, created: CreatedStore
) -> None:
    server = FakePaperless([_doc(100), _doc(101)])
    claude = FakeClaude(ok(_answer(title="未確認のタイトル", confident=False)), ok(_answer()))
    r = run_cycle(server.client(), claude.client(), store, created, today=TODAY)
    assert r.review == 1
    assert "未確認のタイトル" not in claude.calls[1]["argv"][-1]


def test_自動マッチ済みの発行元と種類は同じ周期の例に名前で出る(
    store: FailureStore, created: CreatedStore
) -> None:
    server = FakePaperless([_doc(100, correspondent=10, document_type=20), _doc(101)])
    claude = FakeClaude(ok(_answer(title="取引報告書 2026年9月分")), ok(_answer()))
    run_cycle(server.client(), claude.client(), store, created, today=TODAY)
    assert '"○○銀行"' in claude.calls[1]["argv"][-1]
    assert "取引報告書 2026年9月分" in claude.calls[1]["argv"][-1]
    assert "（発行元なし）" not in claude.calls[1]["argv"][-1]
