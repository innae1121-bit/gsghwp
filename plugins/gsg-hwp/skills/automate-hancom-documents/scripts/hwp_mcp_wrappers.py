from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Final, Literal, NamedTuple, Protocol, TypeVar, cast
from uuid import uuid4

from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel

from hwp_document_convention_contract import (
    ConventionAxis,
    ConventionSection,
    ConventionVariant,
    DocumentConventionProfile,
)
from hwp_errors import HwpLiveError
from hwp_live_contract import DocumentStyleList
from hwp_live_grounding import HwpGroundingReport, reorder_grounding_payload
from hwp_live_structure_contract import (
    DocumentStructure,
    PageParagraph,
    StructurePosition,
    StructureTable,
)
from hwp_mcp_result_envelope import normalize_production_result, transport_error_result
from hwp_operation_contract import (
    HwpOperateGuards,
    HwpOperateInputs,
    OperationResult,
    canonical_workflow,
)


class HwpOperateDispatcher(Protocol):
    async def hwp_operate(
        self,
        intent: str,
        inputs: HwpOperateInputs | None = None,
        guards: HwpOperateGuards | None = None,
    ) -> OperationResult: ...


StructuredPayload = TypeVar("StructuredPayload", bound=BaseModel)
STRUCTURE_RESPONSE_UTF8_LIMIT: Final = 32_000
_STRUCTURE_TEXT_FIELD_LIMIT: Final = 1_024
_StructureResponseSection = Literal[
    "page_text_characters",
    "paragraph_text_characters",
    "cell_text_characters",
    "cell_formats",
    "paragraphs",
    "cells",
    "controls",
    "tables",
    "unsupported_records",
]


class _StructureResponseTruncation(NamedTuple):
    original_utf8_bytes: int
    omissions: tuple[tuple[_StructureResponseSection, int], ...]


def new_wrapper_request_id() -> str:
    return f"hwp-wrapper-{uuid4().hex}"


def _compact_utf8_bytes(payload: BaseModel) -> int:
    serialized = payload.model_dump_json(by_alias=True, exclude_none=True)
    return len(serialized.encode("utf-8"))


def _add_omission(
    omissions: dict[_StructureResponseSection, int],
    section: _StructureResponseSection,
    count: int,
) -> None:
    if count > 0:
        omissions[section] = omissions.get(section, 0) + count


def _replace_cell_text(
    structure: DocumentStructure,
    limit: int,
) -> DocumentStructure:
    tables: list[StructureTable] = []
    for table in structure.tables:
        cells = tuple(
            cell.model_copy(update={"text": cell.text[:limit]}) for cell in table.cells
        )
        tables.append(table.model_copy(update={"cells": cells}))
    return structure.model_copy(update={"tables": tuple(tables)})


def _replace_paragraph_text(
    structure: DocumentStructure,
    targets: tuple[PageParagraph, ...],
) -> DocumentStructure:
    target_set = frozenset(targets)
    paragraphs = tuple(
        paragraph.model_copy(
            update={"text": paragraph.text[:_STRUCTURE_TEXT_FIELD_LIMIT]}
        )
        if paragraph in target_set
        else paragraph
        for paragraph in structure.paragraphs
    )
    return structure.model_copy(update={"paragraphs": paragraphs})


