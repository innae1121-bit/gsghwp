from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal, cast

from pydantic import Field

from hwp_errors import HwpLiveError
from hwp_live_contract import (
    ImageBlock,
    LayoutPlan,
    PageBreakBlock,
    ParagraphBlock,
    TableBlock,
    TableCell,
)
from hwp_live_native_action_models import IntegerValue, ParameterActionCommand
from hwp_live_values import ContractModel
from hwp_reference_layout_contract import ReferenceLayoutBlock, ReferenceStyle
from hwp_reference_layout_evidence import prepare_reference_layout
from hwp_reference_layout_geometry import (
    HWPUNITS_PER_INCH,
    MILLIMETERS_PER_INCH,
    SectionPageGeometry,
)
from hwp_reference_layout_native import (
    compile_reference_layout_command_with_fit_report,
)
from hwp_reference_layout_patch import ReferenceLayoutPatchBlock
from hwp_reference_layout_text_fitting import (
    REFERENCE_TEXT_FIT_FACT_PREFIX,
    ReferenceTextFitReport,
    reference_text_fit_fact,
)
from hwp_reference_layout_text_flow import character_em_width, wrapped_line_count


LayoutOverflow = Literal["none", "possible", "definite"]


class LayoutPreflightProblem(ContractModel):
    block_index: int = Field(ge=0)
    block_kind: str
    reason_code: str
    estimated_width_mm: float = Field(ge=0)
    estimated_height_mm: float = Field(ge=0)


class LayoutPreflightBlock(ContractModel):
    block_index: int = Field(ge=0)
    page_number: int
    block_kind: str
    estimated_width_mm: float = Field(ge=0)
    estimated_height_mm: float = Field(ge=0)


class LayoutPreflightPage(ContractModel):
    """Explicit plan segment, not a prediction of HWP's automatic pagination."""

    page_number: int
    block_indices: tuple[int, ...]
    estimated_height_mm: float = Field(ge=0)
    estimated_height_uncertainty_mm: float = Field(ge=0)
    estimated_height_upper_mm: float = Field(ge=0)
    remaining_height_mm: float
    remaining_height_lower_mm: float
    trailing_paragraph_uncertainty_mm: float = Field(default=0.0, ge=0)


class LayoutObservations(ContractModel):
    pages: tuple[LayoutPreflightPage, ...] = ()
    blocks: tuple[LayoutPreflightBlock, ...] = ()


LAYOUT_OBSERVATIONS_PREFIX: Final = "layout_observations="


class LayoutPreflightResult(ContractModel):
    usable_width_mm: float = Field(gt=0)
    usable_height_mm: float = Field(gt=0)
    estimated_width_mm: float = Field(ge=0)
    estimated_height_mm: float = Field(ge=0)
    overflow: LayoutOverflow
    reason_codes: tuple[str, ...] = ()
    problem_blocks: tuple[LayoutPreflightProblem, ...] = ()
    adjustable_items: tuple[str, ...] = ()
    safe_to_write: bool
    # Additive. ``estimated_height_mm`` alone reads as a measurement and was
    # read as one: a plan estimated at 218.483mm against 234.502mm of usable
    # height returned overflow="none" and then spilled onto a second page
    # (operation-journal f5264e71e6, page_count_delta 1). The 16mm of apparent
    # slack was smaller than what this estimator does not know, and nothing in
    # the response said so. These three fields say it.
    estimated_height_uncertainty_mm: float = Field(default=0.0, ge=0)
    estimated_height_upper_mm: float = Field(default=0.0, ge=0)
    estimate_basis: tuple[str, ...] = ()
    # The verdict from the plan's own stated geometry, without the uncertainty
    # band above. ``overflow`` may be "possible" where this is still "none";
    # that difference is exactly the band. Callers that gated on the old
    # ``overflow == "none"`` read this instead, so widening the band never
    # turns into a new refusal.
    stated_geometry_overflow: LayoutOverflow = "none"


@dataclass(frozen=True, slots=True)
class _BlockEstimate:
    width_mm: float
    height_mm: float
    minimum_height_mm: float
    uncertain: bool
    reason_codes: tuple[str, ...]
    adjustable_items: tuple[str, ...]
    definite: bool = False
    # How much taller than ``height_mm`` this block can render for reasons this
    # estimator can name but not measure. One-sided on purpose: every entry is
    # a way HWP grows a block, never a way it shrinks one.
    uncertainty_mm: float = 0.0
    # Human-readable names of what the numbers above rest on.
    basis: tuple[str, ...] = ()
    # 참조 레이아웃에서만 채워진다. 이 블록의 높이·너비는 텍스트 맞춤이 글자를
    # 줄인 *뒤*의 값이므로, 무엇을 얼마나 줄였는지가 곧 이 추정의 근거다.
    text_fit: ReferenceTextFitReport | None = None


# 25.4mm per inch / 72pt per inch. An exact unit conversion, not a tuning
# constant -- pt and mm are both defined against the inch.
MILLIMETERS_PER_POINT: Final = 25.4 / 72

# What a paragraph's height falls back to when nobody could read the real
# thing. These are not claims about typography and they are not safety
# margins: they are the previous behaviour, kept so an unreadable style does
# not change any number that used to be reported. What is new is that a block
# resting on them says so (``FONT_SIZE_UNRESOLVED`` / ``LINE_SPACING_UNRESOLVED``),
# because they are frequently wrong and wrong in the dangerous direction. In
# the document this was measured against (test.hwp, 237 paragraphs scanned,
# convention-cache-v2 db85fc80) the dominant observed body is 11pt (56% of
# paragraphs; 10pt is 6.8%) at 200% line spacing (50%; 160% is 18%), which
# makes a defaulted line 5.64mm against a real 7.76mm -- 2.1mm, or 37%, short
# on every line of every block that lands here.
_ASSUMED_FONT_SIZE_PT: Final = 10.0
_ASSUMED_LINE_SPACING: Final = 1.6

