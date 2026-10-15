from __future__ import annotations

import hashlib
from base64 import b64decode, b64encode
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final, assert_never

from hwp_errors import HwpLiveError
from hwp_live_native_action_models import (
    ApplyCopiedTableAnchorCommand,
    BooleanValue,
    CallCommand,
    CaptureTableCommand,
    CaptionCommand,
    CaptureDocumentBlockProbeCommand,
    CellCommand,
    CopyControlCommand,
    DeleteControlCommand,
    DeleteTailCommand,
    EnumerationValue,
    InsertPictureCommand,
    InsertTextCommand,
    IntegerValue,
    LeaveTableCommand,
    MergeCommand,
    MillimeterValue,
    MoveDocumentEndCommand,
    MovePageCommand,
    MovePositionCommand,
    NativeActionCommand,
    NativeActionRequest,
    NativeActionResult,
    NativeActionValue,
    NativeBooleanCallResult,
    NativeCallResult,
    NativeCellBorder,
    NativeCellFormat,
    NativeCharacterFormat,
    NativeControlInspectionError,
    NativeDetailedCaption,
    NativeDetailedCell,
    NativeDetailedControl,
    NativeDetailedInspection,
    NativePageControl,
    NativePageCell,
    NativePageInspection,
    NativeIntegerCallResult,
    NativeParagraphStyle,
    NativeParagraphStyleScan,
    NativePosition,
    NativeParagraphFormat,
    NativeSelection,
    NativeSetter,
    NativeSnapshot,
    NativeTextCallResult,
    NativeVoidCallResult,
    ParameterActionCommand,
    PasteTableCommand,
    ReplaceSelectionCommand,
    RestoreDocumentFileCommand,
    RunCommand,
    SaveDocumentFileCommand,
    SelectControlCommand,
    SetCellTextCommand,
    TextPatchCommand,
    TextValue,
)
from hwp_live_native_action_results import (
    NativeDetailedParagraph,
    NativeUnsupportedRecord,
)


NATIVE_ACTION_PAYLOAD_LIMIT: Final = 8_000_000


@dataclass(frozen=True, slots=True)
class NativeActionFailureEvidence:
    code: str
    location: str
    message: str
    commands_completed: int
    failed_step: str | None
    partial_mutation: bool
    retry_safe: bool
    structure_digest_before: str | None
    structure_digest_after: str | None


class NativeActionFailure(HwpLiveError):
    code: str
    location: str
    message: str
    commands_completed: int
    failed_step: str | None
    partial_mutation: bool
    retry_safe: bool
    structure_digest_before: str | None
    structure_digest_after: str | None

    def __init__(self, evidence: NativeActionFailureEvidence) -> None:
        self.code = evidence.code
        self.location = evidence.location
        self.message = evidence.message
        self.commands_completed = evidence.commands_completed
        self.failed_step = evidence.failed_step
        self.partial_mutation = evidence.partial_mutation
        self.retry_safe = evidence.retry_safe
        self.structure_digest_before = evidence.structure_digest_before
        self.structure_digest_after = evidence.structure_digest_after
        suffix = f" {evidence.location}" if evidence.location else ""
        super().__init__(f"네이티브 액션 {evidence.code}{suffix}: {evidence.message}")


@dataclass(frozen=True, slots=True)
class _DecodedNativeSelection(NativeSelection):
    logical_cell_addresses: tuple[str, ...] = ()
    logical_cell_address_error: str = ""


def decoded_logical_cell_selection(
    selection: NativeSelection,
) -> tuple[tuple[str, ...], str] | None:
    if not isinstance(selection, _DecodedNativeSelection):
        return None
    return selection.logical_cell_addresses, selection.logical_cell_address_error


def _encode(value: str) -> str:
    return b64encode(value.encode("utf-8")).decode("ascii") or " "


def _decode(value: str) -> str:
    if value.isspace():
        return ""
    try:
        return b64decode(value, validate=True).decode("utf-8")
    except (UnicodeDecodeError, ValueError) as error:
        raise HwpLiveError(
            "네이티브 실시간 응답 문자열을 해석하지 못했습니다"
        ) from error


def _plain(value: str, label: str) -> str:
    if not value or any(character in value for character in "\t\r\n"):
        raise HwpLiveError(f"네이티브 실시간 {label} 형식이 올바르지 않습니다")
    return value


def _absolute_path(path: Path) -> str:
    return str(path if path.is_absolute() else path.absolute())


def _integer(value: str, label: str) -> int:
    try:
        return int(value)
    except ValueError as error:
        raise HwpLiveError(
            f"네이티브 실시간 {label} 숫자가 올바르지 않습니다"
        ) from error


def _boolean(value: str, label: str) -> bool:
    if value == "0":
        return False
    if value == "1":
        return True
    raise HwpLiveError(f"네이티브 실시간 {label} 불리언 값이 올바르지 않습니다")


def _value_fields(value: NativeActionValue) -> tuple[str, ...]:
    match value:
        case IntegerValue(value=integer):
            return "I4", str(integer)
        case BooleanValue(value=boolean):
            return "BOOL", "1" if boolean else "0"
        case TextValue(value=text):
            return "BSTR", _encode(text)
        case MillimeterValue(value=millimeter):
            return "MM", format(millimeter, ".10g")
        case EnumerationValue(converter=converter, value=text):
            return "ENUM", _plain(converter, "열거 변환기"), _encode(text)
    assert_never(value)


def _setter_line(prefix: str, setter: NativeSetter) -> str:
    return "\t".join(
        (prefix, _plain(setter.path, "파라미터 경로"), *_value_fields(setter.value))
    )