def _reduce_structure_response(
    structure: DocumentStructure,
    omissions: dict[_StructureResponseSection, int],
    protected_positions: frozenset[StructurePosition],
) -> DocumentStructure:
    cell_format_count = sum(len(table.cell_formats) for table in structure.tables)
    if cell_format_count:
        _add_omission(omissions, "cell_formats", cell_format_count)
        tables = tuple(
            table.model_copy(update={"cell_formats": ()}) for table in structure.tables
        )
        return structure.model_copy(update={"tables": tables})
    if structure.page_text:
        _add_omission(omissions, "page_text_characters", len(structure.page_text))
        return structure.model_copy(update={"page_text": ""})

    cell_characters = sum(
        max(0, len(cell.text) - _STRUCTURE_TEXT_FIELD_LIMIT)
        for table in structure.tables
        for cell in table.cells
    )
    if cell_characters:
        _add_omission(omissions, "cell_text_characters", cell_characters)
        return _replace_cell_text(structure, _STRUCTURE_TEXT_FIELD_LIMIT)

    cell_characters = sum(
        len(cell.text) for table in structure.tables for cell in table.cells
    )
    if cell_characters:
        _add_omission(omissions, "cell_text_characters", cell_characters)
        return _replace_cell_text(structure, 0)

    unprotected_paragraphs = tuple(
        paragraph
        for paragraph in structure.paragraphs
        if paragraph.position not in protected_positions
    )
    paragraph_characters = sum(
        max(0, len(paragraph.text) - _STRUCTURE_TEXT_FIELD_LIMIT)
        for paragraph in unprotected_paragraphs
    )
    if paragraph_characters:
        _add_omission(omissions, "paragraph_text_characters", paragraph_characters)
        return _replace_paragraph_text(structure, unprotected_paragraphs)

    if unprotected_paragraphs:
        kept_count = len(unprotected_paragraphs) // 2
        kept_unprotected = frozenset(unprotected_paragraphs[:kept_count])
        kept_paragraphs = tuple(
            paragraph
            for paragraph in structure.paragraphs
            if paragraph.position in protected_positions
            or paragraph in kept_unprotected
        )
        omitted_count = len(unprotected_paragraphs) - kept_count
        _add_omission(omissions, "paragraphs", omitted_count)
        return structure.model_copy(update={"paragraphs": kept_paragraphs})

    if structure.controls:
        kept_count = len(structure.controls) // 2
        _add_omission(omissions, "controls", len(structure.controls) - kept_count)
        return structure.model_copy(
            update={"controls": structure.controls[:kept_count]}
        )

    if structure.unsupported_records:
        kept_count = len(structure.unsupported_records) // 2
        omitted_count = len(structure.unsupported_records) - kept_count
        _add_omission(omissions, "unsupported_records", omitted_count)
        kept = structure.unsupported_records[:kept_count]
        return structure.model_copy(update={"unsupported_records": kept})

    if any(table.cells for table in structure.tables):
        shortened_tables: list[StructureTable] = []
        omitted_cells = 0
        for table in structure.tables:
            kept_count = len(table.cells) // 2
            omitted_cells += len(table.cells) - kept_count
            shortened_tables.append(
                table.model_copy(update={"cells": table.cells[:kept_count]})
            )
        _add_omission(omissions, "cells", omitted_cells)
        return structure.model_copy(update={"tables": tuple(shortened_tables)})

    if structure.tables:
        kept_count = len(structure.tables) // 2
        _add_omission(omissions, "tables", len(structure.tables) - kept_count)
        return structure.model_copy(update={"tables": structure.tables[:kept_count]})

    paragraph_characters = sum(
        max(0, len(paragraph.text) - _STRUCTURE_TEXT_FIELD_LIMIT)
        for paragraph in structure.paragraphs
    )
    if paragraph_characters:
        _add_omission(omissions, "paragraph_text_characters", paragraph_characters)
        return _replace_paragraph_text(structure, structure.paragraphs)

    if structure.paragraphs:
        kept_count = len(structure.paragraphs) // 2
        _add_omission(omissions, "paragraphs", len(structure.paragraphs) - kept_count)
        kept = structure.paragraphs[:kept_count]
        return structure.model_copy(update={"paragraphs": kept})
    return structure


def _bounded_structure_response(
    structure: DocumentStructure,
) -> tuple[DocumentStructure, _StructureResponseTruncation | None]:
    original_utf8_bytes = _compact_utf8_bytes(structure)
    if original_utf8_bytes <= STRUCTURE_RESPONSE_UTF8_LIMIT:
        return structure, None
    bounded = structure
    omissions: dict[_StructureResponseSection, int] = {}
    protected_positions = frozenset(
        position
        for table in structure.tables
        for position in (table.preceding_paragraph, table.following_paragraph)
        if position is not None
    )
    while _compact_utf8_bytes(bounded) > STRUCTURE_RESPONSE_UTF8_LIMIT:
        reduced = _reduce_structure_response(bounded, omissions, protected_positions)
        if reduced is bounded:
            break
        bounded = reduced
    return bounded, _StructureResponseTruncation(
        original_utf8_bytes=original_utf8_bytes,
        omissions=tuple(omissions.items()),
    )


def _response_truncation_fact(truncation: _StructureResponseTruncation) -> str:
    omitted = ",".join(f"{section}:{count}" for section, count in truncation.omissions)
    return " ".join(
        (
            "truncated=true",
            f"original_utf8_bytes={truncation.original_utf8_bytes}",
            f"limit_utf8_bytes={STRUCTURE_RESPONSE_UTF8_LIMIT}",
            "reason=utf8_byte_budget",
            f"omissions={omitted}",
        )
    )