# One line of the document's own body, as this module assumes it. Used only as
# the size of an *uncertainty*, never as an estimate: it is what one paragraph
# of unstated height costs, and it is deliberately the conservative (smaller)
# end -- the same measured distribution quoted above puts the real line at
# 11pt/200% = 7.76mm, so a band built from it understates rather than overstates.
_ASSUMED_DOCUMENT_LINE_MM: Final = (
    _ASSUMED_FONT_SIZE_PT * MILLIMETERS_PER_POINT * _ASSUMED_LINE_SPACING
)

# Names for what an estimate rests on, reported in ``estimate_basis``.
_BASIS_ASSUMED_FONT: Final = (
    f"assumed_font_size_pt={_ASSUMED_FONT_SIZE_PT:g}"
    " (no font was read for at least one block)"
)
_BASIS_ASSUMED_LINE_SPACING: Final = (
    f"assumed_line_spacing_percent={_ASSUMED_LINE_SPACING * 100:g}"
    " (no line spacing was read for at least one block)"
)
_BASIS_ROW_HEIGHT_IS_MINIMUM: Final = (
    "row_heights_mm is a floor, not a cap: HWP grows a row its own content"
    " outgrows, so the taller of the pin and the cell content is charged"
)
_BASIS_ANCHOR_SPACING: Final = (
    "a table or picture sits in an anchor paragraph whose before/after spacing"
    " comes from the document style, not from this plan"
    " (hwp_live_native_layout.build_native_layout_request writes none)"
)
_BASIS_TRAILING_PARAGRAPH: Final = (
    "the applier opens one paragraph after the last table or picture"
    " (ActionImageCaption.cpp LeaveTable BreakPara /"
    " hwp_live_native_layout.py needs_break) and its height is the document's"
)
_BASIS_WRAP_LOWER_BOUND: Final = (
    "line counts ignore word boundaries and are a lower bound; the band carries"
    " the difference against the word-aware model"
)
# 참조 레이아웃의 텍스트 맞춤이 선언된 서식을 실제로 줄였다는 사실.
#
# ``hwp_reference_layout_text_fit_optimizer.fit_text_to_cell`` 은 선언된 셀에
# 글이 안 들어가면 여백 → 줄간격(50%) → 자간(-5%) → 장평(50%) → 글자 크기
# (선언값의 75%) 순서로 줄인다. 그 결과가 어디에도 보고되지 않아, 모델은
# 렌더를 눈으로 보기 전에는 9pt 로 시킨 글이 6.75pt·장평 50% 로 들어갔다는
# 것을 알 수 없었다. 이 코드와, ``estimate_basis`` 에 함께 실리는
# ``reference_text_fit=`` 사실이 그 공백을 메운다.
#
# 판정은 바꾸지 않는다: overflow·safe_to_write 는 종전 그대로다.
REFERENCE_TEXT_AUTO_SHRUNK_REASON_CODE: Final = "REFERENCE_TEXT_AUTO_SHRUNK"
_FIT_RELAY_OMITTED_PREFIX: Final = "reference_text_fit_blocks_omitted="

# 쓰기 응답의 message 는 4,000자 상한이 걸린 계약 필드다
# (hwp_operation_contract.OperationResult.message). 사전 검사 응답에는 그 상한이
# 없어 셀 목록을 24개까지 싣지만, 그 문자열을 그대로 쓰기 메시지로 옮기면 참조
# 블록이 둘만 돼도 상한을 넘어 쓰기 자체가 검증 오류로 죽는다. 그래서 옮길
# 때는 남은 자리만큼만 싣고, 못 실은 셀·블록은 개수로 적는다. 집계
# (조정 셀 수·최소 도달 글자 크기)는 어느 경우에도 잘리지 않는다.
#
# 값이 계약과 어긋나지 않는지는 tests/test_hwp_reference_layout_fit_reporting.py
# 가 OperationResult.message 의 실제 max_length 와 맞대어 지킨다.
LAYOUT_MESSAGE_FIELD_LIMIT: Final = 4_000
# 잘린 블록 수를 적는 한 줄(과 그 앞의 빈칸)은 언제나 실릴 수 있게 자리를
# 먼저 떼어 둔다. 블록은 계획당 최대 네 자리 수를 넘지 않는다.
_FIT_RELAY_OMITTED_RESERVE: Final = len(_FIT_RELAY_OMITTED_PREFIX) + 5


