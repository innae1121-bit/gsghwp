from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final, assert_never

from hwp_errors import HwpLiveError
from hwp_live_native_action_contract import (
    NATIVE_ACTION_PAYLOAD_LIMIT,
    action_command_payload_characters,
    action_request_payload_overhead,
)
from hwp_live_contract import (
    ImageBlock,
    LayoutPlan,
    PageBreakBlock,
    ParagraphBlock,
)
from hwp_live_native_action_models import (
    CaptionCommand,
    CellCommand,
    IntegerValue,
    InsertPictureCommand,
    InsertTextCommand,
    LeaveTableCommand,
    MergeCommand,
    MoveDocumentEndCommand,
    MovePageCommand,
    MovePositionCommand,
    NativeActionCommand,
    NativeActionRequest,
    NativeCharacterFormat,
    NativeParagraphFormat,
    NativePosition,
    NativeSelection,
    ParameterActionCommand,
    RunCommand,
)
from hwp_live_native_layout_format import (
    cell_padding_ranges,
    is_padding_command,
)
from hwp_live_native_table_layout import table_commands
from hwp_live_native_text_format import (
    ParagraphFormatting,
    character_command,
    lead_after_mm,
    lead_before_mm,
    paragraph_command,
    plan_lead_line_spacing,
    style_command,
)
from hwp_live_table_contract import TableBlock
from hwp_table_address import cell_address
from hwp_reference_layout_contract import ReferenceLayoutBlock
from hwp_reference_layout_evidence import prepare_reference_layout
from hwp_reference_layout_geometry import (
    HWPUNITS_PER_INCH,
    MILLIMETERS_PER_INCH,
    SectionPageGeometry,
)
from hwp_reference_layout_native import compile_reference_layout_command
from hwp_reference_layout_patch import (
    ReferenceLayoutPatchBlock,
    compile_reference_layout_patch_commands,
)


LAYOUT_COMMAND_LIMIT: Final = 20_000
# This is the V28 regression ceiling, not a wall-clock latency guarantee.
#
# 실측으로 고른 값이다. 같은 61x23 시트를 예산별로 돌린 결과
# (artifacts/live-defects/batch-cost-*):
#
#   예산      배치 수  최대 배치   배치 합계   총 소요   결과
#   100,000      6      87.5초     246.8초    294.0초   succeeded (경계 수리 전)
#    50,000     11      53.8초     251.1초    299.3초   succeeded (경계 수리 전)
#    50,000     11      52.1초     225.5초    302.2초   succeeded (경계 수리 후)
#    35,000     15      59.3초     344.9초    398.2초   succeeded (경계 수리 후)
#
# 50,000 은 최대 배치를 87.5초에서 52.1초로 줄여, 긴 배치 하나가 "멈췄다"로
# 오판될 창을 좁힌다. 총 소요 +2.8%(294.0→302.2초)가 그 값이다. 더 낮추는 것
# (35,000)은 배치 수·총 소요만 키우고 최대 배치는 오히려 늘었다(59.3초) —
# 병목 배치는 위상 예산이 아니라 병합·기하 명령의 단가가 결정하기 때문이다.
#
# 예전에는 이 값을 낮추면 실행이 깨졌다: 배치가 MergeCommand 로 끝나면 다음
# 네이티브 호출이 표를 다시 못 잡고 "COM_PROPERTY table: CurSelectedCtrl
# failed (HRESULT 0x80020005)" 로 즉사했다(batch-cost-35000, 2/2 재현). 지금은
# _merge_tail_repair 가 병합-꼬리 배치 끝에 주인 셀 재정박을 붙여 그 경계가
# 없다(batch-cost-35000-repaired 실측 succeeded·verified).
#
# 단일 배치가 기본 마감보다 길어질 수 있는 문제는 브리지 쪽에서 다룬다 —
# 배치가 실행 전에 자기 크기를 선언하고, 그 작업량만큼 마감을 늘린다
# (hwp_live_progress.declare_native_work, hwp_live_bridge 의 허용치 계산).
#
# 참고로 이 예산은 시간이 아니다: 위 표에서 배치별 실제 시간은 같은 위상
# 예산을 쓰고도 1.4초에서 59.3초까지 벌어졌다.
LAYOUT_TOPOLOGY_WORK_BUDGET: Final = 50_000