# ── 관례(convention) 응답 예산 ────────────────────────────────────────────────
#
# hwp_list_styles(DocumentStyleList)와 hwp_ground_document(HwpGroundingReport)
# 는 문서 관례 프로필을 통째로 싣는데, 위의 DocumentStructure 바운딩이 이 둘을
# 지나쳤다. 실측(test.hwp의 라이브 관례 캐시): 프로필 하나가 48,087바이트이고
# 그 가운데 38,913바이트가 variants 목록이다(축 32개, variants 315개, 그 안의
# sample_paragraphs 만 4,631바이트). 반면 축의 뼈대 — name/state/scope/
# sample_count/consistency/selected_value/write_fields — 는 32축을 다 합쳐도
# 8,454바이트뿐이다. 즉 부피는 전부 목록에 있고, 판단에 필요한 값은 그 8KB에
# 있다.
#
# 호출자 하네스는 도구 응답을 토큰으로 자르고 무엇을 잘랐는지 말하지 않는다.
# 그래서 상한은 바이트가 아니라 토큰이고, 사라지는 자리는 응답의 꼬리다.
# 아래 두 상수는 이 저장소가 같은 하네스를 상대로 이미 실측해 정착시킨 값이다
# (hwp_reference_image_summary._RESPONSE_TOKEN_BUDGET/_BYTES_PER_TOKEN, 잘린
# 5회의 근거가 그 모듈 주석에 있다). 나눗수는 관측된 최저 밀도(2.31 B/tok)보다
# 낮아, 바이트 어림이 언제나 실제 토큰을 과대평가한다 — 예산을 지키는 쪽으로
# 틀린다.
CONVENTION_RESPONSE_TOKEN_BUDGET: Final = 9_000
CONVENTION_RESPONSE_BYTES_PER_TOKEN: Final = 2.1
CONVENTION_RESPONSE_UTF8_LIMIT: Final = round(
    CONVENTION_RESPONSE_TOKEN_BUDGET * CONVENTION_RESPONSE_BYTES_PER_TOKEN
)
# 누락 신고 자체도 응답에 실리므로 그 몫을 예산에서 빼야 한다. 다만 상수로
# 잡지 않고 신고문을 지어 재고 다시 줄인다(`_bounded_convention_response`).
# 이 값은 그 되풀이의 상한이다. 예약은 되풀이마다 반드시 커지므로(같거나
# 작아지면 그 자리에서 끝난다) 정수 상한 아래에서 반드시 멈춘다. 실측 수렴은
# 2~4회였고(33축 실환경 규모가 4회: 1,333→1,421→1,466→고정), 6은 그 위의
# 여유다. 넘기면 `_maximal_report_overhead` 로 물러난다 — 답은 그래도 예산 안이다.
_CONVENTION_REPORT_PASSES: Final = 6


class _ConventionOmission(NamedTuple):
    label: str
    omitted: int


class _ConventionResponseTruncation(NamedTuple):
    original_utf8_bytes: int
    carried_variants: int
    total_variants: int
    omissions: tuple[_ConventionOmission, ...]


class _ConventionPlan(NamedTuple):
    """축 바깥에서 내줄 수 있는 것들의 다이얼.

    축 자체는 어느 다이얼로도 사라지지 않는다. 줄어드는 것은 축이 가진
    **목록**(variants, sample_paragraphs)과 스타일 관측 목록뿐이다. variants 는
    이 다이얼이 아니라 축별 몫(`_admitted_variants`)이 정한다.
    """

    keep_sample_paragraphs: bool
    keep_lead_samples: bool
    observed_styles: int
    defined_styles: int


def _convention_profile(styles: DocumentStyleList) -> DocumentConventionProfile | None:
    observed = styles.observed_usage
    return None if observed is None else observed.convention_profile


def _profile_sections(
    profile: DocumentConventionProfile,
) -> tuple[tuple[str, ConventionSection], ...]:
    """프로필이 실제로 가진 관례 절을 이름과 함께 낸다.

    절 이름을 박아 두지 않고 모델에서 읽는다 — 절이 늘어도 새 절의 축이 조용히
    바운딩 밖에 남는 일이 없어야 한다.
    """
    sections: list[tuple[str, ConventionSection]] = []
    for name in type(profile).model_fields:
        value = cast(object, getattr(profile, name))
        if isinstance(value, ConventionSection):
            sections.append((name, value))
    return tuple(sections)


def _profile_axes(
    profile: DocumentConventionProfile,
) -> tuple[tuple[str, ConventionAxis], ...]:
    return tuple(
        (f"{section_name}.{axis.name}", axis)
        for section_name, section in _profile_sections(profile)
        for axis in section.axes
    )


def _trimmed_variant(
    variant: ConventionVariant,
    *,
    keep_sample_paragraphs: bool,
) -> ConventionVariant:
    if keep_sample_paragraphs:
        return variant
    return variant.model_copy(update={"sample_paragraphs": ()})


