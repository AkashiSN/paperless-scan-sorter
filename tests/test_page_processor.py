"""page-todo の 1 周期のテスト（偽の Paperless サーバーと偽の測定を使う）。"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
from pathlib import Path

import httpx
import pytest

from paperless_scan_sorter.errors import MissingTagsError
from paperless_scan_sorter.originals import OriginalsStore
from paperless_scan_sorter.page_processor import (
    MAX_PAGES,
    VERSION_POLL_SECONDS,
    VERSION_WAIT_SECONDS,
    PageCycleResult,
    run_page_cycle,
)
from paperless_scan_sorter.pageplan import DEFAULT_THRESHOLDS, PageInfo, Thresholds
from paperless_scan_sorter.paperless import PaperlessClient

INBOX, AI_TODO, AI_DONE, AI_REVIEW, AI_FAILED = 1, 2, 3, 4, 5
PAGE_TODO, PAGE_REVIEW = 6, 7
EXISTING_TAG = 99
DOC_ID = 100
PDF = b"%PDF-fake"
ARCHIVE = b"%PDF-archive"
TEXT = "請求書の本文です。" * 5


def _doc(doc_id: int = DOC_ID, **over: object) -> dict:
    row = {
        "id": doc_id,
        "title": "scan",
        "content": "",
        "correspondent": None,
        "document_type": None,
        "tags": [INBOX, PAGE_TODO, EXISTING_TAG],
    }
    row.update(over)
    return row


class FakePaperless:
    """MockTransport のハンドラ。edit_pdf・版の取得・PATCH を記録する。

    版は書類ごとに `versions` の並びを順に返し、最後の値を繰り返す。
    """

    def __init__(self, docs: list[dict], originals_dir: Path | None = None) -> None:
        self.docs = docs
        self.originals_dir = originals_dir
        self.patches: dict[int, dict] = {}
        self.edits: list[dict] = []
        self.events: list[tuple[str, int]] = []
        self.saved_before_edit: list[bool] = []
        self.versions: dict[int, list[set[int]]] = {}
        self.version_status: dict[int, int] = {}
        self.version_status_after_edit: dict[int, int] = {}
        self.edit_status: dict[int, int] = {}
        self.archive_status: dict[int, int] = {}
        self.patch_status: dict[int, int] = {}
        self.list_status: int | None = None
        # PATCH の直前に読み直すタグ。無ければ一覧の行のタグを返す
        self.current_tags: dict[int, list[int]] = {}
        self.tags_status: dict[int, int] = {}
        self.tag_rows = [
            {"id": INBOX, "name": "inbox"},
            {"id": PAGE_TODO, "name": "page-todo"},
            {"id": PAGE_REVIEW, "name": "page-review"},
            {"id": AI_TODO, "name": "ai-todo"},
            {"id": AI_DONE, "name": "ai-done"},
            {"id": AI_REVIEW, "name": "ai-review"},
            {"id": AI_FAILED, "name": "ai-failed"},
            {"id": EXISTING_TAG, "name": "家計"},
        ]

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
            return httpx.Response(200, json={"results": self.docs, "next": None})
        if method == "GET" and path == "/api/tags/":
            return httpx.Response(200, json={"results": self.tag_rows, "next": None})
        if method == "GET" and path.endswith("/download/"):
            doc_id = int(path.split("/")[3])
            if request.url.params.get("original") == "true":
                self.events.append(("download", doc_id))
                return httpx.Response(200, content=PDF)
            self.events.append(("archive", doc_id))
            if doc_id in self.archive_status:
                return httpx.Response(self.archive_status[doc_id])
            return httpx.Response(200, content=ARCHIVE)
        if method == "POST" and path == "/api/documents/edit_pdf/":
            body = json.loads(request.content)
            doc_id = body["documents"][0]
            self.events.append(("edit", doc_id))
            if self.originals_dir is not None:
                self.saved_before_edit.append((self.originals_dir / f"{doc_id}.pdf").exists())
            if doc_id in self.edit_status:
                return httpx.Response(self.edit_status[doc_id])
            if doc_id in self.version_status_after_edit:
                self.version_status[doc_id] = self.version_status_after_edit[doc_id]
            self.edits.append(body)
            return httpx.Response(200, json={"result": "OK"})
        if method == "GET" and request.url.params.get("fields") == "tags":
            doc_id = int(path.split("/")[3])
            self.events.append(("tags", doc_id))
            if doc_id in self.tags_status:
                return httpx.Response(self.tags_status[doc_id])
            row = next(d for d in self.docs if d["id"] == doc_id)
            return httpx.Response(200, json={"tags": self.current_tags.get(doc_id, row["tags"])})
        if method == "GET" and path.startswith("/api/documents/"):
            doc_id = int(path.split("/")[3])
            self.events.append(("versions", doc_id))
            if doc_id in self.version_status:
                return httpx.Response(self.version_status[doc_id])
            seq = self.versions.setdefault(doc_id, [{1}])
            current = seq.pop(0) if len(seq) > 1 else seq[0]
            return httpx.Response(
                200, json={"versions": [{"id": v, "is_root": v == 1} for v in sorted(current)]}
            )
        if method == "PATCH":
            doc_id = int(path.split("/")[3])
            self.events.append(("patch", doc_id))
            if doc_id in self.patch_status:
                return httpx.Response(self.patch_status[doc_id])
            self.patches[doc_id] = json.loads(request.content)
            return httpx.Response(200, json={})
        return httpx.Response(404)


class FakeClock:
    """sleep で進む時計。600 秒待ちのテストも一瞬で終わる。"""

    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds

    def monotonic(self) -> float:
        return self.t


def _page(n: int, **over: object) -> PageInfo:
    values: dict = {
        "number": n,
        "ink_ratio": 0.1,
        "text": f"{n} ページ目 " + TEXT,
        "dhash": (1 << n) - 1 if n < 64 else n,
        "rotate": None,
        "rotate_confidence": 0.0,
    }
    values.update(over)
    return PageInfo(**values)


def _pages(n: int, **by_number: dict) -> list[PageInfo]:
    # dHash は互いに十分離す（重複と見なさない）
    hashes = [0, (1 << 64) - 1, 0x0F0F0F0F0F0F0F0F, 0xF0F0F0F0F0F0F0F0, 0x3333333333333333]
    return [
        _page(i, dhash=hashes[(i - 1) % len(hashes)], **by_number.get(str(i), {}))
        for i in range(1, n + 1)
    ]


NORMAL = _pages(3)
WITH_BLANK = _pages(3, **{"2": {"ink_ratio": 0.0, "text": ""}})
BORDERLINE = _pages(3, **{"2": {"ink_ratio": 0.005, "text": ""}})


@pytest.fixture
def originals_dir(tmp_path: Path) -> Path:
    return tmp_path / "originals"


@pytest.fixture
def originals(originals_dir: Path) -> OriginalsStore:
    return OriginalsStore(originals_dir)


def _run(
    server: FakePaperless,
    originals: OriginalsStore,
    clock: FakeClock,
    analyze: object = None,
    count: int = 3,
    thresholds: Thresholds = DEFAULT_THRESHOLDS,
) -> PageCycleResult:
    pages = analyze if analyze is not None else (lambda pdf, **kw: NORMAL)
    return run_page_cycle(
        server.client(),
        originals,
        analyze=pages,  # type: ignore[arg-type]
        count_pages=lambda pdf: count,
        thresholds=thresholds,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        now=lambda: 1_000_000_000.0,
    )


def _merged(*extra: int) -> list[int]:
    return sorted({INBOX, EXISTING_TAG, AI_TODO, *extra})


def test_定数() -> None:
    assert MAX_PAGES == 50
    assert VERSION_WAIT_SECONDS == 600
    assert VERSION_POLL_SECONDS == 5


def test_直すところが無ければedit_pdfを呼ばずai_todoへ進める(
    originals: OriginalsStore, originals_dir: Path
) -> None:
    server = FakePaperless([_doc()])
    r = _run(server, originals, FakeClock())
    assert r == PageCycleResult(unchanged=1)
    assert server.edits == []
    assert server.patches[DOC_ID] == {"tags": _merged()}
    assert not (originals_dir / f"{DOC_ID}.pdf").exists()


def test_白紙ページは退避してからedit_pdfで消し版が増えてからPATCHする(
    originals: OriginalsStore, originals_dir: Path
) -> None:
    server = FakePaperless([_doc()], originals_dir)
    server.versions[DOC_ID] = [{1}, {1, 2}]
    r = _run(server, originals, FakeClock(), analyze=lambda pdf, **kw: WITH_BLANK)
    assert r == PageCycleResult(fixed=1)
    assert server.saved_before_edit == [True]
    assert (originals_dir / f"{DOC_ID}.pdf").read_bytes() == PDF
    assert server.edits == [
        {"documents": [DOC_ID], "operations": [{"page": 1}, {"page": 3}], "update_document": True}
    ]
    assert server.events == [
        ("download", DOC_ID),
        ("archive", DOC_ID),
        ("versions", DOC_ID),
        ("edit", DOC_ID),
        ("versions", DOC_ID),
        ("tags", DOC_ID),
        ("patch", DOC_ID),
    ]
    assert server.patches[DOC_ID] == {"tags": _merged()}


def test_版が2回目のポーリングで増える(originals: OriginalsStore) -> None:
    server = FakePaperless([_doc()])
    server.versions[DOC_ID] = [{1}, {1}, {1, 2}]
    clock = FakeClock()
    r = _run(server, originals, clock, analyze=lambda pdf, **kw: WITH_BLANK)
    assert r == PageCycleResult(fixed=1)
    assert clock.sleeps == [5]


def test_版が増えないまま600秒たてばpage_reviewにする(originals: OriginalsStore) -> None:
    server = FakePaperless([_doc()])
    clock = FakeClock()
    r = _run(server, originals, clock, analyze=lambda pdf, **kw: WITH_BLANK)
    assert r == PageCycleResult(review=1)
    assert len(server.edits) == 1
    assert server.patches[DOC_ID] == {"tags": _merged(PAGE_REVIEW)}
    assert clock.t >= VERSION_WAIT_SECONDS
    assert clock.t <= VERSION_WAIT_SECONDS + VERSION_POLL_SECONDS
    assert set(clock.sleeps) == {5}


def test_版の取得が404ならpage_reviewにする(originals: OriginalsStore) -> None:
    server = FakePaperless([_doc()])
    server.version_status_after_edit[DOC_ID] = 404
    r = _run(server, originals, FakeClock(), analyze=lambda pdf, **kw: WITH_BLANK)
    assert r == PageCycleResult(review=1)
    assert len(server.edits) == 1
    assert server.patches[DOC_ID] == {"tags": _merged(PAGE_REVIEW)}


def test_境界の判定は直さずpage_reviewにする(originals: OriginalsStore) -> None:
    server = FakePaperless([_doc()])
    r = _run(server, originals, FakeClock(), analyze=lambda pdf, **kw: BORDERLINE)
    assert r == PageCycleResult(unchanged=1, review=1)
    assert server.edits == []
    assert server.patches[DOC_ID] == {"tags": _merged(PAGE_REVIEW)}


def test_edit_pdfが403ならpage_reviewにして次の書類へ進む(originals: OriginalsStore) -> None:
    server = FakePaperless([_doc(), _doc(101)])
    server.edit_status[DOC_ID] = 403
    server.versions[101] = [{1}, {1, 2}]
    r = _run(server, originals, FakeClock(), analyze=lambda pdf, **kw: WITH_BLANK)
    assert r == PageCycleResult(fixed=1, review=1)
    assert server.patches[DOC_ID] == {"tags": _merged(PAGE_REVIEW)}
    assert server.patches[101] == {"tags": _merged()}
    # 403 の後は版を待たない
    assert server.events.count(("versions", DOC_ID)) == 1


def test_analyzeの想定外の例外はpage_reviewにして型名だけを残す(
    originals: OriginalsStore, caplog: pytest.LogCaptureFixture
) -> None:
    server = FakePaperless([_doc()])

    def broken(pdf: bytes, **kw: object) -> list[PageInfo]:
        raise RuntimeError("本文の断片 秘密")

    with caplog.at_level(logging.DEBUG):
        r = _run(server, originals, FakeClock(), analyze=broken)
    assert r == PageCycleResult(review=1)
    assert server.patches[DOC_ID] == {"tags": _merged(PAGE_REVIEW)}
    assert "RuntimeError" in caplog.text
    assert "秘密" not in caplog.text
    assert all(rec.exc_info is None for rec in caplog.records)


def test_ページが多すぎればanalyzeを呼ばずpage_reviewにする(originals: OriginalsStore) -> None:
    server = FakePaperless([_doc()])
    called: list[bytes] = []

    def analyze(pdf: bytes, text_pdf: bytes | None = None, **kw: object) -> list[PageInfo]:
        called.append(pdf)
        return NORMAL

    r = _run(server, originals, FakeClock(), analyze=analyze, count=51)
    assert called == []
    assert r == PageCycleResult(review=1)
    assert server.patches[DOC_ID] == {"tags": _merged(PAGE_REVIEW)}


def test_ページが50ならanalyzeを呼ぶ(originals: OriginalsStore) -> None:
    server = FakePaperless([_doc()])
    called: list[bytes] = []

    def analyze(pdf: bytes, text_pdf: bytes | None = None, **kw: object) -> list[PageInfo]:
        called.append(pdf)
        return NORMAL

    _run(server, originals, FakeClock(), analyze=analyze, count=50)
    assert called == [PDF]


def test_退避でServiceUnavailableならedit_pdfを呼ばず打ち切る(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_bytes(b"x")
    store = OriginalsStore(blocker / "originals")
    server = FakePaperless([_doc(), _doc(101)])
    r = _run(server, store, FakeClock(), analyze=lambda pdf, **kw: WITH_BLANK)
    assert r == PageCycleResult(aborted=True)
    assert server.edits == []
    assert ("edit", DOC_ID) not in server.events
    assert server.patches == {}
    assert ("download", 101) not in server.events


def test_一覧の取得が503なら打ち切る(originals: OriginalsStore) -> None:
    server = FakePaperless([_doc()])
    server.list_status = 503
    r = _run(server, originals, FakeClock())
    assert r == PageCycleResult(aborted=True)
    assert server.patches == {}


def test_2件目のPATCHが500なら打ち切る(originals: OriginalsStore) -> None:
    server = FakePaperless([_doc(), _doc(101), _doc(102)])
    server.patch_status[101] = 500
    r = _run(server, originals, FakeClock())
    assert r == PageCycleResult(unchanged=1, aborted=True)
    assert list(server.patches) == [DOC_ID]
    assert ("download", 102) not in server.events


def test_PATCHが404なら警告して次の書類へ進む(
    originals: OriginalsStore, caplog: pytest.LogCaptureFixture
) -> None:
    server = FakePaperless([_doc(), _doc(101)])
    server.patch_status[DOC_ID] = 404
    with caplog.at_level(logging.WARNING):
        r = _run(server, originals, FakeClock())
    assert r.aborted is False
    assert server.patches[101] == {"tags": _merged()}
    assert any(rec.levelno == logging.WARNING for rec in caplog.records)


def test_必要なタグが無ければMissingTagsError(originals: OriginalsStore) -> None:
    server = FakePaperless([_doc()])
    server.tag_rows = [t for t in server.tag_rows if t["name"] != "page-review"]
    with pytest.raises(MissingTagsError):
        _run(server, originals, FakeClock())


def test_周期の始めに古い退避ファイルを消す(originals: OriginalsStore, originals_dir: Path) -> None:
    originals_dir.mkdir()
    old = originals_dir / "5.pdf"
    old.write_bytes(PDF)
    os.utime(old, (0, 0))
    server = FakePaperless([])
    _run(server, originals, FakeClock())
    assert not old.exists()


def test_ログに判定理由を出しタイトルと本文を出さない(
    originals: OriginalsStore, caplog: pytest.LogCaptureFixture
) -> None:
    server = FakePaperless([_doc(title="極秘のタイトル", content="極秘の本文")])
    server.versions[DOC_ID] = [{1}, {1, 2}]
    secret_pages = [
        _page(1, text="極秘のページ本文 " + TEXT, dhash=0),
        _page(2, ink_ratio=0.0, text="", dhash=(1 << 64) - 1),
        _page(3, dhash=0x0F0F0F0F0F0F0F0F),
    ]
    with caplog.at_level(logging.DEBUG):
        _run(server, originals, FakeClock(), analyze=lambda pdf, **kw: secret_pages)
    assert "極秘" not in caplog.text
    assert f"書類 {DOC_ID}: pages fixed" in caplog.text
    assert f"書類 {DOC_ID} p2: delete 白紙 (ink=0.0000 text=0)" in caplog.text


def test_analyzeに原本とアーカイブ版の両方を渡し退避するのは原本(
    originals: OriginalsStore, originals_dir: Path
) -> None:
    server = FakePaperless([_doc()], originals_dir)
    seen: list[tuple[bytes, bytes | None]] = []

    def analyze(pdf: bytes, text_pdf: bytes | None = None, **kw: object) -> list[PageInfo]:
        seen.append((pdf, text_pdf))
        return WITH_BLANK

    _run(server, originals, FakeClock(), analyze=analyze)
    assert seen == [(PDF, ARCHIVE)]
    assert (originals_dir / f"{DOC_ID}.pdf").read_bytes() == PDF


@pytest.mark.parametrize("status", [400, 404])
def test_アーカイブ版の取得が書類起因ならpage_reviewにする(
    originals: OriginalsStore, status: int
) -> None:
    server = FakePaperless([_doc()])
    server.archive_status[DOC_ID] = status
    r = _run(server, originals, FakeClock(), analyze=lambda pdf, **kw: WITH_BLANK)
    assert r == PageCycleResult(review=1)
    assert server.edits == []
    assert server.patches[DOC_ID] == {"tags": _merged(PAGE_REVIEW)}


def test_アーカイブ版の取得が503なら打ち切る(originals: OriginalsStore) -> None:
    server = FakePaperless([_doc()])
    server.archive_status[DOC_ID] = 503
    r = _run(server, originals, FakeClock(), analyze=lambda pdf, **kw: WITH_BLANK)
    assert r.aborted
    assert server.edits == []


def test_退避が既にある書類はedit_pdfを呼ばずpage_reviewにする(
    originals: OriginalsStore, originals_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # 前の周期で edit_pdf を送った後に打ち切られた書類。古い版から操作を計算し直して
    # 送ると、取り込み途中の新しい版に当たって別のページを消しうる
    originals_dir.mkdir()
    (originals_dir / f"{DOC_ID}.pdf").write_bytes(b"first original")
    server = FakePaperless([_doc()], originals_dir)
    with caplog.at_level(logging.INFO):
        r = _run(server, originals, FakeClock(), analyze=lambda pdf, **kw: WITH_BLANK)
    assert r == PageCycleResult(review=1)
    assert ("edit", DOC_ID) not in server.events
    assert server.patches[DOC_ID] == {"tags": _merged(PAGE_REVIEW)}
    assert (originals_dir / f"{DOC_ID}.pdf").read_bytes() == b"first original"
    assert "前回の整形が未確認" in caplog.text


def test_退避があっても直すところが無ければunchanged(
    originals: OriginalsStore, originals_dir: Path
) -> None:
    originals_dir.mkdir()
    (originals_dir / f"{DOC_ID}.pdf").write_bytes(b"first original")
    server = FakePaperless([_doc()], originals_dir)
    r = _run(server, originals, FakeClock())
    assert r == PageCycleResult(unchanged=1)
    assert server.patches[DOC_ID] == {"tags": _merged()}


def test_一覧の後に変わったタグを残してマージする(originals: OriginalsStore) -> None:
    server = FakePaperless([_doc()])
    server.versions[DOC_ID] = [{1}, {1, 2}]
    # 版を待つ間に人が inbox を外し、別のタグを付けた
    server.current_tags[DOC_ID] = [PAGE_TODO, EXISTING_TAG, 50]
    r = _run(server, originals, FakeClock(), analyze=lambda pdf, **kw: WITH_BLANK)
    assert r == PageCycleResult(fixed=1)
    assert server.patches[DOC_ID] == {"tags": sorted({EXISTING_TAG, 50, AI_TODO})}


@pytest.mark.parametrize("status", [400, 404])
def test_タグの読み直しが書類起因の失敗ならPATCHせず次の書類へ進む(
    originals: OriginalsStore, caplog: pytest.LogCaptureFixture, status: int
) -> None:
    server = FakePaperless([_doc(), _doc(101)])
    server.tags_status[DOC_ID] = status
    with caplog.at_level(logging.WARNING):
        r = _run(server, originals, FakeClock())
    assert r == PageCycleResult(unchanged=2)
    assert DOC_ID not in server.patches
    assert server.patches[101] == {"tags": _merged()}
    assert "タグの付け替えに失敗" in caplog.text


def test_タグの読み直しが503なら打ち切る(originals: OriginalsStore) -> None:
    server = FakePaperless([_doc(), _doc(101)])
    server.tags_status[DOC_ID] = 503
    r = _run(server, originals, FakeClock())
    assert r == PageCycleResult(aborted=True)
    assert server.patches == {}
    assert ("download", 101) not in server.events


def test_edit_pdfが5xxなら打ち切りPATCHしない(
    originals: OriginalsStore, originals_dir: Path
) -> None:
    server = FakePaperless([_doc(), _doc(101)])
    server.edit_status[DOC_ID] = 502
    r = _run(server, originals, FakeClock(), analyze=lambda pdf, **kw: WITH_BLANK)
    assert r == PageCycleResult(aborted=True)
    assert server.patches == {}
    assert ("download", 101) not in server.events
    assert (originals_dir / f"{DOC_ID}.pdf").read_bytes() == PDF


def test_edit_pdfの後のポーリングが503なら打ち切りPATCHせず退避は残す(
    originals: OriginalsStore, originals_dir: Path
) -> None:
    server = FakePaperless([_doc()])
    server.version_status_after_edit[DOC_ID] = 503
    r = _run(server, originals, FakeClock(), analyze=lambda pdf, **kw: WITH_BLANK)
    assert r == PageCycleResult(aborted=True)
    assert len(server.edits) == 1
    assert server.patches == {}
    assert (originals_dir / f"{DOC_ID}.pdf").read_bytes() == PDF


def test_OSDを省くしきい値をanalyzeに渡す(originals: OriginalsStore) -> None:
    server = FakePaperless([_doc()])
    seen: list[object] = []

    def analyze(pdf: bytes, **kw: object) -> list[PageInfo]:
        seen.append(kw.get("osd_skip_ink"))
        return NORMAL

    thresholds = dataclasses.replace(DEFAULT_THRESHOLDS, blank_ink_review_max=0.02)
    _run(server, originals, FakeClock(), analyze=analyze, thresholds=thresholds)
    assert seen == [0.02]


def test_直してかつ確認待ちならfixed_reviewとログに出す(
    originals: OriginalsStore, caplog: pytest.LogCaptureFixture
) -> None:
    server = FakePaperless([_doc()])
    server.versions[DOC_ID] = [{1}, {1, 2}]
    # 2 ページ目は白紙で消し、3 ページ目は境界なので確認に回す
    pages = _pages(
        3, **{"2": {"ink_ratio": 0.0, "text": ""}, "3": {"ink_ratio": 0.005, "text": ""}}
    )
    with caplog.at_level(logging.INFO):
        r = _run(server, originals, FakeClock(), analyze=lambda pdf, **kw: pages)
    assert r == PageCycleResult(fixed=1, review=1)
    assert f"書類 {DOC_ID}: pages fixed+review" in caplog.text