def _command_lines(command: NativeActionCommand) -> tuple[str, ...]:
    match command:
        case RunCommand(action=action):
            return (f"RUN\t{_plain(action, '액션 이름')}",)
        case ParameterActionCommand(
            action=action,
            parameter_set=parameter_set,
            setters=setters,
            arrays=arrays,
            array_values=array_values,
        ):
            array_counts = {array.name: array.count for array in arrays}
            if len(array_counts) != len(arrays) or any(
                count < 1 for count in array_counts.values()
            ):
                raise HwpLiveError("네이티브 파라미터 배열 정의가 올바르지 않습니다")
            if any(
                item.name not in array_counts
                or item.index < 0
                or item.index >= array_counts[item.name]
                for item in array_values
            ):
                raise HwpLiveError(
                    "네이티브 파라미터 배열 인덱스는 0부터 배열 크기 미만입니다"
                )
            lines = [
                f"ACTION\t{_plain(action, '액션 이름')}\t"
                + _plain(parameter_set, "파라미터셋 이름")
            ]
            lines.extend(_setter_line("SET", setter) for setter in setters)
            lines.extend(
                f"ARRAY\t{_plain(array.name, '배열 이름')}\t{array.count}"
                for array in arrays
            )
            lines.extend(
                "\t".join(
                    (
                        "ASET",
                        _plain(item.name, "배열 이름"),
                        str(item.index),
                        *_value_fields(item.value),
                    )
                )
                for item in array_values
            )
            lines.append("ENDACTION")
            return tuple(lines)
        case CallCommand(method=method, arguments=arguments):
            return (
                f"CALL\t{_plain(method, '메서드 이름')}",
                *("\t".join(("ARG", *_value_fields(value))) for value in arguments),
                "ENDCALL",
            )
        case MovePageCommand(page=page):
            if page < 1:
                raise HwpLiveError("네이티브 이동 쪽은 1 이상이어야 합니다")
            return (f"MOVE_PAGE\t{page}",)
        case MovePositionCommand(position=position):
            if min(position.list_id, position.paragraph, position.character) < 0:
                raise HwpLiveError("네이티브 이동 위치는 음수일 수 없습니다")
            return (
                f"MOVE_POSITION\t{position.list_id}\t{position.paragraph}\t"
                + str(position.character),
            )
        case SelectControlCommand(instance_id=instance_id):
            if not instance_id:
                raise HwpLiveError("네이티브 선택 개체 ID가 비어 있습니다")
            return (f"SELECT_CONTROL\t{_encode(instance_id)}",)
        case DeleteControlCommand(instance_id=instance_id):
            if not instance_id:
                raise HwpLiveError("네이티브 삭제 개체 ID가 비어 있습니다")
            return (f"DELETE_CONTROL\t{_encode(instance_id)}",)
        case CopyControlCommand(instance_id=instance_id):
            if not instance_id:
                raise HwpLiveError("네이티브 복사 개체 ID가 비어 있습니다")
            return (f"COPY_CONTROL\t{_encode(instance_id)}",)
        case SaveDocumentFileCommand(path=path):
            return (f"SAVE_DOCUMENT_FILE\t{_encode(_absolute_path(path))}",)
        case CaptureDocumentBlockProbeCommand(path=path):
            return (f"CAPTURE_DOCUMENT_BLOCK_PROBE\t{_encode(_absolute_path(path))}",)
        case RestoreDocumentFileCommand(
            path=path,
            expected_page_count=expected_page_count,
        ):
            if expected_page_count < 1:
                raise HwpLiveError(
                    "네이티브 복원 문서의 예상 쪽 수는 1 이상이어야 합니다"
                )
            return (
                "RESTORE_DOCUMENT_FILE\t"
                + f"{_encode(_absolute_path(path))}\t{expected_page_count}",
            )
        case ApplyCopiedTableAnchorCommand(instance_id=instance_id):
            if not instance_id:
                raise HwpLiveError("네이티브 표 앵커 적용 개체 ID가 비어 있습니다")
            return (f"APPLY_COPIED_TABLE_ANCHOR\t{_encode(instance_id)}",)
        case PasteTableCommand():
            return ("PASTE_TABLE",)
        case CaptureTableCommand():
            return ("CAPTURE_TABLE",)
        case MoveDocumentEndCommand():
            return ("MOVE_DOC_END",)
        case DeleteTailCommand(start=start, expected_prefix=prefix):
            if not prefix:
                raise HwpLiveError("네이티브 꼬리 교체 확인 문자열이 비어 있습니다")
            return (
                f"DELETE_TAIL\t{start.list_id}\t{start.paragraph}\t"
                + f"{start.character}\t{_encode(prefix)}",
            )
        case InsertTextCommand(text=text):
            return (f"INSERT_TEXT\t{_encode(text)}",)
        case ReplaceSelectionCommand(expected_text=expected, replacement=replacement):
            return (f"REPLACE_SELECTION\t{_encode(expected)}\t{_encode(replacement)}",)
        case TextPatchCommand(
            target=target,
            expected_text=expected,
            replacement=replacement,
            start=start,
            end=end,
            occurrence=occurrence,
            match_case=match_case,
            table_instance_id=table_instance_id,
            cell_address=cell_address,
            preserve_format=preserve_format,
            preflight_only=preflight_only,
            literal_range=literal_range,
        ):
            if literal_range is not None:
                text = literal_range.paragraph_text.encode("utf-16-le")
                left, right = literal_range.start, literal_range.end
                if (
                    not preflight_only
                    or target != "range"
                    or start is None
                    or end is None
                    or (start.list_id, start.paragraph) != (end.list_id, end.paragraph)
                    or not 0 <= left < right <= len(text) // 2
                    or "\r" in literal_range.paragraph_text
                    or "\n" in literal_range.paragraph_text
                    or expected is None
                    or text[2 * left : 2 * right] != expected.encode("utf-16-le")
                ):
                    raise HwpLiveError(
                        "inspected literal range requires read-only preparation"
                    )
                return (
                    "\t".join(
                        (
                            "PREPARE_TEXT",
                            "LITERAL_RANGE",
                            str(start.list_id),
                            str(start.paragraph),
                            str(left),
                            str(right),
                            _encode(literal_range.paragraph_text),
                            _encode(expected),
                            _encode(replacement),
                        )
                    ),
                )
            if target == "current":
                if any(
                    value is not None
                    for value in (
                        start,
                        end,
                        occurrence,
                        table_instance_id,
                        cell_address,
                    )
                ):
                    raise HwpLiveError(
                        "현재 위치 text.patch 대상에 범위·검색·셀 입력을 함께 쓸 수 없습니다"
                    )
                return (
                    "\t".join(
                        (
                            "PATCH_TEXT",
                            "CURRENT",
                            "1" if expected is not None else "0",
                            _encode("" if expected is None else expected),
                            _encode(replacement),
                        )
                        + (("1",) if preserve_format else ())
                    ),
                )
            if preflight_only and (
                expected is not None or target not in {"range", "table_cell"}
            ):
                raise HwpLiveError(
                    "native text.patch preflight command가 올바르지 않습니다"
                )
            if expected is None and target not in {"range", "table_cell"}:
                raise HwpLiveError(
                    "범위·검색·표 셀 text.patch에는 확인할 기존 텍스트가 필요합니다"
                )
            if target == "range":
                if (
                    start is None
                    or end is None
                    or occurrence is not None
                    or table_instance_id is not None
                    or cell_address is not None
                    or start.list_id != end.list_id
                ):
                    raise HwpLiveError("범위 text.patch 좌표가 올바르지 않습니다")
                if preflight_only:
                    return (
                        "\t".join(
                            (
                                "PREPARE_TEXT",
                                "RANGE",
                                str(start.list_id),
                                str(start.paragraph),
                                str(start.character),
                                str(end.list_id),
                                str(end.paragraph),
                                str(end.character),
                                _encode(replacement),
                            )
                        ),
                    )
                return (
                    "\t".join(
                        (
                            "PATCH_TEXT",
                            "RANGE",
                            str(start.list_id),
                            str(start.paragraph),
                            str(start.character),
                            str(end.list_id),
                            str(end.paragraph),
                            str(end.character),
                            _encode("" if expected is None else expected),
                            _encode(replacement),
                        )
                        + (("1",) if preserve_format else ())
                    ),
                )
            occurrence_value = 0 if occurrence is None else occurrence
            if occurrence_value < 0:
                raise HwpLiveError("text.patch 검색 순번은 1 이상이어야 합니다")
            if target == "find":
                if expected is None:
                    raise HwpLiveError("검색 text.patch에는 기존 텍스트가 필요합니다")
                if start is not None or end is not None:
                    raise HwpLiveError(
                        "검색 text.patch 대상에 범위 입력을 함께 쓸 수 없습니다"
                    )
                if (table_instance_id is None) != (cell_address is None):
                    raise HwpLiveError(
                        "표 범위 검색에는 표 ID와 셀 주소가 함께 필요합니다"
                    )
                if table_instance_id is not None and cell_address is not None:
                    return (
                        "\t".join(
                            (
                                "PATCH_TEXT",
                                "FIND",
                                _encode(table_instance_id),
                                _plain(cell_address, "셀 주소").upper(),
                                str(occurrence_value),
                                "1" if match_case else "0",
                                _encode(expected),
                                _encode(replacement),
                            )
                            + (("1",) if preserve_format else ())
                        ),
                    )
                return (
                    "\t".join(
                        (
                            "PATCH_TEXT",
                            "FIND",
                            str(occurrence_value),
                            "1" if match_case else "0",
                            _encode(expected),
                            _encode(replacement),
                        )
                        + (("1",) if preserve_format else ())
                    ),
                )
            if target == "table_cell":
                if (
                    start is not None
                    or end is not None
                    or not table_instance_id
                    or not cell_address
                ):
                    raise HwpLiveError(
                        "표 셀 text.patch에는 표 ID와 셀 주소가 필요합니다"
                    )
                if preflight_only:
                    return (
                        "\t".join(
                            (
                                "PREPARE_TEXT",
                                "CELL",
                                _encode(table_instance_id),
                                _plain(cell_address, "셀 주소").upper(),
                                str(occurrence_value),
                                "1" if match_case else "0",
                                _encode(replacement),
                            )
                        ),
                    )
                return (
                    "\t".join(
                        (
                            "PATCH_TEXT",
                            "CELL",
                            _encode(table_instance_id),
                            _plain(cell_address, "셀 주소").upper(),
                            str(occurrence_value),
                            "1" if match_case else "0",
                            _encode("" if expected is None else expected),
                            _encode(replacement),
                        )
                        + (("1",) if preserve_format else ())
                    ),
                )
            raise HwpLiveError("지원하지 않는 text.patch 대상입니다")
        case InsertPictureCommand(
            path=path,
            width_mm=width,
            height_mm=height,
            crop=crop,
        ):
            if (width is None) != (height is None):
                raise HwpLiveError(
                    "네이티브 그림 배치 영역의 가로와 세로가 함께 필요합니다"
                )
            if width is None or height is None:
                if crop is not None:
                    raise HwpLiveError(
                        "네이티브 그림 자르기에는 배치 영역이 함께 필요합니다"
                    )
                return (f"INSERT_PICTURE\t{_encode(_absolute_path(path))}",)
            if width <= 0 or height <= 0:
                raise HwpLiveError("네이티브 그림 배치 영역은 0보다 커야 합니다")
            box = (
                f"INSERT_PICTURE\t{_encode(_absolute_path(path))}\t"
                + f"{format(width, '.10g')}\t{format(height, '.10g')}"
            )
            if crop is None:
                return (box,)
            edges = (crop.left, crop.top, crop.right, crop.bottom)
            if any(not 0.0 <= edge < 1.0 for edge in edges) or (
                crop.left + crop.right >= 1.0 or crop.top + crop.bottom >= 1.0
            ):
                raise HwpLiveError(
                    "네이티브 그림 자르기 비율은 0 이상 1 미만이고 "
                    + "마주 보는 두 변의 합이 1보다 작아야 합니다"
                )
            return (box + "\t" + "\t".join(format(edge, ".10g") for edge in edges),)
        case CellCommand(address=address):
            return (f"CELL\t{_plain(address, '셀 주소').upper()}",)
        case SetCellTextCommand(
            address=address,
            text=text,
            expected_text=expected,
            preserve_style=preserve_style,
        ):
            if expected is None and not preserve_style:
                return (
                    f"SET_CELL_TEXT\t{_plain(address, '셀 주소').upper()}\t{_encode(text)}",
                )
            if expected is None:
                raise HwpLiveError(
                    "서식 보존 셀 교체에는 확인할 기존 텍스트가 필요합니다"
                )
            return (
                "\t".join(
                    (
                        "SET_CELL_TEXT",
                        _plain(address, "셀 주소").upper(),
                        _encode(expected),
                        _encode(text),
                        "1" if preserve_style else "0",
                    )
                ),
            )
        case MergeCommand(first=first, second=second):
            return (
                f"MERGE\t{_plain(first, '병합 시작 셀').upper()}\t"
                + _plain(second, "병합 끝 셀").upper(),
            )
        case CaptionCommand(
            style_id=style_id,
            title=title,
            character_format=character,
            paragraph_format=paragraph,
            format_source=format_source,
        ):
            if (character is None) != (paragraph is None):
                raise HwpLiveError("표 캡션 글자·문단 서식은 함께 제공해야 합니다")
            if character is None or paragraph is None:
                fields = ["CAPTION", str(style_id), _encode(title)]
                if format_source is not None:
                    fields.extend(
                        (
                            str(format_source.list_id),
                            str(format_source.paragraph),
                            str(format_source.character),
                        )
                    )
                return ("\t".join(fields),)
            if (
                not character.face_name
                or character.height_hwpunit <= 0
                or character.text_color < 0
                or paragraph.alignment not in {0, 1, 2, 3}
                or paragraph.line_spacing <= 0
            ):
                raise HwpLiveError("표 캡션 직접 서식값이 올바르지 않습니다")
            fields = [
                "CAPTION",
                str(style_id),
                _encode(title),
                _encode(character.face_name),
                str(character.height_hwpunit),
                "1" if character.bold else "0",
                str(character.text_color),
                str(paragraph.alignment),
                str(paragraph.line_spacing),
                str(paragraph.left_margin_hwpunit),
                str(paragraph.right_margin_hwpunit),
                str(paragraph.indentation_hwpunit),
                str(paragraph.previous_spacing_hwpunit),
                str(paragraph.next_spacing_hwpunit),
            ]
            if format_source is not None:
                fields.extend(
                    (
                        str(format_source.list_id),
                        str(format_source.paragraph),
                        str(format_source.character),
                    )
                )
            return ("\t".join(fields),)
        case LeaveTableCommand():
            return ("LEAVE_TABLE",)
    assert_never(command)


