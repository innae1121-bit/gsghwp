from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import replace

from hwp_document_style_observation import resolve_document_style_usage
from hwp_document_style_profile import (
    automatic_numbering_notice,
    resolve_layout_style_profile,
)
from hwp_errors import HwpLiveError
from hwp_layout_preflight import (
    LAYOUT_MESSAGE_FIELD_LIMIT,
    preflight_layout,
    reference_text_fit_message_fact,
)
from hwp_live_api import ShapeValue
from hwp_live_caption import (
    caption_format_sources,
    find_hierarchical_table_caption_source,
)
from hwp_live_contract import DocumentStyleList, LayoutPlan, PageBreakBlock
from hwp_live_document_edit_commands import build_delete_page_commands
from hwp_live_inspection import inspect_styles
from hwp_live_layout import observed_table_structure, prepare_layout_assets
from hwp_live_native_action_contract import (
    NativeActionFailure,
    NativeActionFailureEvidence,
)
from hwp_live_native_action_models import MergeCommand, NativePosition
from hwp_live_native_action_results import NativeActionResult, NativeSnapshot
from hwp_live_native_batch import (
    execute_native_actions,
    inspect_native_page,
    read_native_snapshot,
)
from hwp_live_native_layout import (
    NativeLayoutContext,
    NativeLayoutExecutionPlan,
    build_native_layout_execution_plan,
    build_native_layout_request,
)
from hwp_live_progress import declare_native_work, report_native_progress
from hwp_live_operation_recipe_contract import (
    is_document_end_layout_intent as is_document_end_layout_intent,
    layout_operation_result,
)
from hwp_live_rot import HwpDocumentCandidate, attach_wrapper, release_wrapper
from hwp_live_safety import (
    mark_document_unsafe,
    require_writable_document,
    run_layout_mutation,
)
from hwp_operation_contract import (
    OperationResult,
    OperationStatus,
    page_growth_evidence,
)
from hwp_reference_layout_geometry import (
    MILLIMETERS_PER_INCH,
    HWPUNITS_PER_INCH,
    SectionPageGeometry,
)
from hwp_reference_layout_contract import ReferenceLayoutBlock
from hwp_reference_layout_patch import ReferenceLayoutPatchBlock
from hwp_live_table_contract import TableBlock


_STYLE_LIST_UNAVAILABLE_NOTICE = (
    "문서 스타일 목록을 읽지 못해 현재 서식과 기본 스타일 id로 적용했습니다"
)


class _LayoutBatchFailure(HwpLiveError):
    error: HwpLiveError
    batch_index: int
    completed_batches: int
    commands_completed: int
    elapsed_microseconds: int
    completed_group_keys: frozenset[int]

    def __init__(
        self,
        error: HwpLiveError,
        *,
        batch_index: int,
        completed_batches: int,
        commands_completed: int,
        elapsed_microseconds: int,
        completed_group_keys: set[int],
    ) -> None:
        super().__init__(str(error))
        self.error = error
        self.batch_index = batch_index
        self.completed_batches = completed_batches
        self.commands_completed = commands_completed
        self.elapsed_microseconds = elapsed_microseconds
        self.completed_group_keys = frozenset(completed_group_keys)


def _merge_native_results(
    first: NativeActionResult,
    second: NativeActionResult,
) -> NativeActionResult:
    return replace(
        first,
        commands_executed=first.commands_executed + second.commands_executed,
        actions_executed=first.actions_executed + second.actions_executed,
        text_insertions=first.text_insertions + second.text_insertions,
        image_insertions=first.image_insertions + second.image_insertions,
        elapsed_microseconds=(first.elapsed_microseconds + second.elapsed_microseconds),
        created_control_ids=(first.created_control_ids + second.created_control_ids),
        call_results=first.call_results + second.call_results,
        image_timing_count=(first.image_timing_count + second.image_timing_count),
        image_max_microseconds=max(
            first.image_max_microseconds,
            second.image_max_microseconds,
        ),
        image_total_microseconds=(
            first.image_total_microseconds + second.image_total_microseconds
        ),
    )


