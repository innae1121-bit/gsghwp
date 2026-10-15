from __future__ import annotations

from dataclasses import dataclass
from typing import assert_never

from hwp_live_native_action_models import (
    CellCommand,
    EnumerationValue,
    IntegerValue,
    MillimeterValue,
    NativeActionCommand,
    NativeSetter,
    ParameterActionCommand,
    RunCommand,
)
from hwp_live_native_text_format import (
    ParagraphFormatting,
    character_command,
    paragraph_command,
    rgb_value,
    style_command,
)
from hwp_live_table_contract import CellBorders, CellPadding, TableBlock, TableCell
from hwp_table_address import cell_address


@dataclass(frozen=True, slots=True)
class CellPaddingRange:
    anchor: str
    right_steps: int
    down_steps: int
    addresses: tuple[str, ...]
    action: ParameterActionCommand

    @property
    def commands(self) -> tuple[NativeActionCommand, ...]:
        return (
            CellCommand(self.anchor),
            RunCommand("TableCellBlock"),
            RunCommand("TableCellBlockExtend"),
            *(RunCommand("TableRightCell") for _ in range(self.right_steps)),
            *(RunCommand("TableLowerCell") for _ in range(self.down_steps)),
            self.action,
            RunCommand("Cancel"),
        )


def _padding_command(cell: TableCell) -> ParameterActionCommand | None:
    padding = cell.padding
    if padding is None:
        return None
    return ParameterActionCommand(
        action="TablePropertyDialog",
        parameter_set="HShapeObject",
        setters=(
            NativeSetter("HSet/ShapeType", IntegerValue(3)),
            NativeSetter("HSet/ShapeCellSize", IntegerValue(0)),
            NativeSetter("ShapeTableCell/HasMargin", IntegerValue(1)),
            NativeSetter("ShapeTableCell/MarginLeft", MillimeterValue(padding.left_mm)),
            NativeSetter(
                "ShapeTableCell/MarginRight", MillimeterValue(padding.right_mm)
            ),
            NativeSetter("ShapeTableCell/MarginTop", MillimeterValue(padding.top_mm)),
            NativeSetter(
                "ShapeTableCell/MarginBottom", MillimeterValue(padding.bottom_mm)
            ),
        ),
    )


def is_padding_command(command: NativeActionCommand) -> bool:
    return (
        isinstance(command, ParameterActionCommand)
        and command.action == "TablePropertyDialog"
        and command.parameter_set == "HShapeObject"
        and any(setter.path == "ShapeTableCell/HasMargin" for setter in command.setters)
    )


def _padding_key(padding: CellPadding) -> tuple[float, float, float, float]:
    return (
        padding.left_mm,
        padding.right_mm,
        padding.top_mm,
        padding.bottom_mm,
    )


def cell_padding_ranges(block: TableBlock) -> tuple[CellPaddingRange, ...]:
    grouped: dict[
        tuple[float, float, float, float],
        list[tuple[int, int, TableCell]],
    ] = {}
    for row, cells in enumerate(block.rows):
        for column, cell in enumerate(cells):
            if cell.padding is not None:
                grouped.setdefault(_padding_key(cell.padding), []).append(
                    (row, column, cell)
                )

    merged = {
        (row, column)
        for merge in block.merges
        for row in range(merge.row, merge.row + merge.row_span)
        for column in range(merge.column, merge.column + merge.column_span)
    }
    ranges: list[CellPaddingRange] = []
    for cells in grouped.values():
        # A single cell already has one padding action. Turning it into a block
        # selection would add work without reducing topology rebuilds.
        if len(cells) < 2:
            continue
        coordinates = {(row, column) for row, column, _ in cells}
        top = min(row for row, _, _ in cells)
        bottom = max(row for row, _, _ in cells)
        left = min(column for _, column, _ in cells)
        right = max(column for _, column, _ in cells)
        rectangle = {
            (row, column)
            for row in range(top, bottom + 1)
            for column in range(left, right + 1)
        }
        if coordinates != rectangle or rectangle.intersection(merged):
            continue
        action = _padding_command(cells[0][2])
        if action is None:
            continue
        ranges.append(
            CellPaddingRange(
                anchor=cell_address(top, left),
                right_steps=right - left,
                down_steps=bottom - top,
                addresses=tuple(
                    cell_address(row, column)
                    for row in range(top, bottom + 1)
                    for column in range(left, right + 1)
                ),
                action=action,
            )
        )
    return tuple(ranges)


