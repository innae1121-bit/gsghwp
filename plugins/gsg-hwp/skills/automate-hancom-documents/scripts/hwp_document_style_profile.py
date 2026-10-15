from __future__ import annotations

from collections.abc import Iterable
from json import dumps
from unicodedata import category, normalize

from hwp_errors import HwpLiveError
from hwp_document_style_usage import (
    EMPTY_STYLE_USAGE,
    DocumentStyleUsage,
    ObservedParagraphFormat,
    paragraph_lead_marker,
    paragraph_marker_is_unambiguous,
    paragraph_shape,
)
from hwp_document_table_profile import (
    EMPTY_TABLE_CONVENTIONS,
    ObservedTableConventions,
    observed_table_conventions,
    replicate_table_conventions,
    resolve_table_geometry,
    split_wide_table,
)
from hwp_live_contract import (
    DocumentStyle,
    ImageBlock,
    LayoutBlock,
    LayoutPlan,
    ParagraphBlock,
)
from hwp_live_structure_contract import DocumentStructure
from hwp_report_layout import ReportListItemBlock
from hwp_live_table_contract import TableBlock
from hwp_live_values import ALIGNMENT_VALUES
from hwp_reference_layout_geometry import (
    HWPUNITS_PER_INCH,
    MILLIMETERS_PER_INCH,
)
from hwp_table_border_inheritance import (
    explicit_shared_borders,
    inherit_conflicting_shared_borders,
)

_ROLE_ALIASES = {
    "body": ("바탕글", "본문", "normal", "bodytext", "body"),
    "table_title": ("표타이틀", "표제목", "표캡션", "tabletitle", "tablecaption"),
    "table_body": ("표내용", "표본문", "tablebody", "tablecontent"),
    "figure_title": (
        "그림타이틀",
        "그림제목",
        "그림캡션",
        "figuretitle",
        "figurecaption",
    ),
}


def _normalized(value: str) -> str:
    return "".join(
        character.casefold()
        for character in normalize("NFKC", value)
        if not character.isspace()
        and not category(character).startswith(("P", "S"))
        and category(character) != "Cf"
    )


def _numbered_caption_marker(value: str) -> str:
    """Normalized literal before the first caption number, if one exists."""
    text = normalize("NFKC", value).lstrip()
    number_index = next(
        (index for index, character in enumerate(text) if character.isdecimal()),
        None,
    )
    return "" if number_index is None else _normalized(text[:number_index])


def _observed_caption_marker(value: str) -> str:
    """Marker carried by observed caption evidence, without naming its words."""
    return _numbered_caption_marker(value) or _normalized(value)


def _has_matching_automatic_caption_marker(
    caption: str,
    caption_style_id: int | None,
    usage: DocumentStyleUsage,
) -> bool:
    marker = _numbered_caption_marker(caption)
    if not marker:
        return False
    return any(
        paragraph.style_id == caption_style_id
        and (paragraph.format.heading_type or 0) != 0
        and _observed_caption_marker(paragraph.lead_text) == marker
        for paragraph in usage.paragraph_evidence
    )


def _style_keys(style: DocumentStyle) -> tuple[str, ...]:
    values = (style.name, style.english_name or "")
    return tuple(key for value in values if (key := _normalized(value)))


def _named_style(styles: Iterable[DocumentStyle], name: str) -> int:
    key = _normalized(name)
    for style in styles:
        if key in _style_keys(style):
            return style.style_id
    raise HwpLiveError(f"현재 문서에 스타일 '{name}'이 없습니다")


def _role_style(styles: tuple[DocumentStyle, ...], role: str) -> int | None:
    for alias in _ROLE_ALIASES[role]:
        key = _normalized(alias)
        for style in styles:
            if key in _style_keys(style):
                return style.style_id
    return None


def _named_or_role_style(
    styles: tuple[DocumentStyle, ...],
    name: str,
    role: str,
) -> int | None:
    key = _normalized(name)
    if key in {_normalized(alias) for alias in _ROLE_ALIASES[role]}:
        return _role_style(styles, role)
    return _named_style(styles, name)


def _first_style_or_none(*values: int | None) -> int | None:
    return next((value for value in values if value is not None), None)


def _first_style(*values: int | None) -> int:
    resolved = _first_style_or_none(*values)
    if resolved is None:
        raise HwpLiveError("현재 문서에서 적용할 스타일을 찾지 못했습니다")
    return resolved