@dataclass(frozen=True, slots=True)
class NativeLayoutContext:
    document_id: int
    full_name: str
    style_ids: tuple[tuple[str, int], ...]
    text_formats: tuple[
        tuple[str, NativeCharacterFormat, NativeParagraphFormat], ...
    ] = ()
    caption_format_sources: tuple[tuple[str, NativePosition], ...] = ()
    page_count: int | None = None
    page_geometry: SectionPageGeometry | None = None
    page_number: int = 1
    base_style_id: int = 0
    expected_cursor: NativePosition | None = None
    expected_selection: NativeSelection | None = None
    insert_after_control: tuple[NativePosition, str] | None = None


@dataclass(frozen=True, slots=True)
class NativeLayoutCommandGroup:
    key: int
    commands: tuple[NativeActionCommand, ...]
    table_index: int | None
    table_cell_count: int
    addresses: tuple[str, ...]
    payload_characters: int


@dataclass(frozen=True, slots=True)
class NativeLayoutBatchGroup:
    group: NativeLayoutCommandGroup
    start_command: int
    end_command: int


@dataclass(frozen=True, slots=True)
class NativeLayoutBatch:
    request: NativeActionRequest
    groups: tuple[NativeLayoutBatchGroup, ...]
    topology_work: int

    def completed_group_keys(self, commands_completed: int) -> tuple[int, ...]:
        return tuple(
            item.group.key
            for item in self.groups
            if item.end_command <= commands_completed
        )


@dataclass(frozen=True, slots=True)
class NativeLayoutExecutionPlan:
    batches: tuple[NativeLayoutBatch, ...]
    address_requirements: tuple[
        tuple[int, str, frozenset[int]],
        ...,
    ]

    def completed_cells(
        self,
        completed_group_keys: set[int],
    ) -> tuple[tuple[int, str], ...]:
        return tuple(
            (table_index, address)
            for table_index, address, requirements in self.address_requirements
            if requirements.issubset(completed_group_keys)
        )

    def completed_addresses(self, completed_group_keys: set[int]) -> tuple[str, ...]:
        completed = self.completed_cells(completed_group_keys)
        table_indexes = {table_index for table_index, _, _ in self.address_requirements}
        if len(table_indexes) <= 1:
            return tuple(address for _, address in completed)
        return tuple(
            f"table[{table_index + 1}]:{address}" for table_index, address in completed
        )

    def merged_addresses(self) -> tuple[str, ...] | None:
        """모든 병합을 적용한 뒤 문서에 실제로 남는 셀 주소.

        레이아웃은 셀을 먼저 다 채우고(table_commands 의 행·열 순회) 마지막에
        합친다(_merge_commands). 그래서 completed_addresses 는 명령이 짚은
        주소, 즉 병합 전 격자를 그대로 돌려준다 — 실측(2026-08-30 insert_layout
        저널, 병합 10건이 적용된 4x9 표)에서 응답은 A1~D9 36칸을 실었는데 문서에
        남은 셀은 14개였다. 그 응답만 보면 병합이 전멸한 것과 구별되지 않는다.

        여기서는 실행한 MergeCommand 를 그대로 되짚어 남는 주소를 계산한다.
        한/글 주소는 행마다 그 행을 차지하는 셀을 왼쪽부터 세어 붙는다(라이브
        실측: 9b5c659 의 4x4 A1:B4 병합 뒤 1행 C,D가 B,C로 다시 매겨졌다).
        되읽기가 아니라 명령 투영이므로 부르는 쪽은 그 사실을 함께 밝혀야 한다.

        병합이 없거나 명령을 그대로 따라갈 수 없으면 None 을 돌려, 부르는 쪽이
        기존 값을 그대로 쓰게 한다.
        """
        groups = tuple(item.group for batch in self.batches for item in batch.groups)
        projected = _merged_table_addresses(groups)
        if projected is None:
            return None
        table_indexes = {table_index for table_index, _, _ in self.address_requirements}
        surviving = tuple(item for item in projected if item[0] in table_indexes)
        if not surviving:
            return None
        if len(table_indexes) <= 1:
            return tuple(address for _, address in surviving)
        return tuple(
            f"table[{table_index + 1}]:{address}" for table_index, address in surviving
        )


def _observed_table_width_mm(
    context: NativeLayoutContext,
    block: TableBlock,
) -> float | None:
    has_explicit_padding = any(
        cell.padding is not None for row in block.rows for cell in row
    )
    if has_explicit_padding and not cell_padding_ranges(block):
        return None
    geometry = context.page_geometry
    if geometry is None:
        return None
    content_width = (
        geometry.usable_area(page_number=context.page_number).width
        * MILLIMETERS_PER_INCH
        / HWPUNITS_PER_INCH
    )
    return content_width - block.left_margin_mm - block.right_margin_mm


