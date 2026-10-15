from __future__ import annotations

# 그림 편집 recipe 한 갈래. 입력 해석·명령 조립·되읽기를 한 자리에 둔다.

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from time import monotonic
from typing import Final

from hwp_errors import HwpLiveError
from hwp_live_native_action_commands import (
    BooleanValue,
    IntegerValue,
    MillimeterValue,
    MovePositionCommand,
    NativeActionCommand,
    NativeSetter,
    ParameterActionCommand,
    RunCommand,
    SelectControlCommand,
)
from hwp_live_native_action_models import NativePosition
from hwp_live_native_action_results import (
    NativePageCell,
    NativePageInspection,
    NativeSnapshot,
)
from hwp_live_native_batch import inspect_native_page, read_native_snapshot
from hwp_live_api import LiveHwpApplication
from hwp_live_rot import HwpDocumentCandidate
from hwp_object_control_types import PICTURE_CONTROL_TYPES, is_picture_control_type
from hwp_priority_object_inputs import (
    ControlTargetFailure,
    ControlTargetRequest,
    ResolvedObjectControl,
    resolve_control_target,
)
from hwp_operation_contract import (
    HwpOperateTarget,
    OperationInputValue,
    OperationResult,
    WorkflowResolution,
)
from hwp_picture_edit_com import read_picture_geometry
from hwp_picture_edit_evidence import (
    PictureEditEvidence,
    PictureGeometry,
    crop_readback,
)
from hwp_picture_edit_geometry import (
    PictureCropRatios,
    PictureOriginalSize,
    PictureSkip,
    skips_from_ratios,
    visible_fraction,
)
from hwp_priority_object_runtime import (
    execute_object_commands as _execute,
    object_recipe_result as _result,
)
from hwp_priority_recipe_contract import HwpPriorityRecipeInputs


# 이 recipe 가 맡는 작업군. image.resize 는 지금까지 선언만 있고 recipe 가 없던
# 자리다 ("그림 크기 또는 배치 크기 조절"). 자르기는 상자 안에서 보이는 영역을
# 정하는 일이고 셀 이동은 배치를 옮기는 일이라 둘 다 이 자리에 든다.
#
# 새 HwpWorkflowId 를 만들지 않은 이유는 따로 있다. HwpWorkflowId 리터럴은
# hwp_operate·hwp_copy_style·hwp_get_operation_status 세 레거시 도구의 봉인된
# 스키마 안에 그대로 박혀 있다. 항목을 하나 더하면 그 셋의 스키마 해시가 바뀌어
# 봉인 오라클을 다시 만들어야 한다.
PICTURE_EDIT_WORKFLOW: Final = "image.resize"

CROP_PARAMETERS: Final = ("crop_left", "crop_top", "crop_right", "crop_bottom")
SOURCE_TABLE_PARAMETER: Final = "source_table_id"
SOURCE_CELL_PARAMETER: Final = "source_cell"
DESTINATION_TABLE_PARAMETER: Final = "move_to_table_id"
DESTINATION_CELL_PARAMETER: Final = "move_to_cell"
COPY_DESTINATION_TABLE_PARAMETER: Final = "copy_to_table_id"
COPY_DESTINATION_CELL_PARAMETER: Final = "copy_to_cell"

_CONFIRM_REINSPECTION_LIMIT: Final = 3
_CONFIRM_REINSPECTION_DEADLINE_MS: Final = 250

_SKIP_PATHS: Final = (
    "ShapeDrawImageAttr/SkipLeft",
    "ShapeDrawImageAttr/SkipTop",
    "ShapeDrawImageAttr/SkipRight",
    "ShapeDrawImageAttr/SkipBottom",
)


@dataclass(frozen=True, slots=True)
class PictureEditRequest:
    candidate: HwpDocumentCandidate
    hwp: LiveHwpApplication
    routing_page: NativePageInspection
    resolution: WorkflowResolution
    target: HwpOperateTarget | None
    parameters: Mapping[str, OperationInputValue]
    resolve_only: bool
    allow_document_change: bool
    recipe: HwpPriorityRecipeInputs | None = None