# 모양 판별은 hwp_document_style_usage.paragraph_shape 하나만 쓴다. 문단 본문과
# 스타일 이름을 같은 규칙으로 봐야 [모양 -> style_id] 표와 이름 매칭 폴백이
# 같은 어휘로 맞물린다.
_heading_shape = paragraph_shape


def _heading_style(styles: tuple[DocumentStyle, ...], shape: str | None) -> int | None:
    for style in styles:
        candidate_shape = _heading_shape(style.name)
        if candidate_shape is not None and (shape is None or candidate_shape == shape):
            return style.style_id
    return None


def _observed_body_style(usage: DocumentStyleUsage) -> int | None:
    """Body style the document actually applies, ahead of any name guess.

    A style whose paragraphs HWP numbers by itself can never be it. The body
    bucket means "the document types nothing at the head of this paragraph",
    and a caption style only looks like that because its ``<표 N>`` is drawn
    rather than typed. A live run resolved a plain, markerless paragraph to
    ``그림타이틀`` and the document printed ``(그림 5.5.3-16) 표시 없는 …``;
    refusing the automatic-marker style here means the worst that can happen is
    a fall back to the named 바탕글, which is what the caller asked for anyway.
    ``None`` (unreadable) is not automatic -- we do not guess either way.
    """
    style_id = usage.body_style_id
    if style_id is None or usage.marker_is_automatic(style_id) is True:
        return None
    return style_id


def _plain_paragraph_styles(
    styles: tuple[DocumentStyle, ...],
    usage: DocumentStyleUsage,
) -> frozenset[int]:
    """Style ids that mean "this paragraph was never styled" in this document.

    바탕글 is where every HWP paragraph starts, so a ``1.1`` paragraph sitting
    on it is not evidence that the document treats that number as a heading —
    it is evidence that nobody styled that paragraph at all.
    """
    return frozenset(
        value
        for value in (_observed_body_style(usage), _role_style(styles, "body"))
        if value is not None
    )


def _millimeters(urc: int | None, low: float, high: float) -> float | None:
    """An absolute native URC length as mm, or ``None`` when not replayable.

    Out of range is not an error here. The contract bounds what a paragraph may
    ask for; an observation outside them is simply not replayable, and refusing
    the edit over it would be a new refusal where the old behaviour inserted
    the paragraph fine.

    ParaShape's five length fields are URC, not plain HWPUNIT. Bit 0 selects a
    character-relative value; absolute HWPUNIT is stored in the remaining bits.
    A relative observation has no stable millimetre equivalent without its font
    context, so it is deliberately left to the selected style.
    """
    if urc is None or urc & 1:
        return None
    hwpunit = urc >> 1
    value = round(hwpunit * MILLIMETERS_PER_INCH / HWPUNITS_PER_INCH, 4)
    return value if low <= value <= high else None