def _request_prefix_lines(request: NativeActionRequest) -> list[str]:
    lines = ["HCA1", f"DOC\t{request.document_id}\t{_encode(request.full_name)}"]
    if request.atomic:
        lines.append("POLICY\tATOMIC\t1")
    if request.expected_cursor is not None:
        cursor = request.expected_cursor
        lines.append(
            f"EXPECT_CURSOR\t{cursor.list_id}\t{cursor.paragraph}\t{cursor.character}"
        )
    if request.expected_selection is not None:
        selection = request.expected_selection
        lines.append(
            "\t".join(
                (
                    "EXPECT_SELECTION",
                    "1" if selection.selected else "0",
                    str(selection.start.list_id),
                    str(selection.start.paragraph),
                    str(selection.start.character),
                    str(selection.end.list_id),
                    str(selection.end.paragraph),
                    str(selection.end.character),
                )
            )
        )
    return lines


def action_command_payload_characters(command: NativeActionCommand) -> int:
    return sum(len(line) + 1 for line in _command_lines(command))


def action_request_payload_overhead(request: NativeActionRequest) -> int:
    return len("\n".join((*_request_prefix_lines(request), "END")))


def action_request_payload_length(request: NativeActionRequest) -> int:
    return action_request_payload_overhead(request) + sum(
        action_command_payload_characters(command) for command in request.commands
    )


