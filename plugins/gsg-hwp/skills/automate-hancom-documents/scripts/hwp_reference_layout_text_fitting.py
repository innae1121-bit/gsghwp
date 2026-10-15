from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Final

from hwp_live_table_contract import CellPadding
from hwp_reference_layout_contract import (
    ReferenceLayoutBlock,
    ReferenceStyle,
    StyleRegion,
    TextAnchor,
)
from hwp_reference_layout_text_fit_metrics import TextCellFit
from hwp_reference_layout_text_fit_optimizer import fit_text_to_cell
from hwp_reference_layout_text_flow import style_for_text
from hwp_table_address import cell_address


_TEXT_STYLE_FIELDS = (
    "font_name",
    "font_size_pt",
    "bold",
    "text_color",
    "alignment",
    "width_ratio_percent",
    "letter_spacing_percent",
    "line_spacing_type",
    "line_spacing_percent",
    "line_spacing_hwpunit",
    "paragraph_before_mm",
    "paragraph_after_mm",
)


# 목록 상한. 한 참조 레이아웃이 수백 개 셀을 조정할 수 있어서 셀 목록은 잘라
# 싣는다. 잘린 개수(omitted_cells)와 집계(최소 도달 글자 크기·장평·줄간격)는
# 언제나 실으므로, 상한이 최악의 값을 감추지는 못한다. 목록 자체도 가장 많이
# 줄어든 셀부터 싣는다.
FIT_REPORT_CELL_LIMIT: Final = 24

# 사실 한 줄의 이름표. 이 접두사로 사실을 찾아 다른 응답으로 옮기는 쪽이 있다
# (hwp_layout_preflight.reference_text_fit_message_fact).
REFERENCE_TEXT_FIT_FACT_PREFIX: Final = "reference_text_fit="


@dataclass(frozen=True, slots=True)
class ReferenceTextFitAdjustment:
    """fit 캐스케이드가 한 셀에서 실제로 바꾼 값. (선언값, 최종값) 쌍이다.

    ``fit_text_to_cell`` 이 돌려준 최종 스타일을 앵커가 선언한 스타일과
    맞대 본 결과이지, 캐스케이드가 스스로 적어 준 값이 아니다. 그래서 축소
    로직·순서·한계값은 이 기록과 무관하다 -- 여기서 하는 일은 이미 일어난
    일을 읽는 것뿐이다. 바뀌지 않은 항목은 ``None`` 이고, 아무 항목도 바뀌지
    않은 셀은 기록 자체가 만들어지지 않는다.
    """

    row: int
    column: int
    font_size_pt: tuple[float, float] | None = None
    width_ratio_percent: tuple[int, int] | None = None
    letter_spacing_percent: tuple[int, int] | None = None
    line_spacing_percent: tuple[int, int] | None = None
    line_spacing_hwpunit: tuple[int, int] | None = None
    padding_mm: tuple[float, float] | None = None

    @property
    def cell(self) -> str:
        return cell_address(self.row, self.column)

    @property
    def font_scale_percent(self) -> int | None:
        if self.font_size_pt is None or self.font_size_pt[0] <= 0:
            return None
        declared, applied = self.font_size_pt
        return round(applied * 100 / declared)

    def facts(self) -> dict[str, object]:
        facts: dict[str, object] = {"cell": self.cell}
        if self.font_size_pt is not None:
            facts["font_size_pt"] = [self.font_size_pt[0], self.font_size_pt[1]]
            scale = self.font_scale_percent
            if scale is not None:
                facts["font_scale_percent"] = scale
        if self.width_ratio_percent is not None:
            facts["width_ratio_percent"] = [
                self.width_ratio_percent[0],
                self.width_ratio_percent[1],
            ]
        if self.letter_spacing_percent is not None:
            facts["letter_spacing_percent"] = [
                self.letter_spacing_percent[0],
                self.letter_spacing_percent[1],
            ]
        if self.line_spacing_percent is not None:
            facts["line_spacing_percent"] = [
                self.line_spacing_percent[0],
                self.line_spacing_percent[1],
            ]
        if self.line_spacing_hwpunit is not None:
            facts["line_spacing_hwpunit"] = [
                self.line_spacing_hwpunit[0],
                self.line_spacing_hwpunit[1],
            ]
        if self.padding_mm is not None:
            facts["padding_mm"] = [self.padding_mm[0], self.padding_mm[1]]
        return facts


def _severity(item: ReferenceTextFitAdjustment) -> tuple[int, int, int, int, int]:
    """작을수록 많이 줄어든 셀. 같은 값이면 행·열 순서로 고정한다."""
    scale = item.font_scale_percent
    return (
        100 if scale is None else scale,
        100 if item.width_ratio_percent is None else item.width_ratio_percent[1],
        100 if item.line_spacing_percent is None else item.line_spacing_percent[1],
        item.row,
        item.column,
    )


