"""ai-todo の書類を 1 周期分処理する。"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from paperless_scan_sorter.claude import ClaudeClient
from paperless_scan_sorter.decide import Decision, TagIds, decide, failed_tags
from paperless_scan_sorter.errors import DocumentFailure, MissingTagsError, ServiceUnavailable
from paperless_scan_sorter.failures import CreatedStore, FailureStore
from paperless_scan_sorter.paperless import Document, Named, PaperlessClient
from paperless_scan_sorter.prompt import build_prompt
from paperless_scan_sorter.tags import REQUIRED_TAGS
from paperless_scan_sorter.validate import parse_classification

logger = logging.getLogger(__name__)

MAX_FAILURES = 3
CONTENT_LIMIT = 8000
# 前回の試行で発行元・文書タイプを作った書類を確認待ちにするときの理由。
CREATED_BEFORE_REASON = "前回の試行で発行元・文書タイプを新規作成"
MIN_TEXT_CHARS = 100
# claude に読ませる PDF の上限。これを超える書類は読ませずに書類の失敗にする（D-008）。
MAX_PDF_BYTES = 20 * 1024 * 1024


@dataclass
class CycleResult:
    """1 周期の集計。"""

    done: int = 0
    review: int = 0
    retried: int = 0
    failed: int = 0
    aborted: bool = False


def resolve_tag_ids(tags: Sequence[Named]) -> TagIds:
    """状態管理用タグの ID を名前で引く。無いタグがあれば MissingTagsError。"""
    by_name = {t.name: t.id for t in tags}
    missing = [n for n in REQUIRED_TAGS if n not in by_name]
    if missing:
        raise MissingTagsError("Paperless に必要なタグが無い: " + ", ".join(missing))
    return TagIds(
        inbox=by_name["inbox"],
        ai_todo=by_name["ai-todo"],
        ai_done=by_name["ai-done"],
        ai_review=by_name["ai-review"],
        ai_failed=by_name["ai-failed"],
    )


def run_cycle(
    paperless: PaperlessClient,
    claude: ClaudeClient,
    store: FailureStore,
    created: CreatedStore,
    *,
    today: date,
) -> CycleResult:
    """ai-todo の書類を全件取得してから 1 件ずつ処理する。"""
    result = CycleResult()
    try:
        tags = paperless.tags()
        correspondents = paperless.correspondents()
        document_types = paperless.document_types()
        ids = resolve_tag_ids(tags)
        docs = paperless.documents_with_tag(ids.ai_todo)
    except ServiceUnavailable as e:
        logger.error("周期の準備に失敗したため打ち切る: %s", e)
        result.aborted = True
        return result

    tag_names = {t.name: t.id for t in tags}
    prompt = build_prompt(
        correspondents=[n.name for n in correspondents],
        document_types=[n.name for n in document_types],
        tags=tag_names,
    )

    for doc in docs:
        if store.count(doc.id) >= MAX_FAILURES:
            # 前の周期で ai-failed への付け替えだけが失敗した書類。判定し直さず
            # 付け替えだけをやり直す（D-007）。
            try:
                _mark_failed(doc, paperless, store, created, ids, result)
            except ServiceUnavailable as e:
                logger.error("書類 %d の失敗処理中に利用不可となり周期を打ち切る: %s", doc.id, e)
                result.aborted = True
                return result
            continue
        try:
            decision = _classify(
                doc,
                paperless,
                claude,
                prompt,
                correspondents,
                document_types,
                tag_names,
                ids,
                today,
                created,
            )
            fields = _fields(decision)
            paperless.patch_document(doc.id, fields)
        except ServiceUnavailable as e:
            logger.error("書類 %d の処理中に利用不可となり周期を打ち切る: %s", doc.id, e)
            result.aborted = True
            return result
        except Exception as e:
            if isinstance(e, DocumentFailure):
                failure = e
            else:
                # 想定外の例外も書類の失敗として数え、1 件で列全体を止めない。
                # メッセージや traceback には本文が混ざりうるので、型名だけを残す。
                logger.error("書類 %d の処理中に想定外の例外: %s", doc.id, type(e).__name__)
                failure = DocumentFailure(f"想定外の例外 ({type(e).__name__})")
            try:
                _on_failure(doc, failure, paperless, store, created, ids, result)
            except ServiceUnavailable as unavailable:
                logger.error(
                    "書類 %d の失敗処理中に利用不可となり周期を打ち切る: %s", doc.id, unavailable
                )
                result.aborted = True
                return result
            continue
        store.clear(doc.id)
        created.clear(doc.id)
        if decision.review:
            result.review += 1
            logger.info("書類 %d: review (%s)", doc.id, ", ".join(decision.reasons))
        else:
            result.done += 1
            logger.info("書類 %d: done", doc.id)
    return result


def _classify(
    doc: Document,
    paperless: PaperlessClient,
    claude: ClaudeClient,
    prompt: str,
    correspondents: list[Named],
    document_types: list[Named],
    tag_names: dict[str, int],
    ids: TagIds,
    today: date,
    created: CreatedStore,
) -> Decision:
    """書類を判定し、新規の発行元・文書タイプを作って ID に置き換えた Decision を返す。"""
    if sum(1 for ch in doc.content if not ch.isspace()) < MIN_TEXT_CHARS:
        pdf = paperless.download_original(doc.id)
        if len(pdf) > MAX_PDF_BYTES:
            raise DocumentFailure(f"PDF が大きすぎる ({len(pdf)} bytes)")
        text = claude.classify_pdf(prompt, pdf)
    else:
        text = claude.classify_text(prompt, doc.content[:CONTENT_LIMIT])
    classification = parse_classification(text, known_tags=tag_names.keys(), today=today)
    # 前回作った名前は今回は既存として見えるので、記録から確認待ちに回す（D-009）。
    extra = (CREATED_BEFORE_REASON,) if created.contains(doc.id) else ()
    decision = decide(
        doc,
        classification,
        correspondents=correspondents,
        document_types=document_types,
        tag_names=tag_names,
        ids=ids,
        extra_review_reasons=extra,
    )
    correspondent = decision.correspondent
    if isinstance(correspondent, str):
        new = paperless.create_correspondent(correspondent)
        created.mark(doc.id)
        correspondents.append(new)
        logger.info("発行元を新規作成: %s", new.name)
        correspondent = new.id
    document_type = decision.document_type
    if isinstance(document_type, str):
        new = paperless.create_document_type(document_type)
        created.mark(doc.id)
        document_types.append(new)
        logger.info("文書タイプを新規作成: %s", new.name)
        document_type = new.id
    return Decision(
        title=decision.title,
        correspondent=correspondent,
        document_type=document_type,
        created=decision.created,
        tags=decision.tags,
        review=decision.review,
        reasons=decision.reasons,
    )


def _fields(decision: Decision) -> dict[str, object]:
    """PATCH するフィールド。None の項目は送らない。"""
    fields: dict[str, object] = {"tags": sorted(decision.tags)}
    if decision.title is not None:
        fields["title"] = decision.title
    if decision.correspondent is not None:
        fields["correspondent"] = decision.correspondent
    if decision.document_type is not None:
        fields["document_type"] = decision.document_type
    if decision.created is not None:
        fields["created"] = decision.created.isoformat()
    return fields


def _on_failure(
    doc: Document,
    error: DocumentFailure,
    paperless: PaperlessClient,
    store: FailureStore,
    created: CreatedStore,
    ids: TagIds,
    result: CycleResult,
) -> None:
    """失敗を数え、上限に達したら ai-failed にする。"""
    count = store.increment(doc.id)
    if count < MAX_FAILURES:
        result.retried += 1
        logger.info("書類 %d: retry (%d/%d) %s", doc.id, count, MAX_FAILURES, error)
        return
    logger.info("書類 %d: 失敗が上限に達した %s", doc.id, error)
    _mark_failed(doc, paperless, store, created, ids, result)


def _mark_failed(
    doc: Document,
    paperless: PaperlessClient,
    store: FailureStore,
    created: CreatedStore,
    ids: TagIds,
    result: CycleResult,
) -> None:
    """ai-failed に付け替える。付け替えが 400/404 なら回数を残して次の周期に任せる。"""
    try:
        paperless.patch_document(doc.id, {"tags": sorted(failed_tags(doc, ids))})
    except DocumentFailure as e:
        logger.warning("書類 %d: ai-failed への付け替えに失敗: %s", doc.id, e)
    else:
        store.clear(doc.id)
        created.clear(doc.id)
    result.failed += 1
    logger.info("書類 %d: failed", doc.id)
