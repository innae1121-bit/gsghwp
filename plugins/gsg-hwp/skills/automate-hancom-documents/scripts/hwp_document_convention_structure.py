from __future__ import annotations

from collections.abc import Iterable, Mapping
import re
from types import MappingProxyType
from typing import Final, cast

from pydantic import JsonValue

from hwp_document_convention_axis import build_axis, unobserved_axis
from hwp_document_convention_contract import ConventionAxis
from hwp_document_convention_text import numeric_display_pattern
from hwp_live_structure_contract import (
    CellBorderObservation,
    DocumentStructure,
    StructurePosition,
)


def _position(position: StructurePosition | None) -> JsonValue:
    return (
        None if position is None else cast(JsonValue, position.model_dump(mode="json"))
    )


def _border(border: CellBorderObservation) -> JsonValue:
    return cast(JsonValue, border.model_dump(mode="json"))


# Where an object-layout observation may be carried in a layout plan, by the
# field path the public block schema actually has. These are coordinates for
# the model, not an automatic write: several of them are the *only* route an
# axis has, because the value arrives as a raw enumeration number this
# repository has never measured the name of.
#
#   * ``table_borders`` and the border half of the fill/height policy axis
#     report ``HCellBorderFill/BorderType*`` and ``BorderWidth*`` as numbers,
#     while ``CellBorder.style``/``width`` accept only names; HWP resolves the
#     name at write time through ``hwp.HwpLineType``/``HwpLineWidth`` and the
#     reverse direction is measured nowhere here.
#   * ``table_cell_alignment``'s vertical half is ``ShapeTableCell/VertAlign``,
#     which ``TableInspection.cpp`` notes has no write-path counterpart in this
#     repository at all -- vertical alignment is written by the
#     ``TableVAlign*`` actions instead.
#
# ``table_header_repetition`` stays empty on purpose: the field it would name
# (``blocks[].repeat_header``) exists, but the axis is permanently unobserved,
# and pointing at a destination for evidence that never arrives would read as
# a promise. Every other empty tuple below is the same kind of statement.
TABLE_AXIS_WRITE_FIELDS: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        "table_topology": ("blocks[].merges",),
        "table_header_repetition": (),
        "table_cell_alignment": (
            "blocks[].rows[][].alignment",
            "blocks[].rows[][].vertical_alignment",
        ),
        "table_font_weight": (
            "blocks[].rows[][].font_name",
            "blocks[].rows[][].font_size_pt",
            "blocks[].rows[][].bold",
        ),
        "table_borders": ("blocks[].rows[][].borders",),
        "table_border_fill_row_height_policy": (
            "blocks[].rows[][].fill_color",
            "blocks[].rows[][].borders",
            "blocks[].row_heights_mm",
        ),
        "numeric_display_pattern": ("blocks[].rows[][].text",),
        "caption_placement": (
            "blocks[].caption",
            "blocks[].caption_style_id",
        ),
        "caption_sequence": ("blocks[].caption",),
        "figure_layout": ("blocks[].width_mm", "blocks[].height_mm"),
    }
)


def _sampled_axis(
    name: str,
    observations: Iterable[tuple[JsonValue, int]],
) -> ConventionAxis:
    return build_axis(
        name,
        observations,
        write_fields=TABLE_AXIS_WRITE_FIELDS[name],
        scope="current-page",
        complete=False,
    )


def section_axes(
    page_setup: Mapping[str, JsonValue] | None,
    observation_error: str | None,
) -> tuple[ConventionAxis, ...]:
    reason = (observation_error or "현재 구역의 용지 설정을 읽지 못했습니다.")[:300]
    geometry = (
        build_axis(
            "page_geometry_by_section",
            ((cast(JsonValue, dict(page_setup)), 0),),
            scope="current-section",
            complete=False,
        )
        if page_setup is not None
        else build_axis(
            "page_geometry_by_section",
            (),
            scope="current-section",
            unobserved_reason=reason,
        )
    )
    return (
        unobserved_axis(
            "section_boundaries",
            "현재 공개 관측은 다음 구역 시작 위치를 문서 전체 목록으로 만들지 않습니다.",
        ),
        geometry,
    )


