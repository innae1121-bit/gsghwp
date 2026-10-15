from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass(frozen=True, slots=True)
class NativePosition:
    list_id: int
    paragraph: int
    character: int


@dataclass(frozen=True, slots=True)
class NativeSelection:
    selected: bool
    start: NativePosition
    end: NativePosition
    mode: int = 0
    cell_addresses: tuple[str, ...] = ()
    cell_address_error: str = ""

    @property
    def base_mode(self) -> int:
        return self.mode & 0x0F

    @property
    def strict(self) -> bool:
        return bool(self.mode & 0x10)


@dataclass(frozen=True, slots=True)
class NativeCharacterFormat:
    face_name: str
    height_hwpunit: int
    bold: bool
    text_color: int


@dataclass(frozen=True, slots=True)
class PreparedTextPatchTarget:
    selection: NativeSelection
    text: str
    character_format: NativeCharacterFormat
    alignment: int
    line_spacing: int


@dataclass(frozen=True, slots=True)
class NativeParagraphFormat:
    alignment: int
    line_spacing: int
    left_margin_hwpunit: int
    right_margin_hwpunit: int
    indentation_hwpunit: int
    previous_spacing_hwpunit: int
    next_spacing_hwpunit: int
    heading_type: int | None = None
    heading_level: int | None = None
    marker_is_automatic: bool | None = None
    manual_marker_value: str | None = None


@dataclass(frozen=True, slots=True)
class IntegerValue:
    value: int


@dataclass(frozen=True, slots=True)
class BooleanValue:
    value: bool


@dataclass(frozen=True, slots=True)
class TextValue:
    value: str


@dataclass(frozen=True, slots=True)
class MillimeterValue:
    value: float


@dataclass(frozen=True, slots=True)
class EnumerationValue:
    converter: str
    value: str


type NativeActionValue = (
    IntegerValue | BooleanValue | TextValue | MillimeterValue | EnumerationValue
)


@dataclass(frozen=True, slots=True)
class NativeSetter:
    path: str
    value: NativeActionValue


@dataclass(frozen=True, slots=True)
class NativeArray:
    name: str
    count: int


@dataclass(frozen=True, slots=True)
class NativeArrayValue:
    name: str
    index: int
    value: NativeActionValue


@dataclass(frozen=True, slots=True)
class RunCommand:
    action: str


@dataclass(frozen=True, slots=True)
class ParameterActionCommand:
    action: str
    parameter_set: str
    setters: tuple[NativeSetter, ...] = ()
    arrays: tuple[NativeArray, ...] = ()
    array_values: tuple[NativeArrayValue, ...] = ()


@dataclass(frozen=True, slots=True)
class CallCommand:
    method: str
    arguments: tuple[NativeActionValue, ...] = ()


@dataclass(frozen=True, slots=True)
class MovePageCommand:
    page: int


@dataclass(frozen=True, slots=True)
class MovePositionCommand:
    position: NativePosition


@dataclass(frozen=True, slots=True)
class SelectControlCommand:
    instance_id: str


@dataclass(frozen=True, slots=True)
class DeleteControlCommand:
    instance_id: str


@dataclass(frozen=True, slots=True)
class CopyControlCommand:
    instance_id: str


@dataclass(frozen=True, slots=True)
class SaveDocumentFileCommand:
    path: Path


@dataclass(frozen=True, slots=True)
class CaptureDocumentBlockProbeCommand:
    """QA 전용 읽기 전용 계측. 문서를 바꾸지 않는다.

    인코딩 블록 레이아웃이 실제로 어느 크기까지 감당하는지 아무도 재지 않았다.
    캡처는 빈 문자열로 실패하는데, 그 지점이 어디인지는 추정만 있었다. 이 명령은
    엔진에게 블록을 달라고 해서 **문자 수만 응답에 싣고** 블록 자체는 지정한
    파일로 흘려보낸다.
    """

    path: Path


@dataclass(frozen=True, slots=True)
class RestoreDocumentFileCommand:
    path: Path
    expected_page_count: int