def _replicated_updates(
    block: ParagraphBlock,
    observed: ObservedParagraphFormat | None,
) -> dict[str, object]:
    """Fields of ``block`` the document's own paragraphs can fill in.

    Only fields the caller left unset are touched. This is replication, not
    policy: every value comes from paragraphs of the very style this block
    resolved to, so a document whose ``1)`` paragraphs hang at 12mm gets a
    ``1)`` paragraph that hangs at 12mm instead of one flattened to the left
    margin by our own defaults.

    Alignment is the one observation deliberately left out; the comment at its
    place below says why. ``docs/alignment-raw-value.md`` records the measured
    table -- the question it used to pose is settled.
    """
    if observed is None:
        return {}
    updates: dict[str, object] = {}
    if block.font_name is None and observed.face_name:
        updates["font_name"] = observed.face_name
    if block.font_size_pt is None and observed.height is not None:
        size = observed.height / 100
        if 1 <= size <= 96:
            updates["font_size_pt"] = size
    if block.bold is None and observed.bold is not None:
        updates["bold"] = observed.bold
    if block.text_color is None and observed.text_color is not None:
        color = observed.text_color
        if 0 <= color <= 0xFF_FF_FF:
            # Inverse of hwp_live_native_text_format.rgb_value.
            updates["text_color"] = (
                color & 0xFF,
                (color >> 8) & 0xFF,
                (color >> 16) & 0xFF,
            )
    # Auto-resolved prose still inherits alignment: one sampled paragraph must
    # not pin every generated body paragraph. An explicitly selected style is a
    # different contract. The caller chose that observed style for this block,
    # and HWP can retain direct alignment from the previously edited paragraph
    # even after ``Style/Apply``. Replay the selected style's measured alignment
    # so consecutive reports do not depend on cursor history.
    if (
        block.alignment == "inherit"
        and (block.style_id is not None or block.style_name is not None)
        and observed.alignment is not None
    ):
        alignment = next(
            (
                name
                for name, raw_value in ALIGNMENT_VALUES.items()
                if name != "inherit" and raw_value == observed.alignment
            ),
            None,
        )
        if alignment is not None:
            updates["alignment"] = alignment
    if (
        block.line_spacing_percent is None
        and observed.line_spacing is not None
        and 50 <= observed.line_spacing <= 500
    ):
        updates["line_spacing_percent"] = observed.line_spacing
    for name, urc, low, high in (
        ("left_margin_mm", observed.left_margin_hwpunit, 0.0, 100.0),
        ("right_margin_mm", observed.right_margin_hwpunit, 0.0, 100.0),
        ("indentation_mm", observed.indentation_hwpunit, -100.0, 100.0),
        ("space_before_mm", observed.previous_spacing_hwpunit, 0.0, 100.0),
        ("space_after_mm", observed.next_spacing_hwpunit, 0.0, 100.0),
    ):
        # A block placed at an absolute top already says where its own top is.
        # Replicating the style's space-before on top of that lead would add
        # the document's convention to the placement and move the block off
        # its plan, so the one field the placement owns is left alone.
        if name == "space_before_mm" and block.plan_lead_mm is not None:
            continue
        if getattr(block, name) is None:
            millimeters = _millimeters(urc, low, high)
            if millimeters is not None:
                updates[name] = millimeters
    # Never switch automatic numbering on: the number comes from the nested
    # ``ParaShape/Numbering`` set this bridge cannot reproduce. ``Style`` runs
    # before ``ParagraphShape``, though, so a style may already have switched it
    # on. When the caller supplied a literal marker, explicitly switch it back
    # off; leaving ``None`` would preserve the style's numbering and its tab
    # position instead of preserving the caller's text.
    if block.heading_type is None and (
        observed.heading_type == 0 or paragraph_lead_marker(block.text)
    ):
        updates["heading_type"] = 0
    if block.heading_level is None and observed.heading_level is not None:
        if 0 <= observed.heading_level <= 6:
            updates["heading_level"] = observed.heading_level
    return updates


def _replicated(
    block: ParagraphBlock,
    usage: DocumentStyleUsage,
    style_id: int | None,
    updates: dict[str, object],
) -> ParagraphBlock:
    """Apply the resolved style plus the document's observed appearance."""
    updates |= _replicated_updates(block, usage.format_for_style(style_id))
    text = _replicated_marker(block, usage, style_id)
    if text is not None:
        updates["text"] = text
    return block.model_copy(update=updates) if updates else block


def _replicated_marker(
    block: ParagraphBlock,
    usage: DocumentStyleUsage,
    style_id: int | None,
) -> str | None:
    """Replace only an unambiguous glyph with this style's observed raw glyph."""
    if block.preserve_source_text:
        return None
    own = paragraph_lead_marker(block.text)
    if not own or not paragraph_marker_is_unambiguous(own):
        return None
    marker = usage.marker_for_style(style_id)
    if (
        not marker
        or marker == own
        or not paragraph_marker_is_unambiguous(marker)
        or paragraph_shape(marker) != paragraph_shape(own)
    ):
        return None
    stripped = block.text.lstrip()
    if not stripped.startswith(own):
        return None
    leading_space = block.text[: len(block.text) - len(stripped)]
    return leading_space + marker + stripped[len(own) :]


def _report_list_item_style(
    block: ReportListItemBlock,
    styles: tuple[DocumentStyle, ...],
    fallback_style_id: int,
    usage: DocumentStyleUsage,
) -> ParagraphBlock:
    """Use a repeated typed convention, or honestly fall back to plain body.

    Automatic numbering is intentionally not synthesized here. The bridge can
    observe ``HeadingType`` but cannot copy the nested numbering definition that
    chooses the actual glyph; switching it on with a scalar alone has produced
    a table-caption number in place of a list marker. A literal prefix is safe
    only when this document repeatedly typed that exact prefix. Otherwise the
    item remains markerless body text.
    """
    plain = ParagraphBlock.model_validate(block.model_dump())
    marker = usage.preferred_typed_marker()
    if marker is None:
        style_id = _first_style(
            _observed_body_style(usage),
            _role_style(styles, "body"),
            fallback_style_id,
        )
        return _replicated(plain, usage, style_id, {"style_id": style_id})
    style_id, prefix = marker
    return _replicated(
        plain.model_copy(update={"text": f"{prefix}{plain.text}"}),
        usage,
        style_id,
        {"style_id": style_id},
    )