@dataclass(frozen=True, slots=True)
class ReferenceTextFitReport:
    """한 참조 레이아웃 블록에서 캐스케이드가 조정한 셀 전부."""

    adjustments: tuple[ReferenceTextFitAdjustment, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.adjustments)

    def facts(self, *, block_index: int) -> dict[str, object]:
        ordered = sorted(self.adjustments, key=_severity)
        listed = ordered[:FIT_REPORT_CELL_LIMIT]
        font_sizes: list[float] = []
        font_scales: list[int] = []
        width_ratios: list[int] = []
        line_spacings: list[int] = []
        padding_reduced = 0
        padding_removed = 0
        for item in self.adjustments:
            if item.font_size_pt is not None:
                font_sizes.append(item.font_size_pt[1])
                scale = item.font_scale_percent
                if scale is not None:
                    font_scales.append(scale)
            if item.width_ratio_percent is not None:
                width_ratios.append(item.width_ratio_percent[1])
            if item.line_spacing_percent is not None:
                line_spacings.append(item.line_spacing_percent[1])
            if item.padding_mm is not None:
                padding_reduced += 1
                if item.padding_mm[1] == 0:
                    padding_removed += 1
        facts: dict[str, object] = {
            "block_index": block_index,
            "adjusted_cells": len(self.adjustments),
            "listed_cells": len(listed),
            "omitted_cells": len(self.adjustments) - len(listed),
            "font_size_reduced_cells": len(font_sizes),
            "padding_reduced_cells": padding_reduced,
            "padding_removed_cells": padding_removed,
        }
        if font_sizes:
            facts["minimum_font_size_pt"] = min(font_sizes)
        if font_scales:
            facts["minimum_font_scale_percent"] = min(font_scales)
        if width_ratios:
            facts["minimum_width_ratio_percent"] = min(width_ratios)
        if line_spacings:
            facts["minimum_line_spacing_percent"] = min(line_spacings)
        facts["cells_order"] = "most_shrunk_first"
        facts["cells"] = [item.facts() for item in listed]
        return facts