def encode_action_request(request: NativeActionRequest) -> str:
    if request.document_id < 0 or not request.commands:
        raise HwpLiveError("네이티브 실시간 문서 식별값 또는 명령이 올바르지 않습니다")
    lines = _request_prefix_lines(request)
    for command in request.commands:
        lines.extend(_command_lines(command))
    lines.append("END")
    payload = "\n".join(lines)
    if len(payload) > NATIVE_ACTION_PAYLOAD_LIMIT:
        raise HwpLiveError("네이티브 실시간 요청이 8MB 제한을 초과했습니다")
    return payload


def _native_error(fields: list[str]) -> None:
    if len(fields) == 5 and fields[:2] == ["HCA1", "ERROR"]:
        location = _decode(fields[3])
        suffix = f" {location}" if location else ""
        raise HwpLiveError(f"네이티브 액션 {fields[2]}{suffix}: {_decode(fields[4])}")
    if len(fields) == 11 and fields[:2] == ["HCA2", "ERROR"]:
        commands_completed = _integer(fields[5], "완료 명령 건수")
        partial_mutation = _boolean(fields[7], "부분 변경")
        retry_safe = _boolean(fields[8], "재시도 안전성")
        if commands_completed < 0 or (partial_mutation and retry_safe):
            raise HwpLiveError(
                "네이티브 액션 실패 응답의 재시도 증거가 올바르지 않습니다"
            )
        raise NativeActionFailure(
            NativeActionFailureEvidence(
                code=_plain(fields[2], "실패 코드"),
                location=_decode(fields[3]),
                message=_decode(fields[4]),
                commands_completed=commands_completed,
                failed_step=_decode(fields[6]) or None,
                partial_mutation=partial_mutation,
                retry_safe=retry_safe,
                structure_digest_before=_decode(fields[9]) or None,
                structure_digest_after=_decode(fields[10]) or None,
            )
        )
    if len(fields) == 4 and fields[:2] == ["HCI1", "ERROR"]:
        raise HwpLiveError(f"네이티브 조회 {fields[2]}: {_decode(fields[3])}")


def _decode_call_results(payload: str) -> tuple[NativeCallResult, ...]:
    if not payload:
        return ()
    results: list[NativeCallResult] = []
    for record in payload.splitlines():
        fields = record.split("\t")
        if len(fields) < 2:
            raise HwpLiveError("네이티브 CALL 반환 형식이 올바르지 않습니다")
        kind = fields[0]
        method = _plain(_decode(fields[1]), "CALL 메서드 이름")
        if kind == "V" and len(fields) == 2:
            results.append(NativeVoidCallResult(method))
        elif kind == "B" and len(fields) == 3:
            results.append(
                NativeBooleanCallResult(method, _boolean(fields[2], "CALL 반환"))
            )
        elif kind == "I" and len(fields) == 3:
            results.append(
                NativeIntegerCallResult(method, _integer(fields[2], "CALL 반환"))
            )
        elif kind == "S" and len(fields) == 3:
            results.append(NativeTextCallResult(method, _decode(fields[2])))
        else:
            raise HwpLiveError("네이티브 CALL 반환 형식이 올바르지 않습니다")
    return tuple(results)


def decode_action_result(payload: str) -> NativeActionResult:
    fields = payload.split("\t")
    _native_error(fields)
    legacy = len(fields) == 8 and fields[:2] == ["HCA1", "OK"]
    current = len(fields) == 9 and fields[:2] == ["HCA2", "OK"]
    timed = len(fields) == 12 and fields[:2] == ["HCA2", "OK"]
    if not legacy and not current and not timed:
        raise HwpLiveError("네이티브 액션 응답 형식이 올바르지 않습니다")
    counts = (
        _integer(fields[2], "명령 처리 건수"),
        _integer(fields[3], "액션 처리 건수"),
        _integer(fields[4], "텍스트 처리 건수"),
        _integer(fields[5], "그림 처리 건수"),
        _integer(fields[6], "처리 시간"),
    )
    if min(counts) < 0:
        raise HwpLiveError("네이티브 액션 성공 응답에 음수가 있습니다")
    ids = _decode(fields[7])
    call_results = _decode_call_results(_decode(fields[8])) if current or timed else ()
    image_timings = (
        tuple(
            _integer(value, label)
            for value, label in zip(
                fields[9:12],
                ("그림 타이밍 건수", "그림 최대 시간", "그림 총 시간"),
                strict=True,
            )
        )
        if timed
        else (0, 0, 0)
    )
    if timed and (
        min(image_timings) < 0
        or image_timings[0] != counts[3]
        or image_timings[1] > image_timings[2]
    ):
        raise HwpLiveError("네이티브 액션 그림 타이밍 응답이 일관되지 않습니다")
    return NativeActionResult(
        *counts,
        tuple(ids.split(",")) if ids else (),
        call_results,
        *image_timings,
    )


