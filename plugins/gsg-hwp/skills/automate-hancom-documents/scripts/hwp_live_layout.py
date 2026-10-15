from __future__ import annotations

import ntpath
from collections.abc import Callable, Mapping
from pathlib import Path

from hwp_document_style_profile import (
    document_body_style_id,
    resolve_layout_style_profile,
)
from hwp_document_style_usage import DocumentStyleUsage
from hwp_errors import HwpLiveError
from hwp_image_fit import fit_image_in_box
from hwp_live_api import HwpControl, LiveHwpApplication, ShapeValue
from hwp_live_anchor import require_empty_paragraph
from hwp_live_contract import (
    ImageBlock,
    LayoutPlan,
    ParagraphBlock,
)
from hwp_live_formatting import apply_paragraph_style, apply_text_style
from hwp_live_inspection import inspect_styles
from hwp_live_native_text_format import lead_before_mm
from hwp_live_rot import HwpDocumentCandidate
from hwp_live_session_structure_inspection import inspect_candidate_structure
from hwp_live_structure_contract import DocumentStructure
from hwp_live_structure_identity import structure_token
from hwp_live_table import insert_table
from hwp_live_table_contract import TableBlock
from hwp_picture_placement import enforce_picture_size
from hwp_reference_layout_geometry import (
    HWPUNITS_PER_INCH,
    MILLIMETERS_PER_INCH,
    SectionPageGeometry,
)
from hwp_trusted_paths import input_local_image


def _page_value(values: Mapping[str, ShapeValue], key: str) -> float:
    value = values.get(key)
    if isinstance(value, bool):
        return float(int(value))
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0


def document_page_geometry(
    hwp: LiveHwpApplication,
    guard: Callable[[], None],
) -> SectionPageGeometry:
    page = hwp.get_pagedef_as_dict("eng")
    guard()
    return SectionPageGeometry.from_mm(
        paper_width_mm=_page_value(page, "PaperWidth"),
        paper_height_mm=_page_value(page, "PaperHeight"),
        landscape=bool(_page_value(page, "Landscape")),
        left_margin_mm=_page_value(page, "LeftMargin"),
        right_margin_mm=_page_value(page, "RightMargin"),
        top_margin_mm=_page_value(page, "TopMargin"),
        bottom_margin_mm=_page_value(page, "BottomMargin"),
        header_mm=_page_value(page, "HeaderLen"),
        footer_mm=_page_value(page, "FooterLen"),
        gutter_mm=_page_value(page, "GutterLen"),
        gutter_type=int(_page_value(page, "GutterType")),
    )


def document_content_width_mm(geometry: SectionPageGeometry) -> float:
    return (
        geometry.usable_area(page_number=1).width
        * MILLIMETERS_PER_INCH
        / HWPUNITS_PER_INCH
    )


def plan_contains_table(plan: LayoutPlan) -> bool:
    return any(isinstance(block, TableBlock) for block in plan.blocks)


def _usable_table_structure(
    structure: DocumentStructure,
    candidate: HwpDocumentCandidate,
    page: int,
) -> bool:
    """Whether this snapshot really describes the page about to be written.

    The cell sample pool is one page wide, so a snapshot of a *different* page
    would spread that page's header shading and heading font over a table
    somewhere else; identity and page number are checked for exactly that
    reason, and a snapshot handed in from another workflow is refused rather
    than trusted. ``state_token`` is a hash of the snapshot's own content
    (``hwp_live_structure_identity.structure_token``), so one that no longer
    matches has been edited since it was read -- a stale observation is worse
    than no observation, because nothing downstream would say it was stale.
    """
    expected_name = ntpath.normcase(ntpath.normpath(candidate.full_name))
    actual_name = ntpath.normcase(ntpath.normpath(structure.full_name))
    if (
        structure.document_id != candidate.document_id
        or actual_name != expected_name
        or structure.window_handle != candidate.window_handle
    ):
        return False
    if page > 0 and structure.page != page:
        return False
    return structure.state_token == structure_token(structure)


