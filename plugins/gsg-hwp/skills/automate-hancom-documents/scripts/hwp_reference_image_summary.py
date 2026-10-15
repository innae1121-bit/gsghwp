from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Final, Literal, cast, final

from pydantic import Field, JsonValue

from hwp_live_values import ContractModel, Rgb
from hwp_reference_image_analyzer import default_artifact_root
from hwp_reference_image_budget import ReferenceAnalysisStopReason
from hwp_reference_image_contract import (
    NormalizedBox,
    ReferenceBreakpointCandidate,
    ReferenceImageAnalysis,
    ReferenceImageObject,
    ReferenceImageSegment,
    ReferenceImageTile,
    ReferenceProtectedGap,
)
from hwp_reference_image_grouping import (
    FILL_MERGE_DISTANCE,
    ReferenceObjectSample,
    ReferenceSwatchRun,
    ReferenceTextAnchorSample,
    annotate_object,
    color_distance,
    detect_swatch_runs,
    merge_text_fragments,
    merged_text_anchor,
    merged_text_box,
)
from hwp_reference_image_layout_bridge import align_reference_layout_to_analysis
from hwp_reference_layout_contract import (
    ReferenceLayoutBlock,
    ReferenceStyle,
    StyleRegion,
    VisibleEdge,
)


ReferenceExecutionMode = Literal["ordinary_layout", "reference_layout_bulk"]
ReferenceDetailSection = Literal[
    "draft_reference_layout",
    "objects",
    "text_regions",
    "protected_gaps",
    "visible_segments",
    "breakpoint_candidates",
    "tiles",
    "artifacts",
]
ReferenceArtifactKind = Literal[
    "analysis",
    "overlay",
    "contact_sheet",
    "tile",
    "text_crop",
]
# compact 응답이 실어 보내는 관측 축이다. detail section 과 이름이 겹치지만
# 같은 것이 아니다 — `fills` 는 objects 에서 집계한 파생 축이라 자기 section 이
# 없고, `draft_reference_layout`·`artifacts` 는 축이자 section 이다. 그래서
# 누락 보고는 축 이름과 되받을 section 을 따로 적는다.
ReferenceEvidenceAxis = Literal[
    "fills",
    "swatch_runs",
    "objects",
    "text_regions",
    "protected_gaps",
    "breakpoint_candidates",
    "tiles",
    "visible_segments",
    "draft_reference_layout",
    "artifacts",
]


class ReferenceAnalysisCounts(ContractModel):
    objects: int = Field(ge=0)
    text_regions: int = Field(ge=0)
    protected_gaps: int = Field(ge=0)
    visible_segments: int = Field(ge=0)
    breakpoint_candidates: int = Field(ge=0)
    tiles: int = Field(ge=0)
    contact_sheets: int = Field(ge=0)


class ReferenceStructureSummary(ContractModel):
    horizontal_segments: int = Field(ge=0)
    vertical_segments: int = Field(ge=0)
    orthogonal_intersections: int = Field(ge=0)
    dominant_segment_ratio: float = Field(ge=0, le=1)
    dominant_grid_area_ratio: float = Field(ge=0, le=1)
    partial_edge_ratio: float = Field(ge=0, le=1)
    repeated_style_regions: int = Field(ge=0)
    grid_fixed_text_regions: int = Field(ge=0)


class ReferenceArtifact(ContractModel):
    kind: ReferenceArtifactKind
    path: Path
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReferenceFillTally(ContractModel):
    """관측한 채움색 하나와 그 색이 차지한 몫이다.

    objects 를 다 싣지 못해도 이 표는 전량 집계에서 나오므로, 잘린 표본만 보고
    스와치를 세다가 색을 빠뜨리는 일이 없다.

    가까운 계조는 한 줄로 접어서 낸다(FILL_MERGE_DISTANCE). 접지 않으면
    안티에일리어싱이 만든 흰색·회색 계조가 상위권을 다 차지해 정작 디자인이
    쓴 색이 밀려난다 — 실사례에서 149종 가운데 상위 8종이 전부 그런 잡티였고
    먼셀 스와치 6색은 응답에 실리지 못했다.
    """

    fill: Rgb
    object_count: int = Field(ge=1)
    area_ratio: float = Field(ge=0, le=1)
    merged_fill_count: int = Field(default=1, ge=1)
    """이 줄이 흡수한 관측 색의 수(자기 자신 포함).

    1보다 크면 인접 계조를 접은 것이다. `fill` 은 접은 것들의 평균이 아니라
    그중 넓이가 가장 큰 실제 관측색이라, 여기 적힌 색은 언제나 그림에 실제로
    있던 색이다.
    """


class ReferenceEvidenceSample(ContractModel):
    """예산 안에서 실제로 실어 보낸 관측이다.

    비어 있는 축은 "관측이 없다"가 아니라 "이 응답에 싣지 않았다"일 수 있다 —
    어느 쪽인지는 counts 와 omissions 가 말한다.

    `objects`·`text_regions`·`fills`·`swatch_runs` 는 순위로 골라 실은 표본이라
    section 의 앞부분과 같지 않다. 그래서 이 축들의 누락 신고에는 이어받을
    `next_offset` 이 없고(None), 전량이 필요하면 section 을 처음부터 받는다.
    """

    fills: tuple[ReferenceFillTally, ...] = ()
    swatch_runs: tuple[ReferenceSwatchRun, ...] = ()
    objects: tuple[ReferenceObjectSample, ...] = ()
    text_regions: tuple[ReferenceTextAnchorSample, ...] = ()
    protected_gaps: tuple[ReferenceProtectedGap, ...] = ()
    breakpoint_candidates: tuple[ReferenceBreakpointCandidate, ...] = ()
    tiles: tuple[ReferenceImageTile, ...] = ()
    visible_segments: tuple[ReferenceImageSegment, ...] = ()


class ReferenceCompactOmission(ContractModel):
    section: ReferenceDetailSection
    omitted_count: int = Field(ge=1)
    reason: Literal["utf8_byte_budget"] = "utf8_byte_budget"
    # 축 이름과 되받을 자리. next_offset 이 있으면
    # hwp_get_reference_image_analysis_section(section=..., offset=next_offset)
    # 가 이어지는 항목을 그대로 돌려준다.
    #
    # None 인 경우가 두 가지다. 하나는 fills·swatch_runs 처럼 objects 에서
    # 파생된 축이라 원본 주소가 없는 것이고, 다른 하나는 objects·text_regions
    # 처럼 순위로 골라 실어서 인라인 표본이 section 의 앞부분이 아닌 것이다.
    # 둘 다 이어받을 offset 이 없으니 section 을 offset=0 부터 받아야 한다.
    # 골라 실은 축에 kept 를 적으면 "그 자리부터 나머지"라는 거짓말이 된다.
    axis: ReferenceEvidenceAxis | None = None
    next_offset: int | None = Field(default=None, ge=0)


class CompactReferenceImageAnalysis(ContractModel):
    analysis_id: str = Field(pattern=r"^ria-[0-9a-f]{16}$")
    analyzer_version: str
    source_image: Path | None
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_size_bytes: int = Field(ge=0)
    image_width: int = Field(ge=2)
    image_height: int = Field(ge=2)
    analysis_time_ms: float = Field(ge=0)
    cache_hit: bool
    counts: ReferenceAnalysisCounts
    structure: ReferenceStructureSummary
    recommended_execution_mode: ReferenceExecutionMode
    reason_codes: tuple[str, ...]
    # ReferenceExecutionMode literal 은 그대로 둔다 — 기존 소비자가 두 값만
    # 받도록 좁게 쓰고 있어서 값을 늘리면 깨진다. 조기 종료는 별도 필드로
    # 말하고 추천 모드는 ordinary_layout(모델이 직접 판독) 으로 낸다.
    analysis_complete: bool = True
    # 조기 종료한 분석은 캐시에 남지 않으므로 위 analysis_id 로는 아무것도 다시
    # 집을 수 없다. analysis_id 자체를 빼지 않는 이유는 그 필드가 필수라 소비자를
    # 깨뜨리기 때문이고, 대신 재사용 불가를 여기서 명시한다. 이 표가 없으면
    # 소비자가 그 id 를 쓰기 호출에 붙이고 → 쓰기 쪽이 "캐시에 없다"로 거절하고
    # → 같은 분석을 다시 불러 같은 id 로 또 조기 종료하는 맴돌이가 생긴다.
    analysis_id_reusable: bool = True
    stop_reason: ReferenceAnalysisStopReason | None = None
    predicted_seconds: float | None = Field(default=None, ge=0)
    time_budget_seconds: float | None = Field(default=None, gt=0)
    row_breakpoints: tuple[float, ...] = Field(min_length=2, max_length=51)
    column_breakpoints: tuple[float, ...] = Field(min_length=2, max_length=51)
    draft_reference_layout: ReferenceLayoutBlock | None = None
    # draft 를 내지 않는 응답(ordinary_layout)은 예산이 통째로 남는다. 그 자리를
    # 비워 두면 소비자는 좌표 증거가 아예 없는 줄 알고 눈대중으로 그림을 자른다
    # (실측: objects 254·text_regions 89 를 재 놓고 3,896 바이트만 보냈고,
    # 소비자는 section 을 한 번도 부르지 않았다). 잰 값을 우선순위대로 여기에
    # 채워 보낸다.
    evidence: ReferenceEvidenceSample = Field(default_factory=ReferenceEvidenceSample)
    detail_sections: tuple[ReferenceDetailSection, ...]
    analysis_result_path: Path | None
    artifacts: tuple[ReferenceArtifact, ...]
    response_budget_bytes: int = Field(default=100_000, ge=1)
    response_size_bytes: int = Field(default=0, ge=0)
    response_tokens_estimate: int = Field(default=0, ge=0)
    """이 응답을 2-space pretty-print 했을 때의 대략적인 토큰 수.

    이름이 estimate 인 것은 정말 어림이기 때문이다 — 실제 토크나이저를 돌리지
    않고 UTF-8 바이트를 3으로 나눈다(정확한 토크나이저는 의존성을 늘린다).
    실측에서 이 어림은 실제보다 큰 쪽으로 어긋난다(174,395바이트 응답의 실제
    토큰이 43,585인데 어림은 58,131이었다). 예산을 지키는 쪽으로 틀리므로
    이 값이 상한 아래면 실제도 아래다.

    쓰는 법: 호출자 하네스의 출력 상한과 견줘 보면 된다. 이 값이 상한을 넘으면
    응답이 잘려서 꼬리가 아예 닿지 않는다 — 실사례에서 43,585 토큰짜리 응답이
    10,000 토큰 상한에 잘려 objects 254개와 text_regions 89개가 통째로
    사라졌는데, 잘렸다는 사실 자체가 응답 안에 없어서 아무도 몰랐다.
    """
    paging_required: bool = False
    """이 응답이 잰 것을 다 싣지 못했으니 section 을 불러야 한다는 표시.

    omissions 가 비어 있지 않다는 말과 같지만, 불리언 하나로 두면 모델이
    누락 목록을 훑지 않고도 기계적으로 판단할 수 있다. False 면 이 응답이
    관측 전량이고 더 부를 것이 없다.
    """
    omissions: tuple[ReferenceCompactOmission, ...] = ()