def _fit_fact_without_cells(entry: str) -> str:
    """셀 목록만 덜어 낸 같은 사실. 덜어 낸 개수를 대신 적는다."""
    payload = cast(
        "dict[str, object]",
        json.loads(entry.removeprefix(REFERENCE_TEXT_FIT_FACT_PREFIX)),
    )
    cells = payload.get("cells")
    payload["cells"] = []
    payload["cells_omitted_for_message_budget"] = (
        len(cast("list[object]", cells)) if isinstance(cells, list) else 0
    )
    return REFERENCE_TEXT_FIT_FACT_PREFIX + json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def reference_text_fit_message_fact(
    estimate_basis: Sequence[str],
    *,
    budget: int,
) -> str:
    """쓰기 응답 메시지에 붙일 자동 축소 사실. 축소가 없었으면 빈 문자열.

    사전 검사가 이미 잰 것을 옮겨 싣기만 한다 -- 쓰기 경로는 같은 계획을 같은
    용지로 컴파일하므로 두 값은 같은 맞춤의 결과다. 새로 재지 않는다.

    결과 객체가 아니라 ``estimate_basis`` 자체를 받는다. 쓰기 경로의 시험들은
    사전 검사를 ``SimpleNamespace(overflow="none")`` 으로 바꿔 끼우므로, 부르는
    쪽이 ``getattr(preflight, "estimate_basis", ())`` 로 넘긴다 -- 바로 옆
    ``adjustable_items`` 가 이미 쓰고 있는 방식이다.
    """
    entries = [
        entry
        for entry in estimate_basis
        if entry.startswith(REFERENCE_TEXT_FIT_FACT_PREFIX)
    ]
    if not entries:
        return ""
    relayed: list[str] = []
    remaining = budget - _FIT_RELAY_OMITTED_RESERVE
    omitted = 0
    for entry in entries:
        candidate = entry if len(entry) <= remaining else _fit_fact_without_cells(entry)
        if len(candidate) > remaining:
            omitted += 1
            continue
        relayed.append(candidate)
        remaining -= len(candidate) + 1
    if omitted:
        note = f"{_FIT_RELAY_OMITTED_PREFIX}{omitted}"
        # 예약분은 자리를 "떼어 두는" 것이지 자리가 있다는 보장이 아니다.
        # budget 이 예약분보다 작으면 remaining 이 음수로 시작해 모든 블록이
        # omitted 로 떨어지고, 그때 이 표식을 무조건 붙이면 message 가 계약
        # 상한(4,000자)을 넘어 쓰기 응답 자체가 검증 오류로 죽는다. 표식조차
        # 들어갈 자리가 없으면 아무것도 붙이지 않는다 -- 기존 message 를 잘라
        # 자리를 만드는 것은 하지 않는다. 이미 실린 관측 사실이 이 표식보다
        # 무겁다.
        #
        # 남은 자리는 아직 안 쓴 remaining 에 처음 떼어 둔 예약분을 되돌린
        # 값이고, 앞선 사실이 있으면 그 사이의 빈칸 한 칸까지 센다.
        available = remaining + _FIT_RELAY_OMITTED_RESERVE
        if len(note) + (1 if relayed else 0) <= available:
            relayed.append(note)
    return " ".join(relayed)


def _text_width_em(text: str) -> float:
    """The advance one line of text asks for, in ems."""
    return sum(character_em_width(character) for character in text)


def _line_count(text: str, width_mm: float, font_size_pt: float) -> int:
    """How many rendered lines this text needs inside ``width_mm``.

    Measured in ems of the paragraph's own font rather than in characters, so
    a Hangul syllable costs the 1em it actually occupies.

    The flat 0.92em per ``len()`` character this replaces was not as wrong as
    it looks, and the honest measurement should be on the record: spaced
    Korean prose averages 0.79-0.91 em per character (MEASURED over six
    realistic captions and body sentences), so 0.92 was slightly *generous*
    there and the two models return the same line count. It reads narrow only
    where full-width runs are dense -- a space-free 50-syllable line is 1.0
    em/char, where 0.92 fitted 52 syllables on a 170mm 10pt line that holds
    48 and called two lines one. So this is a correctness fix at the edges and
    a provenance fix everywhere: an em is derived from the font, 0.92 was
    derived from nothing anyone recorded.

    Word boundaries are deliberately not modelled here (the reference-layout
    flow does that with the full token walk). Ignoring them can only round a
    line count *down*, so this stays a lower bound on the lines HWP will draw
    -- which is why every wrapped block also reports TEXT_WRAP_ESTIMATED.
    """
    em_per_line = width_mm / (font_size_pt * MILLIMETERS_PER_POINT)
    if em_per_line <= 0:
        return len(text.splitlines() or ("",))
    return sum(
        max(1, math.ceil(_text_width_em(line) / em_per_line))
        for line in text.splitlines() or ("",)
    )


def _word_aware_line_count(text: str, width_mm: float, font_size_pt: float) -> int:
    """The same text counted by the word-boundary model, for the band only.

    ``_line_count`` says in its own docstring that ignoring word boundaries can
    only round a line count down. That makes it a lower bound and leaves the
    difference against the word-aware walk -- the one
    ``hwp_reference_layout_text_flow`` already uses for reference layouts --
    as a measurable piece of this estimate's uncertainty rather than a caveat
    in prose. Nothing here changes ``height_mm``; the difference is charged to
    ``uncertainty_mm``.
    """
    if width_mm <= 0 or not 1 <= font_size_pt <= 96:
        return _line_count(text, width_mm, font_size_pt)
    style = ReferenceStyle(key="preflight", font_size_pt=font_size_pt)
    # `_character_width` measures in hundredths of a point, so the available
    # width has to be expressed in the same unit.
    available = round(width_mm / MILLIMETERS_PER_POINT * 100)
    return wrapped_line_count(text, style, max(1, available))


def _paragraph_line_height_mm(
    block: ParagraphBlock, *, floor_to_glyphs: bool = True
) -> float:
    font_size_pt = (
        _ASSUMED_FONT_SIZE_PT if block.font_size_pt is None else block.font_size_pt
    )
    line_spacing = (
        _ASSUMED_LINE_SPACING
        if block.line_spacing_percent is None
        else block.line_spacing_percent / 100
    )
    return _line_height_mm(font_size_pt, line_spacing, floor_to_glyphs=floor_to_glyphs)


