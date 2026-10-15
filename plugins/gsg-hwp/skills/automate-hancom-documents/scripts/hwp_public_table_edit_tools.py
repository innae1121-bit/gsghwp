from __future__ import annotations

from typing import Annotated, assert_never, final
from pydantic import Field
from typing_extensions import TypeIs

import hwp_public_table_metadata as metadata
from hwp_operation_contract import (
    HwpOperateInputs,
    HwpOperatePolicy,
    HwpOperatePostconditions,
    OperationInputValue,
    OperationResult,
)
from hwp_public_action_contract import PublicOperationId
from hwp_public_cell_selector import (
    PublicCellAddress,
    PublicCellRange,
    PublicCellReference,
)
from hwp_public_contract import PublicActionResult, PublicTableTarget
from hwp_public_contract import refresh_public_target_ids, to_public_action_result
from hwp_public_table_edit_contract import (
    PublicCellFormatSource,
    PublicMergeTableCellsInput,
    PublicSplitTableCellInput,
    PublicTableAlignment,
    PublicTableBorderStyle,
    PublicTableBorderWidth,
    PublicTableBorderSide,
    PublicTableColor,
    PublicTableFormattingInput,
    PublicTableSplitMode,
    PublicTableVerticalAlignment,
)
from hwp_public_table_edit_execution import (
    PublicTableEditExecutionRequest,
    execute_public_table_edit,
)
from hwp_public_table_edit_resolution import (
    NamedPublicCellReference,
    PublicTableEditResolutionRequest,
    ResolvedPublicTableEdit,
    resolve_public_table_edit,
)
from hwp_public_table_plan import PublicTableResolutionRequest
from hwp_operation_registry import operation_registry
from hwp_public_table_target import PublicTableTargetStore
from hwp_public_table_tools import PublicTableExecutor


def _is_resolved_edit(
    value: ResolvedPublicTableEdit | PublicActionResult | OperationResult,
) -> TypeIs[ResolvedPublicTableEdit]:
    return isinstance(value, ResolvedPublicTableEdit)