def _reduced_profile(
    profile: DocumentConventionProfile,
    *,
    quota: Mapping[str, int],
    keep_sample_paragraphs: bool,
) -> DocumentConventionProfile:
    """축은 하나도 버리지 않고 variants 목록만 축별 몫까지 줄인다.

    variants 는 ratio 내림차순이라 뒤에서 잘리는 것은 표본이 가장 적은 이형이다.
    axis 의 state/selected_value/consistency/sample_count/unobserved_reason 은
    이 함수가 만지지 않으므로, 예산이 아무리 빠듯해도 모든 축이 그대로 실린다.
    """
    updates: dict[str, ConventionSection] = {}
    for section_name, section in _profile_sections(profile):
        axes = tuple(
            axis.model_copy(
                update={
                    "variants": tuple(
                        _trimmed_variant(
                            variant,
                            keep_sample_paragraphs=keep_sample_paragraphs,
                        )
                        for variant in axis.variants[
                            : quota.get(f"{section_name}.{axis.name}", 0)
                        ]
                    )
                }
            )
            for axis in section.axes
        )
        updates[section_name] = section.model_copy(update={"axes": axes})
    return profile.model_copy(update=updates)


def _variant_costs(
    profile: DocumentConventionProfile,
    *,
    keep_sample_paragraphs: bool,
) -> dict[str, tuple[int, ...]]:
    """variant 하나가 응답에 더하는 바이트를 미리 잰다.

    쉼표 한 글자를 늘 더해 두는 것은 목록의 첫 항목에는 쉼표가 붙지 않기
    때문이다 — 어림이 실제보다 축마다 1바이트씩 크게 나오므로, 이 값으로 짠
    몫은 실제로도 반드시 예산 안에 들어간다.
    """
    return {
        label: tuple(
            _compact_utf8_bytes(
                _trimmed_variant(
                    variant,
                    keep_sample_paragraphs=keep_sample_paragraphs,
                )
            )
            + 1
            for variant in axis.variants
        )
        for label, axis in _profile_axes(profile)
    }


def _admitted_variants(
    costs: Mapping[str, tuple[int, ...]],
    budget: int,
) -> dict[str, int]:
    """남은 예산을 축들에 순위별로 돌려 가며 나눠 준다.

    같은 상한을 모든 축에 거는 방식은 절벽을 만든다: 축이 33개면 "축마다
    하나씩"만 해도 5KB가 들고, 그 5KB가 없으면 곧바로 0개가 된다(실측: 예산을
    조금만 넘겨도 315개가 통째로 사라졌다). 그래서 상한 대신 바이트로 나눈다 —
    모든 축의 1순위를 먼저 태우고, 남으면 2순위를 태우는 식이라 예산이 줄면
    실리는 수가 매끄럽게 줄어든다.

    한 축이 제 순위에서 자리를 못 얻으면 그 축은 닫는다. 축마다 실리는 것이
    언제나 ratio 상위 몇 개의 **연속된 앞부분**이어야 하기 때문이다.

    한 순위 안에서는 **싼 축부터** 태운다. 선언 순서대로 돌면 앞 섹션의 비싼
    축이 예산을 먼저 먹어 뒤 섹션의 싼 축이 통째로 굶었다(실측: leftover
    2,037바이트에서 object_layout 의 992/361/308바이트가 앞서 먹는 바람에
    61~79바이트짜리 paragraph_layout 여덟 축과 prose_style 두 축이 0을 받았고,
    비어 있지 않은 27축 가운데 13축이 굶었다). 값싼 축을 먼저 태우면 같은
    예산으로 더 많은 축이 제 이형을 하나씩이라도 갖는다. 순위 안에서 비용이
    같으면 축 이름으로 갈라 결과를 결정적으로 만든다.
    """
    kept = {label: 0 for label in costs}
    remaining = budget
    closed: set[str] = set()
    depth = max((len(items) for items in costs.values()), default=0)
    for rank in range(depth):
        ranked = sorted(
            (
                (items[rank], label)
                for label, items in costs.items()
                if label not in closed and rank < len(items)
            ),
        )
        for cost, label in ranked:
            # 오름차순이라 하나가 안 들어가면 뒤도 전부 안 들어간다. 그 축들은
            # 여기서 닫혀 다음 순위를 넘보지 않는다.
            if cost <= remaining:
                remaining -= cost
                kept[label] += 1
            else:
                closed.add(label)
        if len(closed) == len(costs):
            break
    return kept