def _ratio(parameters: Mapping[str, OperationInputValue], name: str) -> float:
    value = parameters.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def crop_ratios_from_parameters(
    parameters: Mapping[str, OperationInputValue],
) -> PictureCropRatios:
    """자르기는 절대값이다. 준 네 변이 그대로 결과가 된다.

    빠뜨린 변은 0 -- 즉 "자르지 않음"이다. 누적이 아니라 선언이라, 같은 요청을
    두 번 보내도 결과가 같고, 되돌리려면 네 값을 0 으로 주면 된다.
    """
    return PictureCropRatios(
        left=_ratio(parameters, "crop_left"),
        top=_ratio(parameters, "crop_top"),
        right=_ratio(parameters, "crop_right"),
        bottom=_ratio(parameters, "crop_bottom"),
    )


def _text(parameters: Mapping[str, OperationInputValue], name: str) -> str | None:
    value = parameters.get(name)
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def requested_crop(parameters: Mapping[str, OperationInputValue]) -> bool:
    return any(name in parameters for name in CROP_PARAMETERS)


def cell_of_picture(
    page: NativePageInspection,
    picture_id: str,
) -> tuple[str | None, str | None]:
    """그림이 들어 있는 표와 셀 주소. 표 밖이면 (None, None)."""
    for control in page.controls:
        if control.instance_id != picture_id:
            continue
        for cell in page.cells:
            if cell.list_id == control.anchor.list_id:
                return cell.table_instance_id, cell.address
        return None, None
    return None, None


def picture_in_cell(
    page: NativePageInspection,
    table_id: str,
    address: str,
) -> str | None:
    """지목한 셀 안에 있는 그림의 개체 ID. 여러 장이면 첫 장."""
    wanted = address.strip().upper()
    lists = frozenset(
        cell.list_id
        for cell in page.cells
        if cell.table_instance_id == table_id and cell.address.upper() == wanted
    )
    for control in page.controls:
        if (
            is_picture_control_type(control.control_type)
            and control.anchor.list_id in lists
        ):
            return control.instance_id
    return None


def pictures_in_list(page: NativePageInspection, list_id: int) -> tuple[str, ...]:
    return tuple(
        control.instance_id
        for control in page.controls
        if is_picture_control_type(control.control_type)
        and control.anchor.list_id == list_id
    )


def destination_cell(
    page: NativePageInspection,
    table_id: str,
    address: str,
) -> NativePageCell | None:
    wanted = address.strip().upper()
    for cell in page.cells:
        if cell.table_instance_id == table_id and cell.address.upper() == wanted:
            return cell
    return None


def picture_move_commands(
    anchor: NativePosition,
    picture_id: str,
    destination_list_id: int,
) -> tuple[NativeActionCommand, ...]:
    """그림 한 장을 다른 셀로 옮기는 명령. 하나 자르고 하나 붙인다.

    한 번의 호출이 정확히 한 번의 Cut 과 한 번의 Paste 만 낸다. 한/글 COM 경로의
    클립보드는 한 칸짜리라, 여러 장을 연달아 자른 뒤 몰아서 붙이면 전부 마지막
    한 장이 나오고 앞의 것들은 영영 사라진다. 여러 장을 옮기는 일은 이 도구를
    여러 번 부르는 것으로만 할 수 있고, 그래서 그 사고가 구조적으로 생기지 않는다.

    MOVE_POSITION 이 SELECT_CONTROL 보다 먼저인 것도 실측 결과다. 캐럿이 그림이
    든 list 밖에 있으면 SelectCtrl 이 아무것도 고르지 않아 뒤따르는 명령이
    엉뚱한 자리에서 실행된다.
    """
    return (
        MovePositionCommand(anchor),
        SelectControlCommand(picture_id),
        RunCommand("Cut"),
        MovePositionCommand(NativePosition(destination_list_id, 0, 0)),
        RunCommand("Paste"),
    )


