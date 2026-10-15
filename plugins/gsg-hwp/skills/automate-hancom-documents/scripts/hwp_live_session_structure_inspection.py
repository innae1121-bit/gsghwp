from __future__ import annotations

import ntpath
from collections.abc import Callable

from hwp_errors import HwpLiveError
from hwp_live_api import LiveHwpApplication
from hwp_live_contract import LiveContext, OpenDocument, ParagraphStyle
from hwp_live_inspection import inspect_native_context
from hwp_live_native_action_models import NativeSnapshot
from hwp_live_native_batch import (
    inspect_native_page,
    inspect_native_structure,
    read_native_paragraph_styles,
    read_native_snapshot,
)
from hwp_live_native_structure import document_structure_from_native
from hwp_live_rot import HwpDocumentCandidate, confirmed_page_count
from hwp_live_structure_contract import DocumentStructure


def connected_document(
    candidate: HwpDocumentCandidate,
    snapshot: NativeSnapshot,
) -> OpenDocument:
    require_snapshot_document(candidate, snapshot, "현재 상태")
    return OpenDocument(
        selector=candidate.selector,
        title=ntpath.basename(candidate.full_name) or "저장되지 않은 문서",
        full_name=candidate.full_name,
        document_id=candidate.document_id,
        format=candidate.document_format,
        edit_mode=candidate.edit_mode,
        modified=snapshot.modified,
        # 네이티브 스냅샷도 같은 PageCount 를 읽는다. 아직 쪽 나누기가 끝나지
        # 않아 0 이면 0 을 사실처럼 싣지 않고 미확정으로 답한다.
        page_count=confirmed_page_count(snapshot.page_count),
        active=True,
        window_handle=candidate.window_handle,
    )


def require_snapshot_document(
    candidate: HwpDocumentCandidate,
    snapshot: NativeSnapshot,
    label: str,
) -> None:
    expected_name = ntpath.normcase(ntpath.normpath(candidate.full_name))
    actual_name = ntpath.normcase(ntpath.normpath(snapshot.full_name))
    if snapshot.document_id != candidate.document_id or actual_name != expected_name:
        raise HwpLiveError(f"한컴 네이티브 {label} 문서가 현재 연결 문서와 다릅니다")


def _enrich_numbering_from_native_scan(
    candidate: HwpDocumentCandidate,
    snapshot: NativeSnapshot,
    paragraph: ParagraphStyle,
    guard: Callable[[], None],
) -> ParagraphStyle:
    """Add numbering fields only from the exact native caret paragraph.

    The older snapshot wire record does not carry these fields. The paragraph
    scan is optional, so an unavailable or mismatched scan leaves the existing
    observation untouched instead of manufacturing a marker value.
    """
    scan = read_native_paragraph_styles(
        candidate.window_handle,
        list_id=snapshot.cursor.list_id,
        start=snapshot.cursor.paragraph,
        limit=1,
        paragraph_shape=True,
    )
    guard()
    if scan is None:
        return paragraph
    expected_name = ntpath.normcase(ntpath.normpath(candidate.full_name))
    actual_name = ntpath.normcase(ntpath.normpath(scan.full_name))
    if (
        scan.document_id != candidate.document_id
        or actual_name != expected_name
        or scan.list_id != snapshot.cursor.list_id
    ):
        return paragraph
    observed = next(
        (
            item
            for item in scan.paragraphs
            if item.paragraph == snapshot.cursor.paragraph
            and (item.style_id is None or item.style_id == snapshot.style_id)
        ),
        None,
    )
    if observed is None:
        return paragraph
    updates: dict[str, object] = {}
    if observed.heading_type is not None:
        updates["heading_type"] = observed.heading_type
        updates["marker_is_automatic"] = observed.heading_type > 0
    if observed.heading_level is not None:
        updates["heading_level"] = observed.heading_level
    return paragraph.model_copy(update=updates) if updates else paragraph


def inspect_candidate_context(
    hwp: LiveHwpApplication,
    candidate: HwpDocumentCandidate,
    guard: Callable[[], None],
) -> LiveContext:
    guard()
    snapshot = read_native_snapshot(candidate.window_handle)
    guard()
    if snapshot is None:
        raise HwpLiveError("한컴 네이티브 현재 상태 조회를 사용할 수 없습니다")
    require_snapshot_document(candidate, snapshot, "현재 상태 조회")
    page = inspect_native_page(
        candidate.window_handle,
        snapshot.current_page,
        include_cells=False,
    )
    guard()
    if page is None:
        raise HwpLiveError("한컴 네이티브 현재 쪽 본문 조회를 사용할 수 없습니다")
    expected_name = ntpath.normcase(ntpath.normpath(candidate.full_name))
    actual_name = ntpath.normcase(ntpath.normpath(page.full_name))
    if (
        page.document_id != candidate.document_id
        or actual_name != expected_name
        or page.page != snapshot.current_page
        or page.page_count != snapshot.page_count
    ):
        raise HwpLiveError(
            "한컴 네이티브 현재 쪽 본문이 현재 상태 조회 결과와 다릅니다"
        )
    page_setup = hwp.get_pagedef_as_dict("eng")
    guard()
    context = inspect_native_context(
        snapshot,
        connected_document(candidate, snapshot),
        page.text,
        page_setup,
    )
    # This is the same live parameter-set read used by the page setup adapter;
    # the older native CHAR record does not include spacing. Missing evidence
    # stays null, never an invented zero.
    try:
        spacing = hwp.get_charshape_as_dict().get("SpacingHangul")
    except AttributeError:
        spacing = None
    guard()
    letter_spacing = (
        spacing
        if isinstance(spacing, int)
        and not isinstance(spacing, bool)
        and -50 <= spacing <= 50
        else None
    )
    return context.model_copy(
        update={
            "character_style": context.character_style.model_copy(
                update={"letter_spacing_percent": letter_spacing}
            ),
            "paragraph_style": _enrich_numbering_from_native_scan(
                candidate,
                snapshot,
                context.paragraph_style,
                guard,
            ),
        }
    )


def inspect_candidate_structure(
    hwp: LiveHwpApplication,
    candidate: HwpDocumentCandidate,
    page: int,
    guard: Callable[[], None],
) -> DocumentStructure:
    _ = hwp
    guard()
    try:
        native_page = page
        if page == 0:
            snapshot = read_native_snapshot(candidate.window_handle)
            if snapshot is None:
                raise HwpLiveError("한컴 네이티브 현재 쪽을 읽을 수 없습니다")
            native_page = -snapshot.current_page
        native = inspect_native_structure(candidate.window_handle, native_page)
        if native is None:
            raise HwpLiveError("한컴 네이티브 상세 구조 조회를 사용할 수 없습니다")
        expected_name = ntpath.normcase(ntpath.normpath(candidate.full_name))
        actual_name = ntpath.normcase(ntpath.normpath(native.full_name))
        if native.document_id != candidate.document_id or actual_name != expected_name:
            raise HwpLiveError(
                "한컴 네이티브 상세 구조 조회 문서가 현재 연결 문서와 다릅니다"
            )
        return document_structure_from_native(
            native,
            selector=candidate.selector,
            window_handle=candidate.window_handle,
        )
    finally:
        guard()


def inspect_candidate_state(
    hwp: LiveHwpApplication,
    candidate: HwpDocumentCandidate,
    page: int,
    guard: Callable[[], None],
) -> tuple[LiveContext, DocumentStructure, int]:
    structure = inspect_candidate_structure(hwp, candidate, page, guard)
    context = inspect_candidate_context(hwp, candidate, guard)
    return context, structure, candidate.window_handle
