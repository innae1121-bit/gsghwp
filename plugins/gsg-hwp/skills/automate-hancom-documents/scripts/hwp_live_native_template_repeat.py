from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from hwp_errors import HwpLiveError
from hwp_live_native_action_models import (
    CaptionCommand,
    CaptureTableCommand,
    CellCommand,
    CopyControlCommand,
    DeleteControlCommand,
    InsertPictureCommand,
    InsertTextCommand,
    IntegerValue,
    LeaveTableCommand,
    MovePositionCommand,
    NativeActionCommand,
    NativeCharacterFormat,
    NativeParagraphFormat,
    NativePageInspection,
    NativePosition,
    NativeSetter,
    ParameterActionCommand,
    PasteTableCommand,
    RunCommand,
    SelectControlCommand,
)

_CAPTION_SEQUENCE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<prefix>.*[-–—_#\s])(?P<number>\d{1,3})(?P<suffix>\s*)$"
)
_AUTOMATIC_CAPTION_LABEL: Final[re.Pattern[str]] = re.compile(r"^(?:표|그림)\s+")
_DISPLAYED_CAPTION_NUMBER: Final[re.Pattern[str]] = re.compile(
    r"^[<(\[]?\s*(?:표|그림)\s+.*\d\s*[>)\]]?$"
)


def caption_display_number(page_text: str, title: str) -> int | None:
    normalized_title = title.strip()
    if not normalized_title:
        return None
    for raw_line in page_text.splitlines():
        line = raw_line.strip()
        if not line.endswith(normalized_title):
            continue
        prefix = line[: -len(normalized_title)].strip()
        if _DISPLAYED_CAPTION_NUMBER.fullmatch(prefix) is not None:
            numbers = re.findall(r"\d+", prefix)
            if numbers:
                return int(numbers[-1])
    return None


def caption_has_display_number(page_text: str, title: str) -> bool:
    return caption_display_number(page_text, title) is not None


def caption_literal_text(text: str, *, automatic_number: bool) -> str:
    title = text.strip()
    if automatic_number:
        title = _AUTOMATIC_CAPTION_LABEL.sub("", title, count=1).strip()
    return title


def copy_caption_title(title: str, offset: int) -> str:
    matched = _CAPTION_SEQUENCE.fullmatch(title)
    if matched is None:
        return title
    number = matched.group("number")
    advanced = str(int(number) + offset).zfill(len(number))
    return f"{matched.group('prefix')}{advanced}{matched.group('suffix')}"


def _replace_current_caption_commands(
    existing_title: str,
    title: str,
) -> tuple[NativeActionCommand, ...]:
    if not title:
        raise HwpLiveError("새 표 캡션 제목이 비어 있습니다")
    delete_existing: tuple[NativeActionCommand, ...] = (
        (
            *(RunCommand("MoveSelLeft") for _ in existing_title),
            RunCommand("Delete"),
        )
        if existing_title
        else ()
    )
    return (
        RunCommand("ShapeObjAttachCaption"),
        RunCommand("MoveParaEnd"),
        *delete_existing,
        InsertTextCommand(title),
        RunCommand("CloseEx"),
    )


def _shape_copy_paste_command() -> ParameterActionCommand:
    return ParameterActionCommand(
        "ShapeCopyPaste",
        "HShapeCopyPaste",
        (NativeSetter("Type", IntegerValue(2)),),
    )


def _new_table_number_command(number: int) -> ParameterActionCommand:
    if number < 1:
        raise HwpLiveError("새 표 캡션 번호는 1 이상이어야 합니다")
    return ParameterActionCommand(
        "NewNumber",
        "HAutoNum",
        (
            NativeSetter("NumType", IntegerValue(4)),
            NativeSetter("NewNumber", IntegerValue(number)),
        ),
    )


def _recreate_numbered_current_caption_commands(
    existing_title: str,
    title: str,
    display_number: int,
    style_transfer: ParameterActionCommand,
) -> tuple[NativeActionCommand, ...]:
    if not existing_title or not title:
        raise HwpLiveError("새 표 캡션 제목이 비어 있습니다")
    return (
        RunCommand("ShapeObjAttachCaption"),
        _new_table_number_command(display_number),
        RunCommand("MoveParaEnd"),
        *(RunCommand("MoveSelLeft") for _ in existing_title),
        RunCommand("Delete"),
        InsertTextCommand(title),
        *(RunCommand("MoveSelLeft") for _ in title),
        style_transfer,
        RunCommand("CloseEx"),
    )


