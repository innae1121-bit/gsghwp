from __future__ import annotations

import re
import shutil
import tempfile
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Protocol, cast, runtime_checkable

from pywintypes import com_error

from hwp_errors import HwpLiveError


@dataclass(frozen=True, slots=True)
class ExcelBorderData:
    line_style: int
    weight: int
    color: int


@dataclass(frozen=True, slots=True)
class ExcelCellData:
    text: str
    covered: bool
    fill_color: int
    fill_pattern: int
    bold: bool
    font_size: float | None
    font_color: int
    font_name: str
    horizontal_alignment: int
    vertical_alignment: int
    wrap_text: bool
    borders: tuple[ExcelBorderData, ExcelBorderData, ExcelBorderData, ExcelBorderData]


@dataclass(frozen=True, slots=True)
class ExcelMergeData:
    row: int
    column: int
    row_span: int
    column_span: int


@dataclass(frozen=True, slots=True)
class ExcelRangeData:
    rows: tuple[tuple[ExcelCellData, ...], ...]
    column_width_points: tuple[float, ...]
    row_height_points: tuple[float, ...]
    merges: tuple[ExcelMergeData, ...]


class _Dimension(Protocol):
    Width: float
    Height: float


class _Border(Protocol):
    LineStyle: int
    Weight: int
    Color: int


class _Borders(Protocol):
    def __call__(self, edge: int) -> _Border: ...


class _Interior(Protocol):
    Color: int
    Pattern: int


class _Font(Protocol):
    Bold: bool
    Size: float | None
    Color: int
    Name: str


class _CellFormatting(Protocol):
    Text: str | int | float | bool | None
    Interior: _Interior
    Font: _Font
    HorizontalAlignment: int
    VerticalAlignment: int
    WrapText: bool
    Borders: _Borders


class _Cell(_CellFormatting, Protocol):
    Address: str
    MergeCells: bool
    MergeArea: _Range


class _Range(Protocol):
    Address: str
    Row: int
    Column: int
    Rows: _Count
    Columns: _Count

    def Cells(self, row: int, column: int) -> _Cell: ...


class _Count(Protocol):
    Count: int


class _Worksheet(Protocol):
    UsedRange: _Range

    def Cells(self, row: int, column: int) -> _Cell: ...

    def Columns(self, column: int) -> _Dimension: ...

    def Rows(self, row: int) -> _Dimension: ...


class _Worksheets(Protocol):
    def __call__(self, key: int | str) -> _Worksheet: ...


class _Workbook(Protocol):
    Worksheets: _Worksheets

    def Close(self, save_changes: bool) -> None: ...


class _Workbooks(Protocol):
    def Open(
        self,
        path: str,
        *,
        UpdateLinks: int,
        ReadOnly: bool,
        IgnoreReadOnlyRecommended: bool,
    ) -> _Workbook: ...


class _ExcelApplication(Protocol):
    Visible: bool
    DisplayAlerts: bool
    Workbooks: _Workbooks

    def Quit(self) -> None: ...


@runtime_checkable
class _Win32Client(Protocol):
    def DispatchEx(self, program_id: str) -> _ExcelApplication: ...


@runtime_checkable
class _PythonCom(Protocol):
    def CoInitialize(self) -> None: ...
    def CoUninitialize(self) -> None: ...


def _python_com() -> _PythonCom:
    """Load pythoncom on use, never at import.

    A module-level ``import pythoncom`` puts the COM runtime into
    ``sys.modules`` for every process that merely imports this module —
    pytest collection included — which breaks the callers that assert no
    Hancom/COM module was ever loaded. ``win32com.client`` below is loaded
    the same way for the same reason.
    """
    module = import_module("pythoncom")
    if not isinstance(module, _PythonCom):
        raise HwpLiveError("pythoncom 모듈 계약이 올바르지 않습니다")
    return module