def _apply_convention_plan(
    styles: DocumentStyleList,
    plan: _ConventionPlan,
    quota: Mapping[str, int],
) -> DocumentStyleList:
    reduced = styles
    profile = _convention_profile(reduced)
    observed = reduced.observed_usage
    if observed is not None:
        updates: dict[str, object] = {
            "styles": tuple(
                entry
                if plan.keep_lead_samples
                else entry.model_copy(update={"lead_samples": ()})
                for entry in observed.styles[: plan.observed_styles]
            )
        }
        if profile is not None:
            updates["convention_profile"] = _reduced_profile(
                profile,
                quota=quota,
                keep_sample_paragraphs=plan.keep_sample_paragraphs,
            )
        reduced = reduced.model_copy(
            update={"observed_usage": observed.model_copy(update=updates)}
        )
    if plan.defined_styles < len(reduced.styles):
        reduced = reduced.model_copy(
            update={"styles": reduced.styles[: plan.defined_styles]}
        )
    return reduced


def _largest_fitting(
    make: Callable[[int], _ConventionPlan],
    fits: Callable[[_ConventionPlan], bool],
    ceiling: int,
) -> int | None:
    """예산에 드는 가장 큰 개수. 0으로도 안 들어가면 None.

    개수가 줄면 응답도 줄어 단조롭기 때문에 이분 탐색이 답을 준다.
    """
    if not fits(make(0)):
        return None
    low, high = 0, ceiling
    while low < high:
        middle = (low + high + 1) // 2
        if fits(make(middle)):
            low = middle
        else:
            high = middle - 1
    return low


def _fitted_level(
    styles: DocumentStyleList,
    plan: _ConventionPlan,
    measure: Callable[[DocumentStyleList], int],
    limit: int,
) -> _ConventionPlan | None:
    """variants 를 다 비운 '바닥'이 예산에 드는 첫 단계를 찾는다.

    내주는 순서는 정보 값이 낮은 쪽부터다. lead_samples 는 같은 스타일의
    shared_lead_prefix/lead_marker 가 이미 접어 말하고 있는 원문 표본이라 먼저
    내준다. 관측 스타일 목록과 정의된 스타일 목록은 마지막이다.

    앞의 둘은 켜고 끄는 스위치라 그대로 재고, 뒤의 두 목록은 **개수**여서
    이분 탐색으로 예산에 드는 가장 큰 수를 고른다. 반씩 접으면 수십 바이트가
    모자란 응답이 목록의 절반을 잃는다(실측: 예산을 45바이트 넘긴 응답이
    18,840바이트에서 9,613바이트로 반토막 났다).
    """

    def fits(candidate: _ConventionPlan) -> bool:
        return measure(_apply_convention_plan(styles, candidate, {})) <= limit

    for switched in (plan, plan._replace(keep_lead_samples=False)):
        if fits(switched):
            return switched
    level = plan._replace(keep_lead_samples=False)
    observed = _largest_fitting(
        lambda count: level._replace(observed_styles=count),
        fits,
        level.observed_styles,
    )
    if observed is not None:
        return level._replace(observed_styles=observed)
    exhausted = level._replace(observed_styles=0)
    defined = _largest_fitting(
        lambda count: exhausted._replace(defined_styles=count),
        fits,
        exhausted.defined_styles,
    )
    if defined is not None:
        return exhausted._replace(defined_styles=defined)
    return None


def _bounded_convention_styles(
    styles: DocumentStyleList,
    measure: Callable[[DocumentStyleList], int],
    limit: int,
) -> DocumentStyleList:
    """부피가 큰 목록부터 줄여 예산에 맞춘다. 축은 하나도 버리지 않는다.

    ``measure`` 는 이 스타일 목록이 들어간 **응답 전체**를 직렬화해 재는 함수다.
    어림 상수로 나눈 값이 아니라 실제 바이트를 보고 멈춘다.

    맨 먼저 내주는 것은 variants 안의 sample_paragraphs 다(실측 4,631바이트,
    variants 부피의 12%). 문단 번호를 가리키는 자취일 뿐이고, 이걸 비우면 그
    만큼 variants 가 더 살아남는다.

    그 다음은 **축의 뼈대만 남긴 바닥**이 예산에 드는 단계를 찾고, 거기서 남은
    자리를 축들에 나눠 variants 를 도로 태운다. 그래서 어느 단계에서 멈추든 그
    단계가 허락하는 만큼 최대로 실린다.
    """
    profile = _convention_profile(styles)
    observed = styles.observed_usage
    plan = _ConventionPlan(
        keep_sample_paragraphs=False,
        keep_lead_samples=True,
        observed_styles=0 if observed is None else len(observed.styles),
        defined_styles=len(styles.styles),
    )
    costs = (
        {} if profile is None else _variant_costs(profile, keep_sample_paragraphs=False)
    )
    level = _fitted_level(styles, plan, measure, limit)
    if level is None:
        # 축의 뼈대만으로 예산을 넘긴 것이다. 축을 버리지는 않는다 — 넘겼다는
        # 사실은 누락 신고와 original_utf8_bytes 가 정직하게 말한다.
        return _apply_convention_plan(
            styles,
            plan._replace(
                keep_lead_samples=False,
                observed_styles=0,
                defined_styles=0,
            ),
            {},
        )
    floor = _apply_convention_plan(styles, level, {})
    candidate = _apply_convention_plan(
        styles,
        level,
        _admitted_variants(costs, limit - measure(floor)),
    )
    # 몫은 축마다 1바이트씩 크게 잡혀 있어 여기서 넘칠 수 없다. 그래도 재서
    # 확인하고, 어긋나면 바닥으로 물러난다 — 예산은 어림이 아니라 실측이 지킨다.
    return candidate if measure(candidate) <= limit else floor


