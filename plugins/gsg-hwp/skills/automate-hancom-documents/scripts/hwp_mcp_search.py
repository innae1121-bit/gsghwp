"""Rank HWP tools from one declarative Korean intent catalog.

# noqa: SIZE_OK — alias data and its scorer are reviewed as one routing contract.
"""

from __future__ import annotations
import re
from collections.abc import Mapping, Sequence
from difflib import SequenceMatcher
from functools import lru_cache
from types import MappingProxyType
from typing import Final, Protocol, TypeVar


class SearchableTool(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def description(self) -> str: ...


SearchableToolT = TypeVar("SearchableToolT", bound=SearchableTool)

_SUFFIXES: Final = (
    "으로",
    "에서",
    "에게",
    "까지",
    "부터",
    "처럼",
    "보다",
    "은",
    "는",
    "이",
    "가",
    "을",
    "를",
    "와",
    "과",
    "로",
    "에",
    "의",
    "도",
    "만",
)
_VERB_SUFFIXES: Final = (
    "해주세요",
    "해줘요",
    "합니다",
    "하도록",
    "해줘",
    "해서",
    "하고",
    "하기",
    "했다",
    "해",
    "줘",
)
_LIVE_VERB_STEMS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "채워": "채우기",
        "바꿔": "바꾸기",
        "고쳐": "바꾸기",
        "넣어": "삽입",
    }
)
_NEGATION_MARKER: Final = re.compile(
    r"(?:하지\s*말고|하지\s*마(?:세요|라|요)?|지\s*말고|지\s*마(?:세요|라|요)?|"
    + r"하지\s*않고|지\s*않고|말고|제외(?:하고|해|하여)?|금지(?:하고|해|하여)?)"
)
_INTENT_ALIASES: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        "hwp_list_open_documents": (
            "열린 문서 목록",
            "열린 한글 문서",
            "열린 HWP",
            "문서 목록 확인",
        ),
        "hwp_open_document": (
            "문서 열기",
            "현재 문서로 잡아",
        ),
        "hwp_connect": ("활성 문서 연결", "한컴 문서 연결", "세션 연결"),
        "hwp_inspect": (
            "현재 쪽 확인",
            "커서와 선택 확인",
            "문서 상태 확인",
            "용지 모양 확인",
        ),
        "hwp_inspect_page_fast": (
            "빠른 구조 조회",
            "구조 조회",
            "현재 문서 구조",
            "셀 높이",
            "셀 간격",
            "그림 포함 여부",
            "렌더 말고",
        ),
        "hwp_find_tables": (
            "문서 전체 표 찾기",
            "사진 표 찾기",
            "그림 개수별 표 검색",
            "표 그림 inventory",
        ),
        "hwp_inspect_structure": (
            "상세 구조 조회",
            "캡션과 병합 셀",
            "문단 표 셀 병합 그림 캡션",
        ),
        "hwp_inspect_patch_plan": (
            "패치 대상 조회",
            "교체 대상 범위 목록",
            "본문 범위와 셀 대상 수집",
        ),
        "hwp_list_styles": (
            "문서 스타일 확인",
            "스타일 목록",
            "표타이틀 스타일",
            "그림타이틀 스타일",
        ),
        "hwp_fill_table": (
            "기존 표 채우기",
            "표 값 채우기",
            "표 데이터 입력",
            "셀 값 입력",
            "기존 셀 값 채워",
            "표 채워줘",
            "표 채워",
            "셀 채워줘",
            "값 채워줘",
        ),
        "hwp_expand_and_fill_table": (
            "행 추가",
            "행 늘려",
            "기존 표 행 늘려",
            "표 행 확장",
            "레코드 추가",
        ),
        "hwp_sync_visibility_analysis_tables": (
            "예비조망점 선정표",
            "가시권 분석표",
            "분석표 동기화",
            "사진 매칭",
            "가시권표 개수",
            "예비조망점표에 맞게",
            "가시권분석표 추가",
        ),
        "hwp_fill_table_images": ("표 안 그림", "표 셀 그림", "셀에 사진"),
        "hwp_repeat_table_template": (
            "표 양식 반복",
            "빈 표 복제",
            "같은 표 여러 개",
        ),
        "hwp_insert_image": (
            "새 그림 삽입",
            "이미지 추가",
            "사진 넣기",
            "문서 끝 그림",
            "그림 자르기",
            "영역 꽉 채우기",
        ),
        "hwp_replace_image": (
            "기존 그림 교체",
            "기존 이미지 교체",
            "그림 바꾸기",
            "사진 교체",
        ),
        "hwp_edit_picture": (
            "기존 그림 자르기",
            "사진 확대",
            "조망점 중심으로 자르기",
            "사업구역 중심으로 확대",
            "그림 다른 칸으로 옮기기",
            "사진 셀 이동",
            "사진 재배치",
        ),
        "hwp_add_caption": (
            "캡션 추가",
            "캡션 수정",
            "캡션 갱신",
            "표 제목",
            "그림 제목",
        ),
        "hwp_build_table_series": (
            "표 반복하고 값 그림 채우기",
            "데이터 포함 표 시리즈",
            "캡션 번호 증가 표 복제",
        ),
        "hwp_merge_table_cells": (
            "표 셀 병합",
            "셀 범위 병합",
            "셀을 하나로",
        ),
        "hwp_split_table_cell": (
            "표 셀 나누기",
            "셀 나누기",
            "셀을 두 칸",
        ),
        "hwp_delete_page": (
            "페이지 삭제",
            "쪽 삭제",
            "빈 페이지 제거",
            "빈 쪽 제거",
        ),
        "hwp_delete_control": (
            "개체 삭제",
            "컨트롤 삭제",
            "독립 표 삭제",
            "잘못 만든 표 제거",
        ),
        "hwp_undo": (
            "실행 취소",
            "되돌리기",
            "롤백",
            "방금 작업 취소",
        ),
        "hwp_redo": (
            "다시 실행",
            "재실행",
            "되살리기",
        ),
        "hwp_apply_style": ("문서 스타일 적용", "스타일 ID 적용"),
        "hwp_format_text": (
            "글자 서식",
            "문단 서식",
            "글꼴 크기 색상 정렬",
        ),
        "hwp_patch_text": (
            "텍스트 삽입",
            "텍스트 바꾸기",
            "현재 커서 텍스트 삽입",
            "글자 바꿔줘",
            "글 바꿔줘",
            "문장 바꿔줘",
            "텍스트 바꿔줘",
        ),
        "hwp_patch_text_batch": (
            "여러 곳 한 번에 바꾸기",
            "떨어진 범위 일괄 교체",
            "패치 묶음 실행",
        ),
        "hwp_replace_selected_text": (
            "드래그 선택 텍스트 변경",
            "선택 영역 글 교체",
            "선택한 문장 바꾸기",
            "선택한 글만 바꿔",
            "선택한 글 바꿔",
        ),
        "hwp_format_table": (
            "표 셀 서식",
            "셀 배경 테두리",
            "표 글자 정렬",
        ),
        "hwp_set_table_size_fixed": (
            "표 크기 고정",
            "표 개체 크기 고정",
            "크기고정",
            "ProtectSize",
        ),
        "hwp_analyze_reference_image": (
            "참고 이미지 분석",
            "전역 좌표 분석",
            "선분 끝점 분석",
            "보호 공백 분석",
        ),
        "hwp_prepare_image_crops": (
            "이미지 영역 자르기",
            "원본 개별 crop",
            "사진 지도 도면 로고 분리",
            "콘텐츠 경계 정밀 보정",
        ),
        "hwp_append_report": (
            "텍스트 보고서",
            "텍스트 표와 글",
            "텍스트 표와 글 보고서 재생성",
            "보고서 양식",
        ),
        "hwp_append_excel_table": (
            "엑셀",
            "Excel",
            "XLSX",
            "빈 바깥 칸",
            "병합 테두리 행 높이",
        ),
        "hwp_insert_current_format_content": (
            "현재 양식",
            "현재 서식",
            "한컴 현재 양식에 맞게",
            "글 사진 PPT 엑셀",
            "16:9 PPT",
            "이웃 문단 글자 모양",
            "목차 순서 번호 체계",
        ),
        "hwp_append_layout": (
            "문서 끝 레이아웃",
            "문단 표 그림 추가",
            "편집 가능한 블록 추가",
        ),
        "hwp_insert_layout": (
            "현재 커서 표 삽입",
            "현재 커서의 본문 중간에 글과 표를 추가",
            "본문 중간 표 삽입",
            "내용 중간 글 추가",
            "8쪽 다음 쪽 추가",
            "특정 페이지 다음 새 페이지",
            "이미지를 편집 가능한 표로",
            "이미지 보고 재생성",
            "그림처럼 페이지 재현",
            "표와 글 페이지 재생성",
            "표와 글 페이지 재현",
            "표와 글 보고서 재생성",
            "PPT 내용 재구성",
            "기존 양식 재구성",
        ),
        "hwp_save_reopen_verify": (
            "저장 후 다시 열기",
            "저장 재개방 검증",
        ),
        "hwp_save": (
            "문서 저장",
            "닫지 않고 저장",
            "일반 저장",
        ),
        "hwp_disconnect": ("문서 연결 해제", "세션 해제"),
        "hwp_get_capabilities": ("도구 기능 목록", "MCP 기능 확인"),
        "hwp_search_tools": ("도구 검색", "작업 도구 찾기"),
        "hwp_runtime_info": (
            "MCP 런타임 정보",
            "서버 PID 버전",
            "소스 스키마 해시",
        ),
        "hwp_execute": ("도구 이름으로 전달 실행", "새 도구 전달 호출"),
        "hwp_get_graph_manifest": (
            "문서 전수 그래프 감사",
            "전체 문서 그래프 추출",
        ),
        "hwp_list_custom_actions": (
            "툴바 목록",
            "리본 탭 목록",
            "등록한 단축 기능 목록",
            "한컴MCP 탭 확인",
        ),
        "hwp_get_custom_action": (
            "단축 기능 하나 확인",
            "툴바 버튼 정의 확인",
        ),
        "hwp_register_custom_action": (
            "툴바에 넣어줘",
            "리본 탭에 버튼 추가",
            "단축 기능 등록",
            "이 작업 버튼으로 만들어",
            "한컴MCP 탭에 추가",
        ),
        "hwp_update_custom_action": (
            "툴바 버튼 수정",
            "단축 기능 이름 바꿔",
            "버튼 동작 바꿔",
        ),
        "hwp_delete_custom_action": (
            "툴바 버튼 삭제",
            "단축 기능 지워",
        ),
        "hwp_reorder_custom_actions": (
            "툴바 버튼 순서",
            "버튼 순서 바꿔",
            "리본 버튼 재정렬",
        ),
        "hwp_remove_custom_action_tab": (
            "툴바 탭 제거",
            "한컴MCP 탭 없애",
            "리본 탭 지워",
        ),
    }
)