@contextmanager
def _readable_copy(path: Path) -> Generator[Path, None, None]:
    """Hand Excel a throwaway copy so the source file is never written to.

    ``Workbooks.Open(..., ReadOnly=True)`` only makes the *document model*
    read-only. Excel still opens the file for writing at the storage layer and
    stamps the modify FILETIME in the OLE2 container root -- measured on a
    legacy ``.xls``: content byte-identical, exactly 8 bytes changed. That is
    enough to break any caller that pins the source by hash. Reading a copy is
    the only way to keep the original bytes fixed, because the write happens
    inside Excel before any Python code can intervene.

    The copy lives in its own temporary directory, so it can keep the source
    file name (Excel picks its parser from the extension) without colliding
    with anything. ``copyfile`` takes the bytes only: a read-only source
    attribute is not carried over, which would otherwise make Excel refuse the
    copy as well.
    """
    source = path.resolve()
    directory = Path(tempfile.mkdtemp(prefix="hwp-excel-read-"))
    try:
        target = directory / source.name
        _ = shutil.copyfile(source, target)
        yield target
    finally:
        # Excel is already closed by the time this runs. ``ignore_errors``
        # keeps a still-locked copy from masking the real failure; the file is
        # in the OS temp directory either way.
        shutil.rmtree(directory, ignore_errors=True)


def _coordinate(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"\$?([A-Z]+)\$?([1-9][0-9]*)", value.upper())
    if match is None:
        raise HwpLiveError(f"엑셀 셀 주소가 올바르지 않습니다: {value}")
    column = 0
    for character in match.group(1):
        column = column * 26 + ord(character) - ord("A") + 1
    return int(match.group(2)) - 1, column - 1


def _range(value: str) -> tuple[int, int, int, int]:
    parts = value.replace("$", "").split(":")
    if len(parts) not in {1, 2}:
        raise HwpLiveError("엑셀 범위는 A1 또는 A1:F37 형식이어야 합니다")
    first = _coordinate(parts[0])
    last = _coordinate(parts[-1])
    if last[0] < first[0] or last[1] < first[1]:
        raise HwpLiveError("엑셀 범위의 끝 셀이 시작 셀보다 앞에 있습니다")
    return first[0], first[1], last[0], last[1]


def _clip_merge(
    *,
    top: int,
    left: int,
    bottom: int,
    right: int,
    first_row: int,
    first_column: int,
    last_row: int,
    last_column: int,
) -> tuple[int, int, int, int]:
    return (
        max(top, first_row),
        max(left, first_column),
        min(bottom, last_row),
        min(right, last_column),
    )


def _border(cell: _CellFormatting, edge: int) -> ExcelBorderData:
    try:
        border = cell.Borders(edge)
        return ExcelBorderData(int(border.LineStyle), int(border.Weight), int(border.Color))
    except (AttributeError, TypeError, ValueError, com_error):
        return ExcelBorderData(-4142, 2, 0)


def _cell(cell: _CellFormatting, covered: bool) -> ExcelCellData:
    text = "" if cell.Text is None else str(cell.Text).strip()
    font_size = cell.Font.Size
    return ExcelCellData(
        text=text,
        covered=covered,
        fill_color=int(cell.Interior.Color),
        fill_pattern=int(cell.Interior.Pattern),
        bold=bool(cell.Font.Bold),
        font_size=None if font_size is None else float(font_size),
        font_color=int(cell.Font.Color),
        font_name=str(cell.Font.Name),
        horizontal_alignment=int(cell.HorizontalAlignment),
        vertical_alignment=int(cell.VerticalAlignment),
        wrap_text=bool(cell.WrapText),
        borders=(
            _border(cell, 7),
            _border(cell, 10),
            _border(cell, 8),
            _border(cell, 9),
        ),
    )


# OLE Automation invoke flags. Spelled out rather than imported, because this
# module must not pull ``pythoncom`` in at import time -- see ``_python_com``.
_LOCALE = 0
_PROPERTY_GET = 2
_PROPERTY_GET_OR_CALL = 3
_DEFAULT_MEMBER = 0


class _Ole(Protocol):
    def GetIDsOfNames(self, name: str) -> int: ...

    def Invoke(
        self, dispid: int, locale: int, flags: int, wants_result: int, *args: int
    ) -> object: ...


def _as_ole(value: object) -> _Ole:
    return cast("_Ole", value)