def _paragraph_style(
    block: ParagraphBlock,
    styles: tuple[DocumentStyle, ...],
    fallback_style_id: int,
    usage: DocumentStyleUsage,
) -> ParagraphBlock:
    if isinstance(block, ReportListItemBlock):
        return _report_list_item_style(block, styles, fallback_style_id, usage)
    if block.style_id is not None:
        return _replicated(block, usage, block.style_id, {})
    if block.style_name is not None:
        named = _named_style(styles, block.style_name)
        return _replicated(
            block,
            usage,
            named,
            {"style_id": named, "style_name": None},
        )
    role = block.style_role
    # 모양은 역할과 무관하게 먼저 읽는다. 호출자가 명시적으로 쓴 기호 문단은
    # style_role="body" 여도 문서가 같은 기호에 실제로 쓰는 스타일을 따라야 한다.
    shape = _heading_shape(block.text)
    # 문서가 그 모양에 실제로 쓰는 스타일이 1순위다. 이름이 "동그라미"든
    # "circle"이든 상관없어지는 지점이 여기다.
    observed = usage.style_for_shape(shape) if shape is not None else None
    # 호출자가 역할을 못 박은 경우에는 구조적으로 모호하지 않은 마커만 그
    # 선언을 뒤집을 수 있다. ``3.5 배 증가했다`` 같은 숫자 토큰은 관측 사실로
    # 남지만, 그것만으로 본문을 제목 스타일에 보내지는 않는다.
    marker_observed = observed if paragraph_marker_is_unambiguous(block.text) else None
    if role in {"auto", "heading"}:
        resolved = observed
        if role == "heading" and resolved in _plain_paragraph_styles(styles, usage):
            # 호출자가 "이건 제목"이라고 못 박았는데 관측 표가 "평범한 문단"
            # 이라고 답하면, 그 관측은 제목의 증거가 아니라 오염의 증거다
            # (본문 문장 "3.5 배 증가했다"가 decimal2 로 잡힌 경우). 문서에
            # 그 모양의 제목 스타일이 실제로 있으면 그쪽을 쓴다.
            resolved = _first_style_or_none(_heading_style(styles, shape), resolved)
        # role="heading" without a recognisable marker walks the document's
        # style catalog order. Only role="auto" may fall through to body.
        if resolved is None and shape is not None:
            resolved = _heading_style(styles, shape)
        if resolved is None and role == "heading":
            resolved = _heading_style(styles, None)
        if resolved is None and role == "auto":
            # 모양이 없거나 그 모양의 스타일이 문서에 없을 때. 호출자가 넘긴
            # fallback 은 "이 문서의 본문"이 아니므로 관측된 본문 스타일과
            # 본문 역할 스타일을 먼저 본다.
            resolved = _first_style_or_none(
                _observed_body_style(usage),
                _role_style(styles, "body"),
            )
    elif role == "table_title":
        resolved = _first_style_or_none(
            _role_style(styles, "table_title"),
            marker_observed,
        )
    elif role == "figure_title":
        resolved = _first_style_or_none(
            _role_style(styles, "figure_title"),
            marker_observed,
        )
    else:
        # role="body". 문서가 같은 명시적 기호 문단에 실제로 쓰는 스타일은
        # 이름 추측과 달리 믿을 수 있으므로 본문 스타일보다 앞선다.
        # 반면 _heading_style 의 이름 매칭은 여기서 쓰지 않고 숫자 모양도 보지
        # 않는다 — "3.5 배 증가했다" 같은 본문 문장이 제목 스타일을 받아 가면
        # 오염이 반대 방향으로 재현된다.
        resolved = _first_style_or_none(
            marker_observed,
            _observed_body_style(usage),
            _role_style(styles, "body"),
        )
    style_id = fallback_style_id if resolved is None else resolved
    return _replicated(block, usage, style_id, {"style_id": style_id})