_GRAPH_AUDIT_TOOLS: Final = frozenset(
    (
        "hwp_get_graph_manifest",
        "hwp_query_graph",
        "hwp_get_graph_node",
        "hwp_get_graph_property",
        "hwp_get_graph_asset",
        "hwp_fetch_graph_artifact",
    )
)
_GRAPH_AUDIT_MARKERS: Final = frozenset(("그래프", "전수", "감사"))
_RECONSTRUCTION_ACTION_MARKERS: Final = frozenset(("재구성", "재현", "재생성"))
_RECONSTRUCTION_CONTEXT_MARKERS: Final = frozenset(
    (
        "이미지",
        "그림",
        "ppt",
        "슬라이드",
        "페이지",
        "쪽",
        "한컴",
        "hwp",
        "양식",
        "그래픽",
        "색상표",
        "사진",
        "지도",
        "도면",
    )
)
_GEOMETRY_EVIDENCE_MARKERS: Final = frozenset(
    ("좌표", "선분", "끝점", "앵커", "픽셀", "rgb")
)
_PROTECTED_GAP_MARKERS: Final = frozenset(("보호", "공백"))
_DOCUMENT_END_MARKERS: Final = frozenset(("끝", "마지막", "document_end"))


def _composite_intent_priority(query_terms: frozenset[str], name: str) -> int:
    explicit_geometry = bool(query_terms & _GEOMETRY_EVIDENCE_MARKERS) or (
        _PROTECTED_GAP_MARKERS <= query_terms
    )
    if name == "hwp_analyze_reference_image" and explicit_geometry:
        return 4
    reconstruction = bool(
        query_terms & _RECONSTRUCTION_ACTION_MARKERS
        and query_terms & _RECONSTRUCTION_CONTEXT_MARKERS
    )
    if not reconstruction:
        return 0
    document_end = "document_end" in query_terms or (
        "문서" in query_terms and bool(query_terms & _DOCUMENT_END_MARKERS)
    )
    final_layout_tool = "hwp_append_layout" if document_end else "hwp_insert_layout"
    if name == final_layout_tool:
        return 3
    if name == "hwp_analyze_reference_image":
        return 2
    if name == "hwp_prepare_image_crops":
        return 1
    return 0