class ReferenceAnalysisDetail(ContractModel):
    analysis_id: str = Field(pattern=r"^ria-[0-9a-f]{16}$")
    section: ReferenceDetailSection
    offset: int = Field(ge=0)
    limit: int = Field(ge=1, le=200)
    total: int = Field(ge=0)
    next_offset: int | None = Field(default=None, ge=0)
    items: tuple[JsonValue, ...]


def _pretty_bytes(payload: JsonValue) -> int:
    return len(
        json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
    )


def _token_estimate(pretty_bytes: int) -> int:
    # 올림한다. 어림이 예산을 지키는 쪽으로 틀리게 두는 편이 낫다.
    return math.ceil(pretty_bytes / _BYTES_PER_TOKEN)


def _with_response_size(
    result: CompactReferenceImageAnalysis,
) -> CompactReferenceImageAnalysis:
    """자기 자신을 세는 보고 필드를 실제 값에 수렴시킨다.

    response_size_bytes·response_tokens_estimate 는 응답 안에 적히는 값이라
    자릿수가 늘면 응답도 커진다(3,896 → 99,412 는 한 자리). paging_required 도
    "true"/"false" 로 길이가 다르다. 그래서 한 번에 맞지 않고, 값을 적고 다시
    재기를 되풀이해 고정점을 찾는다.
    """
    sized = result
    for _ in range(4):
        encoded = sized.model_dump_json()
        encoded_size = len(encoded.encode("utf-8"))
        tokens = _token_estimate(
            _pretty_bytes(cast(JsonValue, json.loads(encoded)))
        )
        paging = bool(sized.omissions)
        if (
            sized.response_size_bytes == encoded_size
            and sized.response_tokens_estimate == tokens
            and sized.paging_required == paging
        ):
            return sized
        sized = sized.model_copy(
            update={
                "response_size_bytes": encoded_size,
                "response_tokens_estimate": tokens,
                "paging_required": paging,
            }
        )
    return sized