def decode_snapshot(payload: str) -> NativeSnapshot:
    error_fields = payload.split("\t")
    _native_error(error_fields)
    lines = payload.splitlines()
    if len(lines) != 11 or lines[0] != "HCS1" or lines[-1] != "END":
        raise HwpLiveError("네이티브 현재 상태 응답 형식이 올바르지 않습니다")
    document = lines[1].split("\t")
    state = lines[2].split("\t")
    cursor = lines[3].split("\t")
    selection = lines[4].split("\t")
    text = lines[5].split("\t")
    context = lines[6].split("\t")
    style = lines[7].split("\t")
    character = lines[8].split("\t")
    paragraph = lines[9].split("\t")
    legacy_selection = len(selection) == 8 and selection[0] == "SELECTION"
    current_selection = len(selection) == 9 and selection[0] == "SELECTION"
    addressed_selection = len(selection) == 10 and selection[0] == "SELECTION"
    diagnostic_selection = len(selection) == 11 and selection[0] == "SELECTION"
    logical_selection = len(selection) in {12, 13} and selection[0] == "SELECTION"
    if (
        len(document) != 3
        or document[0] != "DOC"
        or len(state) != 4
        or state[0] != "STATE"
        or len(cursor) != 4
        or cursor[0] != "CURSOR"
        or not (
            legacy_selection
            or current_selection
            or addressed_selection
            or diagnostic_selection
            or logical_selection
        )
        or len(text) != 2
        or text[0] != "TEXT"
        or len(context) != 4
        or context[0] != "CONTEXT"
        or len(style) != 2
        or style[0] != "STYLE"
        or len(character) != 5
        or character[0] != "CHAR"
        or len(paragraph) != 8
        or paragraph[0] != "PARA"
    ):
        raise HwpLiveError("네이티브 현재 상태 레코드가 올바르지 않습니다")
    current = NativePosition(*(_integer(value, "커서") for value in cursor[1:]))
    selection_position = 2 if legacy_selection else 3
    selected_cells = (
        tuple(
            address.strip().upper()
            for address in _decode(selection[9]).split(",")
            if address.strip()
        )
        if addressed_selection or diagnostic_selection or logical_selection
        else ()
    )
    logical_selected_cells = (
        tuple(
            address.strip().upper()
            for address in _decode(selection[11]).split(",")
            if address.strip()
        )
        if logical_selection
        else ()
    )
    selected_state = _boolean(selection[1], "선택 상태")
    selected_start = NativePosition(
        *(
            _integer(value, "선택 시작")
            for value in selection[selection_position : selection_position + 3]
        )
    )
    selected_end = NativePosition(
        *(
            _integer(value, "선택 끝")
            for value in selection[selection_position + 3 : selection_position + 6]
        )
    )
    selected_mode = 0 if legacy_selection else _integer(selection[2], "선택 모드")
    selected_error = (
        _decode(selection[10]) if diagnostic_selection or logical_selection else ""
    )
    if logical_selection:
        selected = _DecodedNativeSelection(
            selected=selected_state,
            start=selected_start,
            end=selected_end,
            mode=selected_mode,
            cell_addresses=selected_cells,
            cell_address_error=selected_error,
            logical_cell_addresses=logical_selected_cells,
            logical_cell_address_error=(
                _decode(selection[12]) if len(selection) == 13 else ""
            ),
        )
    else:
        selected = NativeSelection(
            selected=selected_state,
            start=selected_start,
            end=selected_end,
            mode=selected_mode,
            cell_addresses=selected_cells,
            cell_address_error=selected_error,
        )
    return NativeSnapshot(
        document_id=_integer(document[1], "문서 ID"),
        full_name=_decode(document[2]),
        current_page=_integer(state[1], "현재 쪽"),
        page_count=_integer(state[2], "전체 쪽"),
        modified=_boolean(state[3], "수정 상태"),
        cursor=current,
        selection=selected,
        selected_text=_decode(text[1]),
        control_type=_decode(context[1]),
        control_instance_id=_decode(context[2]),
        cell_address=_decode(context[3]),
        style_id=_integer(style[1], "문단 스타일"),
        character_format=NativeCharacterFormat(
            face_name=_decode(character[1]),
            height_hwpunit=_integer(character[2], "글자 크기"),
            bold=_boolean(character[3], "진하게"),
            text_color=_integer(character[4], "글자색"),
        ),
        paragraph_format=NativeParagraphFormat(
            alignment=_integer(paragraph[1], "문단 정렬"),
            line_spacing=_integer(paragraph[2], "줄 간격"),
            left_margin_hwpunit=_integer(paragraph[3], "문단 왼쪽 여백"),
            right_margin_hwpunit=_integer(paragraph[4], "문단 오른쪽 여백"),
            indentation_hwpunit=_integer(paragraph[5], "문단 들여쓰기"),
            previous_spacing_hwpunit=_integer(paragraph[6], "문단 위 간격"),
            next_spacing_hwpunit=_integer(paragraph[7], "문단 아래 간격"),
            heading_type=(
                _integer(paragraph[8], "문단 머리 모양")
                if len(paragraph) > 8 and paragraph[8]
                else None
            ),
            heading_level=(
                _integer(paragraph[9], "문단 번호 수준")
                if len(paragraph) > 9 and paragraph[9]
                else None
            ),
            marker_is_automatic=(
                _boolean(paragraph[10], "자동 번호 여부")
                if len(paragraph) > 10 and paragraph[10]
                else None
            ),
            manual_marker_value=(
                _decode(paragraph[11])[:64]
                if len(paragraph) > 11 and paragraph[11]
                else None
            ),
        ),
    )


# CELLFMT: tableId, address, fill(2), four sides of three, margins(4),
# vertical align, paragraph align, face name, character height, bold.
_CELL_FORMAT_FIELDS = 26


def _absent_when_negative(value: str, label: str) -> int | None:
    """-1 on the wire means the bridge could not read the property.

    Absent is not zero. A fill color of 0 is black; a fill color nobody could
    read is ``None``, and only the first one may be replayed onto a new table.
    The CELL record already spells "unreadable" as -1 for width and height, so
    the appearance record spells it the same way.
    """
    number = _integer(value, label)
    return None if number < 0 else number


def _cell_border(fields: list[str], start: int, label: str) -> NativeCellBorder:
    return NativeCellBorder(
        line_type=_absent_when_negative(fields[start], f"{label} 테두리 종류"),
        width=_absent_when_negative(fields[start + 1], f"{label} 테두리 굵기"),
        color=_absent_when_negative(fields[start + 2], f"{label} 테두리 색"),
    )


def _decode_cell_format(fields: list[str]) -> NativeCellFormat:
    bold = _integer(fields[25], "셀 글자 진하게")
    face_name = _decode(fields[23])
    addresses = tuple(
        address.strip().upper()
        for address in _decode(fields[2]).split(",")
        if address.strip()
    )
    if not addresses:
        raise HwpLiveError("네이티브 셀 서식 주소가 비어 있습니다")
    return NativeCellFormat(
        table_instance_id=_decode(fields[1]),
        addresses=addresses,
        fill_color=_absent_when_negative(fields[3], "셀 채우기 색"),
        fill_brush=_absent_when_negative(fields[4], "셀 채우기 종류"),
        border_left=_cell_border(fields, 5, "셀 왼쪽"),
        border_right=_cell_border(fields, 8, "셀 오른쪽"),
        border_top=_cell_border(fields, 11, "셀 위"),
        border_bottom=_cell_border(fields, 14, "셀 아래"),
        margin_left_hwpunit=_absent_when_negative(fields[17], "셀 왼쪽 여백"),
        margin_right_hwpunit=_absent_when_negative(fields[18], "셀 오른쪽 여백"),
        margin_top_hwpunit=_absent_when_negative(fields[19], "셀 위 여백"),
        margin_bottom_hwpunit=_absent_when_negative(fields[20], "셀 아래 여백"),
        vertical_align=_absent_when_negative(fields[21], "셀 세로 정렬"),
        alignment=_absent_when_negative(fields[22], "셀 문단 정렬"),
        face_name=face_name or None,
        character_height=_absent_when_negative(fields[24], "셀 글자 크기"),
        bold=None if bold < 0 else bold == 1,
    )


