from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from hwp_errors import HwpLiveError
from hwp_live_edit_history import (
    NO_CHECKPOINT_EVIDENCE,
    DocumentCheckpointEvidence,
)
from hwp_live_native_action_models import (
    LiteralTextRange,
    NativeActionResult,
    NativePosition,
    NativeSnapshot,
)
from hwp_live_native_format_inputs import TextFormatSpec


type TextPatchTargetKind = Literal["current", "range", "find", "table_cell"]
type TextPatchPostSelection = Literal["keep", "collapse_to_start", "collapse_to_end"]
type TextPatchPhase = Literal[
    "validation",
    "planning_preflight",
    "checkpoint_before",
    "mutation",
    "readback",
    "checkpoint_after_history",
    "rollback",
    "total",
]
type TextPatchTimingSource = Literal["python", "native"]


@dataclass(frozen=True, slots=True)
class TextPatchPhaseTiming:
    phase: TextPatchPhase
    source: TextPatchTimingSource
    elapsed_microseconds: int


@dataclass(frozen=True, slots=True)
class TextPatchTarget:
    kind: TextPatchTargetKind
    start: NativePosition | None = None
    end: NativePosition | None = None
    occurrence: int | None = None
    match_case: bool = False
    table_instance_id: str | None = None
    cell_address: str | None = None


@dataclass(frozen=True, slots=True)
class TextPatchPlanGuard:
    document_id: int
    full_name: str
    content_revision: str


@dataclass(frozen=True, slots=True)
class TextPatchRequest:
    target: TextPatchTarget
    expected_text: str | None
    replacement: str
    formatting: TextFormatSpec | None = None
    post_selection: TextPatchPostSelection = "keep"
    literal_range: LiteralTextRange | None = None


def validate_text_patch_request(
    request: TextPatchRequest,
    *,
    allow_exact_omission: bool = False,
    require_current_expected: bool = False,
) -> None:
    """Apply the established text patch request rules."""
    if (
        request.expected_text is not None and len(request.expected_text) > 1_000_000
    ) or len(request.replacement) > 1_000_000:
        raise HwpLiveError("라이브 text.patch 크기 한도를 초과했습니다")
    if request.expected_text is None and (
        (request.target.kind == "current" and require_current_expected)
        or (
            request.target.kind != "current"
            and not (
                allow_exact_omission and request.target.kind in {"range", "table_cell"}
            )
        )
    ):
        raise HwpLiveError(
            "범위·검색·표 셀 text.patch에는 확인할 기존 텍스트가 필요합니다"
        )
    if not request.replacement and request.formatting is not None:
        raise HwpLiveError(
            "삭제 결과에는 적용할 텍스트가 없으므로 글자 서식을 함께 요청할 수 없습니다"
        )


def normalize_paragraph_text(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def matches_replacement_readback(
    readback: str,
    replacement: str,
    target_kind: TextPatchTargetKind,
) -> bool:
    """교체한 자리에서 다시 읽은 글자가 요청한 교체문인가.

    단일 경로와 배치 경로가 같은 계약을 쓰도록 여기에 둔다 — 두 경로가 서로
    다른 관대함을 갖는 순간, 한쪽에서 거절되는 결과가 다른 쪽에서 성공이 된다.

    정확히 같으면 참이다. 그 밖에 참이 되는 경우는 하나뿐이다 — 한/글이 스스로
    그리는 문단 번호가 앞에 딸려 온 경우. 그 번호는 글자 칸을 차지하지 않아서
    어떤 선택 범위로도 뺄 수 없고, ``GetTextFile("saveblock:true")`` 는 문단 안
    어디를 선택했든 번호를 앞에 붙여 돌려준다(TextPatchReadback.h 의 설명).

    그래서 브리지의 ``WithoutAutomaticNumber``(TextPatchReadback.cpp:64)와 같은
    계약을 쓴다: 벗겨 낼 수 있는 접두는 "번호 한 덩이 + 공백 한 칸" 뿐이고,
    벗겨 낸 나머지는 교체문과 정확히 일치해야 한다. 파이썬 층에는
    ``GetHeadingString`` 값이 없어 번호 자체는 알 수 없지만, 번호가 공백 없는
    한 덩이라는 구조는 그대로 요구할 수 있다 — 표시 문자를 열거해서 맞히지
    않는다.

    이 좁힘이 막는 것: 예전의 "교체문이 판독문 안에 한 번만 나오면 통과"는
    판독 ``XBC`` 와 교체문 ``X`` 를 성공으로 읽었다. 원문 일부가 그대로 남은
    결과인데도 통과였다.
    """
    normalized_readback = normalize_paragraph_text(readback)
    normalized_replacement = normalize_paragraph_text(replacement)
    if normalized_readback == normalized_replacement:
        return True
    if (
        target_kind != "find"
        or not normalized_replacement
        or "\n" in normalized_readback
        or "\n" in normalized_replacement
        or not normalized_readback.endswith(normalized_replacement)
    ):
        return False
    prefix = normalized_readback[: -len(normalized_replacement)]
    if not prefix.endswith(" "):
        return False
    number = prefix[:-1]
    return bool(number) and not any(character.isspace() for character in number)


def text_patch_minimum_protocol(request: TextPatchRequest) -> Literal[11, 12]:
    target = request.target
    if (
        target.kind == "find"
        and target.table_instance_id is not None
        and target.cell_address is not None
    ):
        return 12
    return 11


@dataclass(frozen=True, slots=True)
class TextPatchResult:
    native: NativeActionResult
    before: NativeSnapshot
    after: NativeSnapshot
    # What the caller has to be told on top of "the patch was applied", such as
    # this edit having no MCP undo entry because the document could not be
    # checkpointed. Empty means nothing unusual happened. It never replaces the
    # applied-and-verified message: the edit did happen.
    notice: str = ""
    # What the bridge reported while checkpointing this edit, if it did.
    checkpoint_evidence: DocumentCheckpointEvidence = NO_CHECKPOINT_EVIDENCE
    phase_timings: tuple[TextPatchPhaseTiming, ...] = ()