def _optimized_table_commands(
    block: TableBlock,
    commands: tuple[NativeActionCommand, ...],
) -> tuple[NativeActionCommand, ...]:
    ranges = cell_padding_ranges(block)
    if not ranges:
        return commands
    grouped_addresses = {address for target in ranges for address in target.addresses}
    rows = len(block.rows)
    columns = len(block.rows[0])
    geometry_cells = (columns if block.column_widths_mm is not None else 0) + (
        rows if block.row_heights_mm is not None else 0
    )
    cell_positions = tuple(
        index
        for index, command in enumerate(commands)
        if isinstance(command, CellCommand)
    )
    cell_count = rows * columns
    main_positions = cell_positions[geometry_cells : geometry_cells + cell_count]
    expected_addresses = tuple(
        cell_address(row, column) for row in range(rows) for column in range(columns)
    )
    main_cells = tuple(commands[index] for index in main_positions)
    if (
        len(main_positions) != cell_count
        or not all(isinstance(command, CellCommand) for command in main_cells)
        or tuple(
            command.address
            for command in main_cells
            if isinstance(command, CellCommand)
        )
        != expected_addresses
    ):
        return commands

    next_cell = geometry_cells + cell_count
    if geometry_cells and next_cell < len(cell_positions):
        main_end = cell_positions[next_cell]
    else:
        last_main = main_positions[-1]
        main_end = next(
            (
                index
                for index in range(last_main + 1, len(commands))
                if isinstance(
                    commands[index],
                    (MergeCommand, CaptionCommand, LeaveTableCommand),
                )
            ),
            len(commands),
        )
    insertion = main_positions[0]

    removals: set[int] = set()
    for offset, start in enumerate(main_positions):
        address = expected_addresses[offset]
        if address not in grouped_addresses:
            continue
        end = (
            main_positions[offset + 1] if offset + 1 < len(main_positions) else main_end
        )
        padding_indexes = tuple(
            index
            for index in range(start + 1, end)
            if is_padding_command(commands[index])
        )
        if len(padding_indexes) != 1:
            return commands
        removals.add(padding_indexes[0])

    grouped_commands = tuple(
        command for target in ranges for command in target.commands
    )
    optimized: list[NativeActionCommand] = []
    for index, command in enumerate(commands):
        if index == insertion:
            optimized.extend(grouped_commands)
        if index not in removals:
            optimized.append(command)
    if insertion == len(commands):
        optimized.extend(grouped_commands)
    return tuple(optimized)


