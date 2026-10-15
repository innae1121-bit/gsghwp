from __future__ import annotations

from hwp_errors import HwpLiveError
from hwp_live_native_action_models import (
    CaptureTableCommand,
    CellCommand,
    IntegerValue,
    MovePositionCommand,
    NativeActionCommand,
    NativeActionRequest,
    NativePosition,
    NativeSetter,
    NativeSnapshot,
    ParameterActionCommand,
    RunCommand,
    SelectControlCommand,
)
from hwp_live_native_batch import execute_native_actions, read_native_snapshot
from hwp_live_native_format_commands import TableFormatCommandPlan
from hwp_live_native_format_contract import (
    NativeFormatRecipeRequest,
    PreparedFormatOperation,
)
from hwp_operation_contract import OperationResult


def cell_selection(table_id: str, cell: str) -> tuple[NativeActionCommand, ...]:
    return (
        SelectControlCommand(table_id),
        CaptureTableCommand(),
        CellCommand(cell),
        RunCommand("SelectAll"),
    )


def shape_copy_paste() -> ParameterActionCommand:
    # Hancom copies at a caret and pastes over a text selection. Type 2 includes
    # both character and paragraph shapes, including properties not exposed by
    # the public font/spacing setters.
    return ParameterActionCommand(
        "ShapeCopyPaste", "HShapeCopyPaste", (NativeSetter("Type", IntegerValue(2)),)
    )


def execute_table_format_copy(
    request: NativeFormatRecipeRequest,
    before: NativeSnapshot,
    prepared: PreparedFormatOperation,
    result: OperationResult,
) -> OperationResult:
    plan = prepared.plan
    assert isinstance(plan, TableFormatCommandPlan)
    source_id = plan.formatting.format_source_table_id
    source_cell = plan.formatting.format_source_cell
    assert source_id is not None and source_cell is not None
    commands = 0
    elapsed = 0
    changed = False
    final_snapshot = before
    updated: list[str] = []

    def run(items: tuple[NativeActionCommand, ...]) -> NativeSnapshot:
        nonlocal commands, elapsed
        native = execute_native_actions(
            request.candidate.window_handle,
            NativeActionRequest(
                request.routing_page.document_id, request.routing_page.full_name, items
            ),
            minimum_version=9,
        )
        if native is None:
            raise HwpLiveError("셀 서식 복사용 네이티브 실행기를 사용할 수 없습니다")
        commands += native.commands_executed
        elapsed += native.elapsed_microseconds
        snapshot = read_native_snapshot(request.candidate.window_handle)
        if snapshot is None:
            raise HwpLiveError("셀 서식 복사 상태를 읽지 못했습니다")
        return snapshot

    try:
        if not plan.cells:
            raise HwpLiveError("서식을 복사할 대상 셀 주소가 필요합니다")
        source = run(cell_selection(source_id, source_cell))
        if not source.selection.selected or not source.selected_text:
            raise HwpLiveError("서식 원본 셀의 텍스트를 확인하지 못했습니다")
        source = run((MovePositionCommand(source.selection.start), shape_copy_paste()))
        originals = [
            (cell, run(cell_selection(plan.table.instance_id, cell)))
            for cell in plan.cells
        ]
        if any(
            not item.selection.selected or not item.selected_text
            for _, item in originals
        ):
            raise HwpLiveError("빈 대상 셀에는 원본 서식 복사를 적용할 수 없습니다")
        for cell, original in originals:
            if (plan.table.instance_id, cell) == (source_id, source_cell):
                continue
            # Re-copy explicitly: inspection and earlier paste actions must not
            # determine the clipboard state for the next target.
            _ = run((MovePositionCommand(source.cursor), shape_copy_paste()))
            changed = True
            after = run(
                (*cell_selection(plan.table.instance_id, cell), shape_copy_paste())
            )
            after = run(cell_selection(plan.table.instance_id, cell))
            if after.selected_text != original.selected_text:
                raise HwpLiveError(f"{cell}: 서식 복사 후 텍스트가 달라졌습니다")
            for paragraph in range(
                after.selection.start.paragraph, after.selection.end.paragraph + 1
            ):
                observed = run(
                    (
                        MovePositionCommand(
                            NativePosition(after.selection.start.list_id, paragraph, 0)
                        ),
                    )
                )
                if (
                    observed.character_format != source.character_format
                    or observed.paragraph_format != source.paragraph_format
                ):
                    raise HwpLiveError(
                        f"{cell} 문단 {paragraph}: 원본 글자·문단 서식과 다릅니다"
                    )
            updated.append(cell)
            final_snapshot = after
    except HwpLiveError as error:
        return result.model_copy(
            update={
                "status": "partial_change" if changed else "operation_failed",
                "message": str(error),
                "changed": changed,
                "verified": False,
                "partial_mutation": changed,
                "retry_safe": not changed,
                "reconcile_required": changed,
                "failed_step": "table_format_copy_readback",
                "commands_executed": commands,
                "commands_completed": commands,
                "updated_addresses": tuple(updated),
                "resolved_target_id": prepared.target_id,
                "target_resolution_basis": prepared.target_basis,
            }
        )
    return result.model_copy(
        update={
            "message": "원본 셀 첫 문단의 글자·문단 모양을 복사하고 각 대상 문단의 서식과 내용 보존을 검증했습니다",
            "changed": changed,
            "verified": True,
            "execution_mode": "native_in_process",
            "native_protocol": 9,
            "verification": "native_operation_specific_readback",
            "commands_executed": commands,
            "commands_completed": commands,
            "native_elapsed_microseconds": elapsed,
            "updated_addresses": tuple(updated),
            "current_page": final_snapshot.current_page,
            "page_count": final_snapshot.page_count,
            "partial_mutation": False,
            "retry_safe": True,
            "resolved_target_id": prepared.target_id,
            "target_resolution_basis": prepared.target_basis,
        }
    )