@final
class HwpPublicTableEditTools:
    __slots__ = ("_executor", "_targets")

    def __init__(
        self,
        executor: PublicTableExecutor,
        targets: PublicTableTargetStore | None = None,
    ) -> None:
        self._executor = executor
        self._targets = PublicTableTargetStore() if targets is None else targets

    def _public_result(
        self,
        result: OperationResult,
        document_path: str | None,
    ) -> PublicActionResult:
        target_ids = refresh_public_target_ids(self._targets, result, document_path)
        return to_public_action_result(result, target_ids)

    async def _resolved(
        self,
        request: PublicTableEditResolutionRequest,
    ) -> ResolvedPublicTableEdit | PublicActionResult:
        resolved = await resolve_public_table_edit(
            self._executor,
            self._targets,
            request,
        )
        match resolved:
            case OperationResult() as failed:
                target = request.resolution.target
                document_path = None if target is None else target.document_path
                return self._public_result(failed, document_path)
            case _ as unreachable if not _is_resolved_edit(unreachable):
                assert_never(unreachable)
            case _:
                return resolved

    async def _execute(
        self,
        intent: str,
        inputs: HwpOperateInputs,
    ) -> PublicActionResult:
        result = await execute_public_table_edit(
            self._executor,
            self._targets,
            PublicTableEditExecutionRequest(intent, inputs),
        )
        return self._public_result(result, inputs.document)

    async def hwp_format_table(
        self,
        *,
        operation_id: PublicOperationId,
        target: PublicTableTarget | None = None,
        cell: PublicCellReference | None = None,
        cells: Annotated[
            tuple[PublicCellAddress | PublicCellRange, ...] | None,
            Field(
                max_length=2_000,
                description=(
                    "같은 서식을 적용할 셀들입니다. 'A4:P4'처럼 구간으로 적거나 "
                    "['A4','C4']처럼 주소를 나열합니다. cell과 함께 쓸 수 있고, "
                    "겹치는 주소는 한 번만 적용합니다."
                ),
            ),
        ] = None,
        row_height_mm: Annotated[float | None, Field(ge=1, le=250)] = None,
        column_width_mm: Annotated[float | None, Field(ge=1, le=250)] = None,
        padding_left_mm: Annotated[float | None, Field(ge=0, le=20)] = None,
        padding_right_mm: Annotated[float | None, Field(ge=0, le=20)] = None,
        padding_top_mm: Annotated[float | None, Field(ge=0, le=20)] = None,
        padding_bottom_mm: Annotated[float | None, Field(ge=0, le=20)] = None,
        bold: bool | None = None,
        font_name: Annotated[str | None, Field(min_length=1, max_length=100)] = None,
        font_size_pt: Annotated[float | None, Field(ge=1, le=96)] = None,
        text_color: PublicTableColor | None = None,
        alignment: PublicTableAlignment = "inherit",
        vertical_alignment: PublicTableVerticalAlignment = "inherit",
        line_spacing: Annotated[int | None, Field(ge=50, le=500)] = None,
        fill_color: PublicTableColor | None = None,
        border_style: PublicTableBorderStyle | None = None,
        border_width: PublicTableBorderWidth | None = None,
        border_color: PublicTableColor | None = None,
        border_sides: Annotated[
            tuple[PublicTableBorderSide, ...] | None,
            Field(
                min_length=1,
                max_length=4,
                description="테두리를 변경할 셀 변입니다. 생략하면 네 변 모두 적용하며, 지정하지 않은 변은 보존합니다.",
            ),
        ] = None,
        format_source: Annotated[
            PublicCellFormatSource | None,
            Field(
                description="원본 셀 첫 문단의 글자·문단 모양을 대상 셀 전체에 복사합니다. 대상 cell/cells가 필요하며 개별 서식과 함께 지정할 수 없습니다."
            ),
        ] = None,
    ) -> PublicActionResult:
        request = PublicTableEditResolutionRequest(
            resolution=PublicTableResolutionRequest(
                target=target,
                request_id=operation_id,
                query=metadata.FORMAT_TABLE_INTENT,
            ),
            cells=(() if cell is None else (NamedPublicCellReference("cell", cell),)),
        )
        resolved = await self._resolved(request)
        match resolved:
            case PublicActionResult():
                return resolved
            case _ as unreachable if not _is_resolved_edit(unreachable):
                assert_never(unreachable)
            case _:
                requested = PublicTableFormattingInput(
                    cell=resolved.addresses[0] if resolved.addresses else None,
                    row_height_mm=row_height_mm,
                    column_width_mm=column_width_mm,
                    bold=bold,
                    font_name=font_name,
                    font_size_pt=font_size_pt,
                    text_color=text_color,
                    alignment=alignment,
                    vertical_alignment=vertical_alignment,
                    line_spacing=line_spacing,
                    fill_color=fill_color,
                    border_style=border_style,
                    border_width=border_width,
                    border_color=border_color,
                    border_sides=border_sides,
                )
                parameters = dict(requested.to_parameters())
                if format_source is not None:
                    parameters["format_source_table_id"] = (
                        format_source.table_instance_id
                    )
                    parameters["format_source_cell"] = format_source.cell
                if cells:
                    # OperationInputValue 는 스칼라만 담는다. 구간 전개와 중복
                    # 제거는 parse_table_format 이 한 번에 한다.
                    parameters["cells"] = ",".join(address.upper() for address in cells)
                for name, value in (
                    ("padding_left_mm", padding_left_mm),
                    ("padding_right_mm", padding_right_mm),
                    ("padding_top_mm", padding_top_mm),
                    ("padding_bottom_mm", padding_bottom_mm),
                ):
                    if value is not None:
                        parameters[name] = value
                return await self._execute(
                    request.resolution.query,
                    HwpOperateInputs(
                        request_id=request.resolution.request_id,
                        document=resolved.target.document_path,
                        operation="table.format",
                        target=resolved.target.target,
                        parameters=parameters,
                        policy=HwpOperatePolicy(ambiguity="return_candidates"),
                        postconditions=HwpOperatePostconditions(verify_structure=True),
                    ),
                )

    async def hwp_set_table_size_fixed(
        self,
        *,
        operation_id: PublicOperationId,
        target: PublicTableTarget | None = None,
        fixed: bool = True,
    ) -> PublicActionResult:
        request = PublicTableEditResolutionRequest(
            resolution=PublicTableResolutionRequest(
                target=target,
                request_id=operation_id,
                query=metadata.FORMAT_TABLE_INTENT,
            ),
            cells=(),
        )
        resolved = await self._resolved(request)
        if isinstance(resolved, PublicActionResult):
            return resolved
        return await self._execute(
            request.resolution.query,
            HwpOperateInputs(
                request_id=operation_id,
                document=resolved.target.document_path,
                operation="table.format",
                target=resolved.target.target,
                parameters={"protect_size": fixed},
                policy=HwpOperatePolicy(ambiguity="return_candidates"),
                postconditions=HwpOperatePostconditions(verify_structure=True),
            ),
        )

    async def hwp_merge_table_cells(
        self,
        *,
        operation_id: PublicOperationId,
        target: PublicTableTarget | None = None,
        start_cell: PublicCellReference | None = None,
        end_cell: PublicCellReference | None = None,
    ) -> PublicActionResult:
        # Both omitted means "the cells the user already selected". Requiring
        # the addresses made models invent them and merge the wrong cells.
        if (start_cell is None) != (end_cell is None):
            document_path = None if target is None else target.document_path
            return self._public_result(
                OperationResult(
                    request_id=operation_id,
                    status="schema_conflict",
                    query=metadata.MERGE_TABLE_CELLS_INTENT,
                    registry_entries=operation_registry().count,
                    lookup_microseconds=0,
                    message=(
                        "병합 범위는 시작·끝 셀을 모두 지정하거나 현재 선택을 "
                        "사용하도록 둘 다 생략해야 합니다"
                    ),
                    retry_safe=True,
                ),
                document_path,
            )
        named = tuple(
            (parameter, NamedPublicCellReference(input_name, reference))
            for parameter, input_name, reference in (
                ("start", "start_cell", start_cell),
                ("end", "end_cell", end_cell),
            )
            if reference is not None
        )
        request = PublicTableEditResolutionRequest(
            resolution=PublicTableResolutionRequest(
                target=target,
                request_id=operation_id,
                query=metadata.MERGE_TABLE_CELLS_INTENT,
            ),
            cells=tuple(cell for _, cell in named),
            unique_cells_required=True,
        )
        resolved = await self._resolved(request)
        match resolved:
            case PublicActionResult():
                return resolved
            case _ as unreachable if not _is_resolved_edit(unreachable):
                assert_never(unreachable)
            case _:
                addresses = resolved.addresses
                parameters: dict[str, OperationInputValue]
                if len(addresses) == 2:
                    requested = PublicMergeTableCellsInput(
                        start_cell=addresses[0],
                        end_cell=addresses[1],
                    )
                    parameters = dict(requested.to_parameters())
                else:
                    # No address, or only one: the live layer resolves what is
                    # missing from the selection, or reports it missing.
                    parameters = {
                        parameter: address.upper()
                        for (parameter, _), address in zip(
                            named, addresses, strict=True
                        )
                    }
                return await self._execute(
                    request.resolution.query,
                    HwpOperateInputs(
                        request_id=request.resolution.request_id,
                        document=resolved.target.document_path,
                        operation="table.merge_cells",
                        target=resolved.target.target,
                        parameters=parameters,
                        policy=HwpOperatePolicy(ambiguity="return_candidates"),
                        postconditions=HwpOperatePostconditions(verify_structure=True),
                    ),
                )

    async def hwp_split_table_cell(
        self,
        *,
        operation_id: PublicOperationId,
        target: PublicTableTarget | None = None,
        cell: PublicCellReference | None = None,
        columns: Annotated[int, Field(ge=1, le=65_535)],
        rows: Annotated[int, Field(ge=1, le=65_535)],
        distribute_height: bool = False,
        split_mode: PublicTableSplitMode = "equal",
    ) -> PublicActionResult:
        # Omitted means "the cell the caret is already in".
        request = PublicTableEditResolutionRequest(
            resolution=PublicTableResolutionRequest(
                target=target,
                request_id=operation_id,
                query=metadata.SPLIT_TABLE_CELL_INTENT,
            ),
            cells=() if cell is None else (NamedPublicCellReference("cell", cell),),
        )
        resolved = await self._resolved(request)
        match resolved:
            case PublicActionResult():
                return resolved
            case _ as unreachable if not _is_resolved_edit(unreachable):
                assert_never(unreachable)
            case _:
                parameters: dict[str, OperationInputValue]
                if resolved.addresses:
                    requested = PublicSplitTableCellInput(
                        cell=resolved.addresses[0],
                        columns=columns,
                        rows=rows,
                        distribute_height=distribute_height,
                        split_mode=split_mode,
                    )
                    parameters = dict(requested.to_parameters())
                else:
                    # PublicSplitTableCellInput.to_parameters() without "cell".
                    # parse_split still rejects a 1x1 split.
                    parameters = {
                        "columns": columns,
                        "rows": rows,
                        "distribute_height": distribute_height,
                        "merge": False,
                        "split_mode": split_mode,
                    }
                return await self._execute(
                    request.resolution.query,
                    HwpOperateInputs(
                        request_id=request.resolution.request_id,
                        document=resolved.target.document_path,
                        operation="table.split_cells",
                        target=resolved.target.target,
                        parameters=parameters,
                        policy=HwpOperatePolicy(ambiguity="return_candidates"),
                        postconditions=HwpOperatePostconditions(verify_structure=True),
                    ),
                )
