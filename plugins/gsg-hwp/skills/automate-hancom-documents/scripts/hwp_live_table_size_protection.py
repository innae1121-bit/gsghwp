from __future__ import annotations

from dataclasses import dataclass
from typing import cast
from xml.etree import ElementTree as ET

from hwp_errors import HwpLiveError
from hwp_live_native_action_models import (
    BooleanValue,
    CallCommand,
    CaptureTableCommand,
    NativeActionCommand,
    NativeActionRequest,
    NativeActionResult,
    NativeSetter,
    NativeSnapshot,
    NativeTextCallResult,
    ParameterActionCommand,
    SelectControlCommand,
    TextValue,
)
from hwp_live_native_batch import (
    execute_native_actions,
    inspect_native_structure,
    read_native_snapshot,
)
from hwp_live_native_format_commands import TableFormatCommandPlan
from hwp_live_native_format_contract import (
    NativeFormatRecipeRequest,
    PreparedFormatOperation,
)
from hwp_operation_contract import OperationResult


@dataclass(frozen=True, slots=True)
class TableSizeProtection:
    protected: bool
    unchanged_table: bytes


def parse_table_size_protection(xml: str) -> TableSizeProtection:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as error:
        raise HwpLiveError("표 크기 고정 검증용 HWPML을 읽지 못했습니다") from error
    tables = root.findall(".//TABLE")
    if len(tables) != 1:
        raise HwpLiveError(
            "크기 고정 검증용 선택 블록에서 표를 하나 확인하지 못했습니다"
        )
    table = tables[0]
    size = table.find("SHAPEOBJECT/SIZE")
    if size is None or size.get("Protect") not in {"true", "false", "1", "0"}:
        raise HwpLiveError("대상 표의 SIZE.Protect 속성을 읽지 못했습니다")
    protected = size.attrib.pop("Protect") in {"true", "1"}
    # HWP generates new export-only InstId values on every GetTextFile call.
    # Native selection/snapshot binds the live ID; structural inspection
    # separately verifies the actual control identities.
    for element in table.iter():
        _ = element.attrib.pop("InstId", None)
        _ = element.attrib.pop("InstID", None)
    return TableSizeProtection(
        protected, cast(bytes, ET.tostring(table, encoding="utf-8"))
    )


def _readback(
    request: NativeFormatRecipeRequest,
    instance_id: str,
    prefix: tuple[NativeActionCommand, ...] = (),
) -> tuple[TableSizeProtection, NativeActionResult]:
    native = execute_native_actions(
        request.candidate.window_handle,
        NativeActionRequest(
            request.routing_page.document_id,
            request.routing_page.full_name,
            (
                *prefix,
                SelectControlCommand(instance_id),
                CallCommand(
                    "GetTextFile", (TextValue("HWPML2X"), TextValue("saveblock:true"))
                ),
            ),
        ),
        minimum_version=9,
    )
    if native is None:
        raise HwpLiveError("표 크기 고정 네이티브 실행기를 사용할 수 없습니다")
    texts = tuple(
        item.value
        for item in native.call_results
        if isinstance(item, NativeTextCallResult) and item.method == "GetTextFile"
    )
    if len(texts) != 1:
        raise HwpLiveError("표 크기 고정 검증용 선택 블록을 반환받지 못했습니다")
    selected = read_native_snapshot(request.candidate.window_handle)
    if (
        selected is None
        or selected.control_type != "tbl"
        or selected.control_instance_id != instance_id
    ):
        raise HwpLiveError("크기 고정 검증용 선택이 요청한 실제 표와 다릅니다")
    return parse_table_size_protection(texts[0]), native