# 축을 싣는 차례와 각 축이 받는 예산 몫(천분율)이다.
#
# 예전에는 몫 없이 앞선 축부터 예산을 다 쓰고 처음 잘린 축에서 멈췄다. 예산이
# 100,000바이트일 때는 그래도 뒤쪽까지 닿았지만, 토큰 예산으로 조이고 나면 앞의
# 한 축이 전부를 먹고 뒤가 통째로 0이 된다. 그건 고치려던 실패 그 자체다 —
# 실사례에서 objects 254개가 응답을 채우고 text_regions 89개가 하나도 실리지
# 못했다. 그래서 축마다 제 몫을 먼저 떼어 준다.
#
# 앞 축이 제 몫을 다 쓰지 않으면 남은 만큼 뒤로 넘긴다. 그래서 몫은 상한이
# 아니라 "최소한 이만큼은 이 축 것"이라는 바닥이고, 예산이 남으면 순서대로
# 더 실린다. 어느 축이 왜 잘렸는지는 축마다 제 몫 안에서 잘리므로 여전히
# omissions 하나로 답할 수 있다.
#
# 각 항목은 (축 이름, 그 축을 되받을 section, 천분율 몫) 이다.
_EvidenceOrder = tuple[
    tuple[ReferenceEvidenceAxis, ReferenceDetailSection, int], ...
]
_EVIDENCE_PRIORITY: Final[_EvidenceOrder] = (
    # 팔레트가 먼저다. 접은 뒤라 줄 수가 적고, 색은 다른 축으로 대신 알 수 없다.
    ("fills", "objects", 220),
    # 스와치 배열은 fills 가 말하지 못하는 "몇 행 몇 열"을 준다. 낱개 면적으로는
    # 순위에서 밀리는 칸들이라 묶음으로 실어야 닿는다.
    #
    # 몫을 170 에서 올린 것은 글자 띠에서 끊는 규칙이 들어오면서 한 배열이 두
    # 조각으로 나뉘기 때문이다. 실사례에서 통짜 6칸(1,259+1,998=3,257바이트)이
    # 2칸 선반과 4칸 선반이 되었는데, 170 이면 앞 조각만 실리고 정작 색이 네
    # 개인 뒤 조각이 잘려 나간다 — 끊어 놓고 한쪽만 보내면 끊은 값어치가 없다.
    #
    # 크게 떼어도 손해가 없는 축이라 넉넉히 준다. 배열은 한 페이지에 많아야
    # 서너 묶음이고(실사례 3), 남긴 몫은 곧바로 다음 축인 objects 로 넘어간다.
    # 배열이 아예 없는 문서에서는 이 몫이 통째로 objects 것이 되어, 예전보다
    # 오히려 더 많은 objects 가 실린다.
    ("swatch_runs", "objects", 300),
    # objects 는 254개 가운데 아홉 개를 싣던 축이라(3.5%) 몫을 내주어도 "표본"
    # 이라는 성격이 달라지지 않는다. 크롭 후보는 몫과 무관하게 앞으로 당겨져
    # 있어(_ranked_objects) 여기서 먼저 잘리는 것은 넓이 상위 몇 개다.
    ("objects", "objects", 170),
    ("text_regions", "text_regions", 190),
    ("protected_gaps", "protected_gaps", 60),
    # 타일은 24개 남짓인데 분석기가 실제로 잘라 둔 그림 경로를 준다. 모델이
    # 스스로 눈대중 크롭을 뜨지 않게 하는 축이라, 1443개짜리 분할점 목록의
    # 앞 6%보다 먼저 싣는다.
    ("tiles", "tiles", 60),
    # 몫이 0이라고 못 싣는 것은 아니다 — 앞 축이 남긴 자리를 넘겨받는다. 다만
    # 1443개 중 5개, 535개 중 1개를 싣자고 큰 판을 밀어낼 값어치가 없어서 제
    # 몫을 따로 떼지 않는다. 두 축 다 section 으로 온전히 받을 수 있다.
    ("breakpoint_candidates", "breakpoint_candidates", 0),
    ("visible_segments", "visible_segments", 0),
)
# 순위로 골라 싣는 축. 인라인 표본이 section 의 앞부분이 아니라서 이어받을
# offset 주소가 없다(ReferenceCompactOmission.next_offset 참고).
_RANKED_AXES: Final[frozenset[ReferenceEvidenceAxis]] = frozenset(
    {"fills", "swatch_runs", "objects", "text_regions"}
)
# response_size_bytes 는 자기 자신을 세는 값이라 자릿수가 늘면 응답도 커진다
# (3,896 → 99,412 은 한 자리). 그 되먹임이 예산을 넘기지 않게 남겨 두는 몫이다.
# 토큰 어림과 paging_required 도 같은 되먹임이 있어 넉넉히 잡는다.
_RESPONSE_SIZE_MARGIN: Final = 64
# compact 응답의 진짜 상한. 호출자 하네스가 출력을 토큰으로 자르기 때문에
# 바이트가 아니라 토큰이 구속조건이다(실측: 10,000 토큰 상한에 5회 잘렸다).
# 9,000 으로 둔 것은 상한에 바싹 붙이면 하네스가 덧붙이는 머리말 몇 백 토큰에
# 다시 잘리기 때문이다. 아래 나눗수가 실제 밀도보다 보수적이라 이 값은 곧
# "실제 토큰도 9,000 을 넘지 않는다" 는 보장이 된다.
_RESPONSE_TOKEN_BUDGET: Final = 9_000
# 토큰 어림의 나눗수. 정확한 토크나이저를 쓰지 않는 대신 실측한 밀도보다 낮게
# 잡아, 어림이 언제나 실제보다 크게(예산을 지키는 쪽으로) 틀리게 만든다.
#
# 3 이던 값을 내린 것은 그 어림이 낙관 쪽으로 틀렸기 때문이다. 실측 두 건:
#
#   - ria-574527d96dd1f8a8: pretty 174,395바이트 = 실제 43,585토큰 → 4.00 B/tok
#   - ria-d2397ba31511fce3: pretty  23,778바이트 = 실제 10,294토큰 → 2.31 B/tok
#
# 두 번째 응답은 스스로 7,926토큰이라 신고하고도 하네스의 10,000 상한에 잘렸다.
# 3 은 관측된 밀도의 아래쪽(2.31)보다 커서 어림이 실제를 과소평가한다. 2.1 은
# 그 아래쪽보다 9% 더 낮아, 밀도가 관측 최저치보다 더 빽빽해져도 어림이 먼저
# 걸린다. 예산 9,000 과 짝지으면 실제 토큰은 9,000 을 넘지 못한다.
_BYTES_PER_TOKEN: Final = 2.1
# pretty-print 에서 evidence 항목이 놓이는 깊이(root → evidence → 축 배열 →
# 항목). 항목의 모든 줄이 이만큼 더 들여쓰인다.
_EVIDENCE_INDENT_DEPTH: Final = 3
# 글자 덩어리가 색견본 배열에 "붙어 있다" 고 볼 세로 여유(제 높이 대비).
# 실사례에서 소제목은 배열 바로 위에 있어 제 상자만으로도 배열에 닿는다. 이
# 여유는 라벨과 배열 사이에 한 줄 간격이 있는 흔한 배치까지 담으려는 것이다.
_LABEL_RUN_MARGIN_RATIO: Final = 0.5
# 라벨이 배열 위에 "얹혀 있다" 고 볼 가로 겹침(제 폭 대비).
_LABEL_RUN_COVERAGE_RATIO: Final = 0.5
# 인라인에 실어 보낼 크롭 경로의 총 개수. 한 건이 pretty-print 로 110바이트쯤
# 되어서, 97개에 다 실으면 그것만으로 1만 바이트를 먹고 좌표가 통째로 밀려난다
# (원래 앵커에서 경로를 벗겨 낸 이유가 그것이다). 6개면 실사례의 소제목(세
# 조각)을 조각째 싣고도 다른 라벨 두엇이 더 들어가며 값은 700바이트 언저리다.
_CROP_PATH_BUDGET: Final = 6
# 인라인에 실어 보낼 컨택트시트의 최대 장수.
#
# 시트 한 장은 pretty-print 산출물 항목 하나이고, 값은 고정 190바이트(kind·
# size_bytes·sha256 64자·중괄호·들여쓰기)에 경로 길이가 얹힌다(실측: 경로
# 138자 → 329바이트, 576자 → 786바이트, 1,036자 → 1,266바이트). 상한이 없던
# 시절에는 그 값이 장수만큼 곱해져, 깊이 20 경로에 60장이면 산출물 블록만
# 47,000바이트 — 예산 18,900바이트(=9,000토큰 × 2.1)의 2.5배다. 그래서 골격
# (증거가 한 줄도 없는 응답 뼈대)이 예산 코앞에 서거나 아예 넘겼고, 넘긴
# 자리는 enforce 가 산출물을 통째로 내려 메웠다 — depth 10~20 × sheets 10~60
# 스윕 561점에서 증거 0이 25점, 산출물 0이 399점이었다.
#
# 4로 둔 근거는 세 쪽이다.
#
#   - 밴드가 사라지는 여유가 가장 크다. 561점 스윕은 4·6·8 모두 증거 0을 0점으로
#     만들지만, 경로를 더 깊게(20~40) 밀면 갈린다. 실측 최소 증거 항목 수는
#     4에서 30, 6에서 22, 8에서 14 이고 12에서는 깊이 40이 다시 증거 0을 낸다.
#     4는 그 벼랑에서 가장 멀다.
#   - 대표성이 충분하다. 시트 한 장이 타일 4장을 담으므로 4장이면 타일 16장어치
#     이고, 첫 장만 남기는 것과 달리 앞뒤를 붙들고 고르게 솎으므로
#     (_sampled_sheet_indices) 덮는 자리가 페이지 위쪽에 쏠리지 않는다.
#   - 나머지를 인라인으로 나르는 값어치가 애초에 낮다. 시트는 같은 디렉터리에
#     contact-sheet-NN 으로 놓이므로(save_contact_sheets), 실린 경로 하나와
#     총수만 있으면 나머지 경로는 지어낼 수 있다. 그 총수·나머지 수·집는 길을
#     counts.contact_sheets·omissions·reason_codes 가 함께 신고한다.
#
# 장수가 이 상한 이하인 문서는 예전과 한 바이트도 다르지 않다. 넘는 문서는
# 시트 기록 몇 줄을 좌표 증거로 바꿔 싣는 셈인데, 좌표는 되받을 section 이
# 있어도 예산 안에서 먼저 보여야 값어치가 있고 시트는 이름으로 지어낼 수 있다.
_CONTACT_SHEET_INLINE_BUDGET: Final = 4
# 솎아 냈을 때 응답이 스스로 붙이는 안내다. 스키마를 늘리지 않고 "나머지를
# 어디서 어떻게 집는가" 를 말할 수 있는 자리가 reason_codes 뿐이라 여기에 적는다.
# 첫 줄은 되받을 section 을 지목하고(hwp_get_reference_image_analysis_section
# 이 section="artifacts" 를 실제로 지원한다 — _all_artifacts 가 시트를 전량
# 돌려준다), 둘째 줄은 인라인 경로에서 나머지 경로를 곧바로 지어낼 수 있음을
# 밝힌다(같은 디렉터리, contact-sheet-NN 이라는 결정적 이름).
_CONTACT_SHEET_SAMPLED_CODE: Final = "CONTACT_SHEETS_SAMPLED_SEE_ARTIFACTS_SECTION"
_CONTACT_SHEET_NAMING_CODE: Final = "CONTACT_SHEET_SIBLINGS_NAMED_CONTACT_SHEET_NN"
# 좌표를 적는 자릿수. 이 모듈은 이미 breakpoint 를 6자리로 적고 있고(_cluster_
# coordinates), 정규 좌표 1e-6 은 2613px 폭에서 0.0026px 라 화면 정밀도보다
# 훨씬 잘다. 17자리를 그대로 실으면 같은 예산에 절반밖에 못 싣는다.
_EVIDENCE_DIGITS: Final = 6


def _rounded_box(box: NormalizedBox) -> NormalizedBox:
    left = round(box.left, _EVIDENCE_DIGITS)
    top = round(box.top, _EVIDENCE_DIGITS)
    right = round(box.right, _EVIDENCE_DIGITS)
    bottom = round(box.bottom, _EVIDENCE_DIGITS)
    if left >= right or top >= bottom:
        # 반올림이 넓이를 없앤 아주 얇은 상자는 잰 값 그대로 둔다.
        return box
    return NormalizedBox(left=left, top=top, right=right, bottom=bottom)


def _rounded_segment(segment: ReferenceImageSegment) -> ReferenceImageSegment:
    start_x = round(segment.start_x, _EVIDENCE_DIGITS)
    start_y = round(segment.start_y, _EVIDENCE_DIGITS)
    end_x = round(segment.end_x, _EVIDENCE_DIGITS)
    end_y = round(segment.end_y, _EVIDENCE_DIGITS)
    length = end_x - start_x if segment.orientation == "horizontal" else end_y - start_y
    if length <= 0:
        return segment
    return segment.model_copy(
        update={
            "start_x": start_x,
            "start_y": start_y,
            "end_x": end_x,
            "end_y": end_y,
        }
    )