@dataclass(frozen=True, slots=True)
class _MemberIds:
    """DISPIDs of the members a cell read needs, resolved once per sheet."""

    cells: int
    text: int
    merge_cells: int
    merge_area: int
    address: int
    font: int
    interior: int
    borders: int
    horizontal_alignment: int
    vertical_alignment: int
    wrap_text: int
    font_size: int
    font_bold: int
    font_color: int
    font_name: int
    fill_color: int
    fill_pattern: int
    line_style: int
    weight: int
    border_color: int


def _member_ids(sheet: _Ole) -> _MemberIds:
    cells = sheet.GetIDsOfNames("Cells")
    cell = _as_ole(sheet.Invoke(cells, _LOCALE, _PROPERTY_GET_OR_CALL, 1, 1, 1))
    font = cell.GetIDsOfNames("Font")
    interior = cell.GetIDsOfNames("Interior")
    borders = cell.GetIDsOfNames("Borders")
    font_object = _as_ole(cell.Invoke(font, _LOCALE, _PROPERTY_GET, 1))
    interior_object = _as_ole(cell.Invoke(interior, _LOCALE, _PROPERTY_GET, 1))
    border_object = _as_ole(
        _as_ole(cell.Invoke(borders, _LOCALE, _PROPERTY_GET, 1)).Invoke(
            _DEFAULT_MEMBER, _LOCALE, _PROPERTY_GET_OR_CALL, 1, 7
        )
    )
    return _MemberIds(
        cells=cells,
        text=cell.GetIDsOfNames("Text"),
        merge_cells=cell.GetIDsOfNames("MergeCells"),
        merge_area=cell.GetIDsOfNames("MergeArea"),
        address=cell.GetIDsOfNames("Address"),
        font=font,
        interior=interior,
        borders=borders,
        horizontal_alignment=cell.GetIDsOfNames("HorizontalAlignment"),
        vertical_alignment=cell.GetIDsOfNames("VerticalAlignment"),
        wrap_text=cell.GetIDsOfNames("WrapText"),
        font_size=font_object.GetIDsOfNames("Size"),
        font_bold=font_object.GetIDsOfNames("Bold"),
        font_color=font_object.GetIDsOfNames("Color"),
        font_name=font_object.GetIDsOfNames("Name"),
        fill_color=interior_object.GetIDsOfNames("Color"),
        fill_pattern=interior_object.GetIDsOfNames("Pattern"),
        line_style=border_object.GetIDsOfNames("LineStyle"),
        weight=border_object.GetIDsOfNames("Weight"),
        border_color=border_object.GetIDsOfNames("Color"),
    )


class _Fast:
    """One COM object, read by DISPID."""

    __slots__: tuple[str, ...] = ("_ids", "_ole")

    def __init__(self, ole: _Ole, ids: _MemberIds) -> None:
        self._ole: _Ole = ole
        self._ids: _MemberIds = ids

    def _get(self, dispid: int) -> object:
        return self._ole.Invoke(dispid, _LOCALE, _PROPERTY_GET, 1)


class _FastBorder(_Fast):
    @property
    def LineStyle(self) -> int:
        return cast("int", self._get(self._ids.line_style))

    @property
    def Weight(self) -> int:
        return cast("int", self._get(self._ids.weight))

    @property
    def Color(self) -> int:
        return cast("int", self._get(self._ids.border_color))


class _FastBorders(_Fast):
    def __call__(self, edge: int) -> _FastBorder:
        item = self._ole.Invoke(
            _DEFAULT_MEMBER, _LOCALE, _PROPERTY_GET_OR_CALL, 1, edge
        )
        return _FastBorder(_as_ole(item), self._ids)


class _FastFont(_Fast):
    @property
    def Bold(self) -> bool:
        return cast("bool", self._get(self._ids.font_bold))

    @property
    def Size(self) -> float | None:
        return cast("float | None", self._get(self._ids.font_size))

    @property
    def Color(self) -> int:
        return cast("int", self._get(self._ids.font_color))

    @property
    def Name(self) -> str:
        return cast("str", self._get(self._ids.font_name))