def build_native_layout_request(
    context: NativeLayoutContext,
    plan: LayoutPlan,
    assets: Mapping[Path, Path],
) -> NativeActionRequest:
    commands: list[NativeActionCommand] = []
    match plan.target:
        case "current":
            if context.insert_after_control is not None:
                anchor, _control_id = context.insert_after_control
                commands.extend(
                    (
                        MovePositionCommand(anchor),
                        RunCommand("MoveNextParaBegin"),
                    )
                )
        case "document_end":
            commands.append(MoveDocumentEndCommand())
        case "after_page":
            if plan.page is None:
                raise HwpLiveError("쪽 다음 삽입에는 page가 필요합니다")
            if context.page_count is not None and plan.page > context.page_count:
                raise HwpLiveError("삽입 기준 쪽이 현재 문서 쪽 수를 초과합니다")
            if context.page_count is not None and plan.page == context.page_count:
                commands.extend(
                    (
                        MovePageCommand(plan.page),
                        RunCommand("MovePageEnd"),
                        RunCommand("BreakPage"),
                    )
                )
            else:
                commands.extend(
                    (
                        MovePageCommand(plan.page + 1),
                        RunCommand("MovePageBegin"),
                        RunCommand("BreakPage"),
                        MovePageCommand(plan.page + 1),
                        RunCommand("MovePageBegin"),
                    )
                )
    if plan.replace_selection:
        if (
            context.expected_selection is None
            or not context.expected_selection.selected
        ):
            raise HwpLiveError("선택 영역 교체에는 선택 상태 스냅샷이 필요합니다")
        commands.append(RunCommand("Delete"))
    styles = dict(context.style_ids)
    text_formats = {
        name: (character, paragraph)
        for name, character, paragraph in context.text_formats
    }
    caption_format_sources = dict(context.caption_format_sources)
    layout_page_number = context.page_number
    for block_index, block in enumerate(plan.blocks):
        match block:
            case ParagraphBlock():
                if block.style_id is not None:
                    commands.append(style_command(block.style_id))
                character = character_command(block)
                if character is not None:
                    commands.append(character)
                paragraph = paragraph_command(
                    ParagraphFormatting(
                        alignment=block.alignment,
                        align_type_raw=block.align_type_raw,
                        line_spacing=block.line_spacing_percent,
                        before_mm=lead_before_mm(
                            block.space_before_mm,
                            block.plan_lead_mm,
                        ),
                        after_mm=lead_after_mm(
                            block.space_after_mm,
                            block.plan_trail_mm,
                        ),
                        left_mm=block.left_margin_mm,
                        right_mm=block.right_margin_mm,
                        indentation_mm=block.indentation_mm,
                        heading_type=block.heading_type,
                        heading_level=block.heading_level,
                    )
                )
                if paragraph is not None:
                    commands.append(paragraph)
                if block.runs:
                    for run in block.runs:
                        run_character = character_command(run)
                        if run_character is not None:
                            commands.append(run_character)
                        commands.append(InsertTextCommand(run.text))
                else:
                    commands.append(InsertTextCommand(block.text))
                if block_index + 1 < len(plan.blocks):
                    commands.append(RunCommand("BreakPara"))
                continue
            case TableBlock():
                commands.extend(
                    _optimized_table_commands(
                        block,
                        table_commands(
                            block,
                            assets,
                            styles,
                            text_formats,
                            caption_format_sources,
                            _observed_table_width_mm(context, block),
                        ),
                    )
                )
                continue
            case ReferenceLayoutBlock():
                if context.page_geometry is None:
                    raise HwpLiveError(
                        "참조 이미지 레이아웃에는 현재 구역 용지 정보가 필요합니다"
                    )
                commands.append(
                    compile_reference_layout_command(
                        prepare_reference_layout(block),
                        context.page_geometry,
                        page_number=layout_page_number,
                        base_style_id=context.base_style_id,
                    )
                )
                continue
            case ReferenceLayoutPatchBlock():
                if context.page_geometry is None:
                    raise HwpLiveError(
                        "참조 이미지 부분 보정에는 현재 구역 용지 정보가 필요합니다"
                    )
                commands.extend(
                    compile_reference_layout_patch_commands(
                        block,
                        context.page_geometry,
                        page_number=context.page_number,
                    )
                )
                continue
            case ImageBlock():
                try:
                    image = assets[block.path]
                except KeyError as error:
                    raise HwpLiveError(
                        f"그림 파일이 준비되지 않았습니다: {block.path}"
                    ) from error
                paragraph = paragraph_command(
                    ParagraphFormatting(
                        alignment=block.alignment,
                        before_mm=block.plan_lead_mm,
                        after_mm=block.plan_trail_mm,
                        line_spacing=plan_lead_line_spacing(block.plan_lead_mm),
                    )
                )
                if paragraph is not None:
                    commands.append(paragraph)
                commands.append(
                    InsertPictureCommand(
                        path=image,
                        width_mm=block.width_mm,
                        height_mm=block.height_mm,
                    )
                )
                # The break exists to leave the picture's own paragraph. When
                # nothing follows an absolutely placed picture it leaves an
                # empty paragraph instead, and that paragraph still costs a
                # full document line (MEASURED ~5.6mm): a plan ending 1-3mm
                # above the body bottom spilled onto a third page and G04
                # rolled its own layout back as LAYOUT_OVERFLOW. The caption
                # path has always been conditional this way.
                #
                # Only a placed picture skips it. An ordinary layout call has
                # no page budget to blow and does leave the cursor in a fresh
                # paragraph after the picture, which callers rely on.
                needs_break = (
                    block.caption is not None
                    or block_index + 1 < len(plan.blocks)
                    or block.plan_lead_mm is None
                )
                if needs_break:
                    commands.append(RunCommand("BreakPara"))
                if needs_break and block.plan_lead_mm is not None:
                    # BreakPara hands the picture paragraph's whole ParaShape to
                    # the paragraph it opens, lead included. A 50mm lead written
                    # for one picture therefore reappeared under the last one and
                    # pushed the trailing empty paragraph onto a third page --
                    # G04 refused its own layout as LAYOUT_OVERFLOW. Re-applying
                    # the document's base style puts that paragraph back on the
                    # document's own spacing instead of the placement's.
                    commands.append(style_command(context.base_style_id))
                if block.caption is not None:
                    if block.caption_style_id is not None:
                        commands.append(style_command(block.caption_style_id))
                    caption_paragraph = paragraph_command(
                        ParagraphFormatting(alignment="center")
                    )
                    if caption_paragraph is not None:
                        commands.append(caption_paragraph)
                    commands.append(InsertTextCommand(block.caption))
                    if block_index + 1 < len(plan.blocks):
                        commands.append(RunCommand("BreakPara"))
                continue
            case PageBreakBlock():
                commands.append(RunCommand("BreakPage"))
                layout_page_number += 1
                continue
        assert_never(block)
    return NativeActionRequest(
        document_id=context.document_id,
        full_name=context.full_name,
        commands=tuple(commands),
        expected_cursor=context.expected_cursor,
        expected_selection=context.expected_selection,
    )