def picture_copy_commands(
    anchor: NativePosition,
    picture_id: str,
    destination_list_id: int,
) -> tuple[NativeActionCommand, ...]:
    """원본 개체는 남기고 한/글 자체 Copy/Paste로 그림을 복제한다."""
    return (
        MovePositionCommand(anchor),
        SelectControlCommand(picture_id),
        RunCommand("Copy"),
        MovePositionCommand(NativePosition(destination_list_id, 0, 0)),
        RunCommand("Paste"),
    )


def picture_frame_size_commands(
    anchor: NativePosition,
    picture_id: str,
    width_mm: float,
    height_mm: float,
) -> tuple[NativeActionCommand, ...]:
    """그림 데이터에는 손대지 않고 복제된 개체의 프레임 크기만 정한다."""
    return (
        MovePositionCommand(anchor),
        SelectControlCommand(picture_id),
        ParameterActionCommand(
            "ShapeObjDialog",
            "HShapeObject",
            (NativeSetter("HSet/ProtectSize", BooleanValue(False)),),
        ),
        ParameterActionCommand(
            "ShapeObjDialog",
            "HShapeObject",
            (
                NativeSetter("HSet/WidthRelTo", IntegerValue(4)),
                NativeSetter("HSet/Width", MillimeterValue(width_mm)),
                NativeSetter("HSet/HeightRelTo", IntegerValue(2)),
                NativeSetter("HSet/Height", MillimeterValue(height_mm)),
            ),
        ),
    )


def picture_crop_commands(
    anchor: NativePosition,
    picture_id: str,
    skip: PictureSkip,
) -> tuple[NativeActionCommand, ...]:
    """한/글 자르기를 네 변 모두 한 번에 건다.

    ``ShapeDrawImageAttr/SkipLeft`` 같은 경로는 C++/ATL 의 ApplySetter 가
    ``HShapeObject.ShapeDrawImageAttr`` 를 거쳐 ``SkipLeft`` 를 넣는 것으로
    풀린다(ActionExecutor.cpp:617-633, 928). 캡션 위치를 고치는
    ``ShapeCaption/Side`` 와 같은 길이다.
    """
    return (
        MovePositionCommand(anchor),
        SelectControlCommand(picture_id),
        ParameterActionCommand(
            "ShapeObjDialog",
            "HShapeObject",
            tuple(
                NativeSetter(path, IntegerValue(value))
                for path, value in zip(
                    _SKIP_PATHS,
                    (skip.left, skip.top, skip.right, skip.bottom),
                    strict=True,
                )
            ),
        ),
    )


def moved_picture_id(
    before_ids: frozenset[str],
    page_after: NativePageInspection,
    destination_list_id: int,
) -> str | None:
    """이사한 뒤의 새 개체 ID.

    Copy/Paste와 Cut/Paste는 개체 ID를 반드시 바꾼다(실측: 1175686554 ->
    1175686562). 목적지 칸에서 전에 없던 ID가 정확히 하나일 때만 그것을 고른다.
    같은 칸 복사의 낡은 스냅샷에는 원본 하나만 보이므로 그 ID를 새 개체로 오인하면
    안 된다.
    """
    landed = pictures_in_list(page_after, destination_list_id)
    fresh = tuple(picture for picture in landed if picture not in before_ids)
    return fresh[0] if len(fresh) == 1 else None