def _fill_tallies(
    objects: tuple[ReferenceImageObject, ...],
) -> tuple[ReferenceFillTally, ...]:
    """채움색을 집계하고, 가까운 계조를 접어 넓이 순으로 낸다.

    순서를 개수가 아니라 넓이로 바꾼 것이 D13 수리의 절반이다. 개수로 세면
    안티에일리어싱 계조가 이긴다 — 잡티는 작은 조각이 여럿이라 (188,188,188)
    한 색이 13개로 잡히는 반면, 먼셀 스와치는 한 색이 한 칸씩이라 1개다.
    나머지 절반은 접기다. 접고 넓이로 세우면 149종이 29종으로 줄고 먼셀 6색이
    상위 14위 안에 들어온다(실측).
    """
    counts: Counter[Rgb] = Counter()
    areas: dict[Rgb, float] = {}
    for item in objects:
        if item.fill is None:
            continue
        counts[item.fill] += 1
        areas[item.fill] = areas.get(item.fill, 0.0) + (
            (item.bbox.right - item.bbox.left) * (item.bbox.bottom - item.bbox.top)
        )
    # 넓은 색이 무리의 대표가 된다. 대표는 평균이 아니라 실제 관측색이다.
    ordered = sorted(areas, key=lambda fill: areas[fill], reverse=True)
    representatives: list[Rgb] = []
    absorbed: dict[Rgb, list[Rgb]] = {}
    for fill in ordered:
        for candidate in representatives:
            if color_distance(fill, candidate) <= FILL_MERGE_DISTANCE:
                absorbed[candidate].append(fill)
                break
        else:
            representatives.append(fill)
            absorbed[fill] = [fill]
    tallies = [
        ReferenceFillTally(
            fill=representative,
            object_count=sum(counts[fill] for fill in group),
            # 겹친 객체가 있어 합이 1을 넘을 수 있다. 넓이 비율은 순위를 읽는
            # 값이라 1에서 자른다.
            area_ratio=round(
                min(1.0, sum(areas[fill] for fill in group)),
                _EVIDENCE_DIGITS,
            ),
            merged_fill_count=len(group),
        )
        for representative in representatives
        if (group := absorbed[representative])
    ]
    return tuple(
        sorted(tallies, key=lambda item: item.area_ratio, reverse=True)
    )


def _box_area(box: NormalizedBox) -> float:
    return (box.right - box.left) * (box.bottom - box.top)


def _ranked_objects(
    analysis: ReferenceImageAnalysis,
) -> tuple[ReferenceObjectSample, ...]:
    """넓이 상위 object 에 크롭 후보를 더해 인라인 후보를 세운다.

    넓이만으로 자르면 크롭 후보가 통째로 떨어진다 — 실사례의 크롭 후보 6개는
    220x6~13px 라 254개 중 45위 언저리다. 이 응답이 색으로 되살릴 수 없다고
    표시해 놓고 정작 그 좌표를 안 실으면 표시가 아무 쓸모가 없어서, 넓이
    순위와 별개로 반드시 넣는다.
    """
    annotated = [
        annotate_object(
            item.model_copy(update={"bbox": _rounded_box(item.bbox)}),
            analysis.text_regions,
        )
        for item in analysis.objects
    ]
    ordered = sorted(
        annotated,
        key=lambda item: _box_area(item.bbox),
        reverse=True,
    )
    # 크롭 후보를 앞으로 당긴다. 뒤에 두면 몫이 모자랄 때 잘려 나간다.
    return tuple(
        sorted(ordered, key=lambda item: not item.crop_candidate)
    )


def _beside_a_run(
    box: NormalizedBox,
    runs: Sequence[ReferenceSwatchRun],
) -> bool:
    """이 글자 덩어리가 색견본 배열에 붙어 있는가.

    세로로는 제 높이의 절반만큼 넓혀서 본다 — 배열의 제목·설명은 배열 바로
    위나 아래에 앉지, 한 화면 건너에 앉지 않는다.

    가로로는 "걸친다" 가 아니라 "얹혀 있다" 를 묻는다. 제 폭의 절반 넘게 배열
    위아래에 들어와야 그 배열의 라벨이다. 그냥 겹침으로 물으면 페이지를 가로
    지르는 큰 띠가 옆에 있는 작은 배열의 라벨로 둔갑한다(실사례: 폭 1.0 짜리
    사진 띠가 폭 0.0295 짜리 얼룩 묶음에 3%만 걸치고도 라벨 자리를 차지했다).
    실사례의 진짜 소제목은 제 폭의 85.5%가 배열 위에 얹혀 있다.
    """
    margin = (box.bottom - box.top) * _LABEL_RUN_MARGIN_RATIO
    width = box.right - box.left
    return any(
        min(box.right, run.bbox.right) - max(box.left, run.bbox.left)
        >= _LABEL_RUN_COVERAGE_RATIO * width
        and run.bbox.top < box.bottom + margin
        and box.top - margin < run.bbox.bottom
        for run in runs
    )


def _ranked_text_anchors(
    analysis: ReferenceImageAnalysis,
    runs: Sequence[ReferenceSwatchRun],
) -> tuple[ReferenceTextAnchorSample, ...]:
    """끊긴 조각을 라벨로 붙이고, 배열 곁의 라벨을 앞으로 당겨 세운다.

    면적만으로 세우면 배열을 설명하는 라벨이 통째로 떨어진다 — 실사례에서
    소제목 "그래픽요소 적용" 은 세 조각으로 끊겨 저마다 97개 중 하위권이었고,
    상위 18개만 실리는 예산에서 79개와 함께 잘려 나갔다. 그 결과 응답은 색견본
    2칸과 4칸이 무엇을 위한 것인지 한 글자도 말하지 못했다.

    당기는 방식은 objects 의 크롭 후보 확보와 같다(`_ranked_objects`). 면적
    순위를 먼저 세운 뒤 안정 정렬로 배열 곁의 것만 앞으로 옮기므로, 확보되는
    자리 말고는 순위가 그대로다.

    붙이기가 지나칠 때가 있다는 것도 적어 둔다 — 같은 띠에서 서로 겹친 큰
    영역들(실사례의 사진 띠 6개)은 한 앵커로 합쳐진다. 조각들은 section 에
    그대로 남아 있고 `merged_from_count` 가 합친 수를 밝히므로, 잃는 것은
    인라인 표본의 내부 경계뿐이고 얻는 것은 그만큼의 자리다.
    """
    ordered = sorted(
        merge_text_fragments(
            analysis.text_regions,
            image_width=analysis.image_width,
            image_height=analysis.image_height,
        ),
        key=lambda members: _box_area(merged_text_box(members)),
        reverse=True,
    )
    hoisted = sorted(
        ordered,
        key=lambda members: not _beside_a_run(merged_text_box(members), runs),
    )
    anchors: list[ReferenceTextAnchorSample] = []
    spent = 0
    for members in hoisted:
        # 경로를 실을 값어치가 있는 앵커: 배열 곁에 있거나, 조각을 붙여 만든
        # 것이라 글자를 읽어야 뜻이 서는 라벨이다.
        wanted = len(members) > 1 or _beside_a_run(merged_text_box(members), runs)
        carried = wanted and spent + len(members) <= _CROP_PATH_BUDGET
        if carried:
            spent += len(members)
        anchors.append(merged_text_anchor(members, with_crop_paths=carried))
    return tuple(anchors)


def _evidence_candidates(
    analysis: ReferenceImageAnalysis,
) -> dict[ReferenceEvidenceAxis, tuple[ContractModel, ...]]:
    runs = detect_swatch_runs(
        analysis.objects,
        image_width=analysis.image_width,
        image_height=analysis.image_height,
        text_regions=analysis.text_regions,
    )
    return {
        "fills": _fill_tallies(analysis.objects),
        "swatch_runs": runs,
        "objects": _ranked_objects(analysis),
        "text_regions": _ranked_text_anchors(analysis, runs),
        "protected_gaps": tuple(
            item.model_copy(update={"bbox": _rounded_box(item.bbox)})
            for item in analysis.protected_gaps
        ),
        "breakpoint_candidates": tuple(
            item.model_copy(update={"position": round(item.position, _EVIDENCE_DIGITS)})
            for item in analysis.breakpoint_candidates
        ),
        "tiles": tuple(
            item.model_copy(update={"bbox": _rounded_box(item.bbox)})
            for item in analysis.tiles
        ),
        "visible_segments": tuple(
            _rounded_segment(item) for item in analysis.visible_segments
        ),
    }


def _omission(
    axis: ReferenceEvidenceAxis,
    section: ReferenceDetailSection,
    omitted_count: int,
    next_offset: int | None,
) -> ReferenceCompactOmission:
    return ReferenceCompactOmission(
        section=section,
        omitted_count=omitted_count,
        axis=axis,
        # 골라 실은 축은 이어받을 주소가 없다.
        next_offset=None if axis in _RANKED_AXES else next_offset,
    )


def _item_cost(item: ContractModel) -> int:
    """항목 하나가 pretty-print 응답에서 차지하는 바이트.

    예산이 토큰이고 토큰 어림이 pretty-print 바이트에서 나오므로, 싣고 말고를
    정하는 자도 같은 자여야 한다. compact 바이트로 재면 값이 어긋난다 —
    pretty-print 는 스칼라마다 줄을 바꾸고 들여쓰기를 붙여서 같은 항목이
    1.7배까지 커진다(실측: 99,912 → 174,395).
    """
    text = json.dumps(
        cast(JsonValue, json.loads(item.model_dump_json())),
        indent=2,
        ensure_ascii=False,
    )
    lines = text.count("\n") + 1
    # 배열 안 항목이라 모든 줄이 깊이만큼 더 들여쓰이고, 뒤에 ",\n" 이 붙는다.
    return (
        len(text.encode("utf-8")) + lines * 2 * _EVIDENCE_INDENT_DEPTH + 2
    )


