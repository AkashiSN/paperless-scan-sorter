"""Paperless API クライアントのテスト。"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from paperless_scan_sorter.errors import DocumentFailure, ServiceUnavailable
from paperless_scan_sorter.paperless import Document, Named, PaperlessClient

BASE = "https://paperless.example.com"

Handler = Callable[[httpx.Request], httpx.Response]


def make_client(handler: Handler) -> PaperlessClient:
    return PaperlessClient(BASE, "secret", transport=httpx.MockTransport(handler))


def doc_json(doc_id: int, **extra: object) -> dict[str, object]:
    return {
        "id": doc_id,
        "title": "t",
        "content": "本文",
        "correspondent": None,
        "document_type": 3,
        "tags": [5, 7],
        **extra,
    }


def test_トークンを付けて要求する() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"next": None, "results": []})

    make_client(handler).tags()
    assert seen[0].headers["Authorization"] == "Token secret"
    assert seen[0].headers["Accept"] == "application/json"
    assert seen[0].url.params["page_size"] == "100"
    assert seen[0].url.path == "/api/tags/"


def test_ページを全部辿ってから返す() -> None:
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        if "page=2" in str(request.url):
            return httpx.Response(200, json={"next": None, "results": [doc_json(2)]})
        return httpx.Response(
            200,
            json={
                "next": "http://internal:8000/api/documents/?page=2&tags__id__all=5",
                "results": [doc_json(1)],
            },
        )

    docs = make_client(handler).documents_with_tag(5)
    assert [d.id for d in docs] == [1, 2]
    assert "tags__id__all=5" in seen_urls[0]
    assert "page_size=100" in seen_urls[0]
    assert seen_urls[1].startswith("https://paperless.example.com/api/documents/?page=2")


def test_書類をモデルに変換する() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"next": None, "results": [doc_json(1, extra_key="x")]},
        )

    (doc,) = make_client(handler).documents_with_tag(5)
    assert doc == Document(1, "t", "本文", None, 3, frozenset({5, 7}))


def test_contentがnullなら空文字にする() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"next": None, "results": [doc_json(1, content=None)]})

    (doc,) = make_client(handler).documents_with_tag(5)
    assert doc.content == ""


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("tags", "/api/tags/"),
        ("correspondents", "/api/correspondents/"),
        ("document_types", "/api/document_types/"),
    ],
)
def test_名前付きの一覧を返す(method: str, path: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == path
        return httpx.Response(
            200,
            json={"next": None, "results": [{"id": 1, "name": "a", "slug": "a"}]},
        )

    assert getattr(make_client(handler), method)() == [Named(1, "a")]


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("create_correspondent", "/api/correspondents/"),
        ("create_document_type", "/api/document_types/"),
    ],
)
def test_作成はnameをPOSTする(method: str, path: str) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"id": 9, "name": "○○銀行", "slug": "x"})

    result = getattr(make_client(handler), method)("○○銀行")
    assert result == Named(9, "○○銀行")
    assert seen[0].method == "POST"
    assert seen[0].url.path == path
    assert json.loads(seen[0].content) == {"name": "○○銀行"}


def test_PATCHはJSONで送る() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    make_client(handler).patch_document(4, {"tags": [1, 2]})
    assert seen[0].method == "PATCH"
    assert seen[0].url.path == "/api/documents/4/"
    assert json.loads(seen[0].content) == {"tags": [1, 2]}


def test_原本をダウンロードする() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"%PDF-1.7")

    assert make_client(handler).download_original(4) == b"%PDF-1.7"
    assert seen[0].url.path == "/api/documents/4/download/"
    assert seen[0].url.params["original"] == "true"


def test_download_archiveはoriginalを付けずにダウンロードする() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"%PDF-archive")

    assert make_client(handler).download_archive(4) == b"%PDF-archive"
    assert seen[0].url.path == "/api/documents/4/download/"
    assert "original" not in seen[0].url.params


@pytest.mark.parametrize("status", [400, 404])
def test_書類単位の400と404は書類起因の失敗(status: int) -> None:
    client = make_client(lambda r: httpx.Response(status))
    with pytest.raises(DocumentFailure):
        client.patch_document(1, {"title": "x"})
    with pytest.raises(DocumentFailure):
        client.download_original(1)
    with pytest.raises(DocumentFailure):
        client.download_archive(1)


@pytest.mark.parametrize("status", [400, 404])
def test_一覧の400と404は周期を止める(status: int) -> None:
    client = make_client(lambda r: httpx.Response(status))
    with pytest.raises(ServiceUnavailable):
        client.tags()


@pytest.mark.parametrize("status", [400, 404])
def test_作成の400と404は書類起因の失敗(status: int) -> None:
    client = make_client(lambda r: httpx.Response(status))
    with pytest.raises(DocumentFailure):
        client.create_correspondent("x")
    with pytest.raises(DocumentFailure):
        client.create_document_type("x")


@pytest.mark.parametrize("status", [401, 403, 500])
def test_作成の認証とサーバーの失敗は周期を止める(status: int) -> None:
    client = make_client(lambda r: httpx.Response(status))
    with pytest.raises(ServiceUnavailable):
        client.create_correspondent("x")


@pytest.mark.parametrize("status", [401, 403, 500, 503, 429])
def test_認証とサーバーの失敗は周期を止める(status: int) -> None:
    client = make_client(lambda r: httpx.Response(status))
    with pytest.raises(ServiceUnavailable):
        client.tags()
    with pytest.raises(ServiceUnavailable):
        client.patch_document(1, {"title": "x"})
    with pytest.raises(ServiceUnavailable):
        client.download_original(1)


def test_接続できなければ周期を止める() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    with pytest.raises(ServiceUnavailable):
        make_client(handler).tags()


def test_例外にトークンを含めない() -> None:
    client = make_client(lambda r: httpx.Response(401, text="Token secret"))
    with pytest.raises(ServiceUnavailable) as e:
        client.tags()
    assert "secret" not in str(e.value)
    assert "401" in str(e.value)


def test_作成の応答がJSONでなければ周期を止める() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>秘密の本文</html>")

    with pytest.raises(ServiceUnavailable) as e:
        make_client(handler).create_correspondent("x")
    assert str(e.value) == "POST /api/correspondents/: 応答の形式が不正"


@pytest.mark.parametrize(
    "body",
    [{"results": [{"name": "x"}], "next": None}, {"next": None}, [1, 2], {"results": [1]}],
)
def test_一覧の応答の形が違えば周期を止める(body: object) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    with pytest.raises(ServiceUnavailable) as e:
        make_client(handler).tags()
    assert str(e.value) == "GET /api/tags/: 応答の形式が不正"


def test_書類一覧の応答の形が違えば周期を止める() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [{"id": 1}], "next": None})

    with pytest.raises(ServiceUnavailable):
        make_client(handler).documents_with_tag(2)


def test_edit_pdfは操作を並べて版として取り込み直させる() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"result": "OK"})

    operations = [{"page": 1}, {"page": 3, "rotate": 90}]
    make_client(handler).edit_pdf(7, operations)
    assert seen[0].method == "POST"
    assert seen[0].url.path == "/api/documents/edit_pdf/"
    assert json.loads(seen[0].content) == {
        "documents": [7],
        "operations": operations,
        "update_document": True,
    }


@pytest.mark.parametrize("status", [400, 403, 404])
def test_edit_pdfの400と403と404は書類起因の失敗になる(status: int) -> None:
    client = make_client(lambda request: httpx.Response(status, text="secret body"))
    with pytest.raises(DocumentFailure) as e:
        client.edit_pdf(7, [{"page": 1}])
    assert str(status) in str(e.value)
    assert "secret body" not in str(e.value)


def test_edit_pdfの403には所有者の条件を添える() -> None:
    client = make_client(lambda request: httpx.Response(403))
    with pytest.raises(DocumentFailure, match="所有者"):
        client.edit_pdf(7, [{"page": 1}])


def test_edit_pdfの500は打ち切り対象になる() -> None:
    client = make_client(lambda request: httpx.Response(500))
    with pytest.raises(ServiceUnavailable):
        client.edit_pdf(7, [{"page": 1}])


def test_既存の呼び出しの403は引き続き打ち切り対象になる() -> None:
    client = make_client(lambda request: httpx.Response(403))
    with pytest.raises(ServiceUnavailable):
        client.patch_document(7, {"title": "x"})


def test_書類の版の一覧を返す() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={"versions": [{"id": 1, "is_root": True}, {"id": 4, "is_root": False}]},
        )

    assert make_client(handler).document_versions(7) == frozenset({1, 4})
    assert seen[0].url.path == "/api/documents/7/"
    assert seen[0].url.params["fields"] == "versions"


@pytest.mark.parametrize("status", [400, 404])
def test_版の一覧の400と404は書類起因の失敗になる(status: int) -> None:
    client = make_client(lambda request: httpx.Response(status))
    with pytest.raises(DocumentFailure):
        client.document_versions(7)


def test_versionsが無い応答は打ち切り対象になる() -> None:
    client = make_client(lambda request: httpx.Response(200, json={"id": 7}))
    with pytest.raises(ServiceUnavailable):
        client.document_versions(7)


def test_書類のタグを読み直す() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"tags": [3, 5]})

    assert make_client(handler).document_tags(7) == frozenset({3, 5})
    assert seen[0].method == "GET"
    assert seen[0].url.path == "/api/documents/7/"
    assert seen[0].url.params["fields"] == "tags"


@pytest.mark.parametrize("status", [400, 404])
def test_タグの読み直しの400と404は書類起因の失敗になる(status: int) -> None:
    client = make_client(lambda request: httpx.Response(status))
    with pytest.raises(DocumentFailure):
        client.document_tags(7)


@pytest.mark.parametrize("body", [{"id": 7}, {"tags": None}, [1, 2]])
def test_tagsの形が違う応答は打ち切り対象になる(body: object) -> None:
    client = make_client(lambda request: httpx.Response(200, json=body))
    with pytest.raises(ServiceUnavailable):
        client.document_tags(7)