def _line_height_mm(
    font_size_pt: float, line_spacing: float, *, floor_to_glyphs: bool = True
) -> float:
    """One line of ``font_size_pt`` text at ``line_spacing``.

    Floored at the glyphs' own height the way
    ``hwp_reference_layout_text_flow.reference_line_height`` already floors it:
    line spacing under 100% tightens the leading, it does not shrink the
    characters. Without the floor a 50% paragraph measured 1.76mm at 10pt for
    something that cannot be shorter than 3.53mm -- and test.hwp really does
    carry paragraphs at 50%, 15% and 10% raw line spacing.

    The product itself is sound: hwp_pageplan_g04 records MEASURED 5.64mm
    estimated against 5.70mm rendered for one 10pt line, so the formula is
    within ~1% once its inputs are right.

    ``floor_to_glyphs=False`` is for the one caller that is not estimating a
    height but stating a *floor* under which the plan is refused outright.
    The glyph floor belongs in the estimate, where being 1.76mm short of what
    HWP draws is the error being corrected; feeding it into
    ``minimum_height_mm`` instead multiplies the refusal threshold by up to
    6.7x on a page of 50%/15%/10% paragraphs, and refuses plans that fit.
    A refusal has to be justified by the caller's own requested spacing, not
    by our correction to it.
    """
    spacing = max(1.0, line_spacing) if floor_to_glyphs else line_spacing
    return font_size_pt * MILLIMETERS_PER_POINT * spacing


def paragraph_flow_height_mm(block: ParagraphBlock, usable_width_mm: float) -> float:
    """How much vertical flow the paragraph's own lines take.

    Spacing around the paragraph is deliberately excluded: this is the height
    of the text itself. The G04 lowering advances its placement cursor by this
    so the applier and the preflight measure a paragraph the same way -- a
    paragraph cannot be pinned to a plan box the way a picture or a table can,
    so an estimate is the best either of them has.
    """
    width = max(
        1.0,
        usable_width_mm - (block.left_margin_mm or 0) - (block.right_margin_mm or 0),
    )
    font_size_pt = 10.0 if block.font_size_pt is None else block.font_size_pt
    lines = _line_count(block.text, width, font_size_pt)
    return lines * _paragraph_line_height_mm(block)


def _unresolved_style_reasons(block: ParagraphBlock) -> tuple[str, ...]:
    """Which inputs of this block's height nobody actually read.

    ``hwp_document_style_profile._replicated_updates`` fills these in from the
    document's own paragraphs of the resolved style, but only when the scan
    saw that style (``DocumentStyleUsage.format_for_style`` returns None
    otherwise). Still None here means the height below is an assumption, and
    the caller is entitled to know which part of it is.
    """
    reasons: list[str] = []
    if block.font_size_pt is None:
        reasons.append("FONT_SIZE_UNRESOLVED")
    if block.line_spacing_percent is None:
        reasons.append("LINE_SPACING_UNRESOLVED")
    return tuple(reasons)


def _paragraph_estimate(
    block: ParagraphBlock, usable_width_mm: float
) -> _BlockEstimate:
    font_size_pt = (
        _ASSUMED_FONT_SIZE_PT if block.font_size_pt is None else block.font_size_pt
    )
    width = max(
        1.0,
        usable_width_mm - (block.left_margin_mm or 0) - (block.right_margin_mm or 0),
    )
    line_height = _paragraph_line_height_mm(block)
    lines = _line_count(block.text, width, font_size_pt)
    spacing = (
        (block.space_before_mm or 0)
        + (block.space_after_mm or 0)
        + (block.plan_lead_mm or 0)
    )
    estimated = lines * line_height + spacing
    unresolved = _unresolved_style_reasons(block)
    wrapped = _word_aware_line_count(block.text, width, font_size_pt)
    basis: list[str] = []
    if block.font_size_pt is None:
        basis.append(_BASIS_ASSUMED_FONT)
    if block.line_spacing_percent is None:
        basis.append(_BASIS_ASSUMED_LINE_SPACING)
    if wrapped > lines:
        basis.append(_BASIS_WRAP_LOWER_BOUND)
    return _BlockEstimate(
        width_mm=width,
        height_mm=estimated,
        minimum_height_mm=(
            _paragraph_line_height_mm(block, floor_to_glyphs=False) + spacing
        ),
        # An unread font is uncertainty even when the paragraph is one short
        # line: the whole height scales with a number nobody measured. Saying
        # so costs the plan nothing -- "possible" still writes -- and it is
        # the difference between a number the model can weigh and one it
        # reads as measured. It read 7mm as measured.
        uncertain=bool(unresolved) or (lines > 1 and "\n" not in block.text),
        reason_codes=(("TEXT_WRAP_ESTIMATED",) if lines > 1 else ()) + unresolved,
        adjustable_items=(
            "reduce_paragraph_spacing",
            "insert_page_break",
        ),
        uncertainty_mm=max(0, wrapped - lines) * line_height,
        basis=tuple(basis),
    )


def _column_widths_mm(block: TableBlock, usable_width_mm: float) -> tuple[float, ...]:
    """Per-column widths, from the plan's own widths where it states them.

    Dividing the usable width by the column count -- what this did -- is only
    right for a uniform table. The live plan that motivated this measured its
    first column at 48mm and its second at 112mm; charging both 85mm wrapped
    the narrow column too generously and the wide one too tightly, in a
    function whose whole job is to say how tall a row has to be.
    """
    columns = max(1, len(block.rows[0]))
    if block.column_widths_mm is not None and len(block.column_widths_mm) == columns:
        return tuple(block.column_widths_mm)
    return (usable_width_mm / columns,) * columns