def _confirm_transferred_picture(
    request: PictureEditRequest,
    page: NativePageInspection,
    before_ids: frozenset[str],
    destination_table_id: str,
    destination_address: str,
    destination_list_id: int,
) -> tuple[NativePageInspection, str | None, int]:
    """붙여 넣은 새 ID를 찾되 첫 스냅샷 성공 경로에는 조회를 더하지 않는다."""
    landed = moved_picture_id(before_ids, page, destination_list_id)
    if landed is not None:
        return page, landed, 0

    deadline = monotonic() + (_CONFIRM_REINSPECTION_DEADLINE_MS / 1_000)
    reinspections = 0
    while reinspections < _CONFIRM_REINSPECTION_LIMIT:
        if reinspections and monotonic() >= deadline:
            break
        hint_page = _inspect(request, page.page)
        reinspections += 1
        cache = {hint_page.page: hint_page}
        current_page, current_cell, _ = _destination_cell_in_document(
            request,
            hint_page,
            cache,
            destination_table_id,
            destination_address,
        )
        if current_page is not None and current_cell is not None:
            page = current_page
            destination_list_id = current_cell.list_id
        else:
            page = hint_page
        landed = moved_picture_id(before_ids, page, destination_list_id)
        if landed is not None:
            break
    return page, landed, reinspections


def _copy_action_failed(error: HwpLiveError) -> bool:
    return "ACTION_FAILED Copy: Copy returned false" in str(error)


def _execute_picture_transfer(
    request: PictureEditRequest,
    commands: tuple[NativeActionCommand, ...],
    *,
    copied: bool,
) -> tuple[int, int, int, int, bool, object]:
    """첫 Copy 거부 때만 새로 선택한 뒤 같은 한 장을 한 번 더 시도한다."""
    if not copied:
        return _execute(request.candidate, commands)
    try:
        copied_result = _execute(request.candidate, commands[:3])
    except HwpLiveError as error:
        if not _copy_action_failed(error):
            raise
        copied_result = _execute(request.candidate, commands[:3])
    pasted_result = _execute(request.candidate, commands[3:])
    return (
        copied_result[0] + pasted_result[0],
        copied_result[1] + pasted_result[1],
        *pasted_result[2:],
    )


def _geometry(
    request: PictureEditRequest,
    page: NativePageInspection,
    picture_id: str,
) -> PictureGeometry | None:
    read = read_picture_geometry(request.hwp, picture_id)
    if read is None:
        return None
    original, skip, box_width, box_height = read
    table_id, address = cell_of_picture(page, picture_id)
    return PictureGeometry(
        picture_id=picture_id,
        table_id=table_id,
        cell=address,
        original_width_hwpunit=original.width_hwpunit,
        original_height_hwpunit=original.height_hwpunit,
        box_width_hwpunit=box_width,
        box_height_hwpunit=box_height,
        crop=crop_readback(skip, original),
    )


def _inspect(request: PictureEditRequest, page_number: int) -> NativePageInspection:
    page = inspect_native_page(
        request.candidate.window_handle, page_number, include_cells=True
    )
    if page is None:
        raise HwpLiveError("그림 편집 대상 쪽의 구조를 읽지 못했습니다")
    return page


def _document_pages(
    request: PictureEditRequest,
    first_page: NativePageInspection,
    cache: dict[int, NativePageInspection],
) -> Iterator[NativePageInspection]:
    """힌트와 가까운 쪽부터 찾고, 같은 검사를 되풀이하지 않는다."""
    yield first_page
    pages = sorted(
        (
            page
            for page in range(1, first_page.page_count + 1)
            if page != first_page.page
        ),
        key=lambda page: (abs(page - first_page.page), page < first_page.page),
    )
    for page_number in pages:
        page = cache.get(page_number)
        if page is None:
            page = _inspect(request, page_number)
            cache[page_number] = page
        yield page


def _picture_page(
    request: PictureEditRequest,
    first_page: NativePageInspection,
    cache: dict[int, NativePageInspection],
    picture_id: str,
) -> NativePageInspection | None:
    for page in _document_pages(request, first_page, cache):
        if any(
            control.instance_id == picture_id
            and is_picture_control_type(control.control_type)
            for control in page.controls
        ):
            return page
    return None


def _destination_cell_in_document(
    request: PictureEditRequest,
    first_page: NativePageInspection,
    cache: dict[int, NativePageInspection],
    table_id: str,
    address: str,
) -> tuple[NativePageInspection | None, NativePageCell | None, bool]:
    table_found = False
    for page in _document_pages(request, first_page, cache):
        cell = destination_cell(page, table_id, address)
        if cell is not None:
            return page, cell, True
        table_found = (
            table_found
            or any(candidate.table_instance_id == table_id for candidate in page.cells)
            or any(
                control.control_type == "tbl" and control.instance_id == table_id
                for control in page.controls
            )
        )
    return None, None, table_found