@dataclass(frozen=True, slots=True)
class NativeCellText:
    address: str
    text: str
    replace: bool = True


@dataclass(frozen=True, slots=True)
class NativeCellPicture:
    address: str
    path: Path
    width_mm: float
    height_mm: float


@dataclass(frozen=True, slots=True)
class NativeTableCopy:
    text_cells: tuple[NativeCellText, ...] = ()
    pictures: tuple[NativeCellPicture, ...] = ()
    delete_rows_from: str | None = None
    delete_row_count: int = 0

    def __post_init__(self) -> None:
        addresses = tuple(item.address.upper() for item in self.text_cells)
        picture_addresses = tuple(item.address.upper() for item in self.pictures)
        if any(not address for address in (*addresses, *picture_addresses)):
            raise HwpLiveError("반복 표 셀 주소가 비어 있습니다")
        if len(addresses) != len(set(addresses)):
            raise HwpLiveError("반복 표 텍스트 셀 주소가 중복되었습니다")
        if len(picture_addresses) != len(set(picture_addresses)):
            raise HwpLiveError("반복 표 그림 셀 주소가 중복되었습니다")
        if any(item.width_mm <= 0 or item.height_mm <= 0 for item in self.pictures):
            raise HwpLiveError("반복 표 그림 배치 영역은 0보다 커야 합니다")
        if self.delete_row_count < 0 or (self.delete_rows_from is None) != (
            self.delete_row_count == 0
        ):
            raise HwpLiveError("반복 표 삭제 행 시작 셀과 행 수가 일치하지 않습니다")


@dataclass(frozen=True, slots=True)
class NativeCaptionProfile:
    style_id: int
    character_format: NativeCharacterFormat
    paragraph_format: NativeParagraphFormat
    format_source: NativePosition | None = None
    automatic_number: bool = False
    display_number: int | None = None


def table_copy_content_commands(
    block: NativeTableCopy,
) -> tuple[NativeActionCommand, ...]:
    commands: list[NativeActionCommand] = []
    if block.delete_rows_from is not None:
        commands.extend(
            (
                CellCommand(block.delete_rows_from),
                RunCommand("TableCellBlock"),
                RunCommand("TableCellBlockExtend"),
                RunCommand("TableColPageDown"),
                RunCommand("TableColEnd"),
                RunCommand("TableDeleteCell"),
            )
        )
    for cell in block.text_cells:
        commands.append(CellCommand(cell.address))
        if cell.replace:
            commands.extend((RunCommand("MoveSelParaEnd"), RunCommand("Delete")))
        commands.append(InsertTextCommand(cell.text))
    for picture in block.pictures:
        commands.extend(
            (
                CellCommand(picture.address),
                InsertPictureCommand(
                    picture.path,
                    picture.width_mm,
                    picture.height_mm,
                ),
            )
        )
    return tuple(commands)


def replace_table_cell_pictures_commands(
    page: NativePageInspection,
    table_control_id: str,
    pictures: tuple[NativeCellPicture, ...],
) -> tuple[NativeActionCommand, ...]:
    if not table_control_id or not pictures:
        raise HwpLiveError("그림 교체 대상 표 또는 그림 목록이 비어 있습니다")
    addresses = tuple(picture.address.upper() for picture in pictures)
    if len(addresses) != len(set(addresses)):
        raise HwpLiveError("그림 교체 셀 주소가 중복되었습니다")
    cells = {
        cell.address.upper(): cell
        for cell in page.cells
        if cell.table_instance_id == table_control_id
    }
    missing = tuple(address for address in addresses if address not in cells)
    if missing:
        raise HwpLiveError(f"그림 교체 셀을 찾지 못했습니다: {', '.join(missing)}")
    target_lists = {cells[address].list_id for address in addresses}
    existing = sorted(
        (
            control
            for control in page.controls
            if control.control_type == "gso" and control.anchor.list_id in target_lists
        ),
        key=lambda control: (control.anchor.list_id, control.instance_id),
    )
    if any(not control.instance_id for control in existing):
        raise HwpLiveError("교체할 기존 그림의 개체 ID를 읽지 못했습니다")
    commands: list[NativeActionCommand] = [
        SelectControlCommand(table_control_id),
        CaptureTableCommand(),
    ]
    commands.extend(DeleteControlCommand(control.instance_id) for control in existing)
    for picture in pictures:
        commands.extend(
            (
                CellCommand(picture.address),
                InsertPictureCommand(
                    picture.path,
                    picture.width_mm,
                    picture.height_mm,
                ),
            )
        )
    return tuple(commands)


