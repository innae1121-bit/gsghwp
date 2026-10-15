from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from hwp_live_native_action_models import (
    IntegerValue,
    NativeSetter,
    ParameterActionCommand,
    TextValue,
    CaptureTableCommand,
    CellCommand,
    NativeActionCommand,
    RunCommand,
    SelectControlCommand,
)
from hwp_live_native_layout_format import cell_format_commands
from hwp_live_table_contract import CellBorder, CellBorders, TableCell
from hwp_table_address import cell_address
from hwp_live_values import ContractModel
from hwp_reference_layout_contract import (
    ReferenceLayoutBlock,
    ReferenceMerge,
    ReferenceStyle,
    StyleRegion,
    TextAnchor,
    VisibleEdge,
)
from hwp_reference_layout_gap import (
    ProtectedGap,
    protected_gap_minimums,
)
from hwp_reference_layout_geometry import (
    SectionPageGeometry,
)
from hwp_reference_layout_payload import (
    compile_reference_layout_payload,
    integer_array,
)
from hwp_reference_layout_placement import PlacementFrame
from hwp_reference_layout_native import reserve_text_row_heights


class ReferenceLayoutPatchBlock(ContractModel):
    kind: Literal["reference_layout_patch"]
    target_control_id: str = Field(
        min_length=1,
        max_length=128,
        description="빠른 구조 검사에서 읽은 기존 reference layout 표의 control ID",
    )
    source_image: Path | None = None
    row_breakpoints: tuple[float, ...] = Field(min_length=2, max_length=51)
    column_breakpoints: tuple[float, ...] = Field(min_length=2, max_length=51)
    changed_rows: tuple[int, ...] = Field(
        default=(),
        max_length=50,
        description="기존 표에서 높이를 다시 적용할 0부터 시작하는 행 인덱스",
    )
    changed_columns: tuple[int, ...] = Field(
        default=(),
        max_length=50,
        description="기존 표에서 너비를 다시 적용할 0부터 시작하는 열 인덱스",
    )
    merges: tuple[ReferenceMerge, ...] = Field(default=(), max_length=200)
    styles: tuple[ReferenceStyle, ...] = Field(default=(), max_length=256)
    style_regions: tuple[StyleRegion, ...] = Field(default=(), max_length=1_000)
    edges: tuple[VisibleEdge, ...] = Field(default=(), max_length=5_000)
    placement_edges: tuple[VisibleEdge, ...] = Field(default=(), max_length=5_000)
    placement_styles: tuple[ReferenceStyle, ...] = Field(default=(), max_length=256)
    placement_style_regions: tuple[StyleRegion, ...] = Field(
        default=(),
        max_length=1_000,
    )
    text_styles: tuple[ReferenceStyle, ...] = Field(default=(), max_length=256)
    text_anchors: tuple[TextAnchor, ...] = Field(default=(), max_length=2_500)
    protected_gaps: tuple[ProtectedGap, ...] = Field(default=(), max_length=1_000)

    @model_validator(mode="after")
    def validate_patch(self) -> ReferenceLayoutPatchBlock:
        rows = len(self.row_breakpoints) - 1
        columns = len(self.column_breakpoints) - 1
        if len(set(self.changed_rows)) != len(self.changed_rows) or any(
            row < 0 or row >= rows for row in self.changed_rows
        ):
            raise ValueError("changed rows must be unique row indexes")
        if len(set(self.changed_columns)) != len(self.changed_columns) or any(
            column < 0 or column >= columns for column in self.changed_columns
        ):
            raise ValueError("changed columns must be unique column indexes")
        if self.text_styles and not self.text_anchors:
            raise ValueError("text_styles require text_anchors")
        if not (
            self.changed_rows
            or self.changed_columns
            or self.style_regions
            or self.edges
            or self.text_anchors
        ):
            raise ValueError("reference layout patch has no changes")
        text_targets = {(anchor.row, anchor.column) for anchor in self.text_anchors}
        if len(text_targets) != len(self.text_anchors):
            raise ValueError("text anchor targets must be unique")
        _ = self.combined_styles()
        for region in self.style_regions:
            for merge in self.merges:
                overlaps = (
                    region.top < merge.row + merge.row_span
                    and merge.row < region.bottom
                    and region.left < merge.column + merge.column_span
                    and merge.column < region.right
                )
                contains_merge = (
                    region.top <= merge.row
                    and region.left <= merge.column
                    and region.bottom >= merge.row + merge.row_span
                    and region.right >= merge.column + merge.column_span
                )
                if overlaps and not contains_merge:
                    raise ValueError(
                        "patch style region cannot partially cover a merged cell"
                    )
        _ = self.as_reference_layout()
        return self

    def combined_styles(self) -> tuple[ReferenceStyle, ...]:
        combined = list(self.styles)
        by_key = {style.key: style for style in self.styles}
        text_keys: set[str] = set()
        for style in self.text_styles:
            if style.key in text_keys:
                raise ValueError("text style keys must be unique")
            text_keys.add(style.key)
            existing = by_key.get(style.key)
            if existing is not None:
                if existing != style:
                    raise ValueError(
                        "region and text styles with the same key must be identical"
                    )
                continue
            combined.append(style)
            by_key[style.key] = style
        return tuple(combined)

    def as_reference_layout(self) -> ReferenceLayoutBlock:
        return ReferenceLayoutBlock(
            kind="reference_layout",
            source_image=self.source_image,
            row_breakpoints=self.row_breakpoints,
            column_breakpoints=self.column_breakpoints,
            merges=self.merges,
            visible_edges=self.edges,
            styles=self.combined_styles(),
            style_regions=self.style_regions,
            text_anchors=self.text_anchors,
            protected_gaps=self.protected_gaps,
        )