def decode_page_inspection(payload: str) -> NativePageInspection:
    error_fields = payload.split("\t")
    _native_error(error_fields)
    lines = payload.splitlines()
    if len(lines) < 4 or lines[0] != "HPI1" or lines[-1] != "END":
        raise HwpLiveError("네이티브 쪽 구조 응답 형식이 올바르지 않습니다")
    document = lines[1].split("\t")
    page = lines[2].split("\t")
    if (
        len(document) != 3
        or document[0] != "DOC"
        or len(page) != 4
        or page[0] != "PAGE"
    ):
        raise HwpLiveError("네이티브 쪽 구조 기본 레코드가 올바르지 않습니다")
    controls: list[NativePageControl] = []
    control_formats: dict[str, tuple[int, NativeParagraphFormat]] = {}
    cells: list[NativePageCell] = []
    cell_formats: list[NativeCellFormat] = []
    inspection_errors: list[NativeControlInspectionError] = []
    for line in lines[3:-1]:
        fields = line.split("\t")
        if len(fields) in {8, 10} and fields[0] == "CTRL":
            rows = _integer(fields[6], "표 행 수")
            columns = _integer(fields[7], "표 열 수")
            width = _integer(fields[8], "개체 너비") if len(fields) == 10 else -1
            height = _integer(fields[9], "개체 높이") if len(fields) == 10 else -1
            controls.append(
                NativePageControl(
                    control_type=_decode(fields[1]),
                    instance_id=_decode(fields[2]),
                    anchor=NativePosition(
                        _integer(fields[3], "개체 앵커"),
                        _integer(fields[4], "개체 앵커"),
                        _integer(fields[5], "개체 앵커"),
                    ),
                    rows=None if rows == -1 else rows,
                    columns=None if columns == -1 else columns,
                    width_hwpunit=None if width == -1 else width,
                    height_hwpunit=None if height == -1 else height,
                )
            )
            continue
        if len(fields) == 10 and fields[0] == "CTRL_FORMAT":
            instance_id = _decode(fields[1])
            if not instance_id or instance_id in control_formats:
                raise HwpLiveError(
                    "네이티브 쪽 개체 앵커 형식 레코드가 중복되거나 비어 있습니다"
                )
            control_formats[instance_id] = (
                _integer(fields[2], "개체 앵커 스타일"),
                NativeParagraphFormat(
                    alignment=_integer(fields[3], "개체 앵커 문단 정렬"),
                    line_spacing=_integer(fields[4], "개체 앵커 줄 간격"),
                    left_margin_hwpunit=_integer(fields[5], "개체 앵커 왼쪽 여백"),
                    right_margin_hwpunit=_integer(fields[6], "개체 앵커 오른쪽 여백"),
                    indentation_hwpunit=_integer(fields[7], "개체 앵커 들여쓰기"),
                    previous_spacing_hwpunit=_integer(fields[8], "개체 앵커 위 간격"),
                    next_spacing_hwpunit=_integer(fields[9], "개체 앵커 아래 간격"),
                ),
            )
            continue
        if len(fields) in {7, 9} and fields[0] == "CELL":
            row_span = _integer(fields[4], "셀 행 병합 수")
            column_span = _integer(fields[5], "셀 열 병합 수")
            width = _integer(fields[7], "셀 너비") if len(fields) == 9 else -1
            height = _integer(fields[8], "셀 높이") if len(fields) == 9 else -1
            if row_span < 1 or column_span < 1:
                raise HwpLiveError("네이티브 셀 병합 범위가 올바르지 않습니다")
            cells.append(
                NativePageCell(
                    table_instance_id=_decode(fields[1]),
                    address=_decode(fields[2]),
                    list_id=_integer(fields[3], "셀 리스트"),
                    row_span=row_span,
                    column_span=column_span,
                    text=_decode(fields[6]),
                    width_hwpunit=None if width == -1 else width,
                    height_hwpunit=None if height == -1 else height,
                )
            )
            continue
        if len(fields) == _CELL_FORMAT_FIELDS and fields[0] == "CELLFMT":
            cell_formats.append(_decode_cell_format(fields))
            continue
        if len(fields) == 4 and fields[0] == "CTRL_ERROR":
            inspection_errors.append(
                NativeControlInspectionError(
                    control_instance_id=_decode(fields[1]),
                    code=_decode(fields[2]),
                    message=_decode(fields[3]),
                )
            )
            continue
        raise HwpLiveError("네이티브 쪽 개체 레코드가 올바르지 않습니다")
    controls_with_formats = tuple(
        replace(
            control,
            anchor_style_id=control_formats[control.instance_id][0],
            anchor_paragraph_format=control_formats[control.instance_id][1],
        )
        if control.instance_id in control_formats
        else control
        for control in controls
    )
    return NativePageInspection(
        document_id=_integer(document[1], "문서 ID"),
        full_name=_decode(document[2]),
        page=_integer(page[1], "요청 쪽"),
        page_count=_integer(page[2], "전체 쪽"),
        text=_decode(page[3]),
        controls=controls_with_formats,
        cells=tuple(cells),
        inspection_errors=tuple(inspection_errors),
        cell_formats=tuple(cell_formats),
    )


def decode_page_inspection_batch(payload: str) -> tuple[NativePageInspection, ...]:
    error_fields = payload.split("\t")
    _native_error(error_fields)
    lines = payload.splitlines()
    if len(lines) < 3 or lines[0] != "HPM1" or lines[-1] != "END":
        raise HwpLiveError("네이티브 다중 쪽 구조 응답 형식이 올바르지 않습니다")
    pages: list[NativePageInspection] = []
    seen: set[int] = set()
    for line in lines[1:-1]:
        fields = line.split("\t")
        if len(fields) != 2 or fields[0] != "ITEM":
            raise HwpLiveError("네이티브 다중 쪽 구조 레코드가 올바르지 않습니다")
        page = decode_page_inspection(_decode(fields[1]))
        if page.page in seen:
            raise HwpLiveError("네이티브 다중 쪽 구조 응답에 중복 쪽이 있습니다")
        if pages and (
            page.document_id != pages[0].document_id
            or page.full_name != pages[0].full_name
            or page.page_count != pages[0].page_count
        ):
            raise HwpLiveError(
                "네이티브 다중 쪽 구조 응답의 문서 정보가 일치하지 않습니다"
            )
        seen.add(page.page)
        pages.append(page)
    if not pages:
        raise HwpLiveError("네이티브 다중 쪽 구조 응답이 비어 있습니다")
    return tuple(pages)