def _table_creation_shape(command: NativeActionCommand) -> tuple[int, int] | None:
    if (
        not isinstance(command, ParameterActionCommand)
        or command.action != "TableCreate"
        or command.parameter_set != "HTableCreation"
    ):
        return None
    values = {
        setter.path: setter.value.value
        for setter in command.setters
        if isinstance(setter.value, IntegerValue)
    }
    rows = values.get("Rows")
    columns = values.get("Cols")
    if rows is None or columns is None or rows < 1 or columns < 1:
        return None
    return rows, columns


def _table_creation_cell_count(command: NativeActionCommand) -> int | None:
    shape = _table_creation_shape(command)
    return None if shape is None else shape[0] * shape[1]


def _cell_coordinate(address: str) -> tuple[int, int]:
    split = 0
    while split < len(address) and address[split].isalpha():
        split += 1
    letters = address[:split].upper()
    digits = address[split:]
    if not letters or not digits or not digits.isdecimal():
        raise HwpLiveError(f"표 셀 주소가 올바르지 않습니다: {address}")
    column = 0
    for letter in letters:
        column = column * 26 + ord(letter) - ord("A") + 1
    row = int(digits)
    if row < 1:
        raise HwpLiveError(f"표 셀 주소가 올바르지 않습니다: {address}")
    return row - 1, column - 1


def _rectangle_addresses(first: str, second: str) -> tuple[str, ...]:
    first_row, first_column = _cell_coordinate(first)
    second_row, second_column = _cell_coordinate(second)
    top, bottom = sorted((first_row, second_row))
    left, right = sorted((first_column, second_column))
    return tuple(
        cell_address(row, column)
        for row in range(top, bottom + 1)
        for column in range(left, right + 1)
    )


def _group_addresses(
    commands: tuple[NativeActionCommand, ...],
) -> tuple[str, ...]:
    addresses: list[str] = []
    first = commands[0] if commands else None
    if isinstance(first, CellCommand):
        selected = (first.address,)
        if (
            len(commands) >= 3
            and commands[1] == RunCommand("TableCellBlock")
            and commands[2] == RunCommand("TableCellBlockExtend")
        ):
            right_steps = sum(
                command == RunCommand("TableRightCell") for command in commands[3:]
            )
            down_steps = sum(
                command == RunCommand("TableLowerCell") for command in commands[3:]
            )
            row, column = _cell_coordinate(first.address)
            selected = _rectangle_addresses(
                first.address,
                cell_address(row + down_steps, column + right_steps),
            )
        addresses.extend(selected)
    for command in commands:
        if isinstance(command, MergeCommand):
            addresses.extend(_rectangle_addresses(command.first, command.second))
    return tuple(dict.fromkeys(addresses))


def _layout_command_groups(
    request: NativeActionRequest,
) -> tuple[NativeLayoutCommandGroup, ...]:
    groups: list[NativeLayoutCommandGroup] = []
    current: list[NativeActionCommand] = []
    table_index: int | None = None
    table_cell_count = 0
    next_table_index = 0

    def flush() -> None:
        if not current:
            return
        native_commands = tuple(current)
        groups.append(
            NativeLayoutCommandGroup(
                key=len(groups),
                commands=native_commands,
                table_index=table_index,
                table_cell_count=table_cell_count,
                addresses=_group_addresses(native_commands),
                payload_characters=sum(
                    action_command_payload_characters(command)
                    for command in native_commands
                ),
            )
        )
        current.clear()

    for command in request.commands:
        created_cells = _table_creation_cell_count(command)
        if created_cells is not None:
            table_index = next_table_index
            next_table_index += 1
            table_cell_count = created_cells
            current.append(command)
            continue
        if table_index is None:
            current.append(command)
            flush()
            continue
        if isinstance(command, CellCommand):
            flush()
            current.append(command)
            continue
        current.append(command)
        if isinstance(command, LeaveTableCommand):
            flush()
            table_index = None
            table_cell_count = 0
    flush()
    return tuple(groups)


_TOPOLOGY_PRESERVING_PARAMETER_ACTIONS: Final = frozenset(
    {
        ("Style", "HStyle"),
        ("CharShape", "HCharShape"),
        ("ParagraphShape", "HParaShape"),
        ("CellFill", "HCellBorderFill"),
        ("CellBorder", "HCellBorderFill"),
    }
)
_TOPOLOGY_PRESERVING_RUN_ACTIONS: Final = frozenset(
    {"TableVAlignTop", "TableVAlignCenter", "TableVAlignBottom"}
)