def object_layout_axes(
    structure: DocumentStructure | None,
    observation_error: str | None,
) -> tuple[ConventionAxis, ...]:
    reason = (observation_error or "현재 쪽의 개체 구조 표본을 읽지 못했습니다.")[:300]
    if structure is None:
        return tuple(
            build_axis(
                name,
                (),
                write_fields=write_fields,
                scope="current-page",
                unobserved_reason=reason,
            )
            for name, write_fields in TABLE_AXIS_WRITE_FIELDS.items()
        )
    topology: list[tuple[JsonValue, int]] = []
    alignments: list[tuple[JsonValue, int]] = []
    fonts: list[tuple[JsonValue, int]] = []
    borders: list[tuple[JsonValue, int]] = []
    numeric_display: list[tuple[JsonValue, int]] = []
    captions: list[tuple[JsonValue, int]] = []
    caption_sequence: list[tuple[JsonValue, int]] = []
    table_border_fill_heights: list[tuple[JsonValue, int]] = []
    for table in structure.tables:
        paragraph = table.anchor.paragraph
        topology.append(
            (
                cast(
                    JsonValue,
                    {
                        "rows": table.rows,
                        "columns": table.columns,
                        "merges": [
                            {
                                "row": merge.row,
                                "column": merge.column,
                                "row_span": merge.row_span,
                                "column_span": merge.column_span,
                            }
                            for merge in table.merges
                        ],
                    },
                ),
                paragraph,
            )
        )
        heights = {
            cell.address: cell.height_hwpunit
            for cell in table.cells
            if cell.height_hwpunit is not None
        }
        for observed in table.cell_formats:
            alignments.append(
                (
                    cast(
                        JsonValue,
                        {
                            "horizontal": observed.alignment,
                            "vertical": observed.vertical_align,
                        },
                    ),
                    paragraph,
                )
            )
            fonts.append(
                (
                    cast(
                        JsonValue,
                        {
                            "face_name": observed.face_name,
                            "height_hwpunit": observed.character_height,
                            "bold": observed.bold,
                        },
                    ),
                    paragraph,
                )
            )
            table_border_fill_heights.append(
                (
                    cast(
                        JsonValue,
                        {
                            "addresses": list(observed.addresses),
                            "fill_color": observed.fill_color,
                            "fill_brush": observed.fill_brush,
                            "left": _border(observed.border_left),
                            "right": _border(observed.border_right),
                            "top": _border(observed.border_top),
                            "bottom": _border(observed.border_bottom),
                            "row_heights_hwpunit": {
                                address: heights.get(address)
                                for address in observed.addresses
                            },
                        },
                    ),
                    paragraph,
                )
            )
            borders.append(
                (
                    cast(
                        JsonValue,
                        {
                            "left": _border(observed.border_left),
                            "right": _border(observed.border_right),
                            "top": _border(observed.border_top),
                            "bottom": _border(observed.border_bottom),
                        },
                    ),
                    paragraph,
                )
            )
        numeric_display.extend(
            (pattern, paragraph)
            for cell in table.cells
            if (pattern := numeric_display_pattern(cell.text)) is not None
        )
        if table.caption is not None:
            captions.append(
                (
                    cast(
                        JsonValue,
                        {
                            "text": table.caption.text,
                            "automatic_number": table.caption.automatic_number,
                            "style_id": table.caption.style_id,
                            "style_name": table.caption.style_name,
                            "preceding": _position(table.preceding_paragraph),
                            "anchor": _position(table.anchor),
                            "following": _position(table.following_paragraph),
                        },
                    ),
                    paragraph,
                )
            )
            number = re.search(r"\d+(?:[.]\d+)*", table.caption.text)
            caption_sequence.append(
                (
                    cast(
                        JsonValue,
                        {
                            "automatic_number": table.caption.automatic_number,
                            "number": number.group(0) if number else None,
                        },
                    ),
                    paragraph,
                )
            )
    figures = (
        (
            cast(
                JsonValue,
                {
                    "kind": control.kind,
                    "width_mm": control.width_mm,
                    "height_mm": control.height_mm,
                    "page_start": control.page_start,
                    "page_end": control.page_end,
                    "anchor": _position(control.anchor),
                },
            ),
            control.anchor.paragraph,
        )
        for control in structure.controls
        if control.kind in {"picture", "shape"}
    )
    return (
        _sampled_axis("table_topology", topology),
        build_axis(
            "table_header_repetition",
            (),
            write_fields=TABLE_AXIS_WRITE_FIELDS["table_header_repetition"],
            scope="current-page",
            unobserved_reason="현재 구조 응답에는 반복 머리행 여부가 없습니다.",
        ),
        _sampled_axis("table_cell_alignment", alignments),
        _sampled_axis("table_font_weight", fonts),
        _sampled_axis("table_borders", borders),
        _sampled_axis(
            "table_border_fill_row_height_policy",
            table_border_fill_heights,
        ),
        _sampled_axis("numeric_display_pattern", numeric_display),
        _sampled_axis("caption_placement", captions),
        _sampled_axis("caption_sequence", caption_sequence),
        _sampled_axis("figure_layout", figures),
    )
