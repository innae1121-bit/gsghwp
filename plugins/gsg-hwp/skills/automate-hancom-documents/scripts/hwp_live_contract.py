from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import Field

from hwp_document_convention_contract import DocumentConventionProfile
from hwp_live_layout_contract import (
    ImageBlock as ImageBlock,
    LayoutBlock as LayoutBlock,
    LayoutPlan as LayoutPlan,
    PageBreakBlock as PageBreakBlock,
    ParagraphBlock as ParagraphBlock,
)
from hwp_live_table_contract import (
    TableBlock as TableBlock,
    TableCell as TableCell,
)
from hwp_live_structure_contract import CreatedControl as CreatedControl
from hwp_live_values import Alignment as Alignment
from hwp_live_values import ContractModel as ContractModel
from hwp_live_values import Rgb as Rgb
from hwp_reference_layout_patch import (
    ReferenceLayoutPatchBlock as ReferenceLayoutPatchBlock,
)


class OpenDocument(ContractModel):
    selector: str
    title: str
    full_name: str
    document_id: int
    format: str
    edit_mode: int
    modified: bool
    # 확정된 본문 쪽 수. 0 쪽인 문서는 없으므로 0 은 절대 싣지 않는다. 문서를 막
    # 열어 한/글이 아직 쪽 나누기를 끝내지 않았거나, 비활성 탭이라 읽을 방법이
    # 없으면 null 이다. null 은 "쪽 수가 아직 확정되지 않았다"는 뜻이지 오류가
    # 아니다.
    page_count: int | None = Field(default=None, ge=1)
    active: bool
    window_handle: int = Field(ge=1)


class ResolvedOpenDocument(OpenDocument):
    reference_document: OpenDocument


class OpenDocumentList(ContractModel):
    documents: tuple[OpenDocument, ...]


class ConnectedDocument(ContractModel):
    session_id: str
    document: OpenDocument


class CursorPosition(ContractModel):
    list_id: int
    paragraph: int
    character: int


class SelectionPosition(ContractModel):
    selected: bool
    start_list: int
    start_paragraph: int
    start_character: int
    end_list: int
    end_paragraph: int
    end_character: int


type ActiveTargetKind = Literal[
    "caret",
    "table_cell",
    "selected_text",
    "column_selection",
    "selected_cells",
    "selected_table",
    "selected_control",
    "unknown",
]
type SelectionModeName = Literal[
    "none",
    "text",
    "column",
    "cells",
    "control",
    "unknown",
]


class ActiveHwpTarget(ContractModel):
    basis: Literal["current_or_last_hwp_position"] = "current_or_last_hwp_position"
    kind: ActiveTargetKind
    selection_mode_raw: int = Field(ge=0)
    selection_mode: SelectionModeName
    strict_selection: bool
    multiple_cells: bool
    control_type: str | None = None
    control_instance_id: str | None = None
    cell_address: str | None = Field(default=None, pattern=r"^[A-Z]+[1-9][0-9]*$")
    cell_addresses: tuple[str, ...] = ()
    cell_address_error: str | None = None


class CharacterStyle(ContractModel):
    face_name: str
    height_hwpunit: int
    bold: bool
    text_color: int
    letter_spacing_percent: int | None = Field(default=None, ge=-50, le=50)


class ParagraphStyle(ContractModel):
    align_type: int
    line_spacing: int
    left_margin_hwpunit: int
    right_margin_hwpunit: int
    indentation_hwpunit: int
    previous_spacing_hwpunit: int
    next_spacing_hwpunit: int
    heading_type: int | None = Field(default=None, ge=0, le=3)
    heading_level: int | None = Field(default=None, ge=0, le=6)
    marker_is_automatic: bool | None = None
    manual_marker_value: str | None = Field(default=None, max_length=64)


class PageSetup(ContractModel):
    paper_width_mm: float
    paper_height_mm: float
    landscape: int
    top_margin_mm: float
    bottom_margin_mm: float
    left_margin_mm: float
    right_margin_mm: float
    header_mm: float = 0
    footer_mm: float = 0
    gutter_mm: float = 0
    gutter_type: int = Field(default=0, ge=0, le=2)