def _pack_evidence(
    candidates: dict[ReferenceEvidenceAxis, tuple[ContractModel, ...]],
    headroom: int,
) -> tuple[
    dict[str, tuple[ContractModel, ...]],
    list[ReferenceCompactOmission],
]:
    """축마다 제 몫 안에서 앞에서부터 싣고, 남은 몫은 뒤로 넘긴다."""
    carried: dict[str, tuple[ContractModel, ...]] = {}
    omissions: list[ReferenceCompactOmission] = []
    total_share = sum(share for _, _, share in _EVIDENCE_PRIORITY)
    spare = 0
    for axis, section, share in _EVIDENCE_PRIORITY:
        items = candidates[axis]
        allowance = max(0, headroom) * share // total_share + spare
        kept = 0
        for item in items:
            cost = _item_cost(item)
            if cost > allowance:
                break
            allowance -= cost
            kept += 1
        spare = allowance
        if kept:
            carried[axis] = items[:kept]
        if kept < len(items):
            omissions.append(_omission(axis, section, len(items) - kept, kept))
    return carried, omissions


def _omission_cost(
    existing: int,
    added: Sequence[ReferenceCompactOmission],
) -> int:
    if not added:
        return 0
    # 누락 신고도 pretty-print 로 실려 나가므로 같은 자로 잰다. 신고는
    # evidence 항목보다 한 단계 얕다(root → omissions → 항목).
    return sum(_item_cost(item) for item in added) - (0 if existing else 1)


def fill_compact_evidence(
    result: CompactReferenceImageAnalysis,
    analysis: ReferenceImageAnalysis,
) -> CompactReferenceImageAnalysis:
    """남은 예산을 잰 값으로 채우고, 못 실은 축을 그대로 신고한다."""
    if result.draft_reference_layout is not None or not result.analysis_complete:
        # 초안을 낸 응답은 그 초안이 이미 예산의 임자다. 조기 종료 응답은
        # 캐시가 없어 되받을 section 자체가 없다. 둘 다 손대지 않는다.
        #
        # 다만 "임자다" 는 그 초안이 실제로 실려 나갈 때만 참이다. 예산이
        # 초안을 통째로 내리면 임자가 없어지므로, 그때는 이 함수를 한 번 더
        # 부른다(compact_reference_image_analysis 참고).
        return result
    candidates = _evidence_candidates(analysis)
    base = _with_response_size(
        result.model_copy(update={"evidence": ReferenceEvidenceSample()})
    )
    # 예산은 토큰이고 항목 값도 pretty-print 바이트라, 남은 자리도 같은 자로
    # 잰다. 바이트 예산(response_budget_bytes)은 pretty 가 compact 보다 언제나
    # 크므로 토큰 예산을 지키면 저절로 지켜진다.
    headroom = (
        math.floor(_RESPONSE_TOKEN_BUDGET * _BYTES_PER_TOKEN)
        - _pretty_bytes(cast(JsonValue, json.loads(base.model_dump_json())))
        - _RESPONSE_SIZE_MARGIN
    )
    # 누락 신고 자체도 응답 안에 들어가므로 그 몫을 먼저 떼어 놓는다. 떼는 몫이
    # 커지면 싣는 양이 줄고, 줄면 신고가 늘 수는 있어도 주는 일은 없어서 위로만
    # 수렴한다. 이 몫을 빼먹으면 채우기가 예산을 300바이트 넘겨(실측) 아래
    # enforce 가 축 하나를 통째로 내리는 낭비가 난다.
    carried: dict[str, tuple[ContractModel, ...]] = {}
    added: list[ReferenceCompactOmission] = []
    reserved = 0
    for _ in range(4):
        carried, added = _pack_evidence(candidates, headroom - reserved)
        cost = _omission_cost(len(base.omissions), added)
        if cost <= reserved:
            break
        reserved = cost
    return _with_response_size(
        base.model_copy(
            update={
                "evidence": ReferenceEvidenceSample.model_validate(carried),
                "omissions": (*base.omissions, *added),
            }
        )
    )


def _drop_evidence_axis(
    bounded: CompactReferenceImageAnalysis,
    axis: ReferenceEvidenceAxis,
    section: ReferenceDetailSection,
) -> CompactReferenceImageAnalysis:
    carried = cast(tuple[ContractModel, ...], getattr(bounded.evidence, axis))
    already = sum(item.omitted_count for item in bounded.omissions if item.axis == axis)
    kept = [item for item in bounded.omissions if item.axis != axis]
    kept.append(_omission(axis, section, len(carried) + already, 0))
    return _with_response_size(
        bounded.model_copy(
            update={
                "evidence": bounded.evidence.model_copy(update={axis: ()}),
                "omissions": tuple(kept),
            }
        )
    )


def _within_budget(result: CompactReferenceImageAnalysis) -> bool:
    """바이트 예산과 토큰 예산을 둘 다 지켰는가.

    토큰이 실제 구속조건이고 바이트는 옛 계약이라 둘 다 본다. 토큰 쪽이 거의
    언제나 먼저 걸린다 — pretty-print 가 compact 보다 1.7배 커서다.
    """
    return (
        result.response_size_bytes <= result.response_budget_bytes
        and result.response_tokens_estimate <= _RESPONSE_TOKEN_BUDGET
    )


def enforce_compact_response_budget(
    result: CompactReferenceImageAnalysis,
) -> CompactReferenceImageAnalysis:
    bounded = _with_response_size(result)
    if _within_budget(bounded):
        return bounded
    omissions = list(bounded.omissions)
    draft = bounded.draft_reference_layout
    if draft is not None:
        omissions.append(
            ReferenceCompactOmission(
                section="draft_reference_layout",
                axis="draft_reference_layout",
                omitted_count=max(
                    1,
                    len(draft.visible_edges)
                    + len(draft.styles)
                    + len(draft.style_regions)
                    + len(draft.text_anchors),
                ),
            )
        )
        bounded = _with_response_size(
            bounded.model_copy(
                update={
                    "draft_reference_layout": None,
                    "omissions": tuple(omissions),
                }
            )
        )
    # 증거는 되받을 자리(section)가 있는 축이라 산출물 경로보다 먼저 내린다 —
    # 경로를 먼저 지우면 되받는 길 자체가 사라진다. 낮은 우선순위부터 통째로
    # 내리고, 내린 만큼을 omissions 에 합산해 총수를 정확히 유지한다.
    for axis, section, _ in reversed(_EVIDENCE_PRIORITY):
        if _within_budget(bounded):
            break
        if getattr(bounded.evidence, axis):
            bounded = _drop_evidence_axis(bounded, axis, section)
    omissions = list(bounded.omissions)
    if not _within_budget(bounded) and bounded.artifacts:
        omissions.append(
            ReferenceCompactOmission(
                section="artifacts",
                axis="artifacts",
                omitted_count=len(bounded.artifacts),
            )
        )
        bounded = _with_response_size(
            bounded.model_copy(
                update={
                    "artifacts": (),
                    "omissions": tuple(omissions),
                }
            )
        )
    if not _within_budget(bounded) and (
        bounded.source_image is not None
        or bounded.analysis_result_path is not None
    ):
        omissions.append(
            ReferenceCompactOmission(
                section="artifacts",
                axis="artifacts",
                omitted_count=sum(
                    path is not None
                    for path in (
                        bounded.source_image,
                        bounded.analysis_result_path,
                    )
                ),
            )
        )
        bounded = _with_response_size(
            bounded.model_copy(
                update={
                    "source_image": None,
                    "analysis_result_path": None,
                    "omissions": tuple(omissions),
                }
            )
        )
    if not _within_budget(bounded):
        raise ValueError("compact reference image response exceeds its budget")
    return bounded


@final
class _SegmentComponents:
    segments: tuple[ReferenceImageSegment, ...]
    parents: list[int]

    def __init__(self, segments: tuple[ReferenceImageSegment, ...]) -> None:
        self.segments = segments
        self.parents = list(range(len(segments)))

    def find(self, index: int) -> int:
        parent = self.parents[index]
        while parent != self.parents[parent]:
            parent = self.parents[parent]
        while index != parent:
            next_index = self.parents[index]
            self.parents[index] = parent
            index = next_index
        return parent

    def union(self, left: int, right: int) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            self.parents[right_root] = left_root

    def groups(self) -> tuple[tuple[ReferenceImageSegment, ...], ...]:
        grouped: dict[int, list[ReferenceImageSegment]] = {}
        for index, segment in enumerate(self.segments):
            grouped.setdefault(self.find(index), []).append(segment)
        return tuple(tuple(items) for items in grouped.values())


def _segment_length(segment: ReferenceImageSegment) -> float:
    return (
        segment.end_x - segment.start_x
        if segment.orientation == "horizontal"
        else segment.end_y - segment.start_y
    )


def _segments_touch(
    left: ReferenceImageSegment,
    right: ReferenceImageSegment,
    tolerance: float,
) -> bool:
    if left.orientation == right.orientation == "horizontal":
        return (
            abs(left.start_y - right.start_y) <= tolerance
            and left.start_x <= right.end_x + tolerance
            and right.start_x <= left.end_x + tolerance
        )
    if left.orientation == right.orientation == "vertical":
        return (
            abs(left.start_x - right.start_x) <= tolerance
            and left.start_y <= right.end_y + tolerance
            and right.start_y <= left.end_y + tolerance
        )
    horizontal, vertical = (
        (left, right) if left.orientation == "horizontal" else (right, left)
    )
    return (
        horizontal.start_x - tolerance
        <= vertical.start_x
        <= horizontal.end_x + tolerance
        and vertical.start_y - tolerance
        <= horizontal.start_y
        <= vertical.end_y + tolerance
    )