# One ranking pass re-tokenizes the whole corpus: 211 intent aliases plus every
# spec's name and description, 363 fixed strings, on top of the query's own few.
# Measured over 10 representative queries the distinct argument count is 372 for
# both `_search_terms` and `_compact`, so 1024 keeps the fixed corpus resident
# permanently and still leaves ~660 slots of query history. Bound: ~1024 short
# strings and their frozensets, single-digit MB.
_TERM_CACHE_SIZE: Final = 1024
# `_similar_term` is the hot one -- 60,594 calls in a single `hwp_search_tools`
# against 6,819 distinct (query term, corpus term) pairs, so most calls repeat
# inside one query and the rest repeat across queries that share a term. Sizing
# was swept over a 20-query workload rather than guessed; five-query medians and
# residency at full capacity:
#   4096 -> 54.2ms / 57% hit / 1.5MiB     32768 -> 47.8ms / 67% hit /  6.7MiB
#   8192 -> 51.2ms / 60% hit / 2.1MiB     65536 -> 11.1ms / 85% hit / 12.7MiB
#  16384 -> 52.1ms / 63% hit / 3.7MiB    131072 -> 10.4ms / 93% hit / 17.4MiB
# The knee is 65536: it is where a whole working set stops being evicted between
# queries, and doubling past it buys 6% for another 37% of memory. Keys are
# pairs of strings the corpus and query already hold, so the residency above is
# almost entirely LRU link nodes.
_SIMILARITY_CACHE_SIZE: Final = 65536