class _FastInterior(_Fast):
    @property
    def Color(self) -> int:
        return cast("int", self._get(self._ids.fill_color))

    @property
    def Pattern(self) -> int:
        return cast("int", self._get(self._ids.fill_pattern))


class _FastArea(_Fast):
    @property
    def Address(self) -> str:
        return cast("str", self._get(self._ids.address))


class _FastCell(_Fast):
    """A cell read straight through IDispatch.

    pywin32's dynamic dispatch asks every object it hands back for its type
    information: measured 330-1080us to materialise one Range/Font/Interior/
    Border against 60us to read a property off one that already exists. A cell
    needs six such objects, so on the measured sheet the wrappers cost 6.9x the
    reads. The DISPIDs, the calls and the values are the same ones pywin32
    would have used, so ``_cell`` and ``_border`` read this exactly as before.
    The sub-objects are kept because ``_cell`` asks for ``Font`` four times and
    ``Interior`` twice.
    """

    __slots__: tuple[str, ...] = ("_borders", "_font", "_interior")

    def __init__(self, ole: _Ole, ids: _MemberIds) -> None:
        super().__init__(ole, ids)
        self._font: _FastFont | None = None
        self._interior: _FastInterior | None = None
        self._borders: _FastBorders | None = None

    @property
    def Text(self) -> str | int | float | bool | None:
        return cast("str | int | float | bool | None", self._get(self._ids.text))

    @property
    def MergeCells(self) -> bool:
        return cast("bool", self._get(self._ids.merge_cells))

    @property
    def MergeArea(self) -> _FastArea:
        return _FastArea(_as_ole(self._get(self._ids.merge_area)), self._ids)

    @property
    def Address(self) -> str:
        return cast("str", self._get(self._ids.address))

    @property
    def HorizontalAlignment(self) -> int:
        return cast("int", self._get(self._ids.horizontal_alignment))

    @property
    def VerticalAlignment(self) -> int:
        return cast("int", self._get(self._ids.vertical_alignment))

    @property
    def WrapText(self) -> bool:
        return cast("bool", self._get(self._ids.wrap_text))

    @property
    def Font(self) -> _FastFont:
        if self._font is None:
            self._font = _FastFont(_as_ole(self._get(self._ids.font)), self._ids)
        return self._font

    @property
    def Interior(self) -> _FastInterior:
        if self._interior is None:
            self._interior = _FastInterior(
                _as_ole(self._get(self._ids.interior)), self._ids
            )
        return self._interior

    @property
    def Borders(self) -> _FastBorders:
        if self._borders is None:
            self._borders = _FastBorders(
                _as_ole(self._get(self._ids.borders)), self._ids
            )
        return self._borders


def _cell_reader(worksheet: _Worksheet) -> Callable[[int, int], _Cell]:
    """Pick how cells are fetched: by DISPID when the sheet is a live COM object.

    A worksheet that is not a pywin32 dispatch -- the Protocol stand-ins the
    tests hand over -- keeps the attribute path.
    """
    ole = cast("_Ole | None", getattr(worksheet, "_oleobj_", None))
    if ole is None:
        return worksheet.Cells
    try:
        ids = _member_ids(ole)
    except (AttributeError, TypeError, com_error):
        return worksheet.Cells

    def read(row: int, column: int) -> _Cell:
        cell = ole.Invoke(ids.cells, _LOCALE, _PROPERTY_GET_OR_CALL, 1, row, column)
        # _FastCell answers every member a cell is read for, but it answers
        # with properties and the protocol spells them as writable attributes.
        return cast("_Cell", cast(object, _FastCell(_as_ole(cell), ids)))

    return read


def read_excel_range(
    path: Path,
    *,
    sheet_name: str | None = None,
    sheet_index: int = 0,
    cell_range: str | None = None,
) -> ExcelRangeData:
    """Read a range out of ``path`` without touching a single byte of it."""
    try:
        with _readable_copy(path) as readable:
            return _read_opened_excel_range(
                readable,
                path,
                sheet_name=sheet_name,
                sheet_index=sheet_index,
                cell_range=cell_range,
            )
    except OSError as error:
        raise HwpLiveError(f"Excel 표를 읽지 못했습니다: {path}") from error