def _convention_omissions(
    original: DocumentStyleList,
    bounded: DocumentStyleList,
) -> tuple[_ConventionOmission, ...]:
    """실린 것과 원본을 맞대어 누락을 센다.

    사다리가 무엇을 했는지 되짚지 않고 결과만 비교하므로, 신고한 수의 합은
    정의상 "원본 개수 − 실린 개수"와 같다.
    """
    omissions: list[_ConventionOmission] = []
    before = _convention_profile(original)
    after = _convention_profile(bounded)
    if before is not None and after is not None:
        carried_axes = dict(_profile_axes(after))
        dropped_samples = 0
        for label, axis in _profile_axes(before):
            carried = carried_axes.get(label)
            carried_variants = () if carried is None else carried.variants
            dropped = len(axis.variants) - len(carried_variants)
            if dropped > 0:
                omissions.append(_ConventionOmission(f"{label}/variants", dropped))
            # 실린 variant 에서만 센다. 통째로 빠진 variant 의 표본은 위의
            # variants 누락이 이미 말하고 있어 두 번 세면 합이 어긋난다.
            dropped_samples += sum(
                len(variant.sample_paragraphs)
                for variant in axis.variants[: len(carried_variants)]
            ) - sum(len(variant.sample_paragraphs) for variant in carried_variants)
        if dropped_samples > 0:
            omissions.append(
                _ConventionOmission("variant_sample_paragraphs", dropped_samples)
            )
    before_observed = original.observed_usage
    after_observed = bounded.observed_usage
    if before_observed is not None and after_observed is not None:
        dropped = len(before_observed.styles) - len(after_observed.styles)
        if dropped > 0:
            omissions.append(_ConventionOmission("observed_styles", dropped))
        dropped_leads = sum(
            len(entry.lead_samples)
            for entry in before_observed.styles[: len(after_observed.styles)]
        ) - sum(len(entry.lead_samples) for entry in after_observed.styles)
        if dropped_leads > 0:
            omissions.append(_ConventionOmission("style_lead_samples", dropped_leads))
    dropped = len(original.styles) - len(bounded.styles)
    if dropped > 0:
        omissions.append(_ConventionOmission("defined_styles", dropped))
    return tuple(omissions)


def _carried_variants(styles: DocumentStyleList) -> int:
    profile = _convention_profile(styles)
    if profile is None:
        return 0
    return sum(len(axis.variants) for _, axis in _profile_axes(profile))


def _convention_truncation(
    original: DocumentStyleList,
    bounded: DocumentStyleList,
    original_utf8_bytes: int,
) -> _ConventionResponseTruncation:
    return _ConventionResponseTruncation(
        original_utf8_bytes=original_utf8_bytes,
        carried_variants=_carried_variants(bounded),
        total_variants=_carried_variants(original),
        omissions=_convention_omissions(original, bounded),
    )


def _convention_truncation_fact(truncation: _ConventionResponseTruncation) -> str:
    omitted = ",".join(f"{item.label}:{item.omitted}" for item in truncation.omissions)
    carried = f"{truncation.carried_variants}/{truncation.total_variants}"
    return " ".join(
        (
            "convention_truncated=true",
            f"original_utf8_bytes={truncation.original_utf8_bytes}",
            f"limit_utf8_bytes={CONVENTION_RESPONSE_UTF8_LIMIT}",
            f"budget_tokens={CONVENTION_RESPONSE_TOKEN_BUDGET}",
            "reason=convention_token_budget",
            # 0/315 이면 예산이 축의 뼈대(state/selected_value/consistency/
            # sample_count)만 감당했다는 뜻이다.
            f"variants_carried={carried}",
            f"omissions={omitted}",
        )
    )