def _merge_owners(block: TableBlock) -> dict[tuple[int, int], tuple[int, int]]:
    """[covered coordinate -> anchor coordinate] for every merged region."""
    owners: dict[tuple[int, int], tuple[int, int]] = {}
    for merge in block.merges:
        for row in range(merge.row, merge.row + merge.row_span):
            for column in range(merge.column, merge.column + merge.column_span):
                owners[(row, column)] = (merge.row, merge.column)
    return owners


def _merge_spans(block: TableBlock) -> dict[tuple[int, int], tuple[int, int]]:
    """[anchor coordinate -> (row_span, column_span)]."""
    return {
        (merge.row, merge.column): (merge.row_span, merge.column_span)
        for merge in block.merges
    }


def _cell_content_height_mm(
    cell: TableCell,
    width_mm: float,
) -> tuple[float, tuple[str, ...]]:
    """How tall this one cell's own content is, and what that rested on.

    A cell carries its metrics in two places. ``TableCell.font_size_pt`` /
    ``line_spacing_percent`` are the whole-cell values, but a cell built from
    ``paragraphs`` states them per paragraph and leaves the cell-level fields
    unset -- and that is exactly the shape the layout plans use. Reading only
    the cell level charged such a cell the module's 10pt/160% assumption for
    text the plan had already spelled out as 8pt/145%.
    """
    padding = cell.padding
    vertical_padding = 1.0 if padding is None else padding.top_mm + padding.bottom_mm
    horizontal_padding = 0.0 if padding is None else padding.left_mm + padding.right_mm
    text_width = max(1.0, width_mm - horizontal_padding)
    basis: list[str] = []

    def _metrics(
        font_size_pt: float | None,
        line_spacing_percent: int | None,
    ) -> tuple[float, float]:
        if font_size_pt is None:
            basis.append(_BASIS_ASSUMED_FONT)
        if line_spacing_percent is None:
            basis.append(_BASIS_ASSUMED_LINE_SPACING)
        return (
            _ASSUMED_FONT_SIZE_PT if font_size_pt is None else font_size_pt,
            _ASSUMED_LINE_SPACING
            if line_spacing_percent is None
            else line_spacing_percent / 100,
        )

    text_height = 0.0
    if cell.paragraphs:
        for paragraph in cell.paragraphs:
            font_size, line_spacing = _metrics(
                paragraph.font_size_pt
                if paragraph.font_size_pt is not None
                else cell.font_size_pt,
                paragraph.line_spacing_percent
                if paragraph.line_spacing_percent is not None
                else cell.line_spacing_percent,
            )
            text_height += _line_count(
                paragraph.text, text_width, font_size
            ) * _line_height_mm(font_size, line_spacing)
            text_height += (paragraph.space_before_mm or 0) + (
                paragraph.space_after_mm or 0
            )
    else:
        font_size, line_spacing = _metrics(cell.font_size_pt, cell.line_spacing_percent)
        text_height = _line_count(cell.text, text_width, font_size) * _line_height_mm(
            font_size, line_spacing
        )
    # A picture in a cell is not text and does not wrap: it occupies its own
    # stated height inside the same padding. It was already measured here; what
    # was missing is that a table with pinned row heights never reached this
    # function at all, so an oversized in-cell picture went uncounted.
    image_height = cell.image_height_mm or 0
    return max(text_height, image_height) + vertical_padding, tuple(
        dict.fromkeys(basis)
    )


def _table_content_heights(
    block: TableBlock,
    usable_width_mm: float,
) -> tuple[tuple[float, ...], tuple[str, ...]]:
    widths = _column_widths_mm(block, usable_width_mm)
    owners = _merge_owners(block)
    spans = _merge_spans(block)
    demands: list[list[float]] = [[] for _ in block.rows]
    basis: list[str] = []
    for row_index, row in enumerate(block.rows):
        for column_index, cell in enumerate(row):
            anchor = owners.get((row_index, column_index))
            if anchor is not None and anchor != (row_index, column_index):
                # A covered cell is empty and unformatted by contract
                # (TableBlock._validate_merges); its content lives at the anchor.
                continue
            row_span, column_span = spans.get((row_index, column_index), (1, 1))
            row_span = max(1, min(row_span, len(block.rows) - row_index))
            width = sum(widths[column_index : column_index + column_span])
            height, cell_basis = _cell_content_height_mm(cell, width)
            basis.extend(cell_basis)
            # A cell spanning rows needs its height from all of them, not from
            # the first one. Charging a 50.6mm picture merged across two 25mm
            # rows entirely to row 0 invents a 26mm overflow that is not there;
            # charging it only to row 0's share loses half of a demand the two
            # rows really do have to meet together.
            share = height / row_span
            for offset in range(row_span):
                demands[row_index + offset].append(share)
    return tuple(max(row, default=5.0) for row in demands), tuple(dict.fromkeys(basis))


def table_row_content_heights_mm(
    block: TableBlock,
    usable_width_mm: float,
) -> tuple[float, ...]:
    """The shortest each row can be and still show what is in it.

    Shared with the G04 lowering: a plan that reserves less height for a table
    than its own content needs cannot be honoured, and both the estimate and
    the applied row pin have to agree about where the floor is.
    """
    heights, _ = _table_content_heights(block, usable_width_mm)
    return heights