@dataclass(frozen=True, slots=True)
class _TopologyWorkState:
    topology_available: bool = False
    topology_table: int | None = None
    work: int = 0


def _advance_topology_work(
    state: _TopologyWorkState,
    group: NativeLayoutCommandGroup,
) -> _TopologyWorkState:
    topology_available = state.topology_available
    topology_table = state.topology_table
    work = state.work
    for command in group.commands:
        if isinstance(command, CellCommand):
            if not topology_available or topology_table != group.table_index:
                work += group.table_cell_count
            topology_available = True
            topology_table = group.table_index
            continue
        if isinstance(command, MergeCommand):
            if not topology_available or topology_table != group.table_index:
                work += group.table_cell_count
            topology_available = False
            topology_table = group.table_index
            continue
        if isinstance(command, ParameterActionCommand):
            if (
                command.action,
                command.parameter_set,
            ) not in _TOPOLOGY_PRESERVING_PARAMETER_ACTIONS:
                topology_available = False
            continue
        if isinstance(command, RunCommand):
            if command.action not in _TOPOLOGY_PRESERVING_RUN_ACTIONS:
                topology_available = False
            continue
        if isinstance(command, LeaveTableCommand):
            topology_available = False
            topology_table = None
    return _TopologyWorkState(topology_available, topology_table, work)


def _layout_topology_work(
    groups: tuple[NativeLayoutCommandGroup, ...],
) -> int:
    state = _TopologyWorkState()
    for group in groups:
        state = _advance_topology_work(state, group)
    return state.work


def _merge_tail_repair(
    groups: tuple[NativeLayoutCommandGroup, ...],
) -> CellCommand | None:
    """배치가 병합으로 끝날 때 다음 네이티브 호출을 위해 덧붙일 재정박 명령.

    병합 뒤의 커서·선택 복원은 같은 호출 안에서는 문제가 없지만, 호출이 거기서
    끝나면 다음 호출의 표 재포착(CaptureCurrentTable)이 기대는 ParentCtrl /
    CurSelectedCtrl 어느 쪽도 남아 있지 않을 수 있다. 실측
    (artifacts/live-defects/batch-cost-35000): budget 35,000 에서 batch 9 가
    MergeCommand(J33:K33) 로 끝나자 batch 10 첫 명령이 2/2 회
    "COM_PROPERTY table: CurSelectedCtrl failed (HRESULT 0x80020005)" 로 즉사했다.
    병합 주인 셀로 가는 GoToCell 은 ParentCtrl==tableId 를 검증하며 커서를 표
    안에 남기므로, 다음 호출은 항상 표를 다시 잡을 수 있다.
    """
    if not groups:
        return None
    tail = groups[-1]
    if tail.table_index is None or not tail.commands:
        return None
    last = tail.commands[-1]
    if not isinstance(last, MergeCommand):
        return None
    return CellCommand(last.first)


def _repair_reserve(
    group: NativeLayoutCommandGroup,
) -> tuple[int, int, int]:
    """이 그룹이 배치 꼬리가 될 때 재정박이 요구할 (명령, 작업량, payload)."""
    repair = _merge_tail_repair((group,))
    if repair is None:
        return 0, 0, 0
    # 병합이 topology 를 무효화한 직후라 재정박 GoToCell 은 표 전체를 다시
    # 읽는다(_advance_topology_work 의 CellCommand 규칙과 같은 값).
    return 1, group.table_cell_count, action_command_payload_characters(repair)


def _batch_from_groups(
    request: NativeActionRequest,
    groups: tuple[NativeLayoutCommandGroup, ...],
    *,
    first: bool,
    preserve_request: bool = False,
    tail_repair: CellCommand | None = None,
) -> NativeLayoutBatch:
    commands: list[NativeActionCommand] = []
    batch_groups: list[NativeLayoutBatchGroup] = []
    for group in groups:
        start = len(commands)
        commands.extend(group.commands)
        batch_groups.append(NativeLayoutBatchGroup(group, start, len(commands)))
    repair_work = 0
    if tail_repair is not None:
        # 그룹 뒤에 붙여 그룹 오프셋(start/end)과 완료 주소 계산을 건드리지
        # 않는다. 실행되면 완료 명령 수에는 그대로 잡힌다 — 실제로 실행되는
        # 명령이므로 정직한 수다.
        commands.append(tail_repair)
        repair_work = groups[-1].table_cell_count
    batch_request = (
        request
        if preserve_request
        else replace(
            request,
            commands=tuple(commands),
            expected_cursor=request.expected_cursor if first else None,
            expected_selection=request.expected_selection if first else None,
            atomic=request.atomic if first else False,
        )
    )
    return NativeLayoutBatch(
        request=batch_request,
        groups=tuple(batch_groups),
        topology_work=_layout_topology_work(groups) + repair_work,
    )