def _envelope_utf8_bytes(summary: str) -> int:
    """structuredContent 를 뺀 CallToolResult 껍데기의 바이트.

    하니스가 세는 것은 payload 가 아니라 전선에 나가는 CallToolResult 전체다.
    껍데기는 고정 76바이트에 요약문 길이가 붙는데, 요약문은 짧지 않다 —
    hwp_list_styles 는 최신 보증을 못 한 읽기에 그 사유를 요약문에 실어
    225바이트까지 간다(실측). 그래서 어림 상수로 두지 않고 실제로 직렬화해
    잰다.

    빈 dict 의 `{}` 두 글자를 payload 쪽과 두 번 세게 되므로 이 값은 실제보다
    2바이트 크다 — 예산을 지키는 쪽으로 틀린다.
    """
    return len(
        CallToolResult(
            content=[TextContent(type="text", text=summary)],
            structuredContent={},
        )
        .model_dump_json(by_alias=True, exclude_none=True)
        .encode("utf-8")
    )


def _report_overhead(summary: str, fact: str, envelope: int) -> int:
    """신고문이 껍데기에 더하는 바이트.

    길이를 어림하지 않고 요약문에 실제로 붙여 직렬화한다 — 축 이름에 따옴표
    같은 글자가 있으면 JSON 이 늘려 적기 때문이다.
    """
    return _envelope_utf8_bytes(f"{summary} {fact}") - envelope


def _maximal_report_overhead(
    styles: DocumentStyleList,
    summary: str,
    original_utf8_bytes: int,
) -> int:
    """이 응답이 지을 수 있는 가장 긴 신고문의 몫.

    항목마다 따로 최댓값을 잡아 모아 만든다: 축은 제 variants 를 통째로 잃는
    것이 최대이고, 표본·스타일 항목도 마찬가지다. 실제 신고문은 언제나 이
    항목들의 부분집합에 더 작은 수를 적으므로 이보다 짧다. 아래 되풀이가
    멈추지 않을 때 물러설 자리이며, 이 값을 쓰면 예산 준수가 보장된다.
    """
    profile = _convention_profile(styles)
    observed = styles.observed_usage
    omissions: list[_ConventionOmission] = []
    if profile is not None:
        omissions.extend(
            _ConventionOmission(f"{label}/variants", len(axis.variants))
            for label, axis in _profile_axes(profile)
            if axis.variants
        )
        omissions.append(
            _ConventionOmission(
                "variant_sample_paragraphs",
                sum(
                    len(variant.sample_paragraphs)
                    for _, axis in _profile_axes(profile)
                    for variant in axis.variants
                ),
            )
        )
    if observed is not None:
        omissions.append(_ConventionOmission("observed_styles", len(observed.styles)))
        omissions.append(
            _ConventionOmission(
                "style_lead_samples",
                sum(len(entry.lead_samples) for entry in observed.styles),
            )
        )
    omissions.append(_ConventionOmission("defined_styles", len(styles.styles)))
    total = _carried_variants(styles)
    fact = _convention_truncation_fact(
        _ConventionResponseTruncation(
            original_utf8_bytes=original_utf8_bytes,
            # variants_carried 는 두 수가 같을 때 가장 길게 적힌다.
            carried_variants=total,
            total_variants=total,
            omissions=tuple(omissions),
        )
    )
    return _report_overhead(summary, fact, _envelope_utf8_bytes(summary))


