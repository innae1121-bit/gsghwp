from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
from math import isfinite
from pathlib import Path
from typing import Final, Literal

from PIL import Image, ImageDraw
from pydantic import Field, field_validator, model_validator

from hwp_errors import HwpLiveError
from hwp_live_contract import DocumentStyleList, ObservedDocumentStyle
from hwp_live_structure_contract import DocumentStructure
from hwp_live_values import ContractModel
from hwp_reference_layout_geometry import (
    HWPUNITS_PER_INCH,
    MILLIMETERS_PER_INCH,
    SectionPageGeometry,
)


_PAGE_SETUP_KEYS = (
    "PaperWidth",
    "PaperHeight",
    "Landscape",
    "TopMargin",
    "BottomMargin",
    "LeftMargin",
    "RightMargin",
    "HeaderLen",
    "FooterLen",
    "GutterLen",
    "GutterType",
)
_PAGE_SETUP_FIELD_NAMES = {
    "PaperWidth": "paper_width_mm",
    "PaperHeight": "paper_height_mm",
    "Landscape": "landscape",
    "TopMargin": "top_margin_mm",
    "BottomMargin": "bottom_margin_mm",
    "LeftMargin": "left_margin_mm",
    "RightMargin": "right_margin_mm",
    "HeaderLen": "header_mm",
    "FooterLen": "footer_mm",
    "GutterLen": "gutter_mm",
    "GutterType": "gutter_type",
}


type GroundingRoleName = Literal[
    "heading",
    "body",
    "list",
    "caption",
    "table",
    "marker",
]
type GroundingRoleState = Literal["observed", "observed-but-conflicting", "unobserved"]