def decode_detailed_inspection(payload: str) -> NativeDetailedInspection:
    error_fields = payload.split("\t")
    _native_error(error_fields)
    lines = payload.splitlines()
    if len(lines) < 4 or lines[0] != "HDS1" or lines[-1] != "END":
        raise HwpLiveError("네이티브 상세 구조 응답 형식이 올바르지 않습니다")
    document = lines[1].split("\t")
    page = lines[2].split("\t")
    if (
        len(document) != 3
        or document[0] != "DOC"
        or len(page) != 4
        or page[0] != "PAGE"
    ):
        raise HwpLiveError("네이티브 상세 구조 기본 레코드가 올바르지 않습니다")

    controls: list[NativeDetailedControl] = []
    cells: list[NativeDetailedCell] = []
    cell_formats: list[NativeCellFormat] = []
    captions: list[NativeDetailedCaption] = []
    paragraphs: list[NativeDetailedParagraph] = []
    paragraphs_complete = False
    paragraph_scan_error: str | None = None
    paragraph_scan_reported = False
    unsupported_records: list[NativeUnsupportedRecord] = []
    inspection_errors: list[NativeControlInspectionError] = []
    for line in lines[3:-1]:
        fields = line.split("\t")
        if len(fields) == 14 and fields[0] == "CTRL":
            rows = _integer(fields[10], "표 행 수")
            columns = _integer(fields[11], "표 열 수")
            width = _integer(fields[12], "개체 너비")
            height = _integer(fields[13], "개체 높이")
            page_start = _integer(fields[7], "개체 시작 쪽")
            page_end = _integer(fields[8], "개체 끝 쪽")
            if page_start < 1 or page_end < page_start:
                raise HwpLiveError(
                    "네이티브 상세 구조 개체 쪽 범위가 올바르지 않습니다"
                )
            controls.append(
                NativeDetailedControl(
                    control_type=_decode(fields[1]),
                    instance_id=_decode(fields[2]),
                    user_description=_decode(fields[3]),
                    anchor=NativePosition(
                        _integer(fields[4], "개체 앵커"),
                        _integer(fields[5], "개체 앵커"),
                        _integer(fields[6], "개체 앵커"),
                    ),
                    page_start=page_start,
                    page_end=page_end,
                    top_level=_boolean(fields[9], "최상위 개체"),
                    rows=None if rows == -1 else rows,
                    columns=None if columns == -1 else columns,
                    width_hwpunit=None if width == -1 else width,
                    height_hwpunit=None if height == -1 else height,
                )
            )
            continue
        if len(fields) in {9, 11} and fields[0] == "CELL":
            row_span = _integer(fields[4], "셀 행 병합 수")
            column_span = _integer(fields[5], "셀 열 병합 수")
            page_start = _integer(fields[6], "셀 시작 쪽")
            page_end = _integer(fields[7], "셀 끝 쪽")
            width = _integer(fields[9], "셀 너비") if len(fields) == 11 else -1
            height = _integer(fields[10], "셀 높이") if len(fields) == 11 else -1
            if (
                row_span < 1
                or column_span < 1
                or page_start < 1
                or page_end < page_start
            ):
                raise HwpLiveError("네이티브 상세 구조 셀 범위가 올바르지 않습니다")
            cells.append(
                NativeDetailedCell(
                    table_instance_id=_decode(fields[1]),
                    address=_decode(fields[2]),
                    list_id=_integer(fields[3], "셀 리스트"),
                    row_span=row_span,
                    column_span=column_span,
                    page_start=page_start,
                    page_end=page_end,
                    text=_decode(fields[8]),
                    width_hwpunit=None if width == -1 else width,
                    height_hwpunit=None if height == -1 else height,
                )
            )
            continue
        if len(fields) == _CELL_FORMAT_FIELDS and fields[0] == "CELLFMT":
            cell_formats.append(_decode_cell_format(fields))
            continue
        if len(fields) == 8 and fields[0] == "CAPTION":
            style_id = _integer(fields[4], "캡션 스타일")
            page_start = _integer(fields[6], "캡션 시작 쪽")
            page_end = _integer(fields[7], "캡션 끝 쪽")
            if page_start < 1 or page_end < page_start:
                raise HwpLiveError(
                    "네이티브 상세 구조 캡션 쪽 범위가 올바르지 않습니다"
                )
            captions.append(
                NativeDetailedCaption(
                    table_instance_id=_decode(fields[1]),
                    text=_decode(fields[2]),
                    automatic_number=_boolean(fields[3], "자동 번호"),
                    style_id=None if style_id == -1 else style_id,
                    style_name=_decode(fields[5]) or None,
                    page_start=page_start,
                    page_end=page_end,
                )
            )
            continue
        if len(fields) == 3 and fields[0] == "PARA_SCAN":
            if paragraph_scan_reported:
                raise HwpLiveError("네이티브 상세 구조 문단 조회 상태가 중복되었습니다")
            paragraph_scan_reported = True
            paragraphs_complete = _boolean(fields[1], "문단 조회 완료")
            paragraph_scan_error = _decode(fields[2]) or None
            if paragraphs_complete and paragraph_scan_error is not None:
                raise HwpLiveError(
                    "네이티브 상세 구조 문단 조회 상태가 서로 모순됩니다"
                )
            continue
        if len(fields) == 21 and fields[0] == "PARA":
            page_start = _integer(fields[3], "문단 시작 쪽")
            page_end = _integer(fields[4], "문단 끝 쪽")
            text_available = _boolean(fields[5], "문단 텍스트 관측")
            text = _decode(fields[6])
            style_id = _integer(fields[7], "문단 스타일")
            if page_start < 1 or page_end < page_start:
                raise HwpLiveError(
                    "네이티브 상세 구조 문단 쪽 범위가 올바르지 않습니다"
                )
            if not text_available and text:
                raise HwpLiveError(
                    "네이티브 상세 구조 문단 텍스트 상태가 서로 모순됩니다"
                )
            paragraphs.append(
                NativeDetailedParagraph(
                    position=NativePosition(
                        _integer(fields[1], "문단 리스트"),
                        _integer(fields[2], "문단 번호"),
                        0,
                    ),
                    page_start=page_start,
                    page_end=page_end,
                    text_available=text_available,
                    text=text,
                    style_id=None if style_id < 0 else style_id,
                    face_name=_decode(fields[8]) or None,
                    height_hwpunit=_optional_integer(fields[9], "문단 글자 크기"),
                    bold=(
                        None if fields[10] == "" else _boolean(fields[10], "문단 굵기")
                    ),
                    text_color=_optional_integer(fields[11], "문단 글자색"),
                    alignment=_optional_integer(fields[12], "문단 정렬"),
                    line_spacing=_optional_integer(fields[13], "문단 줄 간격"),
                    left_margin_hwpunit=_optional_integer(
                        fields[14],
                        "문단 왼쪽 여백",
                    ),
                    right_margin_hwpunit=_optional_integer(
                        fields[15],
                        "문단 오른쪽 여백",
                    ),
                    indentation_hwpunit=_optional_integer(
                        fields[16],
                        "문단 들여쓰기",
                    ),
                    previous_spacing_hwpunit=_optional_integer(
                        fields[17],
                        "문단 위 간격",
                    ),
                    next_spacing_hwpunit=_optional_integer(
                        fields[18],
                        "문단 아래 간격",
                    ),
                    heading_type=_optional_integer(fields[19], "문단 번호 종류"),
                    heading_level=_optional_integer(fields[20], "문단 번호 수준"),
                )
            )
            continue
        if len(fields) == 4 and fields[0] == "CTRL_ERROR":
            inspection_errors.append(
                NativeControlInspectionError(
                    control_instance_id=_decode(fields[1]),
                    code=_decode(fields[2]),
                    message=_decode(fields[3]),
                )
            )
            continue
        record_type = _plain(fields[0], "상세 구조 레코드 종류")
        if record_type in {"PARA", "PARA_SCAN"}:
            raise HwpLiveError("네이티브 상세 구조 문단 레코드가 올바르지 않습니다")
        if record_type in {
            "HDS1",
            "DOC",
            "PAGE",
            "CTRL",
            "CELL",
            "CELLFMT",
            "CAPTION",
            "CTRL_ERROR",
            "END",
        }:
            raise HwpLiveError("네이티브 상세 구조 개체 레코드가 올바르지 않습니다")
        if len(record_type) > 100:
            raise HwpLiveError("네이티브 상세 구조 미지원 레코드 종류가 너무 깁니다")
        unsupported_records.append(
            NativeUnsupportedRecord(
                record_type=record_type,
                payload_sha256=hashlib.sha256(line.encode("utf-8")).hexdigest(),
            )
        )
    if not paragraph_scan_reported:
        paragraph_scan_error = (
            "네이티브 브리지가 쪽 문단 관측 지원 여부를 보고하지 않았습니다"
        )
    elif not paragraphs_complete and paragraph_scan_error is None:
        paragraph_scan_error = "네이티브 브리지가 쪽 문단 관측을 완료하지 못했습니다"

    return NativeDetailedInspection(
        document_id=_integer(document[1], "문서 ID"),
        full_name=_decode(document[2]),
        page=_integer(page[1], "요청 쪽"),
        page_count=_integer(page[2], "전체 쪽"),
        text=_decode(page[3]),
        controls=tuple(controls),
        cells=tuple(cells),
        captions=tuple(captions),
        paragraphs=tuple(paragraphs),
        paragraphs_complete=paragraphs_complete,
        paragraph_scan_error=paragraph_scan_error,
        unsupported_records=tuple(unsupported_records),
        inspection_errors=tuple(inspection_errors),
        cell_formats=tuple(cell_formats),
    )


