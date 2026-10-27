"""Paperless-ngx の REST API クライアント。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from paperless_scan_sorter.errors import DocumentFailure, ServiceUnavailable

_PAGE_SIZE = 100


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
        rows = self._list_all("/api/documents/", {"tags__id__all": tag_id, "page_size": _PAGE_SIZE})
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

    def _named(self, path: str) -> list[Named]:
        rows = self._list_all(path, {"page_size": _PAGE_SIZE})
        return [Named(r["id"], r["name"]) for r in rows]

    def _create(self, path: str, name: str) -> Named:
        body = self._request("POST", path, document_scoped=True, json={"name": name}).json()
        return Named(body["id"], body["name"])

    def _list_all(self, path: str, params: dict[str, object]) -> list[dict]:
        """next を辿って全ページを集める。

        next は絶対 URL だが、リバースプロキシ越しでは内部ホスト名を指すことが
        あるため、パスとクエリだけを base_url に付け替えて辿る。
        """
        rows: list[dict] = []
        response = self._request("GET", path, params=params)
        while True:
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
        if document_scoped and status in (400, 404):
            raise DocumentFailure(message)
        raise ServiceUnavailable(message)
