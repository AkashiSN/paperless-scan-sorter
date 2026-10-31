"""Paperless-ngx の REST API クライアント。"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from paperless_scan_sorter.errors import DocumentFailure, ServiceUnavailable

_PAGE_SIZE = 100


@contextmanager
def _parsing(method: str, path: str) -> Iterator[None]:
    """成功応答の読み取りで形が違えば ServiceUnavailable にする。

    応答の形が違うのは書類ではなく Paperless かその手前（リバースプロキシなど）の
    問題なので、書類を数えずに周期を打ち切る。メッセージには応答本文を入れない。
    """
    try:
        yield
    except ValueError, KeyError, TypeError, AttributeError:
        label_path = path.split("?", 1)[0]
        raise ServiceUnavailable(f"{method} {label_path}: 応答の形式が不正") from None


@dataclass(frozen=True)
class Document:
    """分類に使う書類の項目。"""

    id: int
    title: str
    content: str
    correspondent: int | None
    document_type: int | None
    tags: frozenset[int]


@dataclass(frozen=True)
class Titled:
    """タイトルの例に使う書類の項目。"""

    title: str
    correspondent: int | None
    document_type: int | None


@dataclass(frozen=True)
class Named:
    """id と名前だけを持つ項目（タグ・correspondent・書類種別）。"""

    id: int
    name: str


class PaperlessClient:
    """Paperless API を呼ぶ。失敗は errors.py の例外に分類して送出する。"""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._http = httpx.Client(
            headers={
                "Authorization": f"Token {token}",
                "Accept": "application/json",
            },
            transport=transport,
            timeout=timeout,
        )

    def close(self) -> None:
        self._http.close()

    def documents_with_tag(self, tag_id: int) -> list[Document]:
        """タグを持つ書類を全ページ集めてから返す。"""
        path = "/api/documents/"
        rows = self._list_all(path, {"tags__id__all": tag_id, "page_size": _PAGE_SIZE})
        with _parsing("GET", path):
            return [
                Document(
                    id=r["id"],
                    title=r["title"],
                    content=r.get("content") or "",
                    correspondent=r.get("correspondent"),
                    document_type=r.get("document_type"),
                    tags=frozenset(r["tags"]),
                )
                for r in rows
            ]

    def recent_titles(self, tag_id: int, *, limit: int) -> list[Titled]:
        """タグを持つ書類を新しい順に limit 件まで（1 ページだけ）返す。"""
        path = "/api/documents/"
        params = {
            "tags__id__all": tag_id,
            "ordering": "-added",
            "page_size": limit,
            "fields": "id,title,correspondent,document_type",
        }
        response = self._request("GET", path, params=params)
        with _parsing("GET", path):
            return [
                Titled(r["title"], r["correspondent"], r["document_type"])
                for r in response.json()["results"]
            ]

    def tags(self) -> list[Named]:
        return self._named("/api/tags/")

    def correspondents(self) -> list[Named]:
        return self._named("/api/correspondents/")

    def document_types(self) -> list[Named]:
        return self._named("/api/document_types/")

    def create_correspondent(self, name: str) -> Named:
        return self._create("/api/correspondents/", name)

    def create_document_type(self, name: str) -> Named:
        return self._create("/api/document_types/", name)

    def patch_document(self, doc_id: int, fields: Mapping[str, object]) -> None:
        path = f"/api/documents/{doc_id}/"
        self._request("PATCH", path, document_scoped=True, json=dict(fields))

    def download_original(self, doc_id: int) -> bytes:
        path = f"/api/documents/{doc_id}/download/"
        response = self._request("GET", path, document_scoped=True, params={"original": "true"})
        return response.content

    def download_archive(self, doc_id: int) -> bytes:
        """アーカイブ版（OCR のテキスト層付き）をダウンロードする。

        `original` を付けないので、アーカイブ版が無い書類では原本が返る。
        """
        path = f"/api/documents/{doc_id}/download/"
        response = self._request("GET", path, document_scoped=True)
        return response.content

    def edit_pdf(self, doc_id: int, operations: Sequence[Mapping[str, int]]) -> None:
        """残すページを並べた順に、同じ書類 ID の新しい版として取り込み直させる。

        403 は書類の所有者が無いか ai-classifier のユーザーでないときに返り、
        書類ごとに起きるので、書類起因として扱う。
        """
        self._request(
            "POST",
            "/api/documents/edit_pdf/",
            document_statuses=(400, 403, 404),
            forbidden_hint="書類の所有者が無いか ai-classifier のユーザーであること",
            json={
                "documents": [doc_id],
                "operations": [dict(op) for op in operations],
                "update_document": True,
            },
        )

    def document_versions(self, doc_id: int) -> frozenset[int]:
        """書類の版の ID を返す。edit_pdf の完了は新しい ID が増えたことで判定する。"""
        path = f"/api/documents/{doc_id}/"
        response = self._request("GET", path, document_scoped=True, params={"fields": "versions"})
        with _parsing("GET", path):
            return frozenset(v["id"] for v in response.json()["versions"])

    def document_tags(self, doc_id: int) -> frozenset[int]:
        """書類のいまのタグを返す。タグを置き換える PATCH の直前に読み直すために使う。"""
        path = f"/api/documents/{doc_id}/"
        response = self._request("GET", path, document_scoped=True, params={"fields": "tags"})
        with _parsing("GET", path):
            return frozenset(response.json()["tags"])

    def _named(self, path: str) -> list[Named]:
        rows = self._list_all(path, {"page_size": _PAGE_SIZE})
        with _parsing("GET", path):
            return [Named(r["id"], r["name"]) for r in rows]

    def _create(self, path: str, name: str) -> Named:
        response = self._request("POST", path, document_scoped=True, json={"name": name})
        with _parsing("POST", path):
            body = response.json()
            return Named(body["id"], body["name"])

    def _list_all(self, path: str, params: dict[str, object]) -> list[dict]:
        """next を辿って全ページを集める。

        next は絶対 URL だが、リバースプロキシ越しでは内部ホスト名を指すことが
        あるため、パスとクエリだけを base_url に付け替えて辿る。
        """
        rows: list[dict] = []
        response = self._request("GET", path, params=params)
        while True:
            with _parsing("GET", path):
                body = response.json()
                rows.extend(body["results"])
                next_url = body.get("next")
                if not next_url:
                    return rows
                parts = urlsplit(next_url)
                target = parts.path + (f"?{parts.query}" if parts.query else "")
            response = self._request("GET", target)

    def _request(
        self,
        method: str,
        path: str,
        *,
        document_scoped: bool = False,
        document_statuses: tuple[int, ...] | None = None,
        forbidden_hint: str | None = None,
        **kwargs: object,
    ) -> httpx.Response:
        # 例外メッセージにはメソッド・パス・ステータスだけを入れる。
        # トークンとレスポンス本文は入れない。
        label_path = path.split("?", 1)[0]
        try:
            response = self._http.request(method, self._base_url + path, **kwargs)
        except httpx.TransportError as e:
            raise ServiceUnavailable(
                f"{method} {label_path}: 接続に失敗した ({type(e).__name__})"
            ) from None
        status = response.status_code
        if status < 400:
            return response
        message = f"{method} {label_path}: HTTP {status}"
        # 書類起因として扱うステータス。既定は 400/404 で、403 は呼び出しごとに足す。
        if document_statuses is None and document_scoped:
            document_statuses = (400, 404)
        if document_statuses and status in document_statuses:
            if status == 403 and forbidden_hint:
                message = f"{message} ({forbidden_hint})"
            raise DocumentFailure(message)
        raise ServiceUnavailable(message)