def _table_style(
    block: TableBlock,
    styles: tuple[DocumentStyle, ...],
    fallback_style_id: int,
    content_width_mm: float,
    usage: DocumentStyleUsage,
    conventions: ObservedTableConventions = EMPTY_TABLE_CONVENTIONS,
) -> TableBlock:
    block = (
        explicit_shared_borders(block)
        if block.border_mode == "explicit"
        else inherit_conflicting_shared_borders(block)
    )
    base_style_id = block.base_style_id
    if base_style_id is None:
        base_style_id = _first_style(
            _named_or_role_style(styles, block.base_style_name, "table_body")
            if block.base_style_name is not None
            else None,
            _role_style(styles, "table_body"),
            _observed_body_style(usage),
            _role_style(styles, "body"),
            fallback_style_id,
        )
    caption_style_id = block.caption_style_id
    if block.caption is not None and caption_style_id is None:
        caption_style_id = _first_style(
            _named_or_role_style(styles, block.caption_style_name, "table_title")
            if block.caption_style_name is not None
            else None,
            _role_style(styles, "table_title"),
            _observed_body_style(usage),
            _role_style(styles, "body"),
            fallback_style_id,
        )
    styled = block.model_copy(
        update={
            "base_style_id": base_style_id,
            "base_style_name": None,
            "caption_style_id": caption_style_id,
            "caption_style_name": None,
            "rows": tuple(
                tuple(
                    cell.model_copy(
                        update={
                            "paragraphs": tuple(
                                _paragraph_style(
                                    paragraph,
                                    styles,
                                    base_style_id,
                                    usage,
                                )
                                for paragraph in cell.paragraphs
                            )
                        }
                    )
                    for cell in row
                )
                for row in block.rows
            ),
        }
    )
    # The paragraphs inside the cells were just replicated through
    # ``_paragraph_style``; this does the same for the cell fields themselves,
    # which no observation reached before. Both leave every value the caller
    # stated exactly where it was.
    return resolve_table_geometry(
        replicate_table_conventions(styled, conventions),
        content_width_mm,
    )


def _image_style(
    block: ImageBlock,
    styles: tuple[DocumentStyle, ...],
    fallback_style_id: int,
    content_width_mm: float,
    usage: DocumentStyleUsage,
) -> ImageBlock:
    updates: dict[str, object] = {}
    caption_style_id = block.caption_style_id
    if block.caption is not None and caption_style_id is None:
        caption_style_id = _first_style(
            _named_or_role_style(styles, block.caption_style_name, "figure_title")
            if block.caption_style_name is not None
            else None,
            _role_style(styles, "figure_title"),
            _observed_body_style(usage),
            _role_style(styles, "body"),
            fallback_style_id,
        )
        updates["caption_style_id"] = caption_style_id
        updates["caption_style_name"] = None
    if block.caption is not None and _has_matching_automatic_caption_marker(
        block.caption,
        caption_style_id,
        usage,
    ):
        raise HwpLiveError(
            "caption must omit a literal figure number because the observed "
            + "document figure-title style inserts its automatic number"
        )
    if block.width_mm > content_width_mm:
        ratio = content_width_mm / block.width_mm
        updates["width_mm"] = content_width_mm
        updates["height_mm"] = block.height_mm * ratio
    return block.model_copy(update=updates) if updates else block


def document_body_style_id(
    styles: tuple[DocumentStyle, ...],
    usage: DocumentStyleUsage | None = None,
) -> int:
    """Style the document itself uses for plain body text.

    This is the last-resort ``fallback_style_id`` for callers that cannot read
    the caret style: it never raises, so a document whose style list is empty
    or unnamed still resolves to 0 (바탕글) instead of blocking the edit.
    An observed usage table wins over the name guess when one is available.
    """
    observed = None if usage is None else usage.body_style_id
    if observed is not None:
        return observed
    resolved = _role_style(styles, "body")
    if resolved is not None:
        return resolved
    return styles[0].style_id if styles else 0