def observed_table_structure(
    hwp: LiveHwpApplication,
    candidate: HwpDocumentCandidate,
    plan: LayoutPlan,
    guard: Callable[[], None],
    *,
    page: int = 0,
) -> DocumentStructure | None:
    """The write target page's own tables, read once, only when one is needed.

    Cell conventions are the *only* reason this call exists, so a plan with no
    table pays nothing: paragraph and image plans keep exactly the round trips
    they had before conventions were replicated at all.

    Nothing here may stop a write. A bridge that cannot answer, a page that
    cannot be read, a call that times out, a reply this repository's contracts
    reject, or a snapshot that fails ``_usable_table_structure`` all degrade to
    ``None``, which is the same argument ``resolve_layout_style_profile``
    received before this wiring existed and therefore the byte-identical old
    path.
    """
    if not plan_contains_table(plan):
        return None
    try:
        structure = inspect_candidate_structure(hwp, candidate, page, guard)
    except HwpLiveError:
        # The bridge's own way of saying no: a build without the command, a
        # page it could not read, a document identity that did not match, a
        # transport timeout. Expected, and the reason this call was optional
        # from the start.
        structure = None
    except Exception:  # noqa: BLE001 - 표 관례 때문에 쓰기가 죽어서는 안 된다
        # Everything else, and deliberately everything, because the two layers
        # under this call do not raise only ``HwpLiveError``:
        #
        #   * ``document_structure_from_native`` validates the native reply
        #     against the structure contract, so a field outside its bounds
        #     (``DocumentStructure.page`` is ``ge=1`` -- a 0 is enough) leaves
        #     as pydantic's ``ValidationError``, which is a ``ValueError``.
        #   * the dispatch layer beneath it can surface a raw ``OSError`` or
        #     ``com_error`` that never gets wrapped on the way out.
        #
        # Neither has been observed in a live run, which is exactly why the
        # narrow clause looked sufficient -- and exactly why being wrong about
        # it is expensive: the cost is a refused write on a document that had
        # nothing wrong with it, to protect an addition the write never needed.
        # ``KeyboardInterrupt`` and ``SystemExit`` derive from
        # ``BaseException`` and still travel.
        structure = None
    # ``inspect_candidate_structure`` runs the guard inside its own ``finally``,
    # so the excepts above could have swallowed a genuine "the active document
    # moved under us" abort along with a harmless read failure. The guard is a
    # re-checkable condition rather than a one-shot event, so asking it again
    # here re-raises that abort instead of writing into a document this call no
    # longer owns.
    guard()
    if structure is None or not _usable_table_structure(structure, candidate, page):
        return None
    return structure


def resolve_layout_styles(
    hwp: LiveHwpApplication,
    plan: LayoutPlan,
    guard: Callable[[], None],
    *,
    usage: DocumentStyleUsage | None = None,
    candidate: HwpDocumentCandidate | None = None,
) -> tuple[LayoutPlan, SectionPageGeometry]:
    """Bind a bulk layout plan to the styles the open document actually has.

    This used to only rewrite ``TableBlock`` style *names* and left
    ``ParagraphBlock`` untouched, so every paragraph the C++ batch inserted
    reached HWP with no ``Style`` action at all — the document's paragraph
    numbering and bullets (``○``, ``(1)``, ``·``) never came along and the model
    was left typing the glyphs as literal text. Both the bulk path and the
    recipe path now go through the same resolver
    (``hwp_document_style_profile.resolve_layout_style_profile``), which is what
    ``hwp_live_operation_recipe.native_style_plan`` already calls, so the two
    paths agree on which document style each role gets.

    ``usage`` is the observed [leading shape -> style id] table; passing it lets
    this path pick ``○``'s real style (``동그라미``) instead of guessing by name.

    ``candidate`` is what turns table-cell conventions on. The paragraph scan
    behind ``usage`` never descends into a table, so cell appearance can only
    come from a structure read, and that read needs a window handle this
    signature otherwise does not have. Omitting it keeps the previous
    behaviour exactly; supplying it lets the current page's own tables fill in
    the cell fields a table block left unset.
    """
    styles = inspect_styles(hwp, guard).styles
    structure = (
        None
        if candidate is None
        # Page 0 is "wherever the caret is", which for this path is the page
        # the insertion lands on -- read immediately before the write, after
        # the caret and selection checks the caller has already made.
        else observed_table_structure(hwp, candidate, plan, guard)
    )
    page_geometry = document_page_geometry(hwp, guard)
    return (
        resolve_layout_style_profile(
            plan,
            styles,
            fallback_style_id=document_body_style_id(styles, usage),
            content_width_mm=document_content_width_mm(page_geometry),
            usage=usage,
            structure=structure,
        ),
        page_geometry,
    )


def prepare_layout_assets(plan: LayoutPlan) -> dict[Path, Path]:
    assets: dict[Path, Path] = {}
    for block in plan.blocks:
        if isinstance(block, ImageBlock):
            assets[block.path] = input_local_image(block.path)
        elif isinstance(block, TableBlock):
            for row in block.rows:
                for cell in row:
                    if cell.image_path is not None:
                        assets[cell.image_path] = input_local_image(cell.image_path)
    return assets