# Native source: LiveInspection.cpp::kParagraphShape* -- how much of each
# paragraph's real appearance the scan carries back.
PARAGRAPH_SHAPE_DETAIL_NONE: Final = 0
PARAGRAPH_SHAPE_DETAIL_CHARACTER: Final = 1
PARAGRAPH_SHAPE_DETAIL_PARAGRAPH: Final = 2
PARAGRAPH_SHAPE_DETAIL_CONVENTION: Final = 3
# Field count per PSTYLE line, indexed by the detail level the bridge echoes.
_PARAGRAPH_SCAN_FIELDS: Final[tuple[int, ...]] = (4, 7, 17, 26)


def encode_paragraph_style_request(
    *,
    list_id: int,
    start: int,
    limit: int,
    character_shape: bool,
    paragraph_shape: bool = False,
    convention_detail: bool = False,
) -> str:
    """Wire form of the scan request.

    The fourth field kept its position and its old meaning for 0 and 1, so a
    bridge that predates paragraph shape still answers a level-2 request -- it
    simply answers at level 1 and says so in the response.
    """
    if list_id < 0 or start < 0 or limit < 1:
        raise HwpLiveError("네이티브 문단 스타일 조회 범위가 올바르지 않습니다")
    detail = (
        PARAGRAPH_SHAPE_DETAIL_CONVENTION
        if convention_detail
        else PARAGRAPH_SHAPE_DETAIL_PARAGRAPH
        if paragraph_shape
        else PARAGRAPH_SHAPE_DETAIL_CHARACTER
        if character_shape
        else PARAGRAPH_SHAPE_DETAIL_NONE
    )
    return f"{list_id},{start},{limit},{detail}"


def _optional_integer(value: str, label: str) -> int | None:
    """A native field that is empty when the property could not be read.

    Absent is not zero. An indentation of 0 and an indentation nobody could
    read are different answers and only the first may be replayed.
    """
    return None if value == "" else _integer(value, label)


def decode_paragraph_style_scan(payload: str) -> NativeParagraphStyleScan:
    error_fields = payload.split("\t")
    _native_error(error_fields)
    lines = payload.splitlines()
    if len(lines) < 4 or lines[0] != "HPS1" or lines[-1] != "END":
        raise HwpLiveError("네이티브 문단 스타일 응답 형식이 올바르지 않습니다")
    document = lines[1].split("\t")
    scan = lines[2].split("\t")
    if (
        len(document) != 3
        or document[0] != "DOC"
        or len(scan) != 6
        or scan[0] != "SCAN"
    ):
        raise HwpLiveError("네이티브 문단 스타일 기본 레코드가 올바르지 않습니다")
    detail = _integer(scan[5], "문단 모양 상세 수준")
    if not 0 <= detail < len(_PARAGRAPH_SCAN_FIELDS):
        raise HwpLiveError("네이티브 문단 모양 상세 수준을 해석하지 못했습니다")
    expected_fields = _PARAGRAPH_SCAN_FIELDS[detail]
    character_shape = detail >= PARAGRAPH_SHAPE_DETAIL_CHARACTER
    paragraph_shape = detail >= PARAGRAPH_SHAPE_DETAIL_PARAGRAPH
    convention_detail = detail >= PARAGRAPH_SHAPE_DETAIL_CONVENTION
    paragraphs: list[NativeParagraphStyle] = []
    for line in lines[3:-1]:
        fields = line.split("\t")
        if len(fields) != expected_fields or fields[0] != "PSTYLE":
            raise HwpLiveError("네이티브 문단 스타일 레코드가 올바르지 않습니다")
        style_id = _integer(fields[2], "문단 스타일")
        paragraphs.append(
            NativeParagraphStyle(
                paragraph=_integer(fields[1], "문단 번호"),
                style_id=None if style_id < 0 else style_id,
                lead_text=_decode(fields[3]),
                face_name=_decode(fields[4]) or None if character_shape else None,
                height=_integer(fields[5], "문단 글자 크기")
                if character_shape
                else None,
                bold=_boolean(fields[6], "문단 굵기") if character_shape else None,
                text_color=(
                    _optional_integer(fields[7], "문단 글자색")
                    if paragraph_shape
                    else None
                ),
                alignment=(
                    _optional_integer(fields[8], "문단 정렬")
                    if paragraph_shape
                    else None
                ),
                line_spacing=(
                    _optional_integer(fields[9], "문단 줄 간격")
                    if paragraph_shape
                    else None
                ),
                left_margin_hwpunit=(
                    _optional_integer(fields[10], "문단 왼쪽 여백")
                    if paragraph_shape
                    else None
                ),
                right_margin_hwpunit=(
                    _optional_integer(fields[11], "문단 오른쪽 여백")
                    if paragraph_shape
                    else None
                ),
                indentation_hwpunit=(
                    _optional_integer(fields[12], "문단 들여쓰기")
                    if paragraph_shape
                    else None
                ),
                previous_spacing_hwpunit=(
                    _optional_integer(fields[13], "문단 위 간격")
                    if paragraph_shape
                    else None
                ),
                next_spacing_hwpunit=(
                    _optional_integer(fields[14], "문단 아래 간격")
                    if paragraph_shape
                    else None
                ),
                heading_type=(
                    _optional_integer(fields[15], "문단 번호 종류")
                    if paragraph_shape
                    else None
                ),
                heading_level=(
                    _optional_integer(fields[16], "문단 번호 수준")
                    if paragraph_shape
                    else None
                ),
                tail_text=_decode(fields[17]) if convention_detail else "",
                text_length=(
                    _optional_integer(fields[18], "문단 전체 글자 수")
                    if convention_detail
                    else None
                ),
                text_complete=(
                    _boolean(fields[19], "문단 끝 관측 완료")
                    if convention_detail
                    else False
                ),
                face_name_latin=(
                    _decode(fields[20]) or None if convention_detail else None
                ),
                face_name_hanja=(
                    _decode(fields[21]) or None if convention_detail else None
                ),
                face_name_japanese=(
                    _decode(fields[22]) or None if convention_detail else None
                ),
                face_name_other=(
                    _decode(fields[23]) or None if convention_detail else None
                ),
                face_name_symbol=(
                    _decode(fields[24]) or None if convention_detail else None
                ),
                face_name_user=(
                    _decode(fields[25]) or None if convention_detail else None
                ),
            )
        )
    returned = _integer(scan[3], "문단 스타일 개수")
    if returned != len(paragraphs):
        raise HwpLiveError("네이티브 문단 스타일 응답 개수가 맞지 않습니다")
    return NativeParagraphStyleScan(
        document_id=_integer(document[1], "문서 ID"),
        full_name=_decode(document[2]),
        list_id=_integer(scan[1], "문단 리스트"),
        start=_integer(scan[2], "문단 시작 번호"),
        complete=_boolean(scan[4], "문단 훑기 완료"),
        paragraphs=tuple(paragraphs),
    )