def _connected_components(
    analysis: ReferenceImageAnalysis,
) -> tuple[tuple[ReferenceImageSegment, ...], ...]:
    selected = tuple(
        sorted(
            analysis.visible_segments,
            key=_segment_length,
            reverse=True,
        )[:600]
    )
    components = _SegmentComponents(selected)
    tolerance = max(2 / analysis.image_width, 2 / analysis.image_height, 0.001)
    for left in range(len(selected)):
        for right in range(left + 1, len(selected)):
            if _segments_touch(selected[left], selected[right], tolerance):
                components.union(left, right)
    return components.groups()


def _component_box(
    segments: tuple[ReferenceImageSegment, ...],
) -> NormalizedBox:
    left = min(segment.start_x for segment in segments)
    top = min(segment.start_y for segment in segments)
    right = max(segment.end_x for segment in segments)
    bottom = max(segment.end_y for segment in segments)
    if left >= right:
        left = max(0.0, left - 0.0005)
        right = min(1.0, right + 0.0005)
    if top >= bottom:
        top = max(0.0, top - 0.0005)
        bottom = min(1.0, bottom + 0.0005)
    return NormalizedBox(
        left=left,
        top=top,
        right=right,
        bottom=bottom,
    )


def _contains(box: NormalizedBox, left: float, top: float) -> bool:
    return box.left <= left <= box.right and box.top <= top <= box.bottom


def _intersection_count(
    segments: tuple[ReferenceImageSegment, ...],
    tolerance: float,
) -> int:
    horizontal = tuple(
        segment for segment in segments if segment.orientation == "horizontal"
    )
    vertical = tuple(
        segment for segment in segments if segment.orientation == "vertical"
    )
    return sum(
        _segments_touch(left, right, tolerance)
        for left in horizontal
        for right in vertical
    )


def _repeated_style_regions(
    analysis: ReferenceImageAnalysis,
    box: NormalizedBox,
) -> int:
    fills = Counter(
        item.fill
        for item in analysis.objects
        if item.fill is not None
        and _contains(
            box,
            (item.bbox.left + item.bbox.right) / 2,
            (item.bbox.top + item.bbox.bottom) / 2,
        )
    )
    return sum(count for count in fills.values() if count > 1)


def _dominant_component(
    analysis: ReferenceImageAnalysis,
) -> tuple[
    tuple[ReferenceImageSegment, ...],
    NormalizedBox,
    ReferenceStructureSummary,
]:
    components = _connected_components(analysis)
    if not components:
        empty_box = NormalizedBox(left=0, top=0, right=1, bottom=1)
        return (
            (),
            empty_box,
            ReferenceStructureSummary(
                horizontal_segments=0,
                vertical_segments=0,
                orthogonal_intersections=0,
                dominant_segment_ratio=0,
                dominant_grid_area_ratio=0,
                partial_edge_ratio=0,
                repeated_style_regions=0,
                grid_fixed_text_regions=0,
            ),
        )
    tolerance = max(2 / analysis.image_width, 2 / analysis.image_height, 0.001)
    ranked: list[
        tuple[
            int,
            int,
            float,
            tuple[ReferenceImageSegment, ...],
            NormalizedBox,
        ]
    ] = []
    for component in components:
        intersections = _intersection_count(component, tolerance)
        box = _component_box(component)
        area = (box.right - box.left) * (box.bottom - box.top)
        ranked.append((intersections, len(component), area, component, box))
    _, _, _, dominant, box = max(ranked, key=lambda item: item[:3])
    horizontal = tuple(
        segment for segment in dominant if segment.orientation == "horizontal"
    )
    vertical = tuple(
        segment for segment in dominant if segment.orientation == "vertical"
    )
    intersections = _intersection_count(dominant, tolerance)
    box_width = max(box.right - box.left, tolerance)
    box_height = max(box.bottom - box.top, tolerance)
    partial = sum(
        _segment_length(segment)
        < (box_width if segment.orientation == "horizontal" else box_height) * 0.8
        for segment in dominant
    )
    text_regions = sum(
        _contains(
            box,
            (item.bbox.left + item.bbox.right) / 2,
            (item.bbox.top + item.bbox.bottom) / 2,
        )
        for item in analysis.text_regions
    )
    total = max(1, min(len(analysis.visible_segments), 600))
    summary = ReferenceStructureSummary(
        horizontal_segments=len(horizontal),
        vertical_segments=len(vertical),
        orthogonal_intersections=intersections,
        dominant_segment_ratio=round(len(dominant) / total, 4),
        dominant_grid_area_ratio=round(box_width * box_height, 4),
        partial_edge_ratio=round(partial / max(1, len(dominant)), 4),
        repeated_style_regions=_repeated_style_regions(analysis, box),
        grid_fixed_text_regions=text_regions,
    )
    return dominant, box, summary


def _cluster_coordinates(
    values: list[float],
    *,
    tolerance: float,
) -> tuple[float, ...]:
    ordered = sorted(min(1.0, max(0.0, value)) for value in values)
    groups: list[list[float]] = []
    for value in ordered:
        if not groups or value - groups[-1][-1] > tolerance:
            groups.append([value])
        else:
            groups[-1].append(value)
    clustered = [round(sum(group) / len(group), 6) for group in groups]
    if clustered[0] != 0:
        clustered.insert(0, 0.0)
    else:
        clustered[0] = 0.0
    if clustered[-1] != 1:
        clustered.append(1.0)
    else:
        clustered[-1] = 1.0
    unique = list(dict.fromkeys(clustered))
    if len(unique) <= 51:
        return tuple(unique)
    selected = {round(index * (len(unique) - 1) / 50) for index in range(51)}
    return tuple(unique[index] for index in sorted(selected))


def _breakpoints(
    analysis: ReferenceImageAnalysis,
    segments: tuple[ReferenceImageSegment, ...],
) -> tuple[tuple[float, ...], tuple[float, ...]]:
    rows = [0.0, 1.0]
    columns = [0.0, 1.0]
    for segment in segments:
        if segment.orientation == "horizontal":
            rows.append(segment.start_y)
            columns.extend((segment.start_x, segment.end_x))
        else:
            columns.append(segment.start_x)
            rows.extend((segment.start_y, segment.end_y))
    row_tolerance = max(2 / analysis.image_height, 0.001)
    column_tolerance = max(2 / analysis.image_width, 0.001)
    return (
        _cluster_coordinates(rows, tolerance=row_tolerance),
        _cluster_coordinates(columns, tolerance=column_tolerance),
    )


def _nearest(values: tuple[float, ...], coordinate: float) -> int:
    return min(range(len(values)), key=lambda index: abs(values[index] - coordinate))


def _edge_width(
    width_px: int,
) -> Literal[
    "0.12mm",
    "0.2mm",
    "0.3mm",
    "0.5mm",
    "1.0mm",
]:
    if width_px <= 1:
        return "0.12mm"
    if width_px <= 2:
        return "0.2mm"
    if width_px <= 4:
        return "0.3mm"
    if width_px <= 7:
        return "0.5mm"
    return "1.0mm"


def _visible_edges(
    segments: tuple[ReferenceImageSegment, ...],
    rows: tuple[float, ...],
    columns: tuple[float, ...],
) -> tuple[VisibleEdge, ...]:
    edges: dict[tuple[str, int, int, int, Rgb, str], VisibleEdge] = {}
    for segment in segments:
        width = _edge_width(segment.width_px)
        if segment.orientation == "horizontal":
            line = _nearest(rows, segment.start_y)
            start = _nearest(columns, segment.start_x)
            end = _nearest(columns, segment.end_x)
        else:
            line = _nearest(columns, segment.start_x)
            start = _nearest(rows, segment.start_y)
            end = _nearest(rows, segment.end_y)
        if start >= end:
            continue
        key = (segment.orientation, line, start, end, segment.color, width)
        edges[key] = VisibleEdge(
            orientation=segment.orientation,
            line=line,
            start=start,
            end=end,
            width=width,
            color=segment.color,
        )
    return tuple(edges.values())


def _object_styles(
    objects: tuple[ReferenceImageObject, ...],
    rows: tuple[float, ...],
    columns: tuple[float, ...],
    image_width: int,
    image_height: int,
) -> tuple[tuple[ReferenceStyle, ...], tuple[StyleRegion, ...]]:
    colors: tuple[Rgb, ...] = tuple(
        dict.fromkeys(item.fill for item in objects if item.fill is not None)
    )[:64]
    styles = tuple(
        ReferenceStyle(
            key=f"fill_{color[0]:02x}{color[1]:02x}{color[2]:02x}",
            fill_color=color,
        )
        for color in colors
    )
    keys = {
        style.fill_color: style.key for style in styles if style.fill_color is not None
    }
    row_tolerance = max(3 / image_height, 0.008)
    column_tolerance = max(3 / image_width, 0.008)
    regions: list[StyleRegion] = []
    for item in objects:
        if item.fill is None or item.fill not in keys:
            continue
        top = _nearest(rows, item.bbox.top)
        bottom = _nearest(rows, item.bbox.bottom)
        left = _nearest(columns, item.bbox.left)
        right = _nearest(columns, item.bbox.right)
        if (
            top >= bottom
            or left >= right
            or abs(rows[top] - item.bbox.top) > row_tolerance
            or abs(rows[bottom] - item.bbox.bottom) > row_tolerance
            or abs(columns[left] - item.bbox.left) > column_tolerance
            or abs(columns[right] - item.bbox.right) > column_tolerance
        ):
            continue
        regions.append(
            StyleRegion(
                top=top,
                left=left,
                bottom=bottom,
                right=right,
                style_key=keys[item.fill],
            )
        )
    used = {region.style_key for region in regions}
    return (
        tuple(style for style in styles if style.key in used),
        tuple(regions[:1000]),
    )