class LiveContext(ContractModel):
    document: OpenDocument
    current_page: int
    cursor: CursorPosition
    selection: SelectionPosition
    active_target: ActiveHwpTarget
    selected_text: str
    page_text: str
    character_style: CharacterStyle
    paragraph_style: ParagraphStyle
    page_setup: PageSetup


class DocumentStyle(ContractModel):
    style_id: int = Field(ge=0, le=4095)
    name: str = Field(min_length=1, max_length=100)
    english_name: str | None = Field(default=None, max_length=100)


class ObservedDocumentStyle(ContractModel):
    """One style counted in the open document's own body text.

    Every field is an observation, never a classification: the entries are in
    the order the document first uses each style, ``lead_samples`` are its own
    leading characters verbatim, and ``shared_lead_prefix`` is the longest
    prefix shared by the largest group of that style's paragraphs -- the marker
    for a numbering or bullet style, empty for prose. It is a majority, not a
    unanimity: folding by "what every paragraph agrees on" let one stray
    paragraph (including one we appended ourselves) erase the convention, so
    ``_dominant_lead_prefix`` groups by first character and folds the largest
    group. What the levels *mean* is for the reader to decide; this only says
    what is there.
    """

    style_id: int = Field(ge=0, le=4095)
    name: str | None = Field(
        default=None,
        max_length=100,
        description="이 문서에 정의된 스타일 이름. 목록에서 찾지 못하면 null.",
    )
    paragraphs: int = Field(
        ge=1,
        description="관측 범위에서 이 스타일이 적용된, 비어 있지 않은 문단 수.",
    )
    first_paragraph: int = Field(
        ge=0,
        description="이 스타일이 처음 나온 문단 번호. 항목 순서가 곧 문서 순서다.",
    )
    shared_lead_prefix: str = Field(
        default="",
        max_length=64,
        description=(
            "이 스타일 문단 중 최다 무리가 공유하는 앞머리(전원 합의가 아니라 "
            "다수결이다). 없으면 빈 문자열."
        ),
    )
    lead_samples: tuple[str, ...] = Field(
        default=(),
        max_length=3,
        description="실제로 관측한 문단 앞머리 원문 표본.",
    )
    lead_marker: str = Field(
        default="",
        max_length=64,
        description=(
            "이 스타일 문단 다수가 실제로 쓰는 글머리 원문. 넣는 문단도 이 "
            "글자를 쓴다. 빈 문자열이면 글머리 없이 쓰는 스타일이다."
        ),
    )
    font_name: str | None = Field(
        default=None,
        max_length=100,
        description="대표 문단이 실제로 쓰는 한글 글꼴 이름.",
    )
    font_size_pt: float | None = Field(
        default=None,
        description="대표 문단의 실제 글자 크기(pt). 스타일 기본값이 아니라 실측이다.",
    )
    bold: bool | None = None
    alignment: int | None = Field(
        default=None,
        description=(
            "대표 문단의 ParaShape/AlignType 원값 그대로. 이 숫자가 어느 정렬을 "
            "뜻하는지는 확인되지 않았다: 한컴 공식 카탈로그(104쪽)는 0=양쪽 "
            "1=왼쪽 2=오른쪽 3=가운데 4=배분 5=나눔이라고 적고, 이 저장소의 "
            "검증 표는 0=왼쪽 1=가운데 2=오른쪽 3=양쪽이라 서로 반대다. 실기 "
            "왕복 1회로 확정되기 전까지 이 값을 정렬 이름으로 번역하지 마라 "
            "(그래서 문단 삽입 시 정렬은 복제하지 않고 스타일 값을 그대로 "
            "물려받는다)."
        ),
    )
    line_spacing: int | None = None
    left_margin_mm: float | None = None
    right_margin_mm: float | None = None
    indentation_mm: float | None = Field(
        default=None,
        description="음수면 내어쓰기. 목록 스타일의 걸어 내리기가 여기 있다.",
    )
    space_before_mm: float | None = None
    space_after_mm: float | None = None
    heading_type: int | None = Field(
        default=None,
        description=(
            "문단 머리 모양 (ParaShape/HeadingType): 0 없음, 1 개요, 2 번호, "
            "3 불릿. 0이 아니면 한/글이 마커를 스스로 그리므로 본문에 마커를 "
            "쓰면 두 번 나온다. null이면 읽지 못한 것이다."
        ),
    )
    heading_level: int | None = Field(
        default=None,
        description=(
            "문단 번호·개요 단계 (ParaShape/Level, 0~6). 문서가 선언한 목차 "
            "단계이며, 들여쓰기나 등장 순서로 추정한 값이 아니다."
        ),
    )
    marker_is_automatic: bool | None = Field(
        default=None,
        description=(
            "true면 마커는 한/글이 그리므로 글자로 쓰지 않는다. false면 "
            "lead_marker를 그대로 찍는다. null이면 판단 근거가 없다."
        ),
    )
    representative_paragraph: int | None = Field(
        default=None,
        ge=0,
        description=(
            "이 묶음을 대표하는 실제 문단 번호. 위 서식은 이 문단에서 읽은 "
            "값이며, 같은 문단이 모양 복사(ShapeCopyPaste)의 원본이 된다."
        ),
    )
    list_id: int | None = Field(
        default=None,
        ge=0,
        description="대표 문단이 속한 리스트 ID. 본문은 0이다.",
    )