class HwpGroundingRequest(ContractModel):
    """Strict, selector-first input for the one-call G01 public surface."""

    document_path: str | None = Field(default=None, max_length=32_767)
    pages: tuple[int, ...] = Field(min_length=1, max_length=2)
    dpi: int = Field(ge=72, le=600)
    include_overlay: bool = False

    @field_validator("pages")
    @classmethod
    def validate_pages(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if any(page < 1 for page in value):
            raise ValueError("pages must contain positive page numbers")
        if len(set(value)) != len(value):
            raise ValueError("pages must not contain duplicates")
        return value


class GroundedPageSetup(ContractModel):
    """All PageSetup values required by G01, with no synthetic defaults."""

    paper_width_mm: float = Field(gt=0)
    paper_height_mm: float = Field(gt=0)
    landscape: int = Field(ge=0, le=1)
    top_margin_mm: float = Field(ge=0)
    bottom_margin_mm: float = Field(ge=0)
    left_margin_mm: float = Field(ge=0)
    right_margin_mm: float = Field(ge=0)
    header_mm: float = Field(ge=0)
    footer_mm: float = Field(ge=0)
    gutter_mm: float = Field(ge=0)
    gutter_type: int = Field(ge=0, le=2)


class GroundingBoxMm(ContractModel):
    left_mm: float = Field(ge=0)
    top_mm: float = Field(ge=0)
    width_mm: float = Field(gt=0)
    height_mm: float = Field(gt=0)


class GroundingPageGeometry(ContractModel):
    page: int = Field(ge=1)
    page_setup: GroundedPageSetup
    oriented_paper_width_mm: float = Field(gt=0)
    oriented_paper_height_mm: float = Field(gt=0)
    body_box_mm: GroundingBoxMm
    page_setup_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @property
    def body_box(self) -> GroundingBoxMm:
        return self.body_box_mm


class GroundingStyleRole(ContractModel):
    role: GroundingRoleName
    state: GroundingRoleState
    style_id: int | None = Field(default=None, ge=0, le=4095)
    style_name: str | None = Field(default=None, max_length=100)
    representative_paragraph: int | None = Field(default=None, ge=0)
    evidence: tuple[str, ...] = Field(default=(), max_length=8)


class GroundingConventionEvidence(ContractModel):
    currentness: Literal["live_observation"]
    styles: DocumentStyleList
    roles: tuple[GroundingStyleRole, ...] = Field(min_length=6, max_length=6)
    convention_profile_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class GroundingPageState(ContractModel):
    page: int = Field(ge=1)
    state_token: str = Field(min_length=16, max_length=80)


class GroundingProvenance(ContractModel):
    selector: str = Field(min_length=1, max_length=500)
    document_id: int
    normalized_full_name: str = Field(min_length=1, max_length=1_000)
    window_handle: int = Field(ge=1)
    bridge_revision: int = Field(ge=0)
    bridge_revision_after: int = Field(ge=0)
    page_state_tokens: tuple[GroundingPageState, ...] = Field(
        min_length=1,
        max_length=2,
    )
    structure_token: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_signature: str = Field(min_length=4, max_length=200)
    content_signature_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    page_setup_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    convention_profile_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    observation_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class GroundingCleanRender(ContractModel):
    kind: Literal["clean_page_png"]
    page: int = Field(ge=1)
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    width_px: int = Field(gt=0)
    height_px: int = Field(gt=0)
    dpi: int = Field(ge=72, le=600)
    source_page_state_token: str = Field(min_length=16, max_length=80)
    source_content_signature_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_observation_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class GroundingOverlay(ContractModel):
    kind: Literal["analysis_overlay_png"]
    page: int = Field(ge=1)
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    width_px: int = Field(gt=0)
    height_px: int = Field(gt=0)
    dpi: int = Field(ge=72, le=600)
    base_clean_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_page_state_token: str = Field(min_length=16, max_length=80)
    source_observation_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class HwpGroundingReport(ContractModel):
    schema_version: Literal["g01.v1"]
    provenance: GroundingProvenance
    geometry: tuple[GroundingPageGeometry, ...] = Field(min_length=1, max_length=2)
    conventions: GroundingConventionEvidence
    clean_renders: tuple[GroundingCleanRender, ...] = Field(
        min_length=1,
        max_length=2,
    )
    overlays: tuple[GroundingOverlay, ...] = Field(default=(), max_length=2)

    @model_validator(mode="after")
    def validate_artifact_pages(self) -> HwpGroundingReport:
        clean_pages = tuple(item.page for item in self.clean_renders)
        geometry_pages = tuple(item.page for item in self.geometry)
        overlay_pages = tuple(item.page for item in self.overlays)
        if clean_pages != geometry_pages:
            raise ValueError("clean render pages must match geometry pages")
        if overlay_pages not in ((), clean_pages):
            raise ValueError("overlay pages must be empty or match clean pages")
        return self


# 호출자 하네스는 도구 응답의 **꼬리**를 조용히 자른다. 이 보고서에서 부피를
# 가진 절은 conventions 하나뿐이다 — 문서 관례 프로필을 통째로 실어 실측
# 48,087바이트까지 커진다. 나머지 다섯 절(clean_renders·overlays·geometry·
# provenance·schema_version)은 다 합쳐 몇 KB뿐이고, 그 가운데 clean_renders 는
# 이 호출을 하는 이유 그 자체다. 선언 순서대로 내보내면 목적물이 48KB 뒤에
# 놓여 가장 먼저 사라진다.
#
# 그래서 **직렬화 순서만** 바꾼다. 모델의 필드 선언 순서는 그대로 두어야 한다:
# 선언 순서가 곧 outputSchema 의 `required` 배열 순서여서(실측:
# ["schema_version","provenance","geometry","conventions","clean_renders"]),
# 선언을 옮기면 봉인된 legacy_tool_schema_hashes["hwp_ground_document"] 가
# 움직이고 카탈로그·오라클 재봉인 연쇄가 따라온다. JSON 객체의 키 순서는
# 스키마가 규정하는 값이 아니므로 이 재배치는 계약을 건드리지 않는다.
GROUNDING_RESPONSE_FIELD_ORDER: Final[tuple[str, ...]] = (
    "schema_version",
    "clean_renders",
    "overlays",
    "geometry",
    "provenance",
    "conventions",
)


def reorder_grounding_payload[JsonNode](
    payload: dict[str, JsonNode],
) -> dict[str, JsonNode]:
    """작은 목적 필드를 conventions 앞에 놓은 직렬화 순서를 만든다.

    목록에 없는 키는 순서를 지킨 채 뒤에 그대로 붙는다 — 필드가 늘어도 이
    함수가 값을 잃지 않게 하려는 것이다.
    """
    ordered = {
        key: payload[key] for key in GROUNDING_RESPONSE_FIELD_ORDER if key in payload
    }
    for key, value in payload.items():
        if key not in ordered:
            ordered[key] = value
    return ordered


@dataclass(frozen=True, slots=True)
class GroundingReadSnapshot:
    selector: str
    document_id: int
    normalized_full_name: str
    window_handle: int
    content_signature: str
    page_state_tokens: tuple[tuple[int, str], ...]
    page_setup_hash: str
    modified: bool = False
    current_page: int = 1


def canonical_sha256(value: object) -> str:
    if isinstance(value, ContractModel):
        value = value.model_dump(mode="json", exclude_none=False)
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def aggregate_structure_token(page_states: tuple[GroundingPageState, ...]) -> str:
    return canonical_sha256(
        tuple(
            {"page": item.page, "state_token": item.state_token} for item in page_states
        )
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as error:
        raise HwpLiveError("G01 산출물 SHA-256을 읽지 못했습니다") from error
    return digest.hexdigest()


def content_signature_sha256(signature: str | None) -> str | None:
    if not isinstance(signature, str) or not signature.startswith("SIG "):
        return None
    return hashlib.sha256(signature.encode("utf-8")).hexdigest()


def require_live_currentness(currentness: str | None) -> None:
    if currentness != "live_observation":
        raise HwpLiveError("G01 convention profile의 currentness가 live가 아닙니다")


def _observed_number(raw: Mapping[str, object], key: str) -> float:
    value = raw.get(key)
    if isinstance(value, bool):
        numeric = float(int(value))
    elif isinstance(value, (int, float)):
        numeric = float(value)
    else:
        raise HwpLiveError(f"G01 PageSetup 관측값이 없습니다: {key}")
    if not isfinite(numeric):
        raise HwpLiveError(f"G01 PageSetup 관측값이 유한하지 않습니다: {key}")
    return numeric


def _observed_integral(raw: Mapping[str, object], key: str) -> int:
    value = _observed_number(raw, key)
    integer = int(value)
    if value != integer:
        raise HwpLiveError(f"G01 PageSetup 정수 관측값이 아닙니다: {key}")
    return integer


def observed_page_setup(raw: Mapping[str, object]) -> GroundedPageSetup:
    missing = tuple(key for key in _PAGE_SETUP_KEYS if key not in raw)
    if missing:
        raise HwpLiveError(
            "G01 PageSetup 필수 관측값이 없습니다: " + ", ".join(missing)
        )
    values: dict[str, object] = {}
    for key, field_name in _PAGE_SETUP_FIELD_NAMES.items():
        values[field_name] = (
            _observed_integral(raw, key)
            if key in {"Landscape", "GutterType"}
            else _observed_number(raw, key)
        )
    try:
        return GroundedPageSetup.model_validate(values, strict=True)
    except ValueError as error:
        raise HwpLiveError(
            f"G01 PageSetup 관측값이 유효하지 않습니다: {error}"
        ) from error


def _hwpunit_to_mm(value: int) -> float:
    return round(value * MILLIMETERS_PER_INCH / HWPUNITS_PER_INCH, 3)


def build_page_geometry(
    page_setup: GroundedPageSetup,
    page: int,
) -> GroundingPageGeometry:
    try:
        geometry = SectionPageGeometry.from_mm(
            paper_width_mm=page_setup.paper_width_mm,
            paper_height_mm=page_setup.paper_height_mm,
            landscape=bool(page_setup.landscape),
            left_margin_mm=page_setup.left_margin_mm,
            right_margin_mm=page_setup.right_margin_mm,
            top_margin_mm=page_setup.top_margin_mm,
            bottom_margin_mm=page_setup.bottom_margin_mm,
            header_mm=page_setup.header_mm,
            footer_mm=page_setup.footer_mm,
            gutter_mm=page_setup.gutter_mm,
            gutter_type=page_setup.gutter_type,
        )
        oriented_width, oriented_height = geometry.oriented_paper_size()
        usable = geometry.usable_area(page_number=page)
    except ValueError as error:
        raise HwpLiveError(f"G01 본문 영역을 계산하지 못했습니다: {error}") from error
    return GroundingPageGeometry(
        page=page,
        page_setup=page_setup,
        oriented_paper_width_mm=_hwpunit_to_mm(oriented_width),
        oriented_paper_height_mm=_hwpunit_to_mm(oriented_height),
        body_box_mm=GroundingBoxMm(
            left_mm=_hwpunit_to_mm(usable.left),
            top_mm=_hwpunit_to_mm(usable.top),
            width_mm=_hwpunit_to_mm(usable.width),
            height_mm=_hwpunit_to_mm(usable.height),
        ),
        page_setup_hash=canonical_sha256(page_setup),
    )


def _png_dimensions(path: Path) -> tuple[int, int]:
    try:
        with Image.open(path) as image:
            if image.format != "PNG":
                raise HwpLiveError("G01 렌더 artifact가 PNG가 아닙니다")
            _ = image.load()
            width, height = image.size
    except HwpLiveError:
        raise
    except (OSError, ValueError) as error:
        raise HwpLiveError("G01 렌더 PNG를 읽지 못했습니다") from error
    if width < 1 or height < 1:
        raise HwpLiveError("G01 렌더 PNG 크기가 유효하지 않습니다")
    return width, height


def png_dimensions(path: Path) -> tuple[int, int]:
    return _png_dimensions(path)


def create_grounding_overlay(
    clean_path: Path,
    overlay_path: Path,
    geometry: GroundingPageGeometry,
) -> None:
    if clean_path.resolve() == overlay_path.resolve():
        raise HwpLiveError("G01 overlay는 clean render와 다른 경로여야 합니다")
    width, height = _png_dimensions(clean_path)
    paper_width = geometry.oriented_paper_width_mm
    paper_height = geometry.oriented_paper_height_mm
    box = geometry.body_box
    left = round(box.left_mm / paper_width * width)
    top = round(box.top_mm / paper_height * height)
    right = round((box.left_mm + box.width_mm) / paper_width * width)
    bottom = round((box.top_mm + box.height_mm) / paper_height * height)
    right = max(left + 1, min(width - 1, right))
    bottom = max(top + 1, min(height - 1, bottom))
    overlay_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with Image.open(clean_path) as source:
            overlay = source.convert("RGBA")
        try:
            draw = ImageDraw.Draw(overlay)
            draw.rectangle(
                (left, top, right, bottom),
                outline=(220, 0, 0, 255),
                width=max(2, min(width, height) // 400),
            )
            overlay.convert("RGB").save(overlay_path, format="PNG", optimize=False)
        finally:
            overlay.close()
    except (OSError, ValueError) as error:
        overlay_path.unlink(missing_ok=True)
        raise HwpLiveError("G01 overlay PNG를 만들지 못했습니다") from error
    _ = _png_dimensions(overlay_path)


def assert_grounding_stable(
    before: GroundingReadSnapshot,
    after: GroundingReadSnapshot,
) -> None:
    if (
        before.selector,
        before.document_id,
        before.normalized_full_name,
        before.window_handle,
    ) != (
        after.selector,
        after.document_id,
        after.normalized_full_name,
        after.window_handle,
    ):
        raise HwpLiveError("G01 selector/문서 identity가 읽기 전후에 바뀌었습니다")
    if before.content_signature != after.content_signature:
        raise HwpLiveError("G01 문서 본문 content signature가 읽기 전후에 바뀌었습니다")
    if before.page_state_tokens != after.page_state_tokens:
        raise HwpLiveError("G01 page structure state token이 읽기 전후에 바뀌었습니다")
    if before.page_setup_hash != after.page_setup_hash:
        raise HwpLiveError("G01 PageSetup이 읽기 전후에 바뀌었습니다")
    if before.modified != after.modified or before.current_page != after.current_page:
        raise HwpLiveError("G01 문서 상태가 읽기 전후에 바뀌었습니다")


def _role_from_candidates(
    role: GroundingRoleName,
    candidates: tuple[ObservedDocumentStyle, ...],
    style_names: Mapping[int, str],
    evidence: str,
) -> GroundingStyleRole:
    if not candidates:
        return GroundingStyleRole(
            role=role,
            state="unobserved",
            evidence=(evidence,),
        )
    selected = max(
        candidates,
        key=lambda item: (item.paragraphs, -item.first_paragraph),
    )
    style_id = selected.style_id
    return GroundingStyleRole(
        role=role,
        state="observed" if len(candidates) == 1 else "observed-but-conflicting",
        style_id=style_id,
        style_name=style_names.get(style_id),
        representative_paragraph=selected.representative_paragraph,
        evidence=(evidence,),
    )


def build_convention_evidence(
    styles: DocumentStyleList,
    structures: tuple[DocumentStructure, ...],
) -> GroundingConventionEvidence:
    observed = styles.observed_usage
    if observed is None or observed.convention_profile is None:
        raise HwpLiveError("G01 문서 convention/style 관측 결과가 없습니다")
    cache = observed.convention_profile.cache
    require_live_currentness(None if cache is None else cache.currentness)
    style_names = {style.style_id: style.name for style in styles.styles}
    runs = observed.styles
    headings = tuple(
        run
        for run in runs
        if (run.heading_type is not None and run.heading_type >= 1)
        or run.heading_level is not None
    )
    markers = tuple(
        run for run in runs if run.marker_is_automatic is True or bool(run.lead_marker)
    )
    bodies = tuple(
        run
        for run in runs
        if not run.lead_marker
        and run.marker_is_automatic is not True
        and not (
            (run.heading_type is not None and run.heading_type >= 1)
            or run.heading_level is not None
        )
    )
    captions = tuple(
        run
        for run in runs
        if any(
            table.caption is not None and table.caption.style_id == run.style_id
            for structure in structures
            for table in structure.tables
        )
    )
    table_anchor_style_ids = {
        paragraph.style_id
        for structure in structures
        for table in structure.tables
        for paragraph in structure.paragraphs
        if table.preceding_paragraph is not None
        and paragraph.position == table.preceding_paragraph
        and paragraph.style_id is not None
    }
    table_styles = tuple(run for run in runs if run.style_id in table_anchor_style_ids)
    roles = (
        _role_from_candidates("heading", headings, style_names, "heading_type/level"),
        _role_from_candidates("body", bodies, style_names, "markerless body evidence"),
        _role_from_candidates("list", markers, style_names, "observed marker evidence"),
        _role_from_candidates(
            "caption", captions, style_names, "table caption style_id"
        ),
        _role_from_candidates(
            "table",
            table_styles,
            style_names,
            "table anchor paragraph style_id",
        ),
        _role_from_candidates(
            "marker", markers, style_names, "automatic/literal marker"
        ),
    )
    profile_hash = canonical_sha256(
        observed.convention_profile.model_dump(mode="json", exclude={"cache"})
    )
    return GroundingConventionEvidence(
        currentness="live_observation",
        styles=styles,
        roles=roles,
        convention_profile_hash=profile_hash,
    )