def _draft_layout(
    analysis: ReferenceImageAnalysis,
    segments: tuple[ReferenceImageSegment, ...],
    rows: tuple[float, ...],
    columns: tuple[float, ...],
) -> ReferenceLayoutBlock:
    styles, regions = _object_styles(
        analysis.objects,
        rows,
        columns,
        analysis.image_width,
        analysis.image_height,
    )
    block = ReferenceLayoutBlock(
        kind="reference_layout",
        source_image=analysis.source_image,
        analysis_id=analysis.analysis_id,
        row_breakpoints=rows,
        column_breakpoints=columns,
        visible_edges=_visible_edges(segments, rows, columns),
        styles=styles,
        style_regions=regions,
    )
    return align_reference_layout_to_analysis(block, analysis)


def _artifact(path: Path, kind: ReferenceArtifactKind) -> ReferenceArtifact | None:
    if not path.is_file():
        return None
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(chunk)
    return ReferenceArtifact(
        kind=kind,
        path=path,
        size_bytes=path.stat().st_size,
        sha256=checksum.hexdigest(),
    )


def _sampled_sheet_indices(count: int, limit: int) -> tuple[int, ...]:
    """시트 목록에서 인라인에 남길 자리를 고른다.

    앞에서 limit 장을 자르지 않는다. 시트는 타일 읽는 차례(왼쪽 위 → 오른쪽
    아래)대로 묶여 있어서, 앞만 남기면 덮는 자리가 페이지 위쪽에 쏠린다. 첫
    장과 끝 장을 붙들고 그 사이를 고르게 나눠 뽑아, 남긴 몇 장이 페이지 전체에
    퍼지게 한다.
    """
    if count <= limit:
        return tuple(range(count))
    # 여기까지 왔으면 count > limit 이라 count 는 1장 이상이다.
    if limit <= 1:
        return (0,) if limit == 1 else ()
    last = count - 1
    return tuple(
        sorted({round(index * last / (limit - 1)) for index in range(limit)})
    )


def _compact_artifacts(
    analysis: ReferenceImageAnalysis,
) -> tuple[tuple[ReferenceArtifact, ...], int]:
    """인라인 산출물과, 상한에 밀려 빠진 컨택트시트 수를 함께 낸다.

    빠진 수를 함께 내는 것이 이 함수의 계약이다. 개수만 줄이고 말없이 넘기면
    응답은 "시트가 이것뿐" 이라고 거짓말하게 된다 — 부르는 쪽이 이 값으로
    누락 신고를 세운다.
    """
    result_path = analysis.overlay_path.parent / "result.json"
    sheets = analysis.contact_sheet_paths
    picked = frozenset(
        _sampled_sheet_indices(len(sheets), _CONTACT_SHEET_INLINE_BUDGET)
    )
    candidates: list[tuple[Path, ReferenceArtifactKind]] = [
        (result_path, "analysis"),
        (analysis.overlay_path, "overlay"),
    ]
    candidates.extend(
        (path, "contact_sheet")
        for index, path in enumerate(sheets)
        if index in picked
    )
    artifacts = tuple(
        artifact
        for path, kind in candidates
        if (artifact := _artifact(path, kind)) is not None
    )
    # 신고하는 수는 "section 이 돌려줄 텐데 여기 없는 시트" 다. 파일이 사라진
    # 시트는 section(_all_artifacts) 도 내지 않으므로 세지 않는다 — 세면
    # 집을 수 없는 것을 집으라고 보내는 셈이다.
    omitted = sum(
        path.is_file() for index, path in enumerate(sheets) if index not in picked
    )
    return artifacts, omitted


def _stopped_reason_codes(analysis: ReferenceImageAnalysis) -> tuple[str, ...]:
    """조기 종료 사유를 잰 값만으로 말한다.

    여기에 TEXTURE_DOMINANT 를 함께 달던 시절이 있었다. 사전 게이트가 시간을
    많이 쓸 것 같다고 예측했다는 사실만으로 "이 그림은 질감이 지배한다"고
    단정한 것인데, 그 단정을 받쳐 줄 관측이 게이트 안에 없다. 게이트가 실제로
    재는 값(후보 선 수·표본 스캔 시간·외삽 선분 수)을 8종 픽스처에서 재 보면
    방향이 오히려 뒤집힌다 — 후보 선 하나당 선분 수가 잔모자이크·사진 같은
    질감에서 0.00~0.86 인데, 선화인 1400x1000 빗금은 24.73 이고 2400x3300
    글줄은 2.40 이다. 후보 선 밀도는 여덟 종 모두 0.85~1.00 으로 붙어 있어
    아무것도 가르지 못한다. 질감 지배를 가리키는 관측이 없으므로 그 코드를
    붙이지 않는다: 예측이 마감을 넘겼다는 사실만 중립적으로 낸다.
    """
    codes = [
        "ANALYSIS_STOPPED_EARLY",
        "READ_IMAGE_DIRECTLY",
        # 이 응답의 analysis_id 는 캐시에 없어 다시 집을 수 없다. 그 id 를 쓰기
        # 호출에 붙이면 거절당하고, 같은 분석을 되풀이해도 같은 id 로 다시 조기
        # 종료할 뿐이다 — 여기서 끊는다.
        "ANALYSIS_ID_NOT_REUSABLE",
    ]
    if analysis.stop_reason == "predicted_over_deadline":
        codes.append("PREDICTED_OVER_DEADLINE")
    elif analysis.stop_reason == "time_budget_exhausted":
        codes.append("TIME_BUDGET_EXHAUSTED")
    codes.append("FULL_SCAN_AVAILABLE_ON_REQUEST")
    return tuple(codes)


def _build_compact_reference_image_analysis(
    analysis: ReferenceImageAnalysis,
) -> CompactReferenceImageAnalysis:
    dominant, _, structure = _dominant_component(analysis)
    rows, columns = _breakpoints(analysis, dominant)
    coherent = (
        structure.horizontal_segments >= 3
        and structure.vertical_segments >= 3
        and structure.orthogonal_intersections
        >= max(
            4,
            min(
                structure.horizontal_segments,
                structure.vertical_segments,
            ),
        )
    )
    dominant_grid = (
        coherent
        and structure.dominant_segment_ratio >= 0.55
        and structure.dominant_grid_area_ratio >= 0.35
        and (
            structure.horizontal_segments + structure.vertical_segments >= 10
            or structure.partial_edge_ratio >= 0.2
            or structure.repeated_style_regions >= 2
            or structure.grid_fixed_text_regions >= 6
        )
    )
    reasons: list[str] = []
    if dominant_grid:
        reasons.append("DOMINANT_ORTHOGONAL_GRID")
        if structure.partial_edge_ratio >= 0.2:
            reasons.append("DENSE_PARTIAL_EDGES")
        if structure.repeated_style_regions >= 2:
            reasons.append("REPEATED_STYLE_REGIONS")
        if structure.grid_fixed_text_regions >= 6:
            reasons.append("GRID_FIXED_TEXT_REGIONS")
    else:
        if structure.dominant_grid_area_ratio < 0.35:
            reasons.append("GRID_DOES_NOT_DOMINATE_PAGE")
        if analysis.objects or analysis.text_regions:
            reasons.append("INDEPENDENT_PAGE_REGIONS")
        if not coherent:
            reasons.append("LOW_ORTHOGONAL_CONNECTIVITY")
    mode: ReferenceExecutionMode = (
        "reference_layout_bulk" if dominant_grid else "ordinary_layout"
    )
    draft = _draft_layout(analysis, dominant, rows, columns) if dominant_grid else None
    result_path = analysis.overlay_path.parent / "result.json"
    if not analysis.analysis_complete:
        # 미완 분석은 캐시에 없다. 집을 수 없는 경로와 산출물을 광고하지 않고,
        # 사유 코드로 모델 직접 판독을 권한다.
        mode = "ordinary_layout"
        reasons = list(_stopped_reason_codes(analysis))
        draft = None
        result_path = None
    artifacts, omitted_sheets = _compact_artifacts(analysis)
    # 솎아 낸 시트를 응답이 스스로 신고한다. 총수는 counts.contact_sheets 가,
    # 실린 수는 artifacts 가, 나머지 수와 되받을 section 은 이 신고가 말한다.
    #
    # 이 자리가 미완 분석 갈림길 뒤인 것은 사유 코드가 거기서 통째로 갈리기
    # 때문이다. 미완 분석은 contact_sheet_paths 가 언제나 비어 있어(분석기가
    # 시트를 만들기 전에 끊는다) 여기서 코드가 덧붙는 일 자체가 없다.
    sheet_omissions: tuple[ReferenceCompactOmission, ...] = ()
    if omitted_sheets:
        sheet_omissions = (
            ReferenceCompactOmission(
                section="artifacts",
                axis="artifacts",
                omitted_count=omitted_sheets,
            ),
        )
        reasons.extend((_CONTACT_SHEET_SAMPLED_CODE, _CONTACT_SHEET_NAMING_CODE))
    return CompactReferenceImageAnalysis(
        analysis_id=analysis.analysis_id,
        analyzer_version=analysis.analyzer_version,
        source_image=analysis.source_image,
        source_hash=analysis.image_hash,
        source_size_bytes=analysis.source_image.stat().st_size,
        image_width=analysis.image_width,
        image_height=analysis.image_height,
        analysis_time_ms=analysis.analysis_time_ms,
        cache_hit=analysis.cache_hit,
        counts=ReferenceAnalysisCounts(
            objects=len(analysis.objects),
            text_regions=len(analysis.text_regions),
            protected_gaps=len(analysis.protected_gaps),
            visible_segments=len(analysis.visible_segments),
            breakpoint_candidates=len(analysis.breakpoint_candidates),
            tiles=len(analysis.tiles),
            contact_sheets=len(analysis.contact_sheet_paths),
        ),
        structure=structure,
        recommended_execution_mode=mode,
        reason_codes=tuple(reasons),
        analysis_complete=analysis.analysis_complete,
        analysis_id_reusable=analysis.analysis_complete,
        stop_reason=analysis.stop_reason,
        predicted_seconds=analysis.predicted_seconds,
        time_budget_seconds=analysis.time_budget_seconds,
        row_breakpoints=rows,
        column_breakpoints=columns,
        draft_reference_layout=draft,
        detail_sections=(
            "draft_reference_layout",
            "objects",
            "text_regions",
            "protected_gaps",
            "visible_segments",
            "breakpoint_candidates",
            "tiles",
            "artifacts",
        ),
        analysis_result_path=result_path,
        artifacts=artifacts,
        omissions=sheet_omissions,
    )