def repeat_table_commands(
    source_control_id: str,
    insertion_position: NativePosition,
    blocks: tuple[NativeTableCopy, ...],
) -> tuple[NativeActionCommand, ...]:
    if not source_control_id:
        raise HwpLiveError("반복 표 원본 개체 ID가 비어 있습니다")
    if not blocks:
        raise HwpLiveError("반복 생성할 표 데이터가 없습니다")
    commands: list[NativeActionCommand] = [
        CopyControlCommand(source_control_id),
        MovePositionCommand(insertion_position),
    ]
    for index, block in enumerate(blocks):
        commands.append(PasteTableCommand())
        commands.extend(table_copy_content_commands(block))
        commands.append(LeaveTableCommand())
        if index + 1 < len(blocks):
            commands.append(RunCommand("BreakPage"))
    return tuple(commands)


def clone_table_after_source_commands(
    source_control_id: str,
    copy_count: int,
    *,
    caption_title: str | None = None,
    caption_profile: NativeCaptionProfile | None = None,
) -> tuple[NativeActionCommand, ...]:
    if not source_control_id:
        raise HwpLiveError("반복 표 원본 개체 ID가 비어 있습니다")
    if copy_count < 1 or copy_count > 100:
        raise HwpLiveError("반복 표 복제 수는 1~100이어야 합니다")
    if (caption_title is None) != (caption_profile is None):
        raise HwpLiveError("반복 표 캡션 제목과 원본 서식은 함께 필요합니다")
    commands: list[NativeActionCommand] = []
    style_transfer: ParameterActionCommand | None = None
    if caption_profile is not None:
        if caption_profile.format_source is None:
            raise HwpLiveError("원본 캡션의 문단·글자 모양 위치를 읽지 못했습니다")
        style_transfer = _shape_copy_paste_command()
        commands.extend(
            (
                MovePositionCommand(caption_profile.format_source),
                style_transfer,
                RunCommand("CloseEx"),
            )
        )
    commands.extend(
        (
            CopyControlCommand(source_control_id),
            SelectControlCommand(source_control_id),
            CaptureTableCommand(),
            LeaveTableCommand(),
        )
    )
    for copy_index in range(1, copy_count + 1):
        commands.extend((RunCommand("BreakPage"), PasteTableCommand()))
        if caption_title is not None and caption_profile is not None:
            assert style_transfer is not None
            if (
                not caption_profile.automatic_number
                or caption_profile.display_number is None
            ):
                raise HwpLiveError("원본 표 캡션의 표시 자동번호를 읽지 못했습니다")
            commands.extend(
                _recreate_numbered_current_caption_commands(
                    caption_title,
                    copy_caption_title(caption_title, copy_index),
                    caption_profile.display_number + copy_index,
                    style_transfer,
                )
            )
        commands.append(LeaveTableCommand())
    return tuple(commands)


def replace_table_caption_commands(
    table_control_id: str,
    existing_title: str,
    title: str,
) -> tuple[NativeActionCommand, ...]:
    if not table_control_id:
        raise HwpLiveError("표 캡션 개체 ID가 비어 있습니다")
    return (
        SelectControlCommand(table_control_id),
        CaptureTableCommand(),
        *_replace_current_caption_commands(existing_title, title),
    )


def table_caption_commands(
    table_control_id: str,
    style_id: int,
    title: str,
    character_format: NativeCharacterFormat,
    paragraph_format: NativeParagraphFormat,
    format_source: NativePosition | None = None,
) -> tuple[NativeActionCommand, ...]:
    if not table_control_id or not title:
        raise HwpLiveError("표 캡션 개체 ID 또는 제목이 비어 있습니다")
    return (
        SelectControlCommand(table_control_id),
        CaptureTableCommand(),
        CaptionCommand(
            style_id,
            title,
            character_format,
            paragraph_format,
            format_source,
        ),
    )