# Pure: reads only the module-level suffix tuples and the frozen stem mapping,
# returns an immutable frozenset. Safe to memoize.
@lru_cache(maxsize=_TERM_CACHE_SIZE)
def _search_terms(value: str) -> frozenset[str]:
    terms: set[str] = set()
    raw_terms: list[str] = re.findall(r"[0-9a-zA-Z가-힣_]+", value.casefold())
    for raw in raw_terms:
        terms.add(raw)
        numbered_term = re.fullmatch(r"[0-9]+([a-zA-Z가-힣_]+)", raw)
        if numbered_term is not None:
            terms.add(numbered_term.group(1))
        for suffix in _SUFFIXES:
            if raw.endswith(suffix) and len(raw) > len(suffix):
                terms.add(raw[: -len(suffix)])
                break
        for suffix in _VERB_SUFFIXES:
            if raw.endswith(suffix) and len(raw) > len(suffix):
                terms.add(raw[: -len(suffix)])
                break
    stemmed = {_LIVE_VERB_STEMS[term] for term in terms if term in _LIVE_VERB_STEMS}
    terms.update(stemmed)
    return frozenset(term for term in terms if term)


@lru_cache(maxsize=_TERM_CACHE_SIZE)
def _compact(value: str) -> str:
    return "".join(re.findall(r"[0-9a-zA-Z가-힣_]+", value.casefold()))