def _read_opened_excel_range(
    readable: Path,
    source: Path,
    *,
    sheet_name: str | None,
    sheet_index: int,
    cell_range: str | None,
) -> ExcelRangeData:
    pythoncom = _python_com()
    pythoncom.CoInitialize()
    application: _ExcelApplication | None = None
    workbook: _Workbook | None = None
    try:
        client = import_module("win32com.client")
        if not isinstance(client, _Win32Client):
            raise HwpLiveError("win32com.client 모듈 계약이 올바르지 않습니다")
        application = client.DispatchEx("Excel.Application")
        application.Visible = False
        application.DisplayAlerts = False
        workbook = application.Workbooks.Open(
            # The copy, never ``source`` -- see ``_readable_copy``.
            str(readable),
            UpdateLinks=0,
            ReadOnly=True,
            IgnoreReadOnlyRecommended=True,
        )
        worksheet = workbook.Worksheets(sheet_name if sheet_name is not None else sheet_index + 1)
        if cell_range is None:
            used = worksheet.UsedRange
            first_row = int(used.Row) - 1
            first_column = int(used.Column) - 1
            last_row = first_row + int(used.Rows.Count) - 1
            last_column = first_column + int(used.Columns.Count) - 1
        else:
            first_row, first_column, last_row, last_column = _range(cell_range)
        cell_at = _cell_reader(worksheet)
        merges: set[ExcelMergeData] = set()
        rows: list[tuple[ExcelCellData, ...]] = []
        for row in range(first_row, last_row + 1):
            values: list[ExcelCellData] = []
            for column in range(first_column, last_column + 1):
                cell = cell_at(row + 1, column + 1)
                source_cell = cell
                covered = False
                if bool(cell.MergeCells):
                    top, left, bottom, right = _range(cell.MergeArea.Address)
                    clipped_top, clipped_left, clipped_bottom, clipped_right = _clip_merge(
                        top=top,
                        left=left,
                        bottom=bottom,
                        right=right,
                        first_row=first_row,
                        first_column=first_column,
                        last_row=last_row,
                        last_column=last_column,
                    )
                    covered = (row, column) != (clipped_top, clipped_left)
                    if not covered:
                        # The anchor is this very cell unless the merge starts
                        # outside the requested range.
                        if (row, column) != (top, left):
                            source_cell = cell_at(top + 1, left + 1)
                        row_span = clipped_bottom - clipped_top + 1
                        column_span = clipped_right - clipped_left + 1
                        if row_span > 1 or column_span > 1:
                            merges.add(
                                ExcelMergeData(
                                    row=clipped_top - first_row,
                                    column=clipped_left - first_column,
                                    row_span=row_span,
                                    column_span=column_span,
                                )
                            )
                values.append(_cell(source_cell, covered))
            rows.append(tuple(values))
        return ExcelRangeData(
            rows=tuple(rows),
            column_width_points=tuple(
                float(worksheet.Columns(column + 1).Width)
                for column in range(first_column, last_column + 1)
            ),
            row_height_points=tuple(
                float(worksheet.Rows(row + 1).Height)
                for row in range(first_row, last_row + 1)
            ),
            merges=tuple(sorted(merges, key=lambda item: (item.row, item.column))),
        )
    except (
        AttributeError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
        com_error,
    ) as error:
        raise HwpLiveError(f"Excel 표를 읽지 못했습니다: {source}") from error
    finally:
        try:
            if workbook is not None:
                workbook.Close(False)
            if application is not None:
                application.Quit()
        finally:
            pythoncom.CoUninitialize()


def read_excel_matrix(
    path: Path,
    *,
    sheet_name: str | None,
    sheet_index: int,
    cell_range: str | None,
) -> tuple[tuple[str, ...], ...]:
    return tuple(
        tuple(cell.text if not cell.covered else "" for cell in row)
        for row in read_excel_range(
            path,
            sheet_name=sheet_name,
            sheet_index=sheet_index,
            cell_range=cell_range,
        ).rows
    )