def _address_requirements(
    groups: tuple[NativeLayoutCommandGroup, ...],
) -> tuple[tuple[int, str, frozenset[int]], ...]:
    order: list[tuple[int, str]] = []
    requirements: dict[tuple[int, str], set[int]] = {}
    for group in groups:
        if group.table_index is None:
            continue
        for address in group.addresses:
            key = (group.table_index, address)
            if key not in requirements:
                order.append(key)
                requirements[key] = set()
            requirements[key].add(group.key)
    return tuple(
        (table_index, address, frozenset(requirements[(table_index, address)]))
        for table_index, address in order
    )


@dataclass(slots=True)
class _MergedRowSegment:
    """한 행에서 글자 슬롯 하나를 차지하는 셀의 열 구간과 그 셀이 시작한 행."""

    left: int
    right: int
    top_row: int


def _apply_merge_projection(
    rows: list[list[_MergedRowSegment]],
    first: str,
    second: str,
) -> bool:
    """MergeCommand 하나를 행별 슬롯 장부에 반영한다. 따라갈 수 없으면 False."""
    top, first_slot = _cell_coordinate(first)
    bottom, second_slot = _cell_coordinate(second)
    if (
        top > bottom
        or bottom >= len(rows)
        or first_slot >= len(rows[top])
        or second_slot >= len(rows[bottom])
    ):
        return False
    left = rows[top][first_slot].left
    right = rows[bottom][second_slot].right
    if left > right:
        return False
    for row in range(top, bottom + 1):
        segments = rows[row]
        covered = [
            index
            for index, segment in enumerate(segments)
            if segment.left >= left and segment.right <= right
        ]
        if (
            not covered
            or covered != list(range(covered[0], covered[-1] + 1))
            or segments[covered[0]].left != left
            or segments[covered[-1]].right != right
            # 이미 위 행에서 시작한 셀을 삼키는 병합은 겹침이다. 계획 검증이
            # 막는 모양이므로 여기서 만나면 장부를 믿을 수 없다는 뜻이다.
            or any(segments[index].top_row < top for index in covered)
        ):
            return False
        segments[covered[0] : covered[-1] + 1] = [_MergedRowSegment(left, right, top)]
    return True


def _merged_table_addresses(
    groups: tuple[NativeLayoutCommandGroup, ...],
) -> tuple[tuple[int, str], ...] | None:
    """실행한 병합 명령을 되짚어 표별로 남는 (표 index, 주소)를 투영한다."""
    tables: dict[int, list[list[_MergedRowSegment]]] = {}
    order: list[int] = []
    merged = False
    for group in groups:
        if group.table_index is None:
            continue
        for command in group.commands:
            shape = _table_creation_shape(command)
            if shape is not None:
                if group.table_index in tables:
                    return None
                table_rows, table_columns = shape
                tables[group.table_index] = [
                    [
                        _MergedRowSegment(column, column, row)
                        for column in range(table_columns)
                    ]
                    for row in range(table_rows)
                ]
                order.append(group.table_index)
                continue
            if not isinstance(command, MergeCommand):
                continue
            rows = tables.get(group.table_index)
            if rows is None:
                return None
            try:
                applied = _apply_merge_projection(rows, command.first, command.second)
            except HwpLiveError:
                return None
            if not applied:
                return None
            merged = True
    if not merged:
        return None
    return tuple(
        (table_index, cell_address(row, slot))
        for table_index in order
        for row, segments in enumerate(tables[table_index])
        for slot, segment in enumerate(segments)
        if segment.top_row == row
    )