def _bounded_convention_response[Payload: BaseModel](
    payload: Payload,
    styles: DocumentStyleList,
    rebuild: Callable[[DocumentStyleList], Payload],
    summary: str,
) -> tuple[Payload, _ConventionResponseTruncation | None]:
    """예산 안으로 줄이고, 무엇을 뺐는지 정직하게 신고한다.

    예산이 재는 것은 payload 가 아니라 전선에 나가는 CallToolResult 전체다.
    무손질 분기와 절단 분기가 같은 저울을 쓴다 — 예전에는 무손질 분기만 껍데기
    몫을 0으로 두어, payload 18,840바이트가 그대로 통과한 뒤 전선에서
    18,945~19,141바이트가 되어 예산을 넘겼다(실측).

    신고문 몫도 상수로 잡아 두지 않는다. 상수는 양쪽으로 틀렸다 — 축이 많으면
    모자라고 축이 없으면 남았다(실측: 2,048 상수가 소형 프로필에서
    1,708~1,836바이트, 예산의 9~10%를 놀렸다). 그래서 예약하지 않고 **재 본다**:
    한 번 줄여 신고문을 실제로 지어 재고, 그 길이를 예약으로 삼아 다시 줄인다.
    `_envelope_utf8_bytes` 가 요약문을 직렬화해 재는 것과 같은 원리다.

    다시 줄이면 더 잘려 신고문이 길어질 수 있어 한 번에 멈추지 않는다. 예약은
    되풀이마다 커지기만 하고(단조) `_maximal_report_overhead` 라는 상한이
    있으므로 반드시 멈춘다. 실측에서는 두 번이면 끝난다.
    """
    envelope = _envelope_utf8_bytes(summary)
    original_utf8_bytes = _compact_utf8_bytes(payload)
    if original_utf8_bytes + envelope <= CONVENTION_RESPONSE_UTF8_LIMIT:
        return payload, None

    def measure(candidate: DocumentStyleList) -> int:
        return _compact_utf8_bytes(rebuild(candidate))

    def bound(reserve: int) -> tuple[DocumentStyleList, _ConventionResponseTruncation]:
        reduced = _bounded_convention_styles(
            styles,
            measure,
            CONVENTION_RESPONSE_UTF8_LIMIT - envelope - reserve,
        )
        return reduced, _convention_truncation(styles, reduced, original_utf8_bytes)

    reserve = 0
    for _ in range(_CONVENTION_REPORT_PASSES):
        bounded, truncation = bound(reserve)
        needed = _report_overhead(
            summary,
            _convention_truncation_fact(truncation),
            envelope,
        )
        # 신고문이 예약 안에 들어왔다 — payload 는 예산에서 예약을 뺀 자리에
        # 맞춰져 있으므로 둘을 더해도 예산을 넘지 않는다.
        if needed <= reserve:
            return rebuild(bounded), truncation
        reserve = needed
    bounded, truncation = bound(
        _maximal_report_overhead(styles, summary, original_utf8_bytes)
    )
    return rebuild(bounded), truncation


def _bounded_style_list_response(
    payload: DocumentStyleList,
    summary: str,
) -> tuple[DocumentStyleList, _ConventionResponseTruncation | None]:
    return _bounded_convention_response(
        payload,
        payload,
        lambda styles: styles,
        summary,
    )


def _bounded_grounding_response(
    payload: HwpGroundingReport,
    summary: str,
) -> tuple[HwpGroundingReport, _ConventionResponseTruncation | None]:
    evidence = payload.conventions

    # 보고서의 나머지 절(provenance/geometry/clean_renders/overlays)은 줄이지
    # 않는다. 그 절들이 이 호출의 목적물이고, 부피는 conventions 에 있다.
    # 예산은 보고서 **전체**를 직렬화해 재므로 그 몫이 자동으로 빠진다.
    def rebuild(styles: DocumentStyleList) -> HwpGroundingReport:
        return payload.model_copy(
            update={"conventions": evidence.model_copy(update={"styles": styles})}
        )

    return _bounded_convention_response(payload, evidence.styles, rebuild, summary)


def compact_structured_result(
    payload: StructuredPayload,
    *,
    summary: str,
) -> StructuredPayload:
    bounded_payload: BaseModel = payload
    truncation: _StructureResponseTruncation | None = None
    convention_truncation: _ConventionResponseTruncation | None = None
    if isinstance(payload, DocumentStructure):
        bounded_payload, truncation = _bounded_structure_response(payload)
    elif isinstance(payload, DocumentStyleList):
        bounded_payload, convention_truncation = _bounded_style_list_response(
            payload,
            summary,
        )
    elif isinstance(payload, HwpGroundingReport):
        bounded_payload, convention_truncation = _bounded_grounding_response(
            payload,
            summary,
        )
    facts: list[str] = []
    if isinstance(payload, DocumentStructure) and payload.unsupported_records:
        facts.append(f"unsupported={len(payload.unsupported_records)}")
    if truncation is not None:
        facts.append(_response_truncation_fact(truncation))
    if convention_truncation is not None:
        facts.append(_convention_truncation_fact(convention_truncation))
    reported_summary = " ".join((summary, *facts))
    structured = bounded_payload.model_dump(
        mode="json",
        by_alias=True,
        exclude_none=True,
    )
    if isinstance(bounded_payload, HwpGroundingReport):
        # 하니스가 꼬리를 자르므로, 48KB급 conventions 를 마지막에 놓아 작은
        # 목적 필드(clean_renders 등)가 먼저 잘리지 않게 한다.
        structured = reorder_grounding_payload(structured)
    return cast(
        StructuredPayload,
        CallToolResult(
            content=[TextContent(type="text", text=reported_summary)],
            structuredContent=structured,
        ),
    )


async def dispatch_wrapper(
    operation: HwpOperateDispatcher,
    inputs: HwpOperateInputs,
) -> OperationResult:
    workflow = canonical_workflow(inputs)
    assert workflow is not None
    try:
        result = await operation.hwp_operate(workflow, inputs)
    except HwpLiveError as error:
        return transport_error_result(inputs, error)
    return normalize_production_result(result, inputs)