def _post_write_layout_facts(
    *,
    observation_available: bool,
    before: NativeSnapshot,
    after: NativeSnapshot,
    command_start_page: int,
    planned_page_span: int,
    adjustable_items: tuple[str, ...],
) -> str:
    if not observation_available:
        return "post_write_layout_facts=unavailable"
    cursor_page_spread = after.current_page - command_start_page + 1
    overflow = (
        "possible"
        if cursor_page_spread > planned_page_span
        else "not_observed_by_cursor_spread"
    )
    facts: dict[str, object] = {
        "availability": "available",
        "observation_scope": (
            "native_snapshot_cursor_and_page_count_only_"
            "not_geometry_or_inserted_range_readback"
        ),
        "page_count_before": before.page_count,
        "page_count_after": after.page_count,
        "page_count_delta": after.page_count - before.page_count,
        "command_start_page": command_start_page,
        "after_cursor_page": after.current_page,
        "cursor_page_spread": cursor_page_spread,
        "planned_page_span": planned_page_span,
        "overflow": overflow,
    }
    if overflow == "possible":
        facts["adjustable_items_source"] = "preflight"
        facts["adjustable_items"] = list(adjustable_items)
    return "post_write_layout_facts=" + json.dumps(
        facts, ensure_ascii=False, separators=(",", ":")
    )