def _selection_snapshot(request: PictureEditRequest) -> NativeSnapshot | None:
    target = request.target
    if target is None or target.binding != "selection":
        return None
    snapshot = read_native_snapshot(request.candidate.window_handle)
    if snapshot is None:
        raise HwpLiveError("한컴 네이티브 선택 상태를 읽지 못했습니다")
    return snapshot


def _resolve_picture(
    request: PictureEditRequest,
    page: NativePageInspection,
) -> str | ControlTargetFailure | None:
    """개체 ID -> 표·칸 -> 지금 고른 그림 순으로 대상을 찾는다.

    셋 다 아니면 ``None``. 아무 그림도 지목되지 않았다는 뜻이고, 호출부가
    무엇을 달라고 말한다. 대상이 여럿이면 실패가 아니라 후보를 실은
    ControlTargetFailure 가 돌아온다 -- image.replace 와 같은 해석기다.
    """
    target = request.target
    if target is not None and target.control_instance_id:
        return target.control_instance_id
    table_id = _text(request.parameters, SOURCE_TABLE_PARAMETER)
    address = _text(request.parameters, SOURCE_CELL_PARAMETER)
    if table_id is not None and address is not None:
        return picture_in_cell(page, table_id, address)
    if target is None:
        return None
    resolved = resolve_control_target(
        ControlTargetRequest(
            request.routing_page,
            target,
            PICTURE_CONTROL_TYPES,
            _selection_snapshot(request),
            request.candidate.window_handle,
        )
    )
    if isinstance(resolved, ResolvedObjectControl):
        return resolved.instance_id
    return resolved


def crop_notice(skip: PictureSkip, original: PictureOriginalSize) -> tuple[str, ...]:
    if not skip.any_crop:
        return ()
    width_share, height_share = visible_fraction(skip, original)
    return (
        f"자르기를 걸어 원본의 가로 {width_share:.0%}, 세로 {height_share:.0%}만 "
        "보입니다. 그림 상자 크기는 그대로라 남은 부분이 그 상자를 채웁니다.",
    )


def copy_notices(
    occupants: tuple[str, ...], new_id: str, source_id: str
) -> tuple[str, ...]:
    notices = [
        f"원본 그림 {source_id}은 그대로 두고 원본 내장 바이트를 복제한 새 그림 "
        f"{new_id}을 만들었습니다."
    ]
    if occupants:
        notices.append(
            f"목적지 칸에는 이미 그림 {len(occupants)}장이 있었습니다: "
            + ", ".join(occupants)
            + ". 겹쳐 놓았습니다."
        )
    return tuple(notices)


def move_notices(
    occupants: tuple[str, ...], new_id: str, old_id: str
) -> tuple[str, ...]:
    notices = [
        f"이사하면서 개체 ID가 {old_id}에서 {new_id}로 바뀌었습니다. "
        "이 그림을 다시 지목할 때는 새 ID를 쓰세요."
    ]
    if occupants:
        notices.append(
            f"목적지 칸에는 이미 그림 {len(occupants)}장이 있었습니다: "
            + ", ".join(occupants)
            + ". 겹쳐 놓았습니다."
        )
    return tuple(notices)