def _table_caption_height_mm(
    block: TableBlock,
    usable_width_mm: float,
) -> tuple[float, float, tuple[str, ...]]:
    """Estimated caption height, its floor, and what the estimate assumed.

    A table caption is not a fixed slab. HWP attaches it as a real paragraph
    under the table (``hwp_live_native_table_layout`` sends ``CaptionCommand``
    with ``ShapeCaption/Side=3``) and wraps it like any other, so its height is
    a function of the caption's own length and font. This charged a flat 7.0mm
    for any caption of any length in any font, which happens to be close to
    one line at 10pt/160% and is half of what a two-line caption at the
    document's own 11pt/200% occupies (15.5mm).

    The font is genuinely unknown here -- ``TableBlock`` carries the caption's
    *style* but no metrics, and the style catalog (``DocumentStyle``) has only
    ids and names -- so the assumed size is used and named rather than
    silently applied. HWP's own auto-inserted table number ("표 12 ") widens
    the first line by an amount this cannot know either; both live in
    ``TABLE_CAPTION_HEIGHT_ESTIMATED``.

    Returned separately from the floor because only the first line is a floor:
    the caption exists, so it costs at least one line, but whether it wraps is
    the estimated part and a floor is supposed to be what we are sure of.
    """
    if block.caption is None:
        return 0.0, 0.0, ()
    one_line = _line_height_mm(_ASSUMED_FONT_SIZE_PT, _ASSUMED_LINE_SPACING)
    lines = _line_count(block.caption, usable_width_mm, _ASSUMED_FONT_SIZE_PT)
    return (
        lines * one_line,
        one_line,
        ("TABLE_CAPTION_HEIGHT_ESTIMATED", "FONT_SIZE_UNRESOLVED"),
    )


def _table_estimate(block: TableBlock, usable_width_mm: float) -> _BlockEstimate:
    if block.column_widths_mm is None:
        width = usable_width_mm
    else:
        width = (
            sum(block.column_widths_mm) + block.left_margin_mm + block.right_margin_mm
        )
    content_heights, content_basis = _table_content_heights(block, usable_width_mm)
    uncertainty = 0.0
    basis: list[str] = list(content_basis)
    if block.row_heights_mm is not None:
        height = sum(block.row_heights_mm)
        uncertain = False
        reasons: tuple[str, ...] = ()
        # HWP treats a row height as a floor. A pinned table was charged its
        # pins and nothing else, so a cell whose text or picture outgrew its
        # pin cost the page height nobody had budgeted -- and the response said
        # "none". The pins stay the estimate (they are what was asked for); the
        # excess its own content already demands becomes the band.
        outgrown = sum(
            max(0.0, content - pinned)
            for content, pinned in zip(
                content_heights,
                block.row_heights_mm,
                strict=False,
            )
        )
        if outgrown > 0:
            uncertainty += outgrown
            reasons += ("TABLE_ROW_HEIGHT_IS_A_MINIMUM",)
            basis.append(_BASIS_ROW_HEIGHT_IS_MINIMUM)
    else:
        height = sum(content_heights)
        uncertain = True
        reasons = ("AUTO_FIT_TABLE_HEIGHT",)
    caption_height, caption_minimum, caption_reasons = _table_caption_height_mm(
        block,
        usable_width_mm,
    )
    height += caption_height
    if caption_reasons:
        uncertain = True
        reasons += caption_reasons
    # The lead is real vertical space the applier writes as the table
    # paragraph's PrevSpacing, so it has to count against the page the same way
    # a paragraph's own spacing does. Leaving it out let a plan that
    # positions blocks absolutely report "no overflow" for a page it cannot fit.
    lead = block.plan_lead_mm or 0.0
    height += lead
    minimum = (
        sum(block.row_heights_mm) + caption_minimum + lead
        if block.row_heights_mm is not None
        else max(5.0 * len(block.rows), 1.0) + lead
    )
    adjustable = (
        ("confirm_table_row_heights", "insert_page_break")
        if uncertain
        else ("reduce_table_row_heights", "insert_page_break")
    )
    if block.plan_lead_mm is None:
        # Not the table: the paragraph the table is anchored in. The applier
        # sets no PrevSpacing/NextSpacing for a table block, so that paragraph
        # takes the document style's -- MEASURED 566 HWPUNIT (2.0mm) before and
        # after on all three tables of the live page-38 readback, which no
        # part of this estimate had ever counted. A placed table (plan_lead_mm)
        # states its own lead and is excluded.
        uncertainty += _ASSUMED_DOCUMENT_LINE_MM
        basis.append(_BASIS_ANCHOR_SPACING)
    return _BlockEstimate(
        width_mm=width,
        height_mm=height,
        minimum_height_mm=minimum,
        uncertain=uncertain,
        reason_codes=reasons,
        adjustable_items=adjustable,
        uncertainty_mm=uncertainty,
        basis=tuple(dict.fromkeys(basis)),
    )


