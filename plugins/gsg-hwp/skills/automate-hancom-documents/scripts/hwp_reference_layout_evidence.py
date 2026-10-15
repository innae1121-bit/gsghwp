from __future__ import annotations

import hashlib
from pathlib import Path
from threading import Lock

from hwp_reference_image_analyzer import (
    default_artifact_root,
    load_cached_reference_image_analysis,
)
from hwp_reference_image_layout_bridge import align_reference_layout_to_analysis
from hwp_reference_layout_contract import ReferenceLayoutBlock
from hwp_reference_layout_image_analysis import analyze_layout_image
from hwp_reference_layout_ocr_evidence import supplement_text_anchors


# 한 번의 쓰기 작업이 같은 블록으로 prepare_reference_layout 을 두 번 부른다 —
# 사전검사(hwp_layout_preflight)와 실제 쓰기(hwp_live_native_layout)가 각각
# hwp_live_operation_recipe 에서 출발한다. 픽스처 실측으로 회당 486~1,241ms 라
# 두 번째 호출이 통째로 낭비였다. 계산은 (블록, 원본 이미지 파일 내용,
# analysis_id 가 있으면 그 분석 캐시)만의 순수 함수라, 프로세스 안에서만 사는
# 작은 메모를 둔다.
#
# 원본 이미지의 신원은 경로가 아니라 (resolve 한 경로, st_size, st_mtime_ns)라,
# 같은 경로에 다른 그림이 덮어써지면 다시 계산한다. 분석 캐시는 mtime 을 쓸 수
# 없어 내용 해시로 센다 — 이유는 _cache_identity 에 적었다. 파일을 볼 수 없으면
# 키를 만들지 않고 예전 경로 그대로 흘려보내므로, 없는 파일에 대한 오류 문구도
# 그대로 남는다.
REFERENCE_LAYOUT_MEMO_CAPACITY = 8
_MemoFile = tuple[str, int, int]
_MemoCache = tuple[str, int, str]
_MemoKey = tuple[str, _MemoFile, _MemoCache | None]
_MEMO: dict[_MemoKey, ReferenceLayoutBlock] = {}
# 쓰기 작업은 hwp_mcp_dispatch._DEFAULT_OPERATION_WORKERS = 8 로 동시에 들어온다.
# 잠금 없이 넣고 축출하면 삽입과 next(iter(...)) 가 겹쳐 "계산은 끝났는데 저장하다
# 죽는" 사고가 난다 — 검증 실측으로 GIL 전환 간격 1µs 에서 25,600회 중 2,004회가
# RuntimeError(dictionary changed size during iteration)/KeyError 였다. 이 락은
# dict 자체를 지키는 것이지 계산을 직렬화하는 것이 아니다: 무거운 계산은 반드시
# 락 밖에서 돌린다. 같은 블록이 동시에 들어오면 계산이 겹칠 수 있지만, 결과가
# 바이트까지 같은 순수 함수라 무해하다.
_MEMO_LOCK = Lock()


def _file_identity(path: Path) -> _MemoFile | None:
    try:
        resolved = path.expanduser().resolve()
        status = resolved.stat()
    except OSError:
        return None
    return str(resolved), status.st_size, status.st_mtime_ns


def _cache_identity(path: Path) -> _MemoCache | None:
    # 분석 캐시만은 mtime 을 못 쓴다: 캐시를 읽는 쪽
    # (hwp_reference_image_analyzer._load_complete_analysis)이 오래된 항목
    # 정리를 위해 읽을 때마다 result.json 을 touch 한다. 그래서 같은 내용인데도
    # 호출마다 mtime 이 달라져 메모가 절대 맞지 않았다(실측). 내용 해시로 센다 —
    # 실환경 캐시 29건이 419KB~2.06MB 였고 read+sha256 이 0.404~1.185ms
    # (중앙값 0.761ms)라, analysis_id 경로가 미스일 때 40.8ms 인 것에 견주면
    # 값을 치를 만하다(적중 1.87ms).
    try:
        resolved = path.expanduser().resolve()
        payload = resolved.read_bytes()
    except OSError:
        return None
    return str(resolved), len(payload), hashlib.sha256(payload).hexdigest()


def _memo_key(block: ReferenceLayoutBlock) -> _MemoKey | None:
    source = block.source_image
    if source is None:
        # 이미지가 없으면 supplement_text_anchors 만 도는 가벼운 경로다.
        return None
    image = _file_identity(source)
    if image is None:
        return None
    cached: _MemoCache | None = None
    if block.analysis_id is not None:
        # analysis_id 경로가 읽는 것은 load_cached_reference_image_analysis 가
        # 정하는 <artifact_root>/<analysis_id>/result.json 이다. artifact_root
        # 는 LOCALAPPDATA 에 달려 있어 프로세스 안에서도 바뀔 수 있으므로 키에
        # 함께 싣는다. result.json 이 가리키는 산출물 파일들까지는 다시 세지
        # 않는다 — 그 완전성 검사는 항목을 저장하던 계산이 이미 통과했고,
        # 메모는 한 프로세스 안에서만 산다.
        cached = _cache_identity(
            default_artifact_root() / block.analysis_id / "result.json"
        )
        if cached is None:
            return None
    return block.model_dump_json(), image, cached


def reset_reference_layout_memo() -> None:
    """메모를 비운다 — 같은 프로세스에서 계산 경로를 갈아 끼우는 시험용."""
    with _MEMO_LOCK:
        _MEMO.clear()


def prepare_reference_layout(block: ReferenceLayoutBlock) -> ReferenceLayoutBlock:
    key = _memo_key(block)
    if key is not None:
        with _MEMO_LOCK:
            memoized = _MEMO.get(key)
        if memoized is not None:
            # ReferenceLayoutBlock 은 ContractModel(frozen=True) 이고 필드가
            # 전부 스칼라·Path·튜플·같은 frozen 모델이라 트리 전체가 불변이다.
            # 그래서 저장한 객체를 그대로 돌려줘도 호출자끼리 상태를 나눠 갖지
            # 않는다 — 다시 검증해 새 객체를 만들 이유가 없다.
            return memoized
    prepared = _prepare_reference_layout(block)
    if key is not None:
        with _MEMO_LOCK:
            _MEMO[key] = prepared
            while len(_MEMO) > REFERENCE_LAYOUT_MEMO_CAPACITY:
                # dict 는 넣은 차례를 지키므로 맨 앞이 가장 오래된 항목이다.
                del _MEMO[next(iter(_MEMO))]
    return prepared


def _prepare_reference_layout(block: ReferenceLayoutBlock) -> ReferenceLayoutBlock:
    prepared = block
    if block.source_image is not None:
        if block.analysis_id is not None:
            analysis = load_cached_reference_image_analysis(
                block.source_image,
                block.analysis_id,
            )
            prepared = align_reference_layout_to_analysis(block, analysis)
        else:
            prepared = analyze_layout_image(prepared).layout
    return ReferenceLayoutBlock.model_validate(
        {
            **prepared.model_dump(),
            "text_anchors": supplement_text_anchors(prepared),
        }
    )
