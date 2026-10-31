"""ai-todo の書類を 1 周期分処理する。"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date

from paperless_scan_sorter.claude import ClaudeClient
from paperless_scan_sorter.decide import Decision, TagIds, decide, failed_tags
from paperless_scan_sorter.errors import (
    CliFailure,
    DocumentFailure,
    MissingTagsError,
    ServiceUnavailable,
)
from paperless_scan_sorter.failures import CreatedStore, FailureStore
from paperless_scan_sorter.paperless import Document, Named, PaperlessClient
from paperless_scan_sorter.pdfpages import PdfChunk, split_pdf
from paperless_scan_sorter.prompt import TRANSCRIBE_PROMPT, TitleExample, build_prompt, page_note
from paperless_scan_sorter.tags import REQUIRED_TAGS
from paperless_scan_sorter.validate import (
    Classification,
    parse_classification,
    parse_pages,
)

logger = logging.getLogger(__name__)

MAX_FAILURES = 3
# タイトルの例に使う、ai-done の書類の件数（新しい順）。ai-done は Claude が処理した書類に
# しか付かないので、private/ の書類のタイトルは例に入らない（D-022）。
EXAMPLE_DOCS = 200
# 前回の試行で発行元・文書タイプを作った書類を確認待ちにするときの理由。
CREATED_BEFORE_REASON = "前回の試行で発行元・文書タイプを新規作成"
# claude に 1 回で読ませる PDF の上限。書き起こしが出力の上限で切れないようページ数を抑え、
# 大きさはこれを超えないように分ける（D-008, D-020）。
PAGES_PER_CHUNK = 5
MAX_PDF_BYTES = 20 * 1024 * 1024

# 原本の PDF を、claude に 1 回で読ませる大きさに分ける関数。
type Splitter = Callable[[bytes], list[PdfChunk]]


def _split(pdf: bytes) -> list[PdfChunk]:
    return split_pdf(pdf, pages_per_chunk=PAGES_PER_CHUNK, max_bytes=MAX_PDF_BYTES)


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
        page_todo=by_name["page-todo"],
        page_review=by_name["page-review"],
    )


def run_cycle(
    paperless: PaperlessClient,
    claude: ClaudeClient,
    store: FailureStore,
    created: CreatedStore,
    *,
    today: date,
    split: Splitter = _split,
) -> CycleResult:
    """ai-todo の書類を全件取得してから 1 件ずつ処理する。"""
    result = CycleResult()
    try:
        tags = paperless.tags()
        correspondents = paperless.correspondents()
        document_types = paperless.document_types()
        ids = resolve_tag_ids(tags)
        docs = paperless.documents_with_tag(ids.ai_todo)
        titled = paperless.recent_titles(ids.ai_done, limit=EXAMPLE_DOCS)
        # 新規作成の記録が読めないまま進むと、確認待ちに回すべき書類を見落とす（D-009）。
        created.check()
    except ServiceUnavailable as e:
        logger.error("周期の準備に失敗したため打ち切る: %s", e)
        result.aborted = True
        return result

    tag_names = {t.name: t.id for t in tags}
    # 新しい順。この周期で決めたタイトルは先頭に足し、同じ周期の後の書類の例にする。
    examples = [
        _example(t.title, t.correspondent, t.document_type, correspondents, document_types)
        for t in titled
    ]

    # 直前に処理した書類が CLI の失敗で終わったか。障害を示す語を含まない障害でも、
    # 1 周期で数えるのを 1 件までにする（D-010）。
    previous_cli_failure = False
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
        # 同じ周期で作った発行元・文書タイプと決めたタイトルも候補と例に入るよう、書類ごとに組む。
        prompt = build_prompt(
            correspondents=[n.name for n in correspondents],
            document_types=[n.name for n in document_types],
            tags=tag_names,
            examples=examples,
        )
        try:
            decision, content = _classify(
                doc,
                paperless,
                claude,
                split,
                prompt,
                correspondents,
                document_types,
                tag_names,
                ids,
                today,
                created,
            )
            fields = _fields(decision)
            # 書き起こしが空なら、Paperless の OCR の本文を残す。
            if content.strip():
                fields["content"] = content
            paperless.patch_document(doc.id, fields)
        except ServiceUnavailable as e:
            logger.error("書類 %d の処理中に利用不可となり周期を打ち切る: %s", doc.id, e)
            result.aborted = True
            return result
        except Exception as e:
            if isinstance(e, CliFailure) and previous_cli_failure:
                logger.error(
                    "書類 %d でも claude が応答を返さず、連続したため数えずに周期を打ち切る: %s",
                    doc.id,
                    e,
                )
                result.aborted = True
                return result
            previous_cli_failure = isinstance(e, CliFailure)
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
        previous_cli_failure = False
        store.clear(doc.id)
        if decision.title is not None:
            examples.insert(
                0,
                _example(
                    decision.title,
                    decision.correspondent,
                    decision.document_type,
                    correspondents,
                    document_types,
                ),
            )
        if decision.review:
            result.review += 1
            logger.info("書類 %d: review (%s)", doc.id, ", ".join(decision.reasons))
        else:
            result.done += 1
            logger.info("書類 %d: done", doc.id)
        try:
            created.clear(doc.id)
        except ServiceUnavailable as e:
            logger.error("書類 %d の記録の後始末で利用不可となり周期を打ち切る: %s", doc.id, e)
            result.aborted = True
            return result
    return result


def _example(
    title: str,
    correspondent: int | None,
    document_type: int | None,
    correspondents: Sequence[Named],
    document_types: Sequence[Named],
) -> TitleExample:
    """ID で持つ発行元・文書タイプを名前に引いて、タイトルの例にする。"""

    def name(items: Sequence[Named], item_id: int | None) -> str | None:
        return next((n.name for n in items if n.id == item_id), None)

    return TitleExample(
        name(correspondents, correspondent), name(document_types, document_type), title
    )


def _read(
    doc: Document,
    paperless: PaperlessClient,
    claude: ClaudeClient,
    split: Splitter,
    prompt: str,
    known_tags: dict[str, int],
    today: date,
) -> tuple[Classification, str]:
    """原本の PDF を分けて claude に読ませ、分類と本文の書き起こしを返す。

    分類は最初の PDF で決め、2 つ目以降は書き起こしだけを求める。書き起こしは
    すべての PDF を読み終えてからページ順につなぐので、途中で失敗すれば何も返らない。
    """
    chunks = split(paperless.download_original(doc.id))
    total = chunks[-1].last
    if len(chunks) > 1:
        logger.info("書類 %d: %d ページを %d 回に分けて読む", doc.id, total, len(chunks))

    def ask(base: str, chunk: PdfChunk) -> str:
        note = page_note(chunk.first, chunk.last, total)
        return claude.read_pdf(f"{base}\n{note}" if note else base, chunk.data)

    first, *rest = chunks
    text = ask(prompt, first)
    classification = parse_classification(text, known_tags=known_tags.keys(), today=today)
    pages = list(parse_pages(text, expected=first.pages))
    for chunk in rest:
        pages.extend(parse_pages(ask(TRANSCRIBE_PROMPT, chunk), expected=chunk.pages))
    return classification, "\n\n".join(p.text for p in pages)


def _classify(
    doc: Document,
    paperless: PaperlessClient,
    claude: ClaudeClient,
    split: Splitter,
    prompt: str,
    correspondents: list[Named],
    document_types: list[Named],
    tag_names: dict[str, int],
    ids: TagIds,
    today: date,
    created: CreatedStore,
) -> tuple[Decision, str]:
    """書類を判定し、新規の発行元・文書タイプを作って ID に置き換えた Decision と、
    本文の書き起こしを返す。"""
    classification, content = _read(doc, paperless, claude, split, prompt, tag_names, today)
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
    document_type = decision.document_type
    if isinstance(correspondent, str) or isinstance(document_type, str):
        # 作成を試みる前に記録する。作成が Paperless で確定した後に応答が失われても、
        # 次の試行で確認待ちに回せるようにするため（D-009）。
        created.mark(doc.id)
    if isinstance(correspondent, str):
        new = paperless.create_correspondent(correspondent)
        correspondents.append(new)
        logger.info("発行元を新規作成: %s", new.name)
        correspondent = new.id
    if isinstance(document_type, str):
        new = paperless.create_document_type(document_type)
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
    ), content


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