def _insert_paragraph(
    hwp: LiveHwpApplication,
    block: ParagraphBlock,
    guard: Callable[[], None],
) -> None:
    guard()
    if block.style_id is not None and not hwp.set_style(block.style_id):
        raise HwpLiveError("한컴 문단 스타일을 적용하지 못했습니다")
    guard()
    apply_text_style(
        hwp,
        bold=block.bold,
        font_name=block.font_name,
        font_size_pt=block.font_size_pt,
        text_color=block.text_color,
    )
    guard()
    apply_paragraph_style(
        hwp,
        alignment=block.alignment,
        line_spacing=block.line_spacing_percent,
        before=lead_before_mm(block.space_before_mm, block.plan_lead_mm),
        after=block.space_after_mm,
        left_margin=block.left_margin_mm,
        right_margin=block.right_margin_mm,
        indentation=block.indentation_mm,
    )
    guard()
    if not hwp.insert_text(block.text):
        raise HwpLiveError("한컴 문단 텍스트를 삽입하지 못했습니다")
    guard()
    if not hwp.BreakPara():
        raise HwpLiveError("한컴 문단을 삽입하지 못했습니다")
    guard()


def _insert_image(
    hwp: LiveHwpApplication,
    block: ImageBlock,
    assets: dict[Path, Path],
    guard: Callable[[], None],
) -> None:
    guard()
    # The native path also pins this paragraph's line spacing to 100% and puts
    # it back by re-applying the document's base style afterwards. This COM
    # fallback has no style id to go back to and no verb that means "inherit
    # again", so it writes only the lead -- which is restorable -- and carries
    # the line's own leading instead. Position is preserved either way; the
    # per-picture leading the native path removes is still present here.
    apply_paragraph_style(
        hwp,
        alignment=block.alignment,
        before=block.plan_lead_mm,
    )
    guard()
    control = hwp.insert_picture(str(assets[block.path]))
    guard()
    width, height = fit_image_in_box(
        assets[block.path],
        width_mm=block.width_mm,
        height_mm=block.height_mm,
    )
    enforce_picture_size(
        hwp,
        control,
        width_mm=width,
        height_mm=height,
        guard=guard,
    )
    guard()
    if not hwp.BreakPara():
        raise HwpLiveError("그림 다음 문단을 만들지 못했습니다")
    guard()
    if block.plan_lead_mm is not None:
        # BreakPara copies the picture paragraph's shape into the paragraph it
        # opens, lead included, so a 50mm lead would reappear under the picture
        # and push everything after it down. Clearing it is the same leak the
        # native path closes by re-applying the base style.
        apply_paragraph_style(hwp, alignment="inherit", before=0.0)
        guard()
    if block.caption is not None:
        if block.caption_style_id is not None and not hwp.set_style(
            block.caption_style_id
        ):
            raise HwpLiveError("한컴 그림 캡션 스타일을 적용하지 못했습니다")
        guard()
        apply_paragraph_style(hwp, alignment="center")
        guard()
        if not hwp.insert_text(block.caption):
            raise HwpLiveError("한컴 그림 캡션 텍스트를 삽입하지 못했습니다")
        guard()
        if not hwp.BreakPara():
            raise HwpLiveError("그림 캡션을 삽입하지 못했습니다")
        guard()


def _insert_page_break(
    hwp: LiveHwpApplication,
    guard: Callable[[], None],
) -> None:
    guard()
    pages_before = hwp.PageCount
    guard()
    position_before = hwp.get_pos()
    guard()
    result = hwp.BreakPage()
    guard()
    pages_after = hwp.PageCount
    guard()
    position_after = hwp.get_pos()
    guard()
    if result or pages_after != pages_before or position_after != position_before:
        return
    guard()
    if not hwp.BreakPara():
        raise HwpLiveError("한컴 쪽 나누기용 문단을 만들지 못했습니다")
    guard()
    pages_before = hwp.PageCount
    guard()
    position_before = hwp.get_pos()
    guard()
    result = hwp.BreakPage()
    guard()
    pages_after = hwp.PageCount
    guard()
    position_after = hwp.get_pos()
    guard()
    if not result and pages_after == pages_before and position_after == position_before:
        raise HwpLiveError("한컴 쪽 나누기를 삽입하지 못했습니다")


def apply_layout(
    hwp: LiveHwpApplication,
    plan: LayoutPlan,
    assets: dict[Path, Path],
    guard: Callable[[], None],
) -> tuple[HwpControl, ...]:
    created: list[HwpControl] = []
    for index, block in enumerate(plan.blocks):
        guard()
        if isinstance(block, ParagraphBlock):
            _insert_paragraph(hwp, block, guard)
        elif isinstance(block, TableBlock):
            created.append(
                insert_table(
                    hwp,
                    block,
                    assets,
                    check_anchor=index != 0,
                    guard=guard,
                )
            )
        elif isinstance(block, ImageBlock):
            _insert_image(hwp, block, assets, guard)
        else:
            _insert_page_break(hwp, guard)
        guard()
    return tuple(created)


def validate_layout_anchor(
    hwp: LiveHwpApplication,
    plan: LayoutPlan,
    guard: Callable[[], None],
) -> None:
    if isinstance(plan.blocks[0], TableBlock):
        require_empty_paragraph(hwp, guard)