def _block_estimate(
    block: ParagraphBlock
    | TableBlock
    | ImageBlock
    | ReferenceLayoutBlock
    | ReferenceLayoutPatchBlock,
    usable_width_mm: float,
    usable_height_mm: float,
    page_geometry: SectionPageGeometry,
    page_number: int,
) -> _BlockEstimate:
    if isinstance(block, ParagraphBlock):
        return _paragraph_estimate(block, usable_width_mm)
    if isinstance(block, TableBlock):
        return _table_estimate(block, usable_width_mm)
    if isinstance(block, ImageBlock):
        # An image caption normally never reaches here: LayoutPlan.
        # expand_image_frames turns it into a real ParagraphBlock before the
        # preflight sees the plan, and that paragraph is measured like any
        # other. This is the unexpanded path (a direct call), and it is
        # measured the same way rather than charged a flat slab, so both
        # spellings of the same picture report the same height.
        caption_height = 0.0
        caption_reasons: tuple[str, ...] = ()
        if block.caption is not None:
            caption_height = _line_count(
                block.caption,
                usable_width_mm,
                _ASSUMED_FONT_SIZE_PT,
            ) * _line_height_mm(_ASSUMED_FONT_SIZE_PT, _ASSUMED_LINE_SPACING)
            caption_reasons = ("FONT_SIZE_UNRESOLVED",)
        # See _table_estimate: the lead is written as the picture paragraph's
        # PrevSpacing, so it occupies the page and must be estimated.
        occupied = block.height_mm + caption_height + (block.plan_lead_mm or 0.0)
        return _BlockEstimate(
            width_mm=block.width_mm,
            # The picture itself is pinned; only the caption is estimated.
            minimum_height_mm=occupied - caption_height,
            height_mm=occupied,
            uncertain=bool(caption_reasons),
            reason_codes=caption_reasons,
            adjustable_items=("reduce_image_height", "insert_page_break"),
            # See _table_estimate: the picture's own paragraph carries the
            # document style's spacing, which this plan never states.
            uncertainty_mm=(
                0.0 if block.plan_lead_mm is not None else _ASSUMED_DOCUMENT_LINE_MM
            ),
            basis=(() if block.plan_lead_mm is not None else (_BASIS_ANCHOR_SPACING,)),
        )
    if isinstance(block, ReferenceLayoutPatchBlock):
        return _BlockEstimate(0, 0, 0, False, (), ())
    try:
        command, fit_report = compile_reference_layout_command_with_fit_report(
            prepare_reference_layout(block),
            page_geometry,
            page_number=page_number,
            base_style_id=0,
        )
    except (HwpLiveError, OSError, ValueError):
        return _BlockEstimate(
            usable_width_mm,
            usable_height_mm,
            usable_height_mm,
            False,
            ("REFERENCE_LAYOUT_EXECUTION_REJECTED",),
            ("adjust_reference_layout_geometry",),
            definite=True,
        )
    width = _millimeters(_integer_setter(command, "BodyWidth"))
    height = _millimeters(_integer_setter(command, "BodyHeight"))
    return _BlockEstimate(
        width,
        height,
        height,
        False,
        (),
        ("use_reference_layout_patch",),
        text_fit=fit_report if fit_report else None,
    )


def _integer_setter(command: ParameterActionCommand, path: str) -> int:
    value = next(setter.value for setter in command.setters if setter.path == path)
    if not isinstance(value, IntegerValue):
        raise TypeError(f"{path} must be an integer native setter")
    return value.value


def _millimeters(value: int) -> float:
    return value * MILLIMETERS_PER_INCH / HWPUNITS_PER_INCH


def _trailing_paragraph_uncertainty_mm(plan: LayoutPlan) -> float:
    """One line for the paragraph the applier opens after the plan's last block.

    This is not a guess about HWP, it is a reading of the applier's own
    commands. A ``TableBlock`` always ends with ``LeaveTableCommand``
    (hwp_live_native_table_layout.table_commands), whose native implementation
    runs ``BreakPara`` unconditionally (ActionImageCaption.cpp ``LeaveTable``,
    ``appendParagraph`` defaults to true), so the last table in a plan leaves an
    empty paragraph behind it. An ``ImageBlock`` breaks under the same
    condition its own ``needs_break`` states in hwp_live_native_layout.py, and
    that comment already records the cost as MEASURED ~5.6mm -- the same
    paragraph that pushed a G04 page onto a third page. A ``ParagraphBlock``
    last does not break at all.

    It is uncertainty rather than estimate because its height is the
    document's, not the plan's: nobody here has read the base style.
    """
    blocks = tuple(
        block for block in plan.blocks if not isinstance(block, PageBreakBlock)
    )
    if not blocks:
        return 0.0
    last = blocks[-1]
    if isinstance(last, TableBlock):
        return _ASSUMED_DOCUMENT_LINE_MM
    if (
        isinstance(last, ImageBlock)
        and last.caption is None
        and last.plan_lead_mm is None
    ):
        return _ASSUMED_DOCUMENT_LINE_MM
    return 0.0