def build_native_layout_execution_plan(
    request: NativeActionRequest,
) -> NativeLayoutExecutionPlan:
    groups = _layout_command_groups(request)
    requirements = _address_requirements(groups)
    canonical_work = _layout_topology_work(groups)
    canonical_payload = action_request_payload_overhead(request) + sum(
        group.payload_characters for group in groups
    )
    # LAYOUT_COMMAND_LIMIT mirrors kMaximumCommands in ActionProtocol.cpp:21.
    # The parser there rejects the whole script (ActionProtocol.cpp:537) without
    # looking at `atomic`, so an atomic request over the limit can only come
    # back as BAD_REQUEST after a full round trip. Splitting it is not an
    # option — it is one rollback unit — so fail here, where the cause is still
    # nameable. Non-atomic requests keep falling through to the splitter.
    if request.atomic and (
        len(request.commands) > LAYOUT_COMMAND_LIMIT
        or canonical_payload > NATIVE_ACTION_PAYLOAD_LIMIT
    ):
        raise HwpLiveError(
            "원자 레이아웃 요청이 네이티브 호출 한계를 넘었습니다"
            + (
                f" (명령 {len(request.commands)}/{LAYOUT_COMMAND_LIMIT}개,"
                f" payload {canonical_payload}/{NATIVE_ACTION_PAYLOAD_LIMIT}자)."
            )
            + " 원자 실행을 유지하려면 블록 수를 줄여 다시 요청하세요"
        )
    if (
        request.atomic
        or (
            len(request.commands) <= LAYOUT_COMMAND_LIMIT
            and canonical_work <= LAYOUT_TOPOLOGY_WORK_BUDGET
            and canonical_payload <= NATIVE_ACTION_PAYLOAD_LIMIT
        )
        or not groups
    ):
        return NativeLayoutExecutionPlan(
            (
                _batch_from_groups(
                    request,
                    groups,
                    first=True,
                    preserve_request=True,
                ),
            ),
            requirements,
        )

    grouped_batches: list[tuple[NativeLayoutCommandGroup, ...]] = []
    current: list[NativeLayoutCommandGroup] = []
    current_commands = 0
    first_overhead = action_request_payload_overhead(replace(request, commands=()))
    later_overhead = action_request_payload_overhead(
        replace(
            request,
            commands=(),
            expected_cursor=None,
            expected_selection=None,
            atomic=False,
        )
    )
    current_payload = first_overhead
    current_state = _TopologyWorkState()
    for group in groups:
        candidate_commands = current_commands + len(group.commands)
        candidate_payload = current_payload + group.payload_characters
        candidate_state = _advance_topology_work(current_state, group)
        # 이 그룹에서 배치가 끝나면 병합-꼬리 재정박이 함께 실행될 수 있다.
        # 그 몫까지 한도 안에 들어와야 나중에 붙는 재정박이 한도를 넘지 않는다.
        reserve_commands, reserve_work, reserve_payload = _repair_reserve(group)
        if current and (
            candidate_commands + reserve_commands > LAYOUT_COMMAND_LIMIT
            or candidate_state.work + reserve_work > LAYOUT_TOPOLOGY_WORK_BUDGET
            or candidate_payload + reserve_payload > NATIVE_ACTION_PAYLOAD_LIMIT
        ):
            grouped_batches.append(tuple(current))
            current = [group]
            candidate_commands = len(group.commands)
            candidate_payload = later_overhead + group.payload_characters
            candidate_state = _advance_topology_work(_TopologyWorkState(), group)
        else:
            current.append(group)
        current_commands = candidate_commands
        current_payload = candidate_payload
        current_state = candidate_state
        if (
            candidate_commands + reserve_commands > LAYOUT_COMMAND_LIMIT
            or candidate_state.work + reserve_work > LAYOUT_TOPOLOGY_WORK_BUDGET
            or candidate_payload + reserve_payload > NATIVE_ACTION_PAYLOAD_LIMIT
        ):
            raise HwpLiveError(
                "단일 레이아웃 명령 그룹이 네이티브 호출 budget을 초과합니다"
            )
    if current:
        grouped_batches.append(tuple(current))

    def tail_repair(index: int) -> CellCommand | None:
        # 마지막 배치는 뒤가 없으니 재정박도 없다. 다음 배치 머리가 같은 표의
        # 셀 이동으로 시작할 때만 붙인다 — 병합-꼬리 그룹 뒤에는 문법상 항상
        # 같은 표의 CellCommand 그룹이 오지만, 여기서 다시 확인해야 머리가
        # 표 생성으로 바뀌는 날 재정박이 셀 안에 새 표를 만드는 사고가 없다.
        if index + 1 >= len(grouped_batches):
            return None
        head = grouped_batches[index + 1][0]
        tail = grouped_batches[index][-1]
        if (
            head.table_index != tail.table_index
            or not head.commands
            or not isinstance(head.commands[0], CellCommand)
        ):
            return None
        return _merge_tail_repair(grouped_batches[index])

    return NativeLayoutExecutionPlan(
        tuple(
            _batch_from_groups(
                request,
                batch_groups,
                first=index == 0,
                tail_repair=tail_repair(index),
            )
            for index, batch_groups in enumerate(grouped_batches)
        ),
        requirements,
    )