def automatic_numbering_notice(
    plan: LayoutPlan,
    usage: DocumentStyleUsage | None,
) -> str:
    """Say when a resolved paragraph is relying on a number nobody switched on.

    The case this exists for, from the 2026-08-30 live run: a paragraph was
    given ``style_id=16``, whose observed run reports
    ``marker_is_automatic=true, heading_type=2, lead_marker="1)"``. Nothing in
    the response mentioned numbering, the write reported verified=true, and the
    number was simply absent from the render -- so the model read the render,
    concluded the numbering had failed, and patched literal ``1) 2) 3)`` into
    the text. Had the style's numbering in fact fired, that document would now
    carry both.

    What is stated here is only what this repository can show:

    * the resolved style's observed marker is one HWP draws itself
      (``DocumentStyleUsage.marker_is_automatic``, read from the document);
    * this call sends no command that switches numbering on. ``Style/Apply``
      is the only numbering-adjacent command in the layout path
      (``hwp_live_native_layout.build_native_layout_request``), and
      ``paragraph_command`` emits ``HeadingType`` only when the block states
      one -- which ``_replicated_updates`` above never does, deliberately,
      because the glyph lives in the nested ``ParaShape/Numbering`` set this
      bridge cannot reproduce.

    Whether ``Style/Apply`` alone makes HWP draw the style's number is NOT
    claimed either way. It is not decided anywhere in this repository, and the
    one live observation says it did not. So the notice reports the two facts
    and points at the render, instead of predicting an outcome.
    """
    if usage is None:
        return ""
    automatic: dict[int, list[int]] = {}
    for index, block in enumerate(plan.blocks):
        if not isinstance(block, ParagraphBlock) or block.style_id is None:
            continue
        if block.heading_type == 0:
            # The plan already turned numbering off for this paragraph; its
            # marker is whatever its text carries.
            continue
        if paragraph_lead_marker(block.text):
            # The caller typed a marker. Whatever the style does, the text is
            # the one that shows -- and a doubled number would be visible.
            continue
        if usage.marker_is_automatic(block.style_id) is not True:
            continue
        automatic.setdefault(block.style_id, []).append(index)
    if not automatic:
        return ""
    styles = [
        {
            "style_id": style_id,
            "observed_lead_marker": usage.marker_for_style(style_id),
            "block_indexes": indexes,
        }
        for style_id, indexes in sorted(automatic.items())
    ]
    facts = {
        "basis": "observed_style_usage_and_emitted_commands_not_render_readback",
        "styles": styles,
        "numbering_command_sent": False,
        "note": (
            "이 문단들이 받은 스타일은 문서에서 한/글이 직접 번호를 그리는"
            " 스타일로 관측됐습니다. 이 호출은 Style/Apply 외에 번호를 켜는"
            " 명령을 보내지 않으며(ParaShape/Numbering 은 이 브리지가 재현하지"
            " 못합니다), 번호가 실제로 찍혔는지는 이 응답이 확인하지 않았습니다."
            " 렌더로 확인하고, 번호가 없으면 문단 텍스트에 리터럴 마커를"
            " 넣으세요. 번호가 이미 찍혔는데 리터럴을 넣으면 이중 번호가 됩니다"
        ),
    }
    return "automatic_numbering_facts=" + dumps(
        facts, ensure_ascii=False, separators=(",", ":")
    )


def resolve_layout_style_profile(
    plan: LayoutPlan,
    styles: tuple[DocumentStyle, ...],
    *,
    fallback_style_id: int,
    content_width_mm: float,
    usage: DocumentStyleUsage | None = None,
    structure: DocumentStructure | None = None,
) -> LayoutPlan:
    """Bind a plan to this document's style ids.

    ``usage`` is the [leading shape -> style id] table read from the document's
    own paragraphs. When it is present it outranks style-name matching; when it
    is absent (older bridge, scan failed) everything behaves exactly as before.

    ``structure`` is the observed page's own tables, the only place cell
    appearance is reported at all -- the paragraph scan behind ``usage`` never
    descends into a table. When it is present the sampled cells fill in the
    cell fields a table block left unset, exactly as ``usage`` fills in the
    paragraph ones; when it is absent nothing changes.
    """
    if content_width_mm < 5:
        raise HwpLiveError("현재 문서의 본문 폭을 확인할 수 없습니다")
    observed = EMPTY_STYLE_USAGE if usage is None else usage
    conventions = observed_table_conventions(structure)
    blocks: list[LayoutBlock] = []
    for block in plan.blocks:
        if isinstance(block, ParagraphBlock):
            blocks.append(_paragraph_style(block, styles, fallback_style_id, observed))
        elif isinstance(block, TableBlock):
            available = content_width_mm - block.left_margin_mm - block.right_margin_mm
            blocks.extend(
                _table_style(
                    part,
                    styles,
                    fallback_style_id,
                    content_width_mm,
                    observed,
                    conventions,
                )
                for part in split_wide_table(block, available)
            )
        elif isinstance(block, ImageBlock):
            blocks.append(
                _image_style(
                    block,
                    styles,
                    fallback_style_id,
                    content_width_mm,
                    observed,
                )
            )
        else:
            blocks.append(block)
    return plan.model_copy(update={"blocks": tuple(blocks)})
