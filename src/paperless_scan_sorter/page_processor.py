"""page-todo の書類を 1 周期分処理する（白紙・重複・向きを直して ai-todo に渡す）。

判定は pageplan.plan_pages が行い、ここは PDF の取得・退避・`edit_pdf`・版の増加の待ち・
タグの付け替えだけを受け持つ。整形できなくても分類は進めたいので、書類起因の失敗は
数えずに page-review を付けて ai-todo へ進める（D-014）。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from paperless_scan_sorter.decide import TagIds
from paperless_scan_sorter.errors import DocumentFailure, ServiceUnavailable
from paperless_scan_sorter.originals import OriginalsStore
from paperless_scan_sorter.pageplan import DEFAULT_THRESHOLDS, PageInfo, Thresholds, plan_pages
from paperless_scan_sorter.paperless import Document, PaperlessClient
from paperless_scan_sorter.pdfpages import analyze_pdf, page_count
from paperless_scan_sorter.processor import resolve_tag_ids

logger = logging.getLogger(__name__)

# これを超えるページ数の書類は、判定に時間がかかりすぎるため判定しない。
MAX_PAGES = 50
# edit_pdf の後、新しい版が現れるまで待つ上限と確認の間隔（D-013）。
VERSION_WAIT_SECONDS = 600
VERSION_POLL_SECONDS = 5

TOO_MANY_PAGES_REASON = "ページが多すぎる"
VERSION_TIMEOUT_REASON = "新しい版の取り込みを確認できない"
FAILED_REASON = "ページ整形に失敗"


@dataclass
class PageCycleResult:
    """1 周期の集計。"""

    fixed: int = 0  # edit_pdf で直した
    unchanged: int = 0  # 直すところが無かった
    review: int = 0  # page-review を付けた（fixed / unchanged と重なってよい）
    aborted: bool = False


@dataclass
class _Outcome:
    """1 書類の整形の結果。"""

    fixed: bool = False
    unchanged: bool = False
    review_reasons: tuple[str, ...] = ()


def run_page_cycle(
    paperless: PaperlessClient,
    originals: OriginalsStore,
    *,
    analyze: Callable[..., list[PageInfo]] = analyze_pdf,
    count_pages: Callable[[bytes], int] = page_count,
    thresholds: Thresholds = DEFAULT_THRESHOLDS,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    now: Callable[[], float] = time.time,
) -> PageCycleResult:
    """page-todo の書類を全件取得してから 1 件ずつ直し、ai-todo へ進める。

    必要なタグが無ければ MissingTagsError をそのまま送出する。ServiceUnavailable は
    周期を打ち切り、処理中の書類は page-todo のまま残す。
    """
    result = PageCycleResult()
    originals.prune(now())
    try:
        ids = resolve_tag_ids(paperless.tags())
        docs = paperless.documents_with_tag(ids.page_todo)
    except ServiceUnavailable as e:
        logger.error("ページ整形の周期の準備に失敗したため打ち切る: %s", e)
        result.aborted = True
        return result

    for doc in docs:
        try:
            outcome = _fix(
                doc, paperless, originals, analyze, count_pages, thresholds, sleep, monotonic
            )
            _patch_tags(doc, paperless, ids, review=bool(outcome.review_reasons))
        except ServiceUnavailable as e:
            logger.error("書類 %d のページ整形中に利用不可となり周期を打ち切る: %s", doc.id, e)
            result.aborted = True
            return result
        if outcome.fixed:
            result.fixed += 1
        if outcome.unchanged:
            result.unchanged += 1
        if outcome.review_reasons:
            result.review += 1
            logger.info("書類 %d: pages review (%s)", doc.id, ", ".join(outcome.review_reasons))
        elif outcome.fixed:
            logger.info("書類 %d: pages fixed", doc.id)
        else:
            logger.info("書類 %d: pages unchanged", doc.id)
    return result


def _fix(
    doc: Document,
    paperless: PaperlessClient,
    originals: OriginalsStore,
    analyze: Callable[..., list[PageInfo]],
    count_pages: Callable[[bytes], int],
    thresholds: Thresholds,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
) -> _Outcome:
    """書類のページを判定し、要れば edit_pdf で直す。ServiceUnavailable は送出する。"""
    try:
        pdf = paperless.download_original(doc.id)
        pages = count_pages(pdf)
        if pages > MAX_PAGES:
            logger.info("書類 %d: %d ページあるため判定しない", doc.id, pages)
            return _Outcome(review_reasons=(TOO_MANY_PAGES_REASON,))
        # 画像は原本から、ページごとのテキストは OCR 済みのアーカイブ版から取る
        archive = paperless.download_archive(doc.id)
        plan = plan_pages(analyze(pdf, text_pdf=archive), thresholds)
        for a in plan.actions:
            logger.info("書類 %d p%d: %s %s (%s)", doc.id, a.page, a.action, a.reason, a.detail)
        plan_reasons = ("ページの判定が曖昧",) if plan.review else ()
        if plan.operations is None:
            return _Outcome(unchanged=True, review_reasons=plan_reasons)
        originals.save(doc.id, pdf)
        before = paperless.document_versions(doc.id)
        paperless.edit_pdf(doc.id, plan.operations)
    except ServiceUnavailable:
        raise
    except DocumentFailure as e:
        logger.warning("書類 %d: ページ整形に失敗: %s", doc.id, e)
        return _Outcome(review_reasons=(FAILED_REASON,))
    except Exception as e:
        # メッセージや traceback には本文が混ざりうるので、型名だけを残す。
        logger.error("書類 %d のページ整形中に想定外の例外: %s", doc.id, type(e).__name__)
        return _Outcome(review_reasons=(FAILED_REASON,))

    if not _wait_new_version(doc.id, before, paperless, sleep, monotonic):
        return _Outcome(review_reasons=(VERSION_TIMEOUT_REASON, *plan_reasons))
    return _Outcome(fixed=True, review_reasons=plan_reasons)


def _wait_new_version(
    doc_id: int,
    before: frozenset[int],
    paperless: PaperlessClient,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
) -> bool:
    """before に無い版の ID が現れるまで待つ。上限を過ぎるか版が読めなければ False。

    edit_pdf の応答にはタスク ID が無く、タスク API も読めないので、版の増加で完了を
    判定する（D-013）。
    """
    deadline = monotonic() + VERSION_WAIT_SECONDS
    while True:
        try:
            if paperless.document_versions(doc_id) - before:
                return True
        except DocumentFailure as e:
            logger.warning("書類 %d: 版を確認できない: %s", doc_id, e)
            return False
        if monotonic() > deadline:
            logger.warning("書類 %d: %d 秒待っても新しい版が現れない", doc_id, VERSION_WAIT_SECONDS)
            return False
        sleep(VERSION_POLL_SECONDS)


def _patch_tags(doc: Document, paperless: PaperlessClient, ids: TagIds, *, review: bool) -> None:
    """page-todo を外して ai-todo を足す。既存のタグは残す（置き換えずにマージする）。

    400/404 は警告だけにする。書類は page-todo のまま残り、次の周期で判定し直す。
    直した後なら直すところが無いので、二重には直らない。
    """
    tags = (doc.tags - {ids.page_todo}) | {ids.ai_todo}
    if review:
        tags |= {ids.page_review}
    try:
        paperless.patch_document(doc.id, {"tags": sorted(tags)})
    except DocumentFailure as e:
        logger.warning("書類 %d: タグの付け替えに失敗: %s", doc.id, e)