def operate_picture_edit_recipe(request: PictureEditRequest) -> OperationResult | None:
    resolution = request.resolution
    if resolution.workflow_id != PICTURE_EDIT_WORKFLOW:
        return None
    if request.resolve_only:
        return _result(resolution, "resolved", "그림 편집 recipe를 확정했습니다")
    if not request.allow_document_change:
        return _result(
            resolution, "confirmation_required", "문서의 그림을 변경하는 작업입니다"
        )
    target = request.target
    page_number = (
        target.page_hint
        if target is not None and target.page_hint is not None
        else request.routing_page.page
    )
    first_page = _inspect(request, page_number)
    page_cache = {first_page.page: first_page}
    resolved_picture = _resolve_picture(request, first_page)
    if isinstance(resolved_picture, ControlTargetFailure):
        return _result(
            resolution,
            resolved_picture.status,
            resolved_picture.message,
            required_inputs=resolved_picture.required_inputs,
        )
    picture_id = resolved_picture
    if picture_id is None:
        source_table = _text(request.parameters, SOURCE_TABLE_PARAMETER)
        source_address = _text(request.parameters, SOURCE_CELL_PARAMETER)
        if source_table is not None and source_address is not None:
            for candidate_page in _document_pages(request, first_page, page_cache):
                picture_id = picture_in_cell(
                    candidate_page, source_table, source_address
                )
                if picture_id is not None:
                    break
    if picture_id is None:
        return _result(
            resolution,
            "needs_input",
            "편집할 그림을 지목해야 합니다. hwp_inspect_page_fast(include_cells=true)의 "
            "instance_id를 target_id로 주거나, parent_table_instance_id와 "
            "parent_cell_address를 함께 주세요",
            required_inputs=(
                "inputs.target.control_instance_id",
                f"inputs.parameters.{SOURCE_TABLE_PARAMETER}",
                f"inputs.parameters.{SOURCE_CELL_PARAMETER}",
            ),
        )
    page = _picture_page(request, first_page, page_cache, picture_id)
    if page is None:
        return _result(
            resolution,
            "not_found",
            f"그림 개체 {picture_id}을 문서에서 찾지 못했습니다",
            required_inputs=("inputs.target.control_instance_id",),
        )
    before = _geometry(request, page, picture_id)
    if before is None:
        return _result(
            resolution,
            "not_found",
            f"그림 개체 {picture_id}의 원본 크기를 읽지 못했습니다. 찾은 개체가 "
            "그림이 아닐 수 있습니다",
            required_inputs=("inputs.target.control_instance_id",),
        )
    anchor = next(
        control.anchor for control in page.controls if control.instance_id == picture_id
    )
    copy_destination = _text(request.parameters, COPY_DESTINATION_CELL_PARAMETER)
    copied = copy_destination is not None
    destination_address = copy_destination or _text(
        request.parameters, DESTINATION_CELL_PARAMETER
    )
    destination_table_parameter = (
        COPY_DESTINATION_TABLE_PARAMETER if copied else DESTINATION_TABLE_PARAMETER
    )
    destination_cell_parameter = (
        COPY_DESTINATION_CELL_PARAMETER if copied else DESTINATION_CELL_PARAMETER
    )
    destination_table = (
        _text(request.parameters, destination_table_parameter) or before.table_id
    )
    notices: list[str] = []
    occupants: tuple[str, ...] = ()
    executed = 0
    elapsed = 0
    current_page = page.page
    page_count = page.page_count
    modified = False
    moved = False
    current_id = picture_id

    if destination_address is not None:
        if destination_table is None:
            verb = "복사할" if copied else "옮길"
            return _result(
                resolution,
                "needs_input",
                f"{verb} 표를 찾지 못했습니다. 그림이 표 안에 있지 않으면 "
                f"{destination_table_parameter}로 대상 표를 지목하세요",
                required_inputs=(f"inputs.parameters.{destination_table_parameter}",),
            )
        destination_page, cell, table_found = _destination_cell_in_document(
            request,
            first_page,
            page_cache,
            destination_table,
            destination_address,
        )
        if cell is None or destination_page is None:
            if table_found:
                message = (
                    f"표 {destination_table}은 문서에 있지만 "
                    f"{destination_address} 칸이 없습니다"
                )
                required_inputs = (f"inputs.parameters.{destination_cell_parameter}",)
            else:
                message = f"표 {destination_table}을 문서에서 찾지 못했습니다"
                required_inputs = (f"inputs.parameters.{destination_table_parameter}",)
            return _result(
                resolution,
                "not_found",
                message,
                required_inputs=required_inputs,
            )
        occupants = pictures_in_list(destination_page, cell.list_id)
        before_ids = frozenset(
            control.instance_id
            for control in destination_page.controls
            if is_picture_control_type(control.control_type)
        )
        transfer_commands = (
            picture_copy_commands(anchor, picture_id, cell.list_id)
            if copied
            else picture_move_commands(anchor, picture_id, cell.list_id)
        )
        try:
            (
                transfer_executed,
                transfer_elapsed,
                current_page,
                page_count,
                modified,
                _,
            ) = _execute_picture_transfer(request, transfer_commands, copied=copied)
        except HwpLiveError as error:
            if copied and _copy_action_failed(error):
                return _result(
                    resolution,
                    "known_failure",
                    "그림 Copy가 재선택 후에도 실패했습니다. 원본 그림은 그대로 있습니다. "
                    "같은 인자로 새 operation_id로 재시도하세요. 원본 표를 삭제하지 마세요.",
                ).model_copy(
                    update={
                        "commands_executed": executed,
                        "modified": False,
                        "resolved_target_id": picture_id,
                    }
                )
            raise
        executed += transfer_executed
        elapsed += transfer_elapsed
        page = _inspect(request, destination_page.page)
        page, landed, confirmation_reinspections = _confirm_transferred_picture(
            request,
            page,
            before_ids,
            destination_table,
            destination_address,
            cell.list_id,
        )
        if landed is None:
            verb = "복사" if copied else "옮기기"
            return _result(
                resolution,
                "partial_change",
                f"그림을 {destination_address} 칸으로 {verb} 명령은 실행했지만 "
                "새 개체를 다시 찾지 못했습니다. "
                f"{_CONFIRM_REINSPECTION_DEADLINE_MS}ms 마감 안에서 "
                f"{confirmation_reinspections}회 재검사했습니다. 문서를 확인하세요",
            ).model_copy(
                update={
                    "partial_change": True,
                    "commands_executed": executed,
                    "modified": modified,
                    "resolved_target_id": picture_id,
                }
            )
        if copied:
            source_after = _picture_page(
                request,
                page,
                {page.page: page},
                picture_id,
            )
            source_after_geometry = (
                None
                if source_after is None
                else _geometry(request, source_after, picture_id)
            )
            if source_after_geometry != before:
                return _result(
                    resolution,
                    "partial_change",
                    "그림 복사 명령 뒤 원본 개체가 그대로인지 확인하지 못했습니다. "
                    "문서를 확인하세요",
                ).model_copy(
                    update={
                        "partial_change": True,
                        "commands_executed": executed,
                        "modified": modified,
                        "resolved_target_id": landed,
                    }
                )
            notices.extend(copy_notices(occupants, landed, picture_id))
        else:
            moved = True
            notices.extend(move_notices(occupants, landed, current_id))
        current_id = landed
        anchor_after = next(
            (
                control.anchor
                for control in page.controls
                if control.instance_id == current_id
            ),
            None,
        )
        if anchor_after is None:
            raise HwpLiveError("붙여 넣은 그림의 앵커 위치를 읽지 못했습니다")
        anchor = anchor_after

        width = None if request.recipe is None else request.recipe.picture_width_mm
        height = None if request.recipe is None else request.recipe.picture_height_mm
        if copied and width is not None and height is not None:
            size_executed, size_elapsed, current_page, page_count, modified, _ = (
                _execute(
                    request.candidate,
                    picture_frame_size_commands(anchor, current_id, width, height),
                )
            )
            executed += size_executed
            elapsed += size_elapsed
            hint_page = _inspect(request, page.page)
            current_destination_page, current_cell, _ = _destination_cell_in_document(
                request,
                hint_page,
                {hint_page.page: hint_page},
                destination_table,
                destination_address,
            )
            if current_destination_page is None or current_cell is None:
                raise HwpLiveError(
                    "크기를 바꾼 목적지 표의 현재 위치를 찾지 못했습니다"
                )
            page = current_destination_page

    cropped = False
    if requested_crop(request.parameters):
        # 자르기는 이사 뒤에 건다. Cut/Paste 가 개체 ID 를 바꾸므로, 먼저 잘라
        # 두면 그 자르기가 옮겨진 개체에 남아 있는지 따로 확인해야 한다. 순서를
        # 뒤집으면 마지막 상태의 개체에 직접 걸고 그대로 되읽으면 된다.
        geometry_now = _geometry(request, page, current_id)
        if geometry_now is None:
            raise HwpLiveError("자르기 대상 그림의 원본 크기를 읽지 못했습니다")
        original = PictureOriginalSize(
            geometry_now.original_width_hwpunit,
            geometry_now.original_height_hwpunit,
        )
        skip = skips_from_ratios(
            crop_ratios_from_parameters(request.parameters), original
        )
        crop_executed, crop_elapsed, current_page, page_count, modified, _snapshot = (
            _execute(request.candidate, picture_crop_commands(anchor, current_id, skip))
        )
        executed += crop_executed
        elapsed += crop_elapsed
        cropped = skip.any_crop
        notices.extend(crop_notice(skip, original))
        page = _inspect(request, page.page)

    after = _geometry(request, page, current_id)
    if after is None:
        raise HwpLiveError("편집한 그림의 결과를 되읽지 못했습니다")
    if not copied and not moved and not requested_crop(request.parameters):
        return _result(
            resolution,
            "needs_input",
            "무엇을 바꿀지 주세요. crop으로 자르거나 move_to_cell로 다른 칸에 "
            "옮길 수 있습니다",
            required_inputs=(
                "inputs.parameters.crop_left",
                f"inputs.parameters.{DESTINATION_CELL_PARAMETER}",
            ),
        ).model_copy(
            update={"picture_edit": PictureEditEvidence(before=before, after=after)}
        )
    evidence = PictureEditEvidence(
        before=before,
        after=after,
        moved=moved,
        cropped=cropped,
        destination_occupants=occupants,
        notices=tuple(notices),
    )
    return _result(
        resolution,
        "executed",
        _message(evidence, copied=copied),
    ).model_copy(
        update={
            "execution_mode": "native_in_process",
            "native_protocol": 9,
            "verification": "native_operation_specific_readback",
            "verified": True,
            "changed": True,
            "commands_executed": executed,
            "native_elapsed_microseconds": elapsed,
            "current_page": current_page,
            "page_count": page_count,
            "modified": modified,
            "resolved_target_id": current_id,
            "target_resolution_basis": (
                "explicit_control_instance_id"
                if request.target is not None and request.target.control_instance_id
                else "table_cell_lookup"
            ),
            "created_control_ids": (current_id,) if moved or copied else (),
            "picture_edit": evidence,
            "changed_pages": (current_page,),
        }
    )


def _message(evidence: PictureEditEvidence, *, copied: bool = False) -> str:
    parts: list[str] = []
    if copied:
        parts.append(
            f"원본 그림을 그대로 두고 {evidence.after.cell or '새 자리'} 칸에 복제했습니다"
        )
    elif evidence.moved:
        parts.append(
            f"그림을 {evidence.before.cell or '원래 자리'}에서 "
            f"{evidence.after.cell or '새 자리'} 칸으로 옮겼습니다"
        )
    if evidence.cropped:
        crop = evidence.after.crop
        parts.append(
            "자르기를 걸었습니다 "
            f"(왼쪽 {crop.left:.0%}, 위 {crop.top:.0%}, "
            f"오른쪽 {crop.right:.0%}, 아래 {crop.bottom:.0%})"
        )
    elif evidence.before.crop.any_crop and not evidence.after.crop.any_crop:
        parts.append("자르기를 풀어 원본 전체가 보입니다")
    if not parts:
        parts.append("그림을 편집했습니다")
    return ". ".join(parts) + f". 지금 개체 ID는 {evidence.after.picture_id}입니다"