def _fill_command(cell: TableCell) -> ParameterActionCommand | None:
    if cell.fill_color is None:
        return None
    return ParameterActionCommand(
        action="CellFill",
        parameter_set="HCellBorderFill",
        setters=(
            NativeSetter(
                "FillAttr/Type",
                EnumerationValue("BrushType", "NullBrush|WinBrush"),
            ),
            NativeSetter(
                "FillAttr/WinBrushFaceColor",
                IntegerValue(rgb_value(cell.fill_color)),
            ),
            NativeSetter("FillAttr/WinBrushHatchColor", IntegerValue(0x999999)),
            NativeSetter(
                "FillAttr/WinBrushFaceStyle",
                EnumerationValue("HatchStyle", "None"),
            ),
            NativeSetter("FillAttr/WindowsBrush", IntegerValue(1)),
        ),
    )


def _border_command(borders: CellBorders | None) -> ParameterActionCommand | None:
    if borders is None:
        return None
    setters: list[NativeSetter] = []
    sides = (
        ("Left", "BorderCorlorLeft", borders.left),
        ("Right", "BorderColorRight", borders.right),
        ("Top", "BorderColorTop", borders.top),
        ("Bottom", "BorderColorBottom", borders.bottom),
    )
    line_types = {
        "none": "None",
        "solid": "Solid",
        "dash": "Dash",
        "dot": "Dot",
        "dash_dot": "DashDot",
        "dash_dot_dot": "DashDotDot",
        "long_dash": "LongDash",
        "circle": "Circle",
        "double_slim": "DoubleSlim",
        "slim_thick": "SlimThick",
        "thick_slim": "ThickSlim",
        "slim_thick_slim": "SlimThickSlim",
    }
    for side, color_name, border in sides:
        if border is None:
            continue
        if border.style == "none":
            # HWP ignores a missing line's colour/width; requesting those too
            # makes the native readback reject a correctly removed border.
            setters.append(
                NativeSetter(
                    f"HSet/BorderType{side}", EnumerationValue("HwpLineType", "None")
                )
            )
            continue
        setters.extend(
            (
                NativeSetter(
                    f"HSet/BorderType{side}",
                    EnumerationValue("HwpLineType", line_types[border.style]),
                ),
                NativeSetter(
                    f"HSet/BorderWidth{side}",
                    EnumerationValue("HwpLineWidth", border.width),
                ),
                NativeSetter(
                    f"HSet/{color_name}", IntegerValue(rgb_value(border.color))
                ),
            )
        )
    if not setters:
        return None
    return ParameterActionCommand(
        # CellBorder's GetDefault readback aggregates a block and can report
        # the neighbouring line instead of the changed physical side. The
        # border/fill action supports the same side parameters directly.
        action="CellBorderFill"
        if any(border is None or border.style == "none" for _, _, border in sides)
        else "CellBorder",
        parameter_set="HCellBorderFill",
        setters=tuple(setters),
    )


def cell_format_commands(cell: TableCell) -> tuple[NativeActionCommand, ...]:
    commands: list[NativeActionCommand] = []
    if cell.style_id is not None:
        commands.append(style_command(cell.style_id))
    character = character_command(cell)
    if character is not None:
        commands.append(character)
    paragraph = paragraph_command(
        ParagraphFormatting(
            alignment=cell.alignment,
            line_spacing=cell.line_spacing_percent,
            left_mm=0 if cell.image_path is not None else None,
            right_mm=0 if cell.image_path is not None else None,
            indentation_mm=0 if cell.image_path is not None else None,
        )
    )
    if paragraph is not None:
        commands.append(paragraph)
    for command in (
        _fill_command(cell),
        _padding_command(cell),
        _border_command(cell.borders),
    ):
        if command is not None:
            commands.append(command)
    match cell.vertical_alignment:
        case "inherit":
            return tuple(commands)
        case "top":
            commands.append(RunCommand("TableVAlignTop"))
            return tuple(commands)
        case "center":
            commands.append(RunCommand("TableVAlignCenter"))
            return tuple(commands)
        case "bottom":
            commands.append(RunCommand("TableVAlignBottom"))
            return tuple(commands)
    assert_never(cell.vertical_alignment)