def reference_text_fit_fact(
    report: ReferenceTextFitReport,
    *,
    block_index: int,
) -> str:
    """봉인된 응답 스키마를 건드리지 않고 사실을 싣기 위한 한 줄.

    ``hwp_preflight_layout`` 의 outputSchema 는 compatibility-manifest.json 의
    ``legacy_tool_schema_hashes`` 가 봉인한 값이라 보이는 필드를 더할 수 없다.
    그래서 이미 있는 문자열 통로(``estimate_basis``)에 압축 JSON 사실로 싣는다
    -- ``hwp_live_operation_recipe._post_write_layout_facts`` 가 쓰기 응답에서
    쓰는 것과 같은 수법이다.
    """
    return REFERENCE_TEXT_FIT_FACT_PREFIX + json.dumps(
        report.facts(block_index=block_index),
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _padding_total_mm(padding: CellPadding | None) -> float:
    if padding is None:
        return 0.0
    return round(
        padding.left_mm + padding.right_mm + padding.top_mm + padding.bottom_mm,
        3,
    )


def _fit_adjustment(
    anchor: TextAnchor,
    declared: ReferenceStyle,
    declared_padding: CellPadding | None,
    fitted: TextCellFit,
) -> ReferenceTextFitAdjustment | None:
    applied = fitted.style
    font: tuple[float, float] | None = None
    if (
        declared.font_size_pt is not None
        and applied.font_size_pt is not None
        and applied.font_size_pt != declared.font_size_pt
    ):
        font = (declared.font_size_pt, applied.font_size_pt)
    width: tuple[int, int] | None = None
    if applied.width_ratio_percent != declared.width_ratio_percent:
        width = (declared.width_ratio_percent, applied.width_ratio_percent)
    letter: tuple[int, int] | None = None
    if applied.letter_spacing_percent != declared.letter_spacing_percent:
        letter = (
            declared.letter_spacing_percent,
            applied.letter_spacing_percent,
        )
    # 캐스케이드가 None 을 100%(퍼센트) 또는 0(고정·여백)으로 읽으므로
    # 여기서도 같게 읽는다. 그러지 않으면 선언을 생략한 셀의 줄간격 축소가
    # "바뀐 것 없음"으로 보고된다.
    declared_percent = declared.line_spacing_percent or 100
    applied_percent = applied.line_spacing_percent or 100
    percent: tuple[int, int] | None = (
        (declared_percent, applied_percent)
        if applied_percent != declared_percent
        else None
    )
    declared_hwpunit = declared.line_spacing_hwpunit or 0
    applied_hwpunit = applied.line_spacing_hwpunit or 0
    hwpunit: tuple[int, int] | None = (
        (declared_hwpunit, applied_hwpunit)
        if applied_hwpunit != declared_hwpunit
        else None
    )
    before = _padding_total_mm(declared_padding)
    after = _padding_total_mm(fitted.padding)
    padding = (before, after) if after != before else None
    if all(value is None for value in (font, width, letter, percent, hwpunit, padding)):
        return None
    return ReferenceTextFitAdjustment(
        row=anchor.row,
        column=anchor.column,
        font_size_pt=font,
        width_ratio_percent=width,
        letter_spacing_percent=letter,
        line_spacing_percent=percent,
        line_spacing_hwpunit=hwpunit,
        padding_mm=padding,
    )


def _cell_span(
    block: ReferenceLayoutBlock,
    anchor: TextAnchor,
) -> tuple[int, int]:
    for merge in block.merges:
        if merge.row == anchor.row and merge.column == anchor.column:
            return merge.row_span, merge.column_span
    return 1, 1


def _combined_style(
    text_style: ReferenceStyle,
    region_style: ReferenceStyle | None,
    padding: CellPadding | None,
) -> ReferenceStyle:
    if region_style is None:
        values = text_style.model_dump()
    else:
        values = region_style.model_dump()
        for field in _TEXT_STYLE_FIELDS:
            values[field] = getattr(text_style, field)
        if text_style.vertical_alignment != "inherit":
            values["vertical_alignment"] = text_style.vertical_alignment
    values["padding"] = None if padding is None else padding.model_dump()
    return ReferenceStyle.model_validate(values)


def fit_reference_text_styles_with_report(
    block: ReferenceLayoutBlock,
    row_heights: tuple[int, ...],
    column_widths: tuple[int, ...],
) -> tuple[ReferenceLayoutBlock, ReferenceTextFitReport]:
    styles = list(block.styles)
    regions = list(block.style_regions)
    anchors: list[TextAnchor] = []
    adjustments: list[ReferenceTextFitAdjustment] = []
    known_keys = {style.key for style in styles}
    derived: dict[str, str] = {}
    next_key = 1

    def materialize(style: ReferenceStyle) -> str:
        nonlocal next_key
        fingerprint = style.model_dump_json(exclude={"key"})
        existing = derived.get(fingerprint)
        if existing is not None:
            return existing
        while f"fit{next_key:03d}" in known_keys:
            next_key += 1
        key = f"fit{next_key:03d}"
        next_key += 1
        candidate = style.model_copy(update={"key": key})
        styles.append(candidate)
        known_keys.add(key)
        derived[fingerprint] = key
        return key

    for anchor in block.text_anchors:
        text_style, region_style = style_for_text(block, anchor)
        if text_style is None or text_style.font_size_pt is None:
            anchors.append(anchor)
            continue
        marker = anchor.text.strip()
        if len(marker) == 1 and not marker.isalnum():
            anchors.append(anchor)
            continue
        row_span, column_span = _cell_span(block, anchor)
        available_width = sum(
            column_widths[anchor.column : anchor.column + column_span]
        )
        available_height = sum(
            row_heights[anchor.row : anchor.row + row_span]
        )
        fitted = fit_text_to_cell(
            anchor,
            text_style,
            region_style,
            available_width=available_width,
            available_height=available_height,
        )
        if fitted.style == text_style and not fitted.padding_changed:
            anchors.append(anchor)
            continue
        adjustment = _fit_adjustment(
            anchor,
            text_style,
            region_style.padding if region_style is not None else None,
            fitted,
        )
        if adjustment is not None:
            adjustments.append(adjustment)
        combined = _combined_style(
            fitted.style,
            region_style,
            fitted.padding,
        )
        key = materialize(combined)
        anchors.append(anchor.model_copy(update={"style_key": key}))
        if fitted.padding_changed:
            regions.append(
                StyleRegion(
                    top=anchor.row,
                    left=anchor.column,
                    bottom=anchor.row + row_span,
                    right=anchor.column + column_span,
                    style_key=key,
                )
            )

    return (
        ReferenceLayoutBlock.model_validate(
            {
                **block.model_dump(),
                "styles": styles,
                "style_regions": regions,
                "text_anchors": anchors,
            }
        ),
        ReferenceTextFitReport(tuple(adjustments)),
    )


def fit_reference_text_styles(
    block: ReferenceLayoutBlock,
    row_heights: tuple[int, ...],
    column_widths: tuple[int, ...],
) -> ReferenceLayoutBlock:
    return fit_reference_text_styles_with_report(
        block,
        row_heights,
        column_widths,
    )[0]