def compile_reference_layout_patch_command(
    patch: ReferenceLayoutPatchBlock,
    page: SectionPageGeometry,
    *,
    page_number: int,
) -> ParameterActionCommand:
    layout = patch.as_reference_layout()
    area = page.usable_area(page_number=page_number)
    frame = PlacementFrame.from_reference(
        area,
        patch.source_image,
        rows=len(patch.row_breakpoints) - 1,
        columns=len(patch.column_breakpoints) - 1,
        visible_edges=patch.placement_edges or patch.edges,
        styles=patch.placement_styles or patch.styles,
        style_regions=patch.placement_style_regions or patch.style_regions,
    )
    columns = frame.map_columns(patch.column_breakpoints)
    rows = frame.map_rows(patch.row_breakpoints)
    row_heights = reserve_text_row_heights(
        layout,
        rows.sizes,
        columns.sizes,
    )
    payload = compile_reference_layout_payload(layout)
    column_array, column_values = integer_array("ColumnWidths", columns.sizes)
    row_array, row_values = integer_array("RowHeights", row_heights)
    patch_columns_array, patch_column_values = integer_array(
        "PatchColumnIndexes",
        patch.changed_columns,
    )
    patch_rows_array, patch_row_values = integer_array(
        "PatchRowIndexes",
        patch.changed_rows,
    )
    gap_array, gap_values = integer_array(
        "GapMinimums",
        protected_gap_minimums(
            patch.protected_gaps,
            row_heights,
            columns.sizes,
        ),
    )
    return ParameterActionCommand(
        action="ReferenceLayoutPatch",
        parameter_set="HTableCreation",
        setters=(
            NativeSetter("Rows", IntegerValue(len(rows.sizes))),
            NativeSetter("Columns", IntegerValue(len(columns.sizes))),
            NativeSetter("BaseStyleId", IntegerValue(0)),
            NativeSetter("BodyLeft", IntegerValue(frame.left)),
            NativeSetter("BodyTop", IntegerValue(frame.top)),
            NativeSetter("BodyWidth", IntegerValue(frame.width)),
            NativeSetter("BodyHeight", IntegerValue(frame.height)),
            NativeSetter("TargetControlId", TextValue(patch.target_control_id)),
        ),
        arrays=tuple(
            array
            for array in (
                column_array,
                row_array,
                patch_columns_array,
                patch_rows_array,
                gap_array,
                *payload.arrays,
            )
            if array.count > 0
        ),
        array_values=(
            *column_values,
            *row_values,
            *patch_column_values,
            *patch_row_values,
            *gap_values,
            *payload.values,
        ),
    )


def compile_reference_layout_patch_commands(
    patch: ReferenceLayoutPatchBlock,
    page: SectionPageGeometry,
    *,
    page_number: int,
) -> tuple[NativeActionCommand, ...]:
    """Apply appearance-only patches without resetting inherited text styles.

    CellBorder writes real cell sides; CellZoneBorder/SelCellsBorderFill can
    return success while leaving those sides unchanged in existing tables.
    Keep the native patch topology check, then use the established CellFill
    and CellBorder actions for the requested owners only.
    """
    appearance_only = not (
        patch.changed_rows or patch.changed_columns or patch.text_anchors
    ) and all(style.model_fields_set <= {"key", "fill_color"} for style in patch.styles)
    if not appearance_only:
        return (
            compile_reference_layout_patch_command(
                patch, page, page_number=page_number
            ),
        )
    rows, columns = len(patch.row_breakpoints) - 1, len(patch.column_breakpoints) - 1
    owners = {(r, c): (r, c) for r in range(rows) for c in range(columns)}
    for merge in patch.merges:
        for r in range(merge.row, merge.row + merge.row_span):
            for c in range(merge.column, merge.column + merge.column_span):
                owners[r, c] = (merge.row, merge.column)
    fills = {}
    styles = {style.key: style for style in patch.styles}
    for region in patch.style_regions:
        color = styles[region.style_key].fill_color
        if color is not None:
            for r in range(region.top, region.bottom):
                for c in range(region.left, region.right):
                    fills[owners[r, c]] = color
    borders: dict[tuple[int, int], dict[str, CellBorder]] = {}
    for edge in patch.edges:
        border = CellBorder(style=edge.style, width=edge.width, color=edge.color)
        for position in range(edge.start, edge.end):
            if edge.orientation == "vertical":
                adjacent = (
                    (position, edge.line - 1, "right"),
                    (position, edge.line, "left"),
                )
            else:
                adjacent = (
                    (edge.line - 1, position, "bottom"),
                    (edge.line, position, "top"),
                )
            for r, c, side in adjacent:
                if (r, c) in owners:
                    borders.setdefault(owners[r, c], {})[side] = border
    topology_check = patch.model_copy(update={"styles": (), "style_regions": ()})
    commands: list[NativeActionCommand] = []
    if patch.edges:
        commands.append(
            compile_reference_layout_patch_command(
                topology_check, page, page_number=page_number
            )
        )
    commands.extend(
        (SelectControlCommand(patch.target_control_id), CaptureTableCommand())
    )
    for r, c in sorted(fills.keys() | borders.keys()):
        cell = TableCell(
            fill_color=fills.get((r, c)),
            borders=CellBorders.model_validate(borders[r, c])
            if (r, c) in borders
            else None,
        )
        commands.extend(
            (
                CellCommand(cell_address(r, c)),
                RunCommand("TableCellBlock"),
                *cell_format_commands(cell),
                RunCommand("Cancel"),
            )
        )
    commands.append(RunCommand("Cancel"))
    return tuple(commands)