@lru_cache(maxsize=_SIMILARITY_CACHE_SIZE)
def _similar_term(left: str, right: str) -> bool:
    if left == right:
        return True
    shortest = min(len(left), len(right))
    if shortest < 2:
        return False
    if left in right or right in left:
        return True
    if abs(len(left) - len(right)) > 1:
        return False
    if len(left) == len(right) == 2:
        return (
            left[0] == right[0]
            and sum(
                left_character != right_character
                for left_character, right_character in zip(left, right, strict=True)
            )
            == 1
        )
    threshold = 0.78
    return SequenceMatcher(None, left, right).ratio() >= threshold


def _matched_term_count(
    query_terms: frozenset[str],
    candidate_terms: frozenset[str],
) -> tuple[int, int]:
    exact = len(query_terms.intersection(candidate_terms))
    unmatched = query_terms.difference(candidate_terms)
    fuzzy = sum(
        any(_similar_term(term, candidate) for candidate in candidate_terms)
        for term in unmatched
    )
    return exact, fuzzy


def _intent_score(query: str, query_terms: frozenset[str], name: str) -> int:
    query_compact = _compact(query)
    scores = [0]
    for alias in _INTENT_ALIASES.get(name, ()):
        alias_terms = _search_terms(alias)
        exact, fuzzy = _matched_term_count(alias_terms, query_terms)
        matched = exact + fuzzy
        if _compact(alias) in query_compact:
            scores.append(40 + 5 * len(alias_terms))
        elif alias_terms and matched == len(alias_terms):
            scores.append(30 + 5 * exact + 3 * fuzzy)
        elif len(alias_terms) > 1 and matched * 3 >= len(alias_terms) * 2:
            scores.append(12 + 4 * exact + 2 * fuzzy)
    return max(scores)


def _negative_intent_score(query: str, name: str) -> int:
    terms = _search_terms(query)
    if not terms:
        return 0
    return _intent_score(query, terms, name)


def _split_query_polarity(query: str) -> tuple[str, tuple[str, ...]]:
    remaining = query
    negatives: list[str] = []
    while match := _NEGATION_MARKER.search(remaining):
        negative = remaining[: match.start()].strip(" ,.;")
        if negative:
            negatives.append(negative)
        remaining = remaining[match.end() :].strip(" ,.;")
    return remaining, tuple(negatives)


def _text_score(query_terms: frozenset[str], name: str, description: str) -> int:
    name_terms = _search_terms(name.replace("_", " "))
    description_terms = _search_terms(description)
    score = 0
    for term in query_terms:
        if term in name_terms or term in name:
            score += 10
        elif term in description_terms or term in description:
            score += 4
        elif any(_similar_term(term, candidate) for candidate in name_terms):
            score += 5
        elif any(_similar_term(term, candidate) for candidate in description_terms):
            score += 2
    return score


def rank_tool_specs(
    query: str,
    specs: Sequence[SearchableToolT],
    *,
    limit: int,
) -> tuple[SearchableToolT, ...]:
    positive_query, negative_queries = _split_query_polarity(query)
    terms = _search_terms(positive_query)
    if not terms:
        return ()
    ranked: list[tuple[int, int, SearchableToolT]] = []
    for spec in specs:
        name = spec.name.casefold()
        description = spec.description.casefold()
        exact_identity = _compact(query) in {_compact(name), _compact(description)}
        if not exact_identity and any(
            _negative_intent_score(negative, spec.name) >= 24
            for negative in negative_queries
        ):
            continue
        if exact_identity:
            score = 2_000
        elif _compact(positive_query) in {_compact(name), _compact(description)}:
            score = 1_000
        else:
            score = _intent_score(positive_query, terms, spec.name) + _text_score(
                terms,
                name,
                description,
            )
            if spec.name in _GRAPH_AUDIT_TOOLS and not (terms & _GRAPH_AUDIT_MARKERS):
                score = 0
        priority = 3 if exact_identity else _composite_intent_priority(terms, spec.name)
        if score or priority:
            ranked.append((priority, score, spec))
    ranked.sort(key=lambda item: (-item[0], -item[1], item[2].name))
    return tuple(spec for _, _, spec in ranked[:limit])
