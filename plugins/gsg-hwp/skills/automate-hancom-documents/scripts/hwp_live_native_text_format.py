from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, assert_never

from hwp_live_native_action_models import (
    BooleanValue,
    EnumerationValue,
    IntegerValue,
    NativeSetter,
    ParameterActionCommand,
    TextValue,
)
from hwp_live_values import Alignment, Rgb
from hwp_reference_layout_geometry import mm_to_hwpunit


class CharacterFormatting(Protocol):
    bold: bool | None
    font_name: str | None
    font_size_pt: float | None
    text_color: Rgb | None


@dataclass(frozen=True, slots=True)
class ParagraphFormatting:
    alignment: Alignment
    align_type_raw: int | None = None
    line_spacing: int | None = None
    before_mm: float | None = None
    after_mm: float | None = None
    left_mm: float | None = None
    right_mm: float | None = None
    indentation_mm: float | None = None
    # ParaShape/HeadingType (0 none, 1 outline, 2 number, 3 bullet) and
    # ParaShape/Level (0-6). Setting them is what makes HWP draw the document's
    # own automatic number on a paragraph instead of leaving it bare.
    heading_type: int | None = None
    heading_level: int | None = None
    keep_with_next: bool | None = None
    page_break_before: bool | None = None


def lead_before_mm(
    space_before_mm: float | None,
    plan_lead_mm: float | None,
) -> float | None:
    """Combine an author's paragraph spacing with an absolute-plan lead.

    ``plan_lead_mm`` is what a PagePlan block needs above it to land on its own
    ``box.top_mm``; ``space_before_mm`` is what the caller asked for as style.
    They are different intents that share one native slot (ParaShape
    PrevSpacing), so they add. ``None`` lead leaves the caller's value -- and
    its "inherit the style" ``None`` -- exactly as it was.
    """
    if plan_lead_mm is None:
        return space_before_mm
    return round((space_before_mm or 0.0) + plan_lead_mm, 4)


def lead_after_mm(
    space_after_mm: float | None,
    plan_trail_mm: float | None,
) -> float | None:
    """The trailing half of :func:`lead_before_mm`.

    A table's own paragraph cannot carry a large "space before" -- Hangul moves
    the whole paragraph to the next page (MEASURED: 50mm stays put, 70mm adds a
    page). The gap in front of a table is therefore written as the *previous*
    block's space-after, which is this slot, and pictures and text paragraphs
    were measured holding 148mm and 175mm there without moving.
    """
    if plan_trail_mm is None:
        return space_after_mm
    return round((space_after_mm or 0.0) + plan_trail_mm, 4)


def plan_lead_line_spacing(plan_lead_mm: float | None) -> int | None:
    """Line spacing for a paragraph that only holds an absolutely placed object.

    A picture or a table is inserted "as a character", so it sits on a line of
    its own and the paragraph's inherited line spacing pads that line. MEASURED
    on Hangul 2024, four 20.0mm pictures rendered at 200dpi: each paragraph
    consumed 22.10mm, i.e. 2.10mm more than the object, and the surplus
    compounded down the page (+1.95, +4.06, +6.13mm). At 100% the same run
    consumed 19.69mm per picture and the error stopped compounding
    (-0.17, -0.21, -0.25, -0.22mm, which is render rounding, not drift).

    Only a block that carries an absolute plan lead gets this: it has declared
    where it must sit, so its paragraph has to consume exactly its own height.
    A block without one keeps whatever spacing the document already applies.
    """
    return None if plan_lead_mm is None else 100


def rgb_value(color: Rgb) -> int:
    red, green, blue = color
    return red | green << 8 | blue << 16


def style_command(style_id: int) -> ParameterActionCommand:
    return ParameterActionCommand(
        action="Style",
        parameter_set="HStyle",
        setters=(NativeSetter("Apply", IntegerValue(style_id)),),
    )


def character_command(
    formatting: CharacterFormatting,
    *,
    letter_spacing_percent: int | None = None,
) -> ParameterActionCommand | None:
    setters: list[NativeSetter] = []
    if letter_spacing_percent is not None:
        for language in (
            "Hangul",
            "Latin",
            "Hanja",
            "Japanese",
            "Other",
            "Symbol",
            "User",
        ):
            setters.append(
                NativeSetter(f"Spacing{language}", IntegerValue(letter_spacing_percent))
            )
    if formatting.bold is not None:
        setters.append(NativeSetter("Bold", BooleanValue(formatting.bold)))
    if formatting.font_size_pt is not None:
        setters.append(
            NativeSetter("Height", IntegerValue(round(formatting.font_size_pt * 100)))
        )
    if formatting.text_color is not None:
        setters.append(
            NativeSetter("TextColor", IntegerValue(rgb_value(formatting.text_color)))
        )
    if formatting.font_name is not None:
        for language in (
            "Hangul",
            "Latin",
            "Hanja",
            "Japanese",
            "Other",
            "Symbol",
            "User",
        ):
            setters.append(
                NativeSetter(f"FaceName{language}", TextValue(formatting.font_name))
            )
            setters.append(NativeSetter(f"FontType{language}", IntegerValue(1)))
    if not setters:
        return None
    return ParameterActionCommand(
        action="CharShape",
        parameter_set="HCharShape",
        setters=tuple(setters),
    )


def _alignment_value(alignment: Alignment) -> str | None:
    match alignment:
        case "inherit":
            return None
        case "left":
            return "Left"
        case "center":
            return "Center"
        case "right":
            return "Right"
        case "justify":
            return "Justify"
    assert_never(alignment)


def _absolute_urc(millimeters: float) -> int:
    """Encode an absolute ParaShape length in HWP's URC representation."""
    return mm_to_hwpunit(millimeters) << 1


def paragraph_command(
    formatting: ParagraphFormatting,
) -> ParameterActionCommand | None:
    setters: list[NativeSetter] = []
    if formatting.align_type_raw is not None:
        setters.append(
            NativeSetter("AlignType", IntegerValue(formatting.align_type_raw))
        )
    else:
        alignment = _alignment_value(formatting.alignment)
        if alignment is not None:
            setters.append(
                NativeSetter("AlignType", EnumerationValue("HAlign", alignment))
            )
    if formatting.line_spacing is not None:
        setters.append(
            NativeSetter("LineSpacing", IntegerValue(formatting.line_spacing))
        )
    for name, value in (
        ("PrevSpacing", formatting.before_mm),
        ("NextSpacing", formatting.after_mm),
        ("LeftMargin", formatting.left_mm),
        ("RightMargin", formatting.right_mm),
        ("Indentation", formatting.indentation_mm),
    ):
        if value is not None:
            setters.append(NativeSetter(name, IntegerValue(_absolute_urc(value))))
    for name, level in (
        ("HeadingType", formatting.heading_type),
        ("Level", formatting.heading_level),
    ):
        if level is not None:
            setters.append(NativeSetter(name, IntegerValue(level)))
    for name, value in (
        ("KeepWithNext", formatting.keep_with_next),
        ("PagebreakBefore", formatting.page_break_before),
    ):
        if value is not None:
            setters.append(NativeSetter(name, BooleanValue(value)))
    if not setters:
        return None
    return ParameterActionCommand(
        action="ParagraphShape",
        parameter_set="HParaShape",
        setters=tuple(setters),
    )
