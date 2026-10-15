from __future__ import annotations

import ntpath
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Final, cast

from pydantic import JsonValue

from hwp_document_convention_profile import build_document_convention_profile
from hwp_document_style_observation import resolve_document_style_usage
from hwp_document_style_usage import EMPTY_STYLE_USAGE, ObservedStyleRun
from hwp_errors import HwpLiveError
from hwp_live_contract import (
    DocumentStyleList,
    DocumentStyleObservation,
    LiveContext,
    ObservedDocumentStyle,
    PreviewResult,
)
from hwp_live_grounding import (
    GroundingCleanRender,
    GroundingOverlay,
    GroundingPageState,
    GroundingProvenance,
    GroundingReadSnapshot,
    GroundedPageSetup,
    HwpGroundingReport,
    HwpGroundingRequest,
    assert_grounding_stable,
    aggregate_structure_token,
    build_convention_evidence,
    build_page_geometry,
    canonical_sha256,
    content_signature_sha256,
    create_grounding_overlay,
    observed_page_setup,
    png_dimensions,
    sha256_file,
)
from hwp_live_inspection import inspect_styles
from hwp_live_native_action_models import NativePageInspection
from hwp_live_native_batch import (
    forget_cached_content_signatures,
    inspect_native_page,
    read_native_content_revision,
    read_native_content_signature,
    read_native_snapshot,
)
from hwp_live_native_structure import fast_cell_format
from hwp_live_preview import PreviewApplication, render_page
from hwp_live_rot import HwpDocumentCandidate
from hwp_live_session_core import LiveHwpSessionCore
from hwp_live_session_structure import (
    inspect_candidate_context,
    inspect_candidate_state,
    inspect_candidate_structure,
)
from hwp_live_session_structure_inspection import require_snapshot_document
from hwp_reference_layout_geometry import urc_to_mm
from hwp_live_structure_contract import (
    DocumentStructure,
    FastControlInspectionError,
    FastPageCell,
    FastPageCellFormat,
    FastPageControl,
    FastPageInspection,
    FastParagraphFormat,
    StructurePosition,
)


def _hwpunit_to_mm(value: int | None) -> float | None:
    """A plain HWPUNIT length in mm. Not for ParaShape lengths -- see below."""
    return None if value is None else round(value / 283.4645669, 3)


def _fast_cell_formats(
    inspected: NativePageInspection,
) -> tuple[FastPageCellFormat, ...]:
    """Cell appearance the bridge sampled, tagged with the table it came from.

    Empty when the loaded bridge does not report cell appearance, which is the
    same answer as before this record existed: the caller sees no claim rather
    than a made-up one.
    """
    formats: list[FastPageCellFormat] = []
    for native_format in inspected.cell_formats:
        observed = fast_cell_format(native_format)
        if observed is not None:
            formats.append(observed)
    return tuple(formats)


# The scan is capped natively; this only bounds the folded answer so a document
# that applies dozens of styles cannot make the response grow without limit.
_OBSERVED_STYLE_LIMIT: Final = 64
# The bridge already caps a paragraph lead at 24 characters. This bounds the
# folded answer against any other source of the same shape.
_OBSERVED_LEAD_LIMIT: Final = 64


def _observed_style(
    run: ObservedStyleRun,
    name: str | None,
    list_id: int,
) -> ObservedDocumentStyle:
    """One observed run as the tool reports it.

    The representative paragraph's real appearance rides along so a caller can
    see *why* an inserted paragraph looks the way it does -- and check it
    against the document -- instead of only being told which style id won. It
    is one paragraph per style, not per paragraph, so the section grows with
    the number of styles a document uses (tens), never with its length.
    """
    shape = run.format
    return ObservedDocumentStyle(
        style_id=run.style_id,
        name=name,
        paragraphs=run.paragraphs,
        first_paragraph=run.first_paragraph,
        shared_lead_prefix=run.shared_lead_prefix[:_OBSERVED_LEAD_LIMIT],
        lead_samples=tuple(
            sample[:_OBSERVED_LEAD_LIMIT] for sample in run.lead_samples
        ),
        lead_marker=run.lead_marker[:_OBSERVED_LEAD_LIMIT],
        font_name=None if shape is None else shape.face_name,
        font_size_pt=(
            None
            if shape is None or shape.height is None
            else round(shape.height / 100, 2)
        ),
        bold=None if shape is None else shape.bold,
        alignment=None if shape is None else shape.alignment,
        line_spacing=None if shape is None else shape.line_spacing,
        left_margin_mm=(
            None if shape is None else urc_to_mm(shape.left_margin_hwpunit)
        ),
        right_margin_mm=(
            None if shape is None else urc_to_mm(shape.right_margin_hwpunit)
        ),
        indentation_mm=(
            None if shape is None else urc_to_mm(shape.indentation_hwpunit)
        ),
        space_before_mm=(
            None if shape is None else urc_to_mm(shape.previous_spacing_hwpunit)
        ),
        space_after_mm=(
            None if shape is None else urc_to_mm(shape.next_spacing_hwpunit)
        ),
        heading_type=None if shape is None else shape.heading_type,
        heading_level=None if shape is None else shape.heading_level,
        marker_is_automatic=run.marker_is_automatic,
        representative_paragraph=run.representative_paragraph,
        list_id=list_id,
    )