def execute_table_size_protection(
    request: NativeFormatRecipeRequest,
    before: NativeSnapshot,
    prepared: PreparedFormatOperation,
    result: OperationResult,
) -> OperationResult:
    plan = prepared.plan
    assert isinstance(plan, TableFormatCommandPlan)
    requested = plan.formatting.protect_size
    assert requested is not None
    mutation_started = False
    commands = 0
    elapsed = 0
    try:
        original, native = _readback(request, plan.table.instance_id)
        commands += native.commands_executed
        elapsed += native.elapsed_microseconds
        original_structure = inspect_native_structure(
            request.candidate.window_handle, plan.table.page
        )
        if original_structure is None:
            raise HwpLiveError("크기 고정 전에 표와 그림의 실제 구조를 읽지 못했습니다")
        observed = original
        if original.protected != requested:
            mutation_started = True
            observed, native = _readback(
                request,
                plan.table.instance_id,
                (
                    SelectControlCommand(plan.table.instance_id),
                    CaptureTableCommand(),
                    ParameterActionCommand(
                        "TablePropertyDialog",
                        "HShapeObject",
                        (NativeSetter("ProtectSize", BooleanValue(requested)),),
                    ),
                ),
            )
            commands += native.commands_executed
            elapsed += native.elapsed_microseconds
        else:
            observed, native = _readback(request, plan.table.instance_id)
            commands += native.commands_executed
            elapsed += native.elapsed_microseconds
        if observed.protected != requested:
            raise HwpLiveError("표 크기 고정 속성이 요청값과 다릅니다")
        if observed.unchanged_table != original.unchanged_table:
            offset = next(
                (
                    index
                    for index, (left, right) in enumerate(
                        zip(original.unchanged_table, observed.unchanged_table)
                    )
                    if left != right
                ),
                min(len(original.unchanged_table), len(observed.unchanged_table)),
            )
            raise HwpLiveError(
                "크기 고정 외의 표 크기·내용·속성 비교가 일치하지 않습니다. "
                f"before={original.unchanged_table[max(0, offset - 40) : offset + 100].decode('utf-8', errors='replace')}, "
                f"after={observed.unchanged_table[max(0, offset - 40) : offset + 100].decode('utf-8', errors='replace')}"
            )
        observed_structure = inspect_native_structure(
            request.candidate.window_handle, plan.table.page
        )
        if (
            observed_structure is None
            or observed_structure.controls != original_structure.controls
            or observed_structure.cells != original_structure.cells
        ):
            raise HwpLiveError("크기 고정 후 실제 표·그림·셀 구조가 원본과 다릅니다")
        after = read_native_snapshot(request.candidate.window_handle)
        if after is None or after.page_count != before.page_count:
            raise HwpLiveError("표 크기 고정 후 페이지 수 보존을 확인하지 못했습니다")
    except HwpLiveError as error:
        return result.model_copy(
            update={
                "status": "partial_change" if mutation_started else "operation_failed",
                "message": str(error),
                "changed": mutation_started,
                "verified": False,
                "partial_mutation": mutation_started,
                "retry_safe": not mutation_started,
                "reconcile_required": mutation_started,
                "failed_step": "table_size_protection_readback",
                "resolved_target_id": prepared.target_id,
                "target_resolution_basis": prepared.target_basis,
                "commands_executed": commands,
                "commands_completed": commands,
            }
        )
    return result.model_copy(
        update={
            "message": (
                f"표 크기 고정 ProtectSize={str(observed.protected).lower()}를 "
                "선택 표 HWPML SIZE.Protect에서 검증했고 표 크기·내용·속성과 페이지 수를 보존했습니다"
            ),
            "changed": mutation_started,
            "execution_mode": "native_in_process",
            "native_protocol": 9,
            "verification": "native_operation_specific_readback",
            "verified": True,
            "commands_executed": commands,
            "commands_completed": commands,
            "native_elapsed_microseconds": elapsed,
            "current_page": after.current_page,
            "page_count": after.page_count,
            "modified": after.modified,
            "partial_mutation": False,
            "retry_safe": True,
            "resolved_target_id": prepared.target_id,
            "target_resolution_basis": prepared.target_basis,
        }
    )