def _payload_tally(result: CompactReferenceImageAnalysis) -> tuple[int, int, int]:
    """이 응답이 실제로 나르는 것의 수 — 증거 항목·산출물·되받을 경로.

    누락 신고는 세지 않는다. 신고는 "여기 더 있다" 는 말이지 그 자체가 관측이
    아니어서, 신고를 늘리고 관측을 줄인 응답을 나아졌다고 셈하면 안 된다.
    """
    evidence = sum(
        len(cast(tuple[ContractModel, ...], getattr(result.evidence, axis)))
        for axis, _, _ in _EVIDENCE_PRIORITY
    )
    paths = sum(
        item is not None for item in (result.source_image, result.analysis_result_path)
    )
    return evidence, len(result.artifacts), paths


def compact_reference_image_analysis(
    analysis: ReferenceImageAnalysis,
) -> CompactReferenceImageAnalysis:
    built = _build_compact_reference_image_analysis(analysis)
    bounded = enforce_compact_response_budget(fill_compact_evidence(built, analysis))
    dropped_draft = (
        built.draft_reference_layout is not None
        and bounded.draft_reference_layout is None
    )
    if not dropped_draft:
        return bounded
    # 초안이 예산에 밀려 통째로 내려갔다. 채우기는 "초안이 예산의 임자" 라며
    # 비켜섰는데 그 임자가 사라졌으니, 임자 없는 예산을 그대로 비워 보내면
    # 응답에 좌표가 한 줄도 없다 — 실측에서 조밀한 격자 페이지가 objects 254·
    # text_regions 89 를 재 놓고 2,537바이트짜리 껍데기만 냈고, omissions 에는
    # draft 한 줄뿐이라 소비자는 되집을 축이 있다는 사실조차 알지 못했다.
    #
    # 그래서 내려간 뒤에 다시 채운다. 미리 초안 크기를 재서 갈림길을 앞당기는
    # 길도 있지만, 그러려면 "예산을 넘는가" 를 여기서 한 번 더 판정해야 하고 그
    # 판정은 enforce 가 이미 가진 것이다. 두 벌로 두면 어긋난다.
    #
    # 되채운 응답에는 초안 누락 신고가 그대로 남아 있고(bounded.omissions 를
    # 물려받는다), 새로 못 실은 축은 채우기가 제 몫대로 덧붙인다. 마지막
    # enforce 는 채우기가 예산을 넘겼을 때만 일하지만, 넘기지 않는다는 보장을
    # 이 자리에서 다시 세워 둔다.
    refilled = enforce_compact_response_budget(fill_compact_evidence(bounded, analysis))
    # 되채우기가 언제나 남는 장사는 아니다. 초안을 내린 골격이 이미 예산의
    # 코앞이면(실측: 컨택트시트 24장이 깊은 경로에 놓인 페이지가 8,608토큰)
    # 남은 자리가 항목 하나 값도 못 되는데, 채우기는 그래도 축마다 누락 신고를
    # 덧붙여 예산을 넘기고, 그 초과를 enforce 가 산출물 경로를 통째로 내려
    # 메운다 — 증거 0·산출물 0 짜리 1,918토큰 응답이 나와서 되채우기 전보다
    # 오히려 적게 실린다(실측 3구간: 8,608/24 → 1,918/0, 8,859/24 → 1,918/0,
    # 8,652/22 → 1,918/0).
    #
    # 그래서 되채우기는 제안이지 확정이 아니다. 증거를 한 항목도 싣지 못했거나
    # 나르던 것을 하나라도 줄였으면 제안을 버리고 골격을 그대로 낸다. 이 비교는
    # 값이 세 개짜리 튜플 둘을 견주는 것이라 값싸고, "되채우기는 절대 손해가
    # 아니다" 를 계약으로 만든다.
    before = _payload_tally(bounded)
    after = _payload_tally(refilled)
    if after[0] == 0 or any(now < was for now, was in zip(after, before, strict=True)):
        return bounded
    return refilled


def _load_analysis_by_id(
    analysis_id: str,
    artifact_root: Path | None,
) -> ReferenceImageAnalysis:
    if re.fullmatch(r"ria-[0-9a-f]{16}", analysis_id) is None:
        raise ValueError("analysis_id is invalid")
    root = (artifact_root or default_artifact_root()).resolve()
    result_path = (root / analysis_id / "result.json").resolve()
    if result_path.parent.parent != root or not result_path.is_file():
        raise ValueError("reference image analysis is not cached")
    analysis = ReferenceImageAnalysis.model_validate_json(
        result_path.read_text(encoding="utf-8")
    )
    if analysis.analysis_id != analysis_id:
        raise ValueError("reference image analysis cache identity is invalid")
    return analysis.model_copy(update={"cache_hit": True})


def _all_artifacts(
    analysis: ReferenceImageAnalysis,
) -> tuple[ReferenceArtifact, ...]:
    candidates: list[tuple[Path, ReferenceArtifactKind]] = [
        (analysis.overlay_path.parent / "result.json", "analysis"),
        (analysis.overlay_path, "overlay"),
    ]
    candidates.extend((path, "contact_sheet") for path in analysis.contact_sheet_paths)
    candidates.extend((item.path, "tile") for item in analysis.tiles)
    candidates.extend((item.crop_path, "text_crop") for item in analysis.text_regions)
    return tuple(
        artifact
        for path, kind in candidates
        if (artifact := _artifact(path, kind)) is not None
    )


def _detail_models(
    analysis: ReferenceImageAnalysis,
    section: ReferenceDetailSection,
) -> tuple[ContractModel, ...]:
    if section == "objects":
        return analysis.objects
    if section == "text_regions":
        return analysis.text_regions
    if section == "protected_gaps":
        return analysis.protected_gaps
    if section == "visible_segments":
        return analysis.visible_segments
    if section == "breakpoint_candidates":
        return analysis.breakpoint_candidates
    if section == "tiles":
        return analysis.tiles
    raise ValueError("artifacts are not contract-model detail items")


def reference_image_analysis_section(
    analysis_id: str,
    section: ReferenceDetailSection,
    *,
    artifact_root: Path | None = None,
    offset: int = 0,
    limit: int = 100,
) -> ReferenceAnalysisDetail:
    if offset < 0:
        raise ValueError("offset must be non-negative")
    if limit < 1 or limit > 200:
        raise ValueError("limit must be between 1 and 200")
    analysis = _load_analysis_by_id(analysis_id, artifact_root)
    if section == "draft_reference_layout":
        draft = _build_compact_reference_image_analysis(
            analysis
        ).draft_reference_layout
        values = (
            ()
            if draft is None
            else (cast(JsonValue, draft.model_dump(mode="json")),)
        )
    elif section == "artifacts":
        values = tuple(
            cast(JsonValue, item.model_dump(mode="json"))
            for item in _all_artifacts(analysis)
        )
    else:
        values = tuple(
            cast(JsonValue, item.model_dump(mode="json"))
            for item in _detail_models(analysis, section)
        )
    items = values[offset : offset + limit]
    next_offset = offset + len(items)
    return ReferenceAnalysisDetail(
        analysis_id=analysis_id,
        section=section,
        offset=offset,
        limit=limit,
        total=len(values),
        next_offset=next_offset if next_offset < len(values) else None,
        items=items,
    )