def _merge_projection_facts(
    *,
    merge_commands: int,
    cells_before_merge: int,
    cells_after_merge: int,
) -> str:
    """병합이 든 레이아웃에서 updated_addresses 가 무엇인지 밝히는 사실.

    되읽기가 아니라 실행한 MergeCommand 를 되짚은 투영이다. 그 출처를 적지
    않으면 관측한 값과 구별되지 않으므로 basis 를 함께 싣는다.
    """
    return "merge_projection=" + json.dumps(
        {
            "basis": "executed_merge_commands_not_document_readback",
            "updated_addresses_space": "after_merge",
            "merge_commands": merge_commands,
            "cells_before_merge": cells_before_merge,
            "cells_after_merge": cells_after_merge,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _layout_batch_failure_result(
    query: str,
    plan: LayoutPlan,
    execution: NativeLayoutExecutionPlan,
    before: NativeSnapshot,
    native_protocol: int,
    lookup_microseconds: int,
    failure: _LayoutBatchFailure,
) -> OperationResult:
    error = failure.error
    partial_mutation: bool | None = (
        True
        if failure.completed_batches > 0
        else error.partial_mutation
        if isinstance(error, NativeActionFailure)
        else None
    )
    retry_safe = (
        isinstance(error, NativeActionFailure)
        and error.retry_safe
        and partial_mutation is False
    )
    completed_addresses = execution.completed_addresses(
        set(failure.completed_group_keys)
    )
    failed_step = (
        error.failed_step
        if isinstance(error, NativeActionFailure)
        else f"layout_batch:{failure.batch_index + 1}"
    )
    base = layout_operation_result(
        query,
        "partial_change" if partial_mutation is True else "operation_failed",
        (
            f"레이아웃 호출 {failure.batch_index + 1}/"
            f"{len(execution.batches)}에서 실패했습니다. "
            f"완료 batch {failure.completed_batches}개, "
            f"완료 셀 {len(completed_addresses)}개: {error}"
        ),
        lookup_microseconds,
        plan,
    )
    return base.model_copy(
        update={
            "changed": partial_mutation is True,
            "execution_mode": "native_in_process",
            "native_protocol": native_protocol,
            "verification": "native_action_result",
            "verified": False,
            "commands_executed": failure.commands_completed,
            "commands_completed": failure.commands_completed,
            "native_elapsed_microseconds": failure.elapsed_microseconds,
            "current_page": before.current_page,
            "page_count": before.page_count,
            "modified": (
                True
                if partial_mutation is True
                else before.modified
                if partial_mutation is False
                else None
            ),
            "partial_change": partial_mutation is True,
            "partial_mutation": partial_mutation,
            "retry_safe": retry_safe,
            "reconcile_required": partial_mutation is not False or not retry_safe,
            "failed_step": failed_step,
            "structure_digest_before": (
                error.structure_digest_before
                if isinstance(error, NativeActionFailure)
                else None
            ),
            "structure_digest_after": (
                error.structure_digest_after
                if isinstance(error, NativeActionFailure)
                else None
            ),
            "updated_addresses": completed_addresses,
        }
    )


def _page_value(values: Mapping[str, ShapeValue], key: str) -> float:
    value = values.get(key)
    if isinstance(value, bool):
        return float(int(value))
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0


def reference_append_page_count(
    plan: LayoutPlan,
    current_page_count: int,
) -> int:
    reference_blocks = tuple(
        block for block in plan.blocks if isinstance(block, ReferenceLayoutBlock)
    )
    if (
        plan.target != "after_page"
        or plan.page != current_page_count
        or not reference_blocks
        or len(plan.blocks) != len(reference_blocks) * 2 - 1
        or not all(
            isinstance(block, ReferenceLayoutBlock | PageBreakBlock)
            for block in plan.blocks
        )
        or sum(isinstance(block, PageBreakBlock) for block in plan.blocks)
        != len(reference_blocks) - 1
    ):
        return 0
    return len(reference_blocks)


def native_style_plan(
    candidate: HwpDocumentCandidate,
    plan: LayoutPlan,
    style_id: int,
    guard: Callable[[], None],
    *,
    setup_page: int,
    style_list: DocumentStyleList | None = None,
) -> tuple[
    LayoutPlan,
    SectionPageGeometry,
    tuple[tuple[str, NativePosition], ...],
    str,
]:
    # The same paragraph walk that reports document style usage also supplies
    # repeated, same-style caption prefixes. Read it before caption selection so
    # an unwrapped source can be recognized from observations rather than words.
    usage = resolve_document_style_usage(
        candidate.window_handle,
        candidate.document_id,
        candidate.full_name,
    )
    guard()
    wrapper = attach_wrapper(candidate)
    style_notice = ""
    try:
        try:
            styles = (
                inspect_styles(wrapper, guard) if style_list is None else style_list
            ).styles
        except HwpLiveError:
            styles = ()
            style_notice = _STYLE_LIST_UNAVAILABLE_NOTICE
        if style_list is not None and not style_list.styles and not style_notice:
            style_notice = _STYLE_LIST_UNAVAILABLE_NOTICE
        guard()
        cursor = wrapper.get_pos()
        guard()
        try:
            if wrapper.current_page != setup_page:
                _ = wrapper.goto_page(setup_page)
                guard()
            page = wrapper.get_pagedef_as_dict("eng")
            guard()
        finally:
            restored = wrapper.set_pos(*cursor)
            guard()
            if not restored:
                raise HwpLiveError(
                    "구역 용지 정보를 읽은 뒤 한컴 커서를 복원하지 못했습니다"
                )
        caption_source = (
            find_hierarchical_table_caption_source(
                candidate,
                wrapper,
                guard,
                setup_page=setup_page,
                usage=usage,
            )
            if any(
                isinstance(block, TableBlock) and block.caption is not None
                for block in plan.blocks
            )
            else None
        )
        # The setup page is the existing page whose section governs this write,
        # and it is the page whose tables may be observed. ``layout_page`` is
        # not usable here: for target="after_page" it is setup_page + 1, a page
        # this call is about to create, so it has no tables to learn from.
        #
        # The caption search above also reads a native structure, but not one
        # that can be reused: it walks *backwards* from the setup page until it
        # finds a hierarchical caption, so the structure it ends on usually
        # belongs to an earlier page, and it returns a caption source rather
        # than the snapshot. A one-page-wide cell sample from the wrong page is
        # exactly the contamination this must not introduce, so the structure
        # is read for the page that is actually being written.
        structure = observed_table_structure(
            wrapper,
            candidate,
            plan,
            guard,
            page=setup_page,
        )
    finally:
        release_wrapper(wrapper)
    geometry = SectionPageGeometry.from_mm(
        paper_width_mm=_page_value(page, "PaperWidth"),
        paper_height_mm=_page_value(page, "PaperHeight"),
        landscape=bool(_page_value(page, "Landscape")),
        left_margin_mm=_page_value(page, "LeftMargin"),
        right_margin_mm=_page_value(page, "RightMargin"),
        top_margin_mm=_page_value(page, "TopMargin"),
        bottom_margin_mm=_page_value(page, "BottomMargin"),
        header_mm=_page_value(page, "HeaderLen"),
        footer_mm=_page_value(page, "FooterLen"),
        gutter_mm=_page_value(page, "GutterLen"),
        gutter_type=int(_page_value(page, "GutterType")),
    )
    usable_area = geometry.usable_area(page_number=setup_page)
    content_width_mm = usable_area.width * MILLIMETERS_PER_INCH / HWPUNITS_PER_INCH
    content_height_mm = usable_area.height * MILLIMETERS_PER_INCH / HWPUNITS_PER_INCH
    resolved = resolve_layout_style_profile(
        plan.expand_image_frames(content_width_mm, content_height_mm),
        styles,
        fallback_style_id=style_id,
        content_width_mm=content_width_mm,
        usage=usage,
        structure=structure,
    )
    # Additive fact on the existing notice channel: a paragraph that resolved
    # to an automatically numbered style is relying on a marker no command in
    # this path switches on, and the model has no way to know that before it
    # looks at a render. Saying it here is what stops the next step from
    # patching a literal "1)" on top of a number that may already be there.
    numbering_notice = automatic_numbering_notice(resolved, usage)
    if numbering_notice:
        style_notice = (
            f"{style_notice} {numbering_notice}" if style_notice else numbering_notice
        )
    return (
        resolved,
        geometry,
        caption_format_sources(resolved, caption_source),
        style_notice,
    )


def operate_layout(
    candidate: HwpDocumentCandidate,
    query: str,
    plan: LayoutPlan | None,
    *,
    resolve_only: bool,
    allow_document_change: bool,
    expected_cursor: tuple[int, int, int] | None,
    unsafe_selectors: set[str],
    guard: Callable[[], None],
    atomic: bool = False,
    style_list: DocumentStyleList | None = None,
    insert_after_control: tuple[NativePosition, str, int] | None = None,
) -> OperationResult:
    started = time.perf_counter_ns()
    lookup_microseconds = (time.perf_counter_ns() - started) // 1_000
    if resolve_only:
        return layout_operation_result(
            query,
            "resolved",
            "요청한 삽입 위치와 네이티브 레이아웃 적용 레시피를 확정했습니다",
            lookup_microseconds,
            plan,
        )
    if not allow_document_change:
        return layout_operation_result(
            query,
            "confirmation_required",
            "문서 변경 레시피입니다. 사용자 요청 범위와 일치할 때 allow_document_change=true로 다시 호출하세요",
            lookup_microseconds,
            plan,
        )
    if plan is None:
        return layout_operation_result(
            query,
            "needs_input",
            "추가할 표·문단·그림을 layout에 전달하세요",
            lookup_microseconds,
            plan,
        )

    require_writable_document(unsafe_selectors, candidate.selector)
    guard()
    before = read_native_snapshot(candidate.window_handle)
    if before is None:
        raise HwpLiveError("네이티브 레시피 실행 전 한컴 문서 상태를 읽지 못했습니다")
    if (
        plan.target == "current"
        and insert_after_control is None
        and before.selection.selected
        and not plan.replace_selection
    ):
        return layout_operation_result(
            query,
            "needs_input",
            "현재 선택 영역이 있습니다. 삽입하려면 선택을 해제하고, 선택 내용을 교체하려면 replace_selection=true를 지정하세요",
            lookup_microseconds,
            plan,
        )

    setup_page = (
        plan.page
        if plan.target == "after_page" and plan.page is not None
        else before.page_count
        if plan.target == "document_end"
        else insert_after_control[2]
        if insert_after_control is not None
        else before.current_page
    )
    layout_page = (
        plan.page + 1
        if plan.target == "after_page" and plan.page is not None
        else setup_page
    )
    resolved_plan, page_geometry, table_caption_sources, style_notice = (
        native_style_plan(
            candidate,
            plan,
            before.style_id,
            guard,
            setup_page=setup_page,
            style_list=style_list,
        )
    )
    preflight = preflight_layout(
        resolved_plan,
        page_geometry,
        page_number=layout_page,
    )
    if preflight.overflow == "definite":
        base = layout_operation_result(
            query,
            "needs_input",
            (
                "레이아웃 사전 검사에서 명확한 쪽 넘침을 탐지해 문서를 변경하지 "
                f"않았습니다: usable={preflight.usable_width_mm}x"
                f"{preflight.usable_height_mm}mm, estimated="
                f"{preflight.estimated_width_mm}x"
                f"{preflight.estimated_height_mm}mm"
            ),
            lookup_microseconds,
            plan,
        )
        return base.model_copy(
            update={
                "verified": False,
                "retry_safe": True,
                "partial_mutation": False,
            }
        )
    assets = prepare_layout_assets(resolved_plan)
    request = replace(
        build_native_layout_request(
            NativeLayoutContext(
                document_id=candidate.document_id,
                full_name=candidate.full_name,
                style_ids=(),
                page_count=before.page_count,
                page_geometry=page_geometry,
                page_number=layout_page,
                base_style_id=before.style_id,
                caption_format_sources=table_caption_sources,
                expected_cursor=(
                    None
                    if insert_after_control is not None
                    else before.cursor
                    if expected_cursor is None
                    else NativePosition(*expected_cursor)
                ),
                expected_selection=(
                    before.selection if plan.replace_selection else None
                ),
                insert_after_control=(
                    None
                    if insert_after_control is None
                    else (insert_after_control[0], insert_after_control[1])
                ),
            ),
            resolved_plan,
            assets,
        ),
        atomic=atomic,
    )
    execution = build_native_layout_execution_plan(request)
    native_protocol = (
        10
        if any(
            isinstance(block, ReferenceLayoutBlock | ReferenceLayoutPatchBlock)
            for block in resolved_plan.blocks
        )
        else 9
    )
    reference_append_pages = reference_append_page_count(
        resolved_plan,
        before.page_count,
    )
    trailing_page = (
        before.page_count + reference_append_pages + 1
        if reference_append_pages
        else None
    )

    def mutate_and_snapshot() -> tuple[NativeActionResult, NativeSnapshot, int]:
        expected_commands = sum(
            len(batch.request.commands) for batch in execution.batches
        )
        native_result: NativeActionResult | None = None
        commands_completed = 0
        elapsed_microseconds = 0
        completed_group_keys: set[int] = set()
        for batch_index, batch in enumerate(execution.batches):
            # 이 배치는 네이티브 호출 한 번이라 안에서는 진행을 알릴 수 없다.
            # 들어가기 전에 크기를 선언해 두어야, 기다리는 쪽이 "아직 일하는
            # 중"과 "멈췄다"를 구분할 근거를 갖는다. 배치가 하나뿐인 요청
            # (원자 요청 포함)도 이 선언 하나로 같은 보호를 받는다.
            declare_native_work(
                topology_units=batch.topology_work,
                commands=len(batch.request.commands),
                label="layout_batch:" + f"{batch_index + 1}/{len(execution.batches)}",
            )
            try:
                batch_result = execute_native_actions(
                    candidate.window_handle,
                    batch.request,
                    minimum_version=native_protocol,
                )
                if batch_result is None:
                    raise HwpLiveError(
                        "한컴 네이티브 레시피 실행기를 사용할 수 없습니다"
                    )
                if batch_result.commands_executed != len(batch.request.commands):
                    partial_mutation = batch_result.commands_executed > 0
                    raise NativeActionFailure(
                        NativeActionFailureEvidence(
                            code="LAYOUT_BATCH_INCOMPLETE",
                            location=(f"{batch_index + 1}/{len(execution.batches)}"),
                            message=(
                                "네이티브 레이아웃 batch 완료 명령 수가 요청과 다릅니다"
                            ),
                            commands_completed=batch_result.commands_executed,
                            failed_step=f"layout_batch:{batch_index + 1}",
                            partial_mutation=partial_mutation,
                            retry_safe=not partial_mutation,
                            structure_digest_before=None,
                            structure_digest_after=None,
                        )
                    )
            except HwpLiveError as error:
                if len(execution.batches) == 1:
                    raise
                local_completed = (
                    min(
                        max(error.commands_completed, 0),
                        len(batch.request.commands),
                    )
                    if isinstance(error, NativeActionFailure)
                    else 0
                )
                completed_group_keys.update(batch.completed_group_keys(local_completed))
                raise _LayoutBatchFailure(
                    error,
                    batch_index=batch_index,
                    completed_batches=batch_index,
                    commands_completed=commands_completed + local_completed,
                    elapsed_microseconds=elapsed_microseconds,
                    completed_group_keys=completed_group_keys,
                ) from error
            native_result = (
                batch_result
                if native_result is None
                else _merge_native_results(native_result, batch_result)
            )
            commands_completed += batch_result.commands_executed
            elapsed_microseconds += batch_result.elapsed_microseconds
            completed_group_keys.update(item.group.key for item in batch.groups)
            # 배치 하나가 끝났다는 것은 한/글이 살아서 일하고 있다는 증거다.
            # 브리지 마감이 이 증거를 기준으로 다시 세어지므로, 배치 여러 개로
            # 나뉘는 큰 삽입이 "진행 중인데 죽었다" 로 끊기지 않는다.
            report_native_progress(
                "layout_batch:"
                + f"{batch_index + 1}/{len(execution.batches)}"
                + f":commands={commands_completed}/{expected_commands}",
                commands_completed,
            )
        if native_result is None:
            raise HwpLiveError("네이티브 레이아웃 batch 결과가 없습니다")
        after_snapshot = read_native_snapshot(candidate.window_handle)
        if after_snapshot is None:
            raise HwpLiveError(
                "네이티브 레시피 실행 후 한컴 문서 상태를 읽지 못했습니다"
            )
        if trailing_page is not None and after_snapshot.page_count == trailing_page:
            page = inspect_native_page(
                candidate.window_handle,
                trailing_page,
                include_cells=False,
            )
            if page is None:
                raise HwpLiveError(
                    "참조 레이아웃 뒤쪽의 빈 쪽 여부를 확인하지 못했습니다"
                )
            if not page.text.strip() and not page.controls:
                cleanup_request = replace(
                    request,
                    commands=build_delete_page_commands(trailing_page),
                    expected_cursor=None,
                    expected_selection=None,
                    atomic=False,
                )
                cleanup_result = execute_native_actions(
                    candidate.window_handle,
                    cleanup_request,
                    minimum_version=native_protocol,
                )
                if cleanup_result is None:
                    raise HwpLiveError("확인된 빈 꼬리 쪽을 정리하지 못했습니다")
                native_result = _merge_native_results(
                    native_result,
                    cleanup_result,
                )
                expected_commands += len(cleanup_request.commands)
                after_snapshot = read_native_snapshot(candidate.window_handle)
                if after_snapshot is None:
                    raise HwpLiveError("빈 꼬리 쪽 정리 후 문서 상태를 읽지 못했습니다")
        return native_result, after_snapshot, expected_commands

    try:
        native, after, expected_commands = run_layout_mutation(
            unsafe_selectors,
            candidate.selector,
            mutate_and_snapshot,
        )
    except _LayoutBatchFailure as failure:
        return _layout_batch_failure_result(
            query,
            plan,
            execution,
            before,
            native_protocol,
            lookup_microseconds,
            failure,
        )

    snapshot_verified = (
        native.commands_executed == expected_commands
        and after.document_id == before.document_id
        and after.modified
    )
    if plan.target == "after_page":
        snapshot_verified = (
            snapshot_verified
            and plan.page is not None
            and after.page_count >= before.page_count + 1
            and after.current_page >= plan.page + 1
        )
    if reference_append_pages:
        snapshot_verified = (
            snapshot_verified
            and after.page_count == before.page_count + reference_append_pages
        )
    if not snapshot_verified:
        mark_document_unsafe(unsafe_selectors, candidate.selector)
    table_caption_visibility_unverified = snapshot_verified and any(
        isinstance(block, TableBlock) and block.caption is not None
        for block in resolved_plan.blocks
    )
    verified: bool | None = (
        None if table_caption_visibility_unverified else snapshot_verified
    )
    status: OperationStatus = "executed" if snapshot_verified else "partial_change"
    layout_call_count = len(execution.batches)
    message = (
        (
            "요청한 위치 이동과 ApplyLayout Bulk를 프로토콜 "
            f"{native_protocol} C++/ATL 네이티브 경로로 "
            f"{layout_call_count}회 실행했고 네이티브 전후 상태는 일치했습니다. "
            "표 캡션 명령은 실행됐지만 구조 및 렌더 가시성은 확인하지 못했습니다"
        )
        if table_caption_visibility_unverified
        else (
            "요청한 위치 이동과 ApplyLayout Bulk를 프로토콜 "
            f"{native_protocol} C++/ATL 네이티브 경로로 "
            f"{layout_call_count}회 실행하고 전후 상태를 검증했습니다"
        )
        if snapshot_verified
        else "네이티브 레이아웃 배치는 실행됐지만 삽입 위치 또는 문서 상태의 사후 검증이 일치하지 않았습니다"
    )
    if snapshot_verified:
        # verified=true 가 무엇을 통과했다는 말인지 요약에서 바로 읽히게 한다.
        # post_write_layout_facts 의 observation_scope 는 같은 사실을 적고
        # 있었지만, 요약만 읽으면 기하·삽입 범위까지 검증한 인상을 준다.
        message = (
            f"{message}. 검증 범위는 네이티브 전후 스냅샷"
            "(문서 동일성·수정 표시·쪽 수·커서 쪽)이고, 표 기하와 삽입 범위 "
            "되읽기는 이 범위 밖입니다"
        )
    planned_page_span = 1 + sum(
        isinstance(block, PageBreakBlock) for block in resolved_plan.blocks
    )
    observation_available = (
        snapshot_verified
        and after.current_page >= layout_page
        and not (
            plan.target == "current"
            and insert_after_control is None
            and plan.replace_selection
            and before.selection.selected
        )
    )
    message = f"{message} " + _post_write_layout_facts(
        observation_available=observation_available,
        before=before,
        after=after,
        command_start_page=layout_page,
        planned_page_span=planned_page_span,
        adjustable_items=getattr(preflight, "adjustable_items", ()),
    )
    completed_group_keys = {
        item.group.key for batch in execution.batches for item in batch.groups
    }
    completed_addresses = execution.completed_addresses(completed_group_keys)
    # 셀을 먼저 채우고 마지막에 합치는 레이아웃에서 completed_addresses 는 병합
    # 전 격자다. 병합이 있으면 남는 주소로 바꿔 싣고, 그 값이 되읽기가 아니라
    # 명령 투영이라는 사실을 요약에 함께 적는다.
    merged_addresses = execution.merged_addresses()
    updated_addresses = (
        completed_addresses if merged_addresses is None else merged_addresses
    )
    if merged_addresses is not None:
        message = f"{message} " + _merge_projection_facts(
            merge_commands=sum(
                isinstance(command, MergeCommand)
                for batch in execution.batches
                for command in batch.request.commands
            ),
            cells_before_merge=len(completed_addresses),
            cells_after_merge=len(merged_addresses),
        )
    if style_notice:
        separator = " " if message.endswith((".", "!", "?")) else ". "
        message = f"{message}{separator}{style_notice}"
    # 참조 레이아웃의 텍스트 맞춤이 선언된 글자 크기·장평·자간·줄간격·여백을
    # 실제로 줄였다는 사실을, 위의 사전 검사가 이미 잰 그대로 옮겨 싣는다.
    # 그러지 않으면 이 응답 어디에도 "줄였다"가 없어, 모델은 렌더를 눈으로 볼
    # 때까지 9pt 로 시킨 글이 6.75pt·장평 50% 로 들어갔다는 것을 알 수 없다.
    # 줄인 게 없으면 아무것도 붙지 않는다.
    #
    # 메시지가 다 자란 뒤 마지막에 붙이는 이유는 message 의 4,000자 계약 상한
    # 이다 -- 남은 자리를 정확히 알아야 사실을 잘라 넣을지 말지를 정직하게
    # 정할 수 있다.
    fit_fact = reference_text_fit_message_fact(
        getattr(preflight, "estimate_basis", ()),
        budget=LAYOUT_MESSAGE_FIELD_LIMIT - len(message) - 1,
    )
    if fit_fact:
        message = f"{message} {fit_fact}"
    changed_pages = set(
        page_growth_evidence(
            before_page_count=before.page_count,
            after_page_count=after.page_count,
            current_page=after.current_page,
        )
    )
    if observation_available:
        changed_pages.update(range(layout_page, after.current_page + 1))
    base = layout_operation_result(query, status, message, lookup_microseconds, plan)
    return base.model_copy(
        update={
            "changed": True,
            "execution_mode": "native_in_process",
            "native_protocol": native_protocol,
            "verification": "native_snapshot_before_after",
            "verified": verified,
            "failure_stage": (
                "table_caption_visibility_verification"
                if table_caption_visibility_unverified
                else None
            ),
            "commands_executed": native.commands_executed,
            "native_actions_executed": native.actions_executed,
            "native_elapsed_microseconds": native.elapsed_microseconds,
            "blocks_applied": len(resolved_plan.blocks),
            "created_control_ids": native.created_control_ids,
            "current_page": after.current_page,
            "page_count": after.page_count,
            "changed_pages": tuple(sorted(changed_pages)),
            "modified": after.modified,
            "retry_safe": None if verified is None else verified,
            "partial_mutation": not snapshot_verified,
            "commands_completed": native.commands_executed,
            "updated_addresses": updated_addresses,
        }
    )