def with_observed_usage(
    styles: DocumentStyleList,
    candidate: HwpDocumentCandidate,
    *,
    page_setup: Mapping[str, JsonValue] | None = None,
    structure: DocumentStructure | None = None,
    context_observation_error: str | None = None,
    structure_observation_error: str | None = None,
) -> DocumentStyleList:
    """Attach what the document actually does to the list of what it defines.

    The traversal is the bridge's (``LiveInspection.cpp::InspectParagraphStyles``)
    and its result is cached per document, so a second ``hwp_list_styles`` on an
    unchanged document costs nothing. Observation never blocks the read: a
    bridge that predates the scan, a failed scan or a scan of another document
    leaves explicit ``unobserved`` axes and the defined-style list intact.
    """
    paragraph_error: str | None = None
    try:
        usage = resolve_document_style_usage(
            candidate.window_handle,
            candidate.document_id,
            candidate.full_name,
        )
    except (HwpLiveError, OSError, ValueError) as error:
        usage = EMPTY_STYLE_USAGE
        paragraph_error = str(error)
    if not usage.observed_styles:
        paragraph_error = (
            paragraph_error or "네이티브 문단 관례 상세 관측을 사용할 수 없습니다."
        )
        usage = replace(usage, complete=False)
    names = {style.style_id: style.name for style in styles.styles}
    observed = tuple(
        _observed_style(run, names.get(run.style_id), usage.list_id)
        for run in usage.observed_styles
        if 0 <= run.style_id <= 4_095
    )
    return styles.model_copy(
        update={
            "observed_usage": DocumentStyleObservation(
                scanned_paragraphs=usage.scanned_paragraphs,
                counted_paragraphs=usage.observed_paragraphs,
                # Truncating the fold is itself an incompleteness: say so
                # rather than presenting a cut list as the whole document.
                complete=usage.complete and len(observed) <= _OBSERVED_STYLE_LIMIT,
                styles=observed[:_OBSERVED_STYLE_LIMIT],
                convention_profile=build_document_convention_profile(
                    usage,
                    style_names=names,
                    page_setup=page_setup,
                    structure=structure,
                    paragraph_observation_error=paragraph_error,
                    context_observation_error=context_observation_error,
                    structure_observation_error=structure_observation_error,
                ),
            )
        }
    )