@dataclass(frozen=True, slots=True)
class ApplyCopiedTableAnchorCommand:
    instance_id: str


@dataclass(frozen=True, slots=True)
class PasteTableCommand:
    pass


@dataclass(frozen=True, slots=True)
class CaptureTableCommand:
    pass


@dataclass(frozen=True, slots=True)
class MoveDocumentEndCommand:
    pass


@dataclass(frozen=True, slots=True)
class DeleteTailCommand:
    start: NativePosition
    expected_prefix: str


@dataclass(frozen=True, slots=True)
class InsertTextCommand:
    text: str


@dataclass(frozen=True, slots=True)
class ReplaceSelectionCommand:
    expected_text: str
    replacement: str


@dataclass(frozen=True, slots=True)
class LiteralTextRange:
    """UTF-16 offsets in inspected paragraph text, never native cursor offsets."""

    paragraph_text: str
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class TextPatchCommand:
    target: Literal["current", "range", "find", "table_cell"]
    expected_text: str | None
    replacement: str
    start: NativePosition | None = None
    end: NativePosition | None = None
    occurrence: int | None = None
    match_case: bool = False
    table_instance_id: str | None = None
    cell_address: str | None = None
    preserve_format: bool = False
    preflight_only: bool = False
    literal_range: LiteralTextRange | None = None


@dataclass(frozen=True, slots=True)
class PictureCrop:
    """한/글 자르기로 감출 원본 가장자리 비율.

    네 값은 모두 0 이상 1 미만이고 마주 보는 두 변의 합이 1 미만이다. 길이가
    아니라 비율인 이유는 한/글 SkipLeft/SkipTop/SkipRight/SkipBottom 이 쓰는
    단위가 그림 개체의 OriginalSizeX/OriginalSizeY 이고, 그 값은 실행 중에
    C++/ATL 브리지만 읽을 수 있기 때문이다. 원본 파일은 읽기만 한다.
    """

    left: float = 0.0
    top: float = 0.0
    right: float = 0.0
    bottom: float = 0.0


@dataclass(frozen=True, slots=True)
class InsertPictureCommand:
    path: Path
    width_mm: float | None = None
    height_mm: float | None = None
    crop: PictureCrop | None = None


@dataclass(frozen=True, slots=True)
class CellCommand:
    address: str


@dataclass(frozen=True, slots=True)
class SetCellTextCommand:
    address: str
    text: str
    expected_text: str | None = None
    preserve_style: bool = False


@dataclass(frozen=True, slots=True)
class MergeCommand:
    first: str
    second: str


@dataclass(frozen=True, slots=True)
class CaptionCommand:
    style_id: int
    title: str
    character_format: NativeCharacterFormat | None = None
    paragraph_format: NativeParagraphFormat | None = None
    format_source: NativePosition | None = None


@dataclass(frozen=True, slots=True)
class LeaveTableCommand:
    pass


type NativeActionCommand = (
    RunCommand
    | ParameterActionCommand
    | CallCommand
    | MovePageCommand
    | MovePositionCommand
    | SelectControlCommand
    | DeleteControlCommand
    | CopyControlCommand
    | SaveDocumentFileCommand
    | CaptureDocumentBlockProbeCommand
    | RestoreDocumentFileCommand
    | ApplyCopiedTableAnchorCommand
    | PasteTableCommand
    | CaptureTableCommand
    | MoveDocumentEndCommand
    | DeleteTailCommand
    | InsertTextCommand
    | ReplaceSelectionCommand
    | TextPatchCommand
    | InsertPictureCommand
    | CellCommand
    | SetCellTextCommand
    | MergeCommand
    | CaptionCommand
    | LeaveTableCommand
)


@dataclass(frozen=True, slots=True)
class NativeActionRequest:
    document_id: int
    full_name: str
    commands: tuple[NativeActionCommand, ...]
    expected_cursor: NativePosition | None = None
    expected_selection: NativeSelection | None = None
    atomic: bool = False