class DocumentStyleObservation(ContractModel):
    """이 문서가 실제로 쓰는 체계. 규칙이 아니라 센 결과다.

    Read from the open document in one native paragraph scan. ``complete``
    false means the scan stopped at its limit and the counts describe only the
    first ``scanned_paragraphs`` paragraphs of the body.

    The scan is taken once per open document and reused for the rest of the
    session, so paragraphs this session appends afterwards are not in the
    counts. What is being reported is the document's established convention,
    which is exactly the thing that does not move while it is being followed.
    """

    scanned_paragraphs: int = Field(
        ge=0,
        description="네이티브 문단 순회가 실제로 방문한 문단 수.",
    )
    counted_paragraphs: int = Field(
        ge=0,
        description="그 중 비어 있지 않아 집계에 들어간 문단 수.",
    )
    complete: bool = Field(
        description="false면 상한에서 끊겼고 집계는 앞부분만 설명한다.",
    )
    styles: tuple[ObservedDocumentStyle, ...] = Field(
        default=(),
        max_length=64,
        description="문서가 처음 쓴 순서대로 나열한, 실제로 쓰인 스타일.",
    )
    convention_profile: DocumentConventionProfile | None = Field(
        default=None,
        description=(
            "관례 축마다 관측·충돌 관측·미관측 상태와 표본 분포를 그대로 "
            "제공한다. 불확실성은 편집 거부가 아니라 이 데이터로 표현한다."
        ),
    )


class DocumentStyleList(ContractModel):
    styles: tuple[DocumentStyle, ...]
    # null 은 "관측하지 못했다"는 뜻이지 "쓰는 체계가 없다"가 아니다. 관측에
    # 실패해도 스타일 목록은 그대로 돌려주고 이 절만 비운다.
    observed_usage: DocumentStyleObservation | None = Field(
        default=None,
        description=(
            "문서를 한 번 순회해 센 결과. null이면 관측하지 못한 것이며 "
            "스타일 목록 자체는 유효하다."
        ),
    )


@dataclass(frozen=True, slots=True)
class StyleReadOutcome:
    """A style list plus whether the bridge could vouch that it is current.

    Deliberately *not* a ``ContractModel``: it never reaches a published tool
    schema. ``hwp_list_styles`` keeps returning ``DocumentStyleList`` and puts
    ``unverified_reason`` into the human-readable summary instead, so the
    caller is told the read was not verified without the compatibility
    manifest's pinned schema hash moving.
    """

    styles: DocumentStyleList
    unverified_reason: str | None = None


class MutationResult(ContractModel):
    action: str
    current_page: int
    modified: bool


class PreviewResult(ContractModel):
    page: int
    path: Path


class LayoutResult(ContractModel):
    blocks_applied: int
    current_page: int
    modified: bool
    created_controls: tuple[CreatedControl, ...] = ()
    created_control_ids: tuple[str, ...] = ()
    execution_mode: Literal["native_in_process"] = "native_in_process"
    native_elapsed_microseconds: int = Field(ge=0)