class LiveHwpInspectionSession(LiveHwpSessionCore):
    __slots__: tuple[str, ...] = ()
    _structure_snapshot: DocumentStructure | None

    def context(self, session_id: str) -> LiveContext:
        candidate, hwp = self._validate(session_id)
        return inspect_candidate_context(
            hwp,
            candidate,
            self._guard(candidate, hwp),
        )

    def styles(
        self,
        session_id: str,
        *,
        allow_historical_convention: bool = True,
    ) -> DocumentStyleList:
        candidate, hwp = self._validate(session_id)
        guard = self._guard(candidate, hwp)
        state_token = self.style_state_token(session_id)

        def read() -> DocumentStyleList:
            return inspect_styles(hwp, guard)

        if state_token is None:
            styles = read()
        else:
            styles = self._style_cache.resolve(
                candidate.document_id,
                candidate.full_name,
                state_token,
                read,
            )
        modified = bool(candidate.document.Modified)
        cached = self._convention_cache.lookup(
            Path(candidate.full_name),
            modified=modified or not allow_historical_convention,
        )
        if cached.profile is not None:
            profile = cached.profile
            # 적중은 미스와 같은 답을 내야 한다. 스타일별 관측(글꼴·크기·글머리
            # ·대표 문단)은 관례 프로필에 접혀 있지 않으므로 캐시가 따로 실어
            # 둔 목록을 그대로 돌려주고, 완전성도 저장 당시의 값을 쓴다.
            return styles.model_copy(
                update={
                    "observed_usage": DocumentStyleObservation(
                        scanned_paragraphs=profile.scanned_paragraphs,
                        counted_paragraphs=profile.counted_paragraphs,
                        complete=cached.styles_complete,
                        styles=cached.styles,
                        convention_profile=profile,
                    )
                }
            )
        page_setup: Mapping[str, JsonValue] | None = None
        structure: DocumentStructure | None = None
        context_error: str | None = None
        structure_error: str | None = None
        try:
            context = inspect_candidate_context(hwp, candidate, guard)
            page_setup = cast(
                Mapping[str, JsonValue],
                context.page_setup.model_dump(mode="json"),
            )
        except HwpLiveError as error:
            context_error = str(error)
        try:
            structure = inspect_candidate_structure(hwp, candidate, 0, guard)
        except HwpLiveError as error:
            structure_error = str(error)
        observed_styles = with_observed_usage(
            styles,
            candidate,
            page_setup=page_setup,
            structure=structure,
            context_observation_error=context_error,
            structure_observation_error=structure_error,
        )
        observed = observed_styles.observed_usage
        if observed is None or observed.convention_profile is None:
            return observed_styles
        profile = observed.convention_profile
        metadata = cached.metadata
        if not modified and allow_historical_convention:
            stored = self._convention_cache.store(
                Path(candidate.full_name),
                profile,
                styles=observed.styles,
                styles_complete=observed.complete,
            )
            metadata = metadata.model_copy(update={"created_at": stored.created_at})
        return observed_styles.model_copy(
            update={
                "observed_usage": observed.model_copy(
                    update={
                        "convention_profile": profile.model_copy(
                            update={"cache": metadata}
                        )
                    }
                )
            }
        )

    def finish_public_convention_read(self) -> None:
        self._convention_cache.finish_public_read()

    def content_revision(self, session_id: str) -> str:
        candidate, hwp = self._validate(session_id)
        guard = self._guard(candidate, hwp)
        forget_cached_content_signatures(candidate.window_handle)
        revision = read_native_content_revision(candidate.window_handle)
        guard()
        if revision is None:
            raise HwpLiveError("한컴 네이티브 content revision을 읽을 수 없습니다")
        return revision

    def structure(self, session_id: str, page: int = 0) -> DocumentStructure:
        candidate, hwp = self._validate(session_id)
        self._structure_snapshot = inspect_candidate_structure(
            hwp,
            candidate,
            page,
            self._guard(candidate, hwp),
        )
        return self._structure_snapshot

    def inspect_page_fast(
        self,
        session_id: str,
        page: int = 0,
        *,
        include_cells: bool = False,
    ) -> FastPageInspection:
        candidate, hwp = self._validate(session_id)
        guard = self._guard(candidate, hwp)
        target_page = page
        if page == 0:
            snapshot = read_native_snapshot(candidate.window_handle)
            guard()

            if snapshot is None:
                raise HwpLiveError("한컴 네이티브 현재 쪽을 읽을 수 없습니다")
            expected_name = ntpath.normcase(ntpath.normpath(candidate.full_name))
            actual_name = ntpath.normcase(ntpath.normpath(snapshot.full_name))
            if (
                snapshot.document_id != candidate.document_id
                or actual_name != expected_name
            ):
                raise HwpLiveError(
                    "한컴 네이티브 현재 쪽 문서가 현재 연결 문서와 다릅니다"
                )
            target_page = snapshot.current_page
        guard()
        inspected = inspect_native_page(
            candidate.window_handle,
            target_page,
            include_cells=include_cells,
        )
        if inspected is None:
            raise HwpLiveError(
                "한컴 네이티브 인프로세스 구조 조회를 사용할 수 없습니다"
            )
        expected_name = ntpath.normcase(ntpath.normpath(candidate.full_name))
        actual_name = ntpath.normcase(ntpath.normpath(inspected.full_name))
        if (
            inspected.document_id != candidate.document_id
            or actual_name != expected_name
        ):
            raise HwpLiveError(
                "한컴 네이티브 구조 조회 문서가 현재 연결 문서와 다릅니다"
            )
        return FastPageInspection(
            document_id=inspected.document_id,
            full_name=inspected.full_name,
            page=inspected.page,
            page_count=inspected.page_count,
            text=inspected.text,
            controls=tuple(
                FastPageControl(
                    control_type=control.control_type,
                    instance_id=control.instance_id,
                    anchor=StructurePosition(
                        list_id=control.anchor.list_id,
                        paragraph=control.anchor.paragraph,
                        character=control.anchor.character,
                    ),
                    rows=control.rows,
                    columns=control.columns,
                    width_hwpunit=control.width_hwpunit,
                    height_hwpunit=control.height_hwpunit,
                    width_mm=_hwpunit_to_mm(control.width_hwpunit),
                    height_mm=_hwpunit_to_mm(control.height_hwpunit),
                    anchor_style_id=control.anchor_style_id,
                    anchor_paragraph_format=None
                    if control.anchor_paragraph_format is None
                    else FastParagraphFormat(
                        alignment=control.anchor_paragraph_format.alignment,
                        line_spacing=control.anchor_paragraph_format.line_spacing,
                        left_margin_hwpunit=control.anchor_paragraph_format.left_margin_hwpunit,
                        right_margin_hwpunit=control.anchor_paragraph_format.right_margin_hwpunit,
                        indentation_hwpunit=control.anchor_paragraph_format.indentation_hwpunit,
                        previous_spacing_hwpunit=control.anchor_paragraph_format.previous_spacing_hwpunit,
                        next_spacing_hwpunit=control.anchor_paragraph_format.next_spacing_hwpunit,
                    ),
                )
                for control in inspected.controls
            ),
            cells=tuple(
                FastPageCell(
                    table_instance_id=cell.table_instance_id,
                    address=cell.address,
                    list_id=cell.list_id,
                    row_span=cell.row_span,
                    column_span=cell.column_span,
                    text=cell.text,
                    width_hwpunit=cell.width_hwpunit,
                    height_hwpunit=cell.height_hwpunit,
                    width_mm=_hwpunit_to_mm(cell.width_hwpunit),
                    height_mm=_hwpunit_to_mm(cell.height_hwpunit),
                )
                for cell in inspected.cells
            ),
            inspection_errors=tuple(
                FastControlInspectionError(
                    control_instance_id=error.control_instance_id,
                    code=error.code,
                    message=error.message,
                )
                for error in inspected.inspection_errors
            ),
            cell_formats=_fast_cell_formats(inspected),
        )

    def inspect_state(
        self,
        session_id: str,
        page: int = 0,
    ) -> tuple[LiveContext, DocumentStructure, int]:
        candidate, hwp = self._validate(session_id)
        return inspect_candidate_state(
            hwp,
            candidate,
            page,
            self._guard(candidate, hwp),
        )

    def render_page(
        self,
        session_id: str,
        page: int,
        dpi: int,
    ) -> PreviewResult:
        candidate, hwp = self._validate(session_id)
        preview_session = self._sessions[session_id].preview_session
        result = render_page(
            candidate,
            cast(PreviewApplication, cast(object, hwp)),
            page,
            dpi,
            session_id,
            self._guard(candidate, hwp),
            directory=preview_session.directory,
        )
        preview_session.commit(result.path)
        return result

    def _grounding_page_setup(
        self,
        session_id: str,
        page: int,
        current_page: int,
    ) -> GroundedPageSetup:
        candidate, hwp = self._validate(session_id)
        guard = self._guard(candidate, hwp)
        if page == current_page:
            raw = hwp.get_pagedef_as_dict("eng")
            guard()
            return observed_page_setup(raw)

        cursor = hwp.get_pos()
        guard()
        selection = hwp.get_selected_pos()
        guard()
        selection_mode = int(hwp.SelectionMode)
        guard()
        if selection[0] and selection_mode != 1:
            raise HwpLiveError(
                "G01은 텍스트 선택 이외의 현재 선택 영역을 보존할 수 없습니다"
            )
        try:
            if selection[0]:
                if not hwp.Cancel():
                    raise HwpLiveError("G01 PageSetup 전에 선택 영역을 접지 못했습니다")
                guard()
            moved = hwp.goto_page(page)
            guard()
            landed_page = int(moved[1])
            guard()
            if landed_page != page:
                raise HwpLiveError("G01 PageSetup 대상 쪽으로 이동하지 못했습니다")
            raw = hwp.get_pagedef_as_dict("eng")
            guard()
            return observed_page_setup(raw)
        finally:
            if selection[0]:
                if not hwp.select_text(selection):
                    raise HwpLiveError(
                        "G01 PageSetup 후 선택 영역을 복원하지 못했습니다"
                    )
                guard()
            elif not hwp.set_pos(*cursor):
                raise HwpLiveError("G01 PageSetup 후 커서를 복원하지 못했습니다")
            guard()

    def _grounding_bridge_revision(self, session_id: str) -> int:
        state_token = self.style_state_token(session_id)
        if not state_token or ":" not in state_token:
            raise HwpLiveError("G01 bridge revision을 확인할 수 없습니다")
        try:
            return int(state_token.rsplit(":", 1)[1], 16)
        except ValueError as error:
            raise HwpLiveError(
                "G01 bridge revision token이 유효하지 않습니다"
            ) from error

    def ground_document(
        self,
        session_id: str,
        request: HwpGroundingRequest,
    ) -> HwpGroundingReport:
        candidate, _ = self._validate(session_id)
        before_native = read_native_snapshot(candidate.window_handle)
        if before_native is None:
            raise HwpLiveError("G01 현재 문서 상태를 읽을 수 없습니다")
        require_snapshot_document(candidate, before_native, "G01 시작")
        content_signature = read_native_content_signature(candidate.window_handle)
        content_hash = content_signature_sha256(content_signature)
        if content_signature is None or content_hash is None:
            raise HwpLiveError("한컴 네이티브 문서 본문 지문 조회에 실패했습니다")
        if before_native.page_count < max(request.pages):
            raise HwpLiveError("G01 요청 쪽이 현재 문서의 확정 쪽 수를 벗어났습니다")

        structures = tuple(self.structure(session_id, page) for page in request.pages)
        setups = tuple(
            self._grounding_page_setup(
                session_id,
                page,
                before_native.current_page,
            )
            for page in request.pages
        )
        geometry = tuple(
            build_page_geometry(setup, page)
            for page, setup in zip(request.pages, setups, strict=True)
        )
        styles = self.styles(session_id, allow_historical_convention=False)
        self.finish_public_convention_read()
        conventions = build_convention_evidence(styles, structures)
        bridge_revision = self._grounding_bridge_revision(session_id)
        page_states = tuple(
            GroundingPageState(page=structure.page, state_token=structure.state_token)
            for structure in structures
        )
        structure_token = aggregate_structure_token(page_states)
        setup_hash = canonical_sha256(
            tuple(
                {"page": item.page, "page_setup_hash": item.page_setup_hash}
                for item in geometry
            )
        )
        observation_hash = canonical_sha256(
            {
                "selector": candidate.selector,
                "document_id": candidate.document_id,
                "normalized_full_name": ntpath.normcase(
                    ntpath.normpath(candidate.full_name)
                ),
                "window_handle": candidate.window_handle,
                "bridge_revision": bridge_revision,
                "structure_token": structure_token,
                "page_state_tokens": [
                    item.model_dump(mode="json") for item in page_states
                ],
                "content_signature_sha256": content_hash,
                "page_setup_hash": setup_hash,
                "convention_profile_hash": conventions.convention_profile_hash,
            }
        )
        before_state = GroundingReadSnapshot(
            selector=candidate.selector,
            document_id=candidate.document_id,
            normalized_full_name=ntpath.normcase(ntpath.normpath(candidate.full_name)),
            window_handle=candidate.window_handle,
            content_signature=content_signature,
            page_state_tokens=tuple(
                (item.page, item.state_token) for item in page_states
            ),
            page_setup_hash=setup_hash,
            modified=before_native.modified,
            current_page=before_native.current_page,
        )
        clean_renders: list[GroundingCleanRender] = []
        overlays: list[GroundingOverlay] = []
        created_paths: list[Path] = []
        try:
            for item in geometry:
                rendered = self.render_page(session_id, item.page, request.dpi)
                clean_path = rendered.path
                created_paths.append(clean_path)
                width, height = png_dimensions(clean_path)
                state = next(state for state in page_states if state.page == item.page)
                clean_hash = sha256_file(clean_path)
                clean_renders.append(
                    GroundingCleanRender(
                        kind="clean_page_png",
                        page=item.page,
                        path=str(clean_path),
                        sha256=clean_hash,
                        width_px=width,
                        height_px=height,
                        dpi=request.dpi,
                        source_page_state_token=state.state_token,
                        source_content_signature_sha256=content_hash,
                        source_observation_hash=observation_hash,
                    )
                )
                if request.include_overlay:
                    overlay_path = clean_path.with_name(
                        clean_path.name.replace("page-", "page-overlay-", 1)
                    )
                    create_grounding_overlay(clean_path, overlay_path, item)
                    self._sessions[session_id].preview_session.commit(overlay_path)
                    created_paths.append(overlay_path)
                    overlay_width, overlay_height = png_dimensions(overlay_path)
                    overlays.append(
                        GroundingOverlay(
                            kind="analysis_overlay_png",
                            page=item.page,
                            path=str(overlay_path),
                            sha256=sha256_file(overlay_path),
                            width_px=overlay_width,
                            height_px=overlay_height,
                            dpi=request.dpi,
                            base_clean_sha256=clean_hash,
                            source_page_state_token=state.state_token,
                            source_observation_hash=observation_hash,
                        )
                    )
            after_native = read_native_snapshot(candidate.window_handle)
            if after_native is None:
                raise HwpLiveError("G01 현재 문서 상태를 사후 확인할 수 없습니다")
            require_snapshot_document(candidate, after_native, "G01 사후")
            after_content_signature = read_native_content_signature(
                candidate.window_handle
            )
            after_content_hash = content_signature_sha256(after_content_signature)
            if after_content_signature is None or after_content_hash is None:
                raise HwpLiveError("한컴 네이티브 문서 본문 지문 조회에 실패했습니다")
            after_structures = tuple(
                self.structure(session_id, page) for page in request.pages
            )
            after_setups = tuple(
                self._grounding_page_setup(
                    session_id,
                    page,
                    after_native.current_page,
                )
                for page in request.pages
            )
            after_setup_hash = canonical_sha256(
                tuple(
                    {
                        "page": page,
                        "page_setup_hash": canonical_sha256(setup),
                    }
                    for page, setup in zip(request.pages, after_setups, strict=True)
                )
            )
            after_page_states = tuple(
                GroundingPageState(page=item.page, state_token=item.state_token)
                for item in after_structures
            )
            after_structure_token = aggregate_structure_token(after_page_states)
            after_styles = self.styles(session_id, allow_historical_convention=False)
            self.finish_public_convention_read()
            after_conventions = build_convention_evidence(
                after_styles,
                after_structures,
            )
            after_bridge_revision = self._grounding_bridge_revision(session_id)
            after_state = GroundingReadSnapshot(
                selector=candidate.selector,
                document_id=candidate.document_id,
                normalized_full_name=ntpath.normcase(
                    ntpath.normpath(candidate.full_name)
                ),
                window_handle=candidate.window_handle,
                content_signature=after_content_signature,
                page_state_tokens=tuple(
                    (item.page, item.state_token) for item in after_page_states
                ),
                page_setup_hash=after_setup_hash,
                modified=after_native.modified,
                current_page=after_native.current_page,
            )
            assert_grounding_stable(before_state, after_state)
            if after_content_hash != content_hash:
                raise HwpLiveError("G01 content signature hash가 사후에 바뀌었습니다")
            if after_bridge_revision != bridge_revision:
                raise HwpLiveError("G01 bridge revision이 읽기 전후에 바뀌었습니다")
            if after_structure_token != structure_token:
                raise HwpLiveError("G01 structure token이 읽기 전후에 바뀌었습니다")
            if (
                after_conventions.convention_profile_hash
                != conventions.convention_profile_hash
            ):
                raise HwpLiveError("G01 convention profile이 읽기 전후에 바뀌었습니다")
            return HwpGroundingReport(
                schema_version="g01.v1",
                provenance=GroundingProvenance(
                    selector=candidate.selector,
                    document_id=candidate.document_id,
                    normalized_full_name=ntpath.normcase(
                        ntpath.normpath(candidate.full_name)
                    ),
                    window_handle=candidate.window_handle,
                    bridge_revision=bridge_revision,
                    bridge_revision_after=after_bridge_revision,
                    page_state_tokens=page_states,
                    structure_token=structure_token,
                    content_signature=content_signature,
                    content_signature_sha256=content_hash,
                    page_setup_hash=setup_hash,
                    convention_profile_hash=conventions.convention_profile_hash,
                    observation_hash=observation_hash,
                ),
                geometry=geometry,
                conventions=conventions,
                clean_renders=tuple(clean_renders),
                overlays=tuple(overlays),
            )
        except BaseException:
            for path in created_paths:
                path.unlink(missing_ok=True)
            raise