def preflight_layout(
    plan: LayoutPlan,
    page_geometry: SectionPageGeometry,
    *,
    page_number: int,
) -> LayoutPreflightResult:
    usable = page_geometry.usable_area(page_number=page_number)
    usable_width = _millimeters(usable.width)
    usable_height = _millimeters(usable.height)
    page_estimated = 0.0
    page_minimum = 0.0
    page_uncertainty = 0.0
    maximum_estimated = 0.0
    maximum_minimum = 0.0
    maximum_upper = 0.0
    maximum_width = 0.0
    uncertain = False
    estimates: list[tuple[int, str, _BlockEstimate]] = []
    reason_codes: list[str] = []
    adjustable: list[str] = []
    basis: list[str] = []
    layout_page_number = page_number
    page_totals: list[tuple[int, float, float, tuple[int, ...]]] = []
    page_block_indices: list[int] = []
    block_summaries: list[LayoutPreflightBlock] = []

    for index, block in enumerate(plan.blocks):
        if isinstance(block, PageBreakBlock):
            page_totals.append(
                (
                    layout_page_number,
                    page_estimated,
                    page_uncertainty,
                    tuple(page_block_indices),
                )
            )
            page_block_indices = []
            maximum_estimated = max(maximum_estimated, page_estimated)
            maximum_minimum = max(maximum_minimum, page_minimum)
            maximum_upper = max(maximum_upper, page_estimated + page_uncertainty)
            page_estimated = 0.0
            page_minimum = 0.0
            page_uncertainty = 0.0
            layout_page_number += 1
            continue
        estimate = _block_estimate(
            block,
            usable_width,
            usable_height,
            page_geometry,
            layout_page_number,
        )
        estimates.append((index, block.kind, estimate))
        page_block_indices.append(index)
        block_summaries.append(
            LayoutPreflightBlock(
                block_index=index,
                page_number=layout_page_number,
                block_kind=block.kind,
                estimated_width_mm=round(estimate.width_mm, 3),
                estimated_height_mm=round(estimate.height_mm, 3),
            )
        )
        page_estimated += estimate.height_mm
        page_minimum += estimate.minimum_height_mm
        page_uncertainty += estimate.uncertainty_mm
        maximum_width = max(maximum_width, estimate.width_mm)
        uncertain = uncertain or estimate.uncertain
        reason_codes.extend(estimate.reason_codes)
        adjustable.extend(estimate.adjustable_items)
        basis.extend(estimate.basis)
        if estimate.text_fit is not None:
            reason_codes.append(REFERENCE_TEXT_AUTO_SHRUNK_REASON_CODE)
            basis.append(reference_text_fit_fact(estimate.text_fit, block_index=index))

    trailing = _trailing_paragraph_uncertainty_mm(plan)
    if trailing > 0:
        page_uncertainty += trailing
        reason_codes.append("LAYOUT_TRAILING_PARAGRAPH_UNMEASURED")
        basis.append(_BASIS_TRAILING_PARAGRAPH)
    # Preserve empty segments from leading/consecutive/final breaks. The existing
    # estimator assigns trailing-paragraph uncertainty to the final segment,
    # even after a final break; expose it separately instead of moving it.
    page_totals.append(
        (
            layout_page_number,
            page_estimated,
            page_uncertainty,
            tuple(page_block_indices),
        )
    )
    maximum_estimated = max(maximum_estimated, page_estimated)
    maximum_minimum = max(maximum_minimum, page_minimum)
    maximum_upper = max(maximum_upper, page_estimated + page_uncertainty)
    uncertainty = max(0.0, maximum_upper - maximum_estimated)
    definite = (
        any(estimate.definite for _, _, estimate in estimates)
        or maximum_minimum > usable_height + 0.1
        or maximum_width > usable_width + 0.1
    )
    stated_possible = uncertain or maximum_estimated > usable_height + 0.1
    # The margin the plan appears to have is only real if it is bigger than
    # what this estimate does not know. When it is not, the honest answer is
    # "possible", not "none" -- and the caller can see exactly how much of the
    # difference is band, because ``stated_geometry_overflow`` still reports
    # the verdict without it.
    within_band = not stated_possible and maximum_upper > usable_height + 0.1
    possible = stated_possible or within_band
    overflow: LayoutOverflow = (
        "definite" if definite else "possible" if possible else "none"
    )
    stated_geometry_overflow: LayoutOverflow = (
        "definite" if definite else "possible" if stated_possible else "none"
    )
    if within_band:
        reason_codes.append("ESTIMATE_MARGIN_WITHIN_UNCERTAINTY")
    problems = tuple(
        LayoutPreflightProblem(
            block_index=index,
            block_kind=kind,
            reason_code=(
                estimate.reason_codes[0]
                if estimate.reason_codes
                else "CONTRIBUTES_TO_OVERFLOW"
            ),
            estimated_width_mm=round(estimate.width_mm, 3),
            estimated_height_mm=round(estimate.height_mm, 3),
        )
        for index, kind, estimate in estimates
        if overflow != "none"
        and (estimate.height_mm > 0 or estimate.width_mm > usable_width + 0.1)
    )
    if definite:
        reason_codes.append("DEFINITE_PAGE_OVERFLOW")
    elif possible:
        reason_codes.append("LAYOUT_REQUIRES_CONFIRMATION")
    observation = LayoutObservations(
        pages=tuple(
            LayoutPreflightPage(
                page_number=number,
                block_indices=indices,
                estimated_height_mm=round(height, 3),
                estimated_height_uncertainty_mm=round(band, 3),
                estimated_height_upper_mm=round(height + band, 3),
                remaining_height_mm=round(usable_height - height, 3),
                remaining_height_lower_mm=round(usable_height - height - band, 3),
                trailing_paragraph_uncertainty_mm=(
                    round(trailing, 3) if number == layout_page_number else 0.0
                ),
            )
            for number, height, band, indices in page_totals
        ),
        blocks=tuple(block_summaries),
    )
    basis.append(LAYOUT_OBSERVATIONS_PREFIX + observation.model_dump_json())
    return LayoutPreflightResult(
        usable_width_mm=round(usable_width, 3),
        usable_height_mm=round(usable_height, 3),
        estimated_width_mm=round(maximum_width, 3),
        estimated_height_mm=round(maximum_estimated, 3),
        overflow=overflow,
        reason_codes=tuple(dict.fromkeys(reason_codes)),
        problem_blocks=problems,
        adjustable_items=tuple(dict.fromkeys(adjustable)),
        safe_to_write=overflow != "definite",
        estimated_height_uncertainty_mm=round(uncertainty, 3),
        estimated_height_upper_mm=round(maximum_estimated + uncertainty, 3),
        estimate_basis=tuple(dict.fromkeys(basis)),
        stated_geometry_overflow=stated_geometry_overflow,
    )
