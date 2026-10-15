from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

from hwp_color_normalization import ColorInput, canonical_rgb_hex
from hwp_live_values import ContractModel
from hwp_mcp_wrapper_inputs import HwpBorderStyle, HwpBorderWidth
from hwp_operation_contract import OperationInputValue
from hwp_public_cell_selector import PublicCellAddress as PublicCellAddress


type PublicTableAlignment = Literal["left", "center", "right", "justify", "inherit"]
type PublicTableVerticalAlignment = Literal["top", "center", "bottom", "inherit"]
type PublicTableColor = ColorInput
type PublicTableBorderStyle = HwpBorderStyle
type PublicTableBorderWidth = HwpBorderWidth
type PublicTableBorderSide = Literal["left", "right", "top", "bottom"]
type PublicTableSplitMode = Literal["equal", "existing_grid"]


class PublicCellFormatSource(ContractModel):
    table_instance_id: str = Field(min_length=1, max_length=256)
    cell: PublicCellAddress


class PublicTableFormattingInput(ContractModel):
    cell: PublicCellAddress | None = None
    row_height_mm: float | None = Field(default=None, ge=1, le=250)
    column_width_mm: float | None = Field(default=None, ge=1, le=250)
    bold: bool | None = None
    font_name: str | None = Field(default=None, min_length=1, max_length=100)
    font_size_pt: float | None = Field(default=None, ge=1, le=96)
    text_color: PublicTableColor | None = None
    alignment: PublicTableAlignment = "inherit"
    vertical_alignment: PublicTableVerticalAlignment = "inherit"
    line_spacing: int | None = Field(default=None, ge=50, le=500)
    fill_color: PublicTableColor | None = None
    border_style: PublicTableBorderStyle | None = None
    border_width: PublicTableBorderWidth | None = None
    border_color: PublicTableColor | None = None
    border_sides: tuple[PublicTableBorderSide, ...] | None = Field(
        default=None, min_length=1, max_length=4
    )

    def to_parameters(self) -> Mapping[str, OperationInputValue]:
        # "inherit" means "change nothing". Emitting these two keys even then
        # made every request carry a formatting parameter, which defeated the
        # empty-request guard in hwp_live_native_format_inputs.parse_table_format
        # (`has_format`): a request with nothing to apply ran SelectControl and
        # Capture with no setter, then reported every cell as updated and
        # verified. parse_table_format defaults both back to "inherit" when the
        # key is absent, so omitting them changes nothing for real requests.
        parameters: dict[str, OperationInputValue] = {}
        if self.alignment != "inherit":
            parameters["alignment"] = self.alignment
        if self.vertical_alignment != "inherit":
            parameters["vertical_alignment"] = self.vertical_alignment
        if self.cell is not None:
            parameters["cell"] = self.cell.upper()
        if self.row_height_mm is not None:
            parameters["row_height_mm"] = self.row_height_mm
        if self.column_width_mm is not None:
            parameters["column_width_mm"] = self.column_width_mm
        if self.bold is not None:
            parameters["bold"] = self.bold
        if self.font_name is not None:
            parameters["font_name"] = self.font_name
        if self.font_size_pt is not None:
            parameters["font_size_pt"] = self.font_size_pt
        if self.text_color is not None:
            parameters["text_color"] = canonical_rgb_hex(self.text_color)
        if self.line_spacing is not None:
            parameters["line_spacing"] = self.line_spacing
        if self.fill_color is not None:
            parameters["fill_color"] = canonical_rgb_hex(self.fill_color)
        if self.border_style is not None:
            parameters["border_style"] = self.border_style
        if self.border_width is not None:
            parameters["border_width"] = self.border_width
        if self.border_color is not None:
            parameters["border_color"] = canonical_rgb_hex(self.border_color)
        if self.border_sides is not None:
            parameters["border_sides"] = ",".join(dict.fromkeys(self.border_sides))
        return parameters


class PublicMergeTableCellsInput(ContractModel):
    start_cell: PublicCellAddress
    end_cell: PublicCellAddress

    def to_parameters(self) -> Mapping[str, OperationInputValue]:
        return {"start": self.start_cell.upper(), "end": self.end_cell.upper()}


class PublicSplitTableCellInput(ContractModel):
    cell: PublicCellAddress
    columns: int = Field(ge=1, le=65_535)
    rows: int = Field(ge=1, le=65_535)
    distribute_height: bool = False
    split_mode: PublicTableSplitMode = "equal"

    @model_validator(mode="after")
    def require_meaningful_split(self) -> PublicSplitTableCellInput:
        if self.columns == 1 and self.rows == 1:
            raise PydanticCustomError(
                "public_split_one_by_one",
                "셀 분할은 칸 또는 줄 수가 2 이상이어야 합니다",
            )
        return self

    def to_parameters(self) -> Mapping[str, OperationInputValue]:
        return {
            "cell": self.cell.upper(),
            "columns": self.columns,
            "rows": self.rows,
            "distribute_height": self.distribute_height,
            "merge": False,
            "split_mode": self.split_mode,
        }
