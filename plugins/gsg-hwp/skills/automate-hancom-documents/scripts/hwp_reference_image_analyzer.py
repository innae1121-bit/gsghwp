from __future__ import annotations

import hashlib
import math
import os
import tempfile
import time
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from hwp_reference_image_artifacts import (
    ARTIFACT_IMAGE_SUFFIX,
    save_contact_sheets,
    save_overlay,
    save_tiles,
)
from hwp_reference_image_compile import (
    compile_breakpoints,
    compile_gaps,
    compile_objects,
    compile_segments,
    compile_texts,
)
from hwp_reference_image_budget import (
    DEFAULT_ANALYSIS_TIME_BUDGET_SECONDS,
    ReferenceAnalysisTimeBudget,
    ReferenceScanEstimate,
)
from hwp_reference_image_cache import (
    DEFAULT_REFERENCE_IMAGE_LIMITS,
    ReferenceImageResourceLimits,
    acquire_reference_image_lock,
    directory_size,
    prune_reference_image_cache,
    remove_reference_cache_directory,
)
from hwp_reference_image_contract import ReferenceImageAnalysis
from hwp_reference_image_gaps import detect_protected_gaps
from hwp_reference_image_objects import detect_objects
from hwp_reference_image_pixels import PixelCanvas
from hwp_reference_image_segment_gaps import exclude_gap_crossing_segments
from hwp_reference_image_segments import detect_visible_segments, estimate_scan_cost
from hwp_reference_image_text import detect_text_regions


# 1.2.0: 너무 높은 행 띠를 통째로 버리던 텍스트 검출이 열 조각별 재훑기로
# 바뀌어 여러 단 지면의 캡션 줄을 되찾는다. 검출 출력이 달라지므로 판을 올려
# 예전 캐시를 자연 무효화한다(분석 id 와 캐시 적중 판정 모두 이 값을 문다).
ANALYZER_VERSION = "1.2.0"


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def default_artifact_root() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir()))
    return base / "GSG_HWP_BETA" / "reference-image-analysis"


def _artifact_paths(analysis: ReferenceImageAnalysis) -> tuple[Path, ...]:
    return (
        analysis.overlay_path,
        *analysis.contact_sheet_paths,
        *(tile.path for tile in analysis.tiles),
        *(region.crop_path for region in analysis.text_regions),
    )


def _has_complete_artifacts(
    analysis: ReferenceImageAnalysis,
    directory: Path,
) -> bool:
    resolved_directory = directory.resolve()
    for path in _artifact_paths(analysis):
        try:
            resolved = path.resolve()
            if not resolved.is_relative_to(resolved_directory) or not resolved.is_file():
                return False
        except OSError:
            return False
    return True


def _load_complete_analysis(
    result_path: Path,
    *,
    source: Path,
    image_hash: str,
    analysis_id: str,
    resource_fingerprint: str | None,
) -> ReferenceImageAnalysis | None:
    try:
        cached = ReferenceImageAnalysis.model_validate_json(
            result_path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None
    if (
        cached.analysis_id != analysis_id
        or cached.analyzer_version != ANALYZER_VERSION
        or cached.image_hash != image_hash
        or cached.source_image.resolve() != source
        or (
            resource_fingerprint is not None
            and cached.resource_fingerprint != resource_fingerprint
        )
        or not _has_complete_artifacts(cached, result_path.parent)
    ):
        return None
    try:
        result_path.touch()
    except OSError:
        return None
    return cached.model_copy(update={"cache_hit": True})


def _source_metadata(
    source: Path,
    limits: ReferenceImageResourceLimits,
) -> tuple[int, int, int]:
    source_bytes = source.stat().st_size
    if source_bytes > limits.max_source_bytes:
        raise ValueError("reference image source byte limit exceeded")
    try:
        with Image.open(source) as image:
            width, height = image.size
    except (OSError, UnidentifiedImageError) as error:
        raise ValueError(f"reference image cannot be decoded: {source}") from error
    if width < 2 or height < 2:
        raise ValueError("reference image dimensions must be at least 2 by 2")
    if width * height > limits.max_source_pixels:
        raise ValueError("reference image source pixel limit exceeded")
    return source_bytes, width, height


def _analysis_size(
    width: int,
    height: int,
    max_pixels: int,
) -> tuple[int, int]:
    pixels = width * height
    if pixels <= max_pixels:
        return width, height
    scale = math.sqrt(max_pixels / pixels)
    target_width = max(2, math.floor(width * scale))
    target_height = max(2, math.floor(height * scale))
    while target_width * target_height > max_pixels:
        if target_width >= target_height:
            target_width -= 1
        else:
            target_height -= 1
    return target_width, target_height


def _load_canvas(
    source: Path,
    target_size: tuple[int, int],
) -> PixelCanvas:
    try:
        with Image.open(source) as image:
            if image.size != target_size:
                _ = image.draft("RGB", target_size)
                image.thumbnail(
                    target_size,
                    resample=Image.Resampling.LANCZOS,
                    reducing_gap=3,
                )
            return PixelCanvas.from_image(image)
    except (OSError, UnidentifiedImageError) as error:
        raise ValueError(f"reference image cannot be decoded: {source}") from error


def load_cached_reference_image_analysis(
    source_image: Path,
    analysis_id: str,
    *,
    artifact_root: Path | None = None,
) -> ReferenceImageAnalysis:
    source = source_image.expanduser().resolve()
    result_path = (
        (artifact_root or default_artifact_root()).resolve()
        / analysis_id
        / "result.json"
    )
    if not result_path.is_file():
        # 조기 종료한 분석은 캐시에 남기지 않으므로 여기로 온다. 예전 문구는
        # "먼저 hwp_analyze_reference_image 를 부르라"로 시작해서, 이미 부른
        # 호출자를 같은 호출로 되돌려 보냈다: 그 분석은 또 조기 종료하고, 같은
        # 재사용 불가 id 를 돌려주고, 쓰기 호출이 다시 여기서 거절된다 — 맴돌이다.
        # 그래서 되풀이가 답이 아니라는 사실을 먼저 말하고, 맴돌지 않는 두 출구를
        # 이름으로 준다.
        message = "; ".join(
            (
                "reference image analysis is not cached",
                "an analysis that stopped early is never cached, so its analysis_id"
                + " can never be loaded",
                "repeating the same analysis does not help: it stops early again and"
                + " returns the same unusable analysis_id",
                "omit analysis_id to proceed without pixel evidence (the plan is"
                + " marked degraded), or rerun that image with full_scan=True and"
                + " attach the analysis_id it returns to pin coordinates",
            )
        )
        raise ValueError(message)
    cached = _load_complete_analysis(
        result_path,
        source=source,
        image_hash=_file_hash(source),
        analysis_id=analysis_id,
        resource_fingerprint=None,
    )
    if cached is None:
        raise ValueError("reference image analysis cache is stale or incomplete")
    return cached


def _final_path(path: Path, temporary: Path, directory: Path) -> Path:
    return directory / path.relative_to(temporary)


def _stopped_analysis(
    source: Path,
    *,
    source_bytes: int,
    source_width: int,
    source_height: int,
    image_hash: str,
    analysis_id: str,
    resource_fingerprint: str,
    canvas: PixelCanvas,
    directory: Path,
    budget: ReferenceAnalysisTimeBudget,
    estimate: ReferenceScanEstimate | None,
    started: float,
) -> ReferenceImageAnalysis:
    """조기 종료를 오류가 아니라 미완 표시가 붙은 구조화 결과로 낸다.

    검출 묶음을 비워 두는 것은 정직함 때문이다 — 중간까지 훑은 선분은 이미지
    위쪽만 담고 있어서 완전한 좌표 증거처럼 쓰이면 틀린 배치를 만든다. 값이
    필요한 호출자는 full_scan 으로 오늘과 같은 완전 스캔을 고를 수 있다.
    """
    return ReferenceImageAnalysis(
        analysis_id=analysis_id,
        analyzer_version=ANALYZER_VERSION,
        image_hash=image_hash,
        source_image=source,
        image_width=source_width,
        image_height=source_height,
        source_bytes=source_bytes,
        analysis_width=canvas.width,
        analysis_height=canvas.height,
        analysis_downsampled=(canvas.width, canvas.height)
        != (source_width, source_height),
        resource_fingerprint=resource_fingerprint,
        analysis_time_ms=(time.perf_counter() - started) * 1000,
        cache_hit=False,
        analysis_complete=False,
        stop_reason=budget.stop_reason,
        predicted_seconds=None if estimate is None else estimate.predicted_seconds,
        time_budget_seconds=budget.seconds,
        objects=(),
        text_regions=(),
        protected_gaps=(),
        visible_segments=(),
        breakpoint_candidates=(),
        tiles=(),
        contact_sheet_paths=(),
        overlay_path=directory / f"evidence-overlay{ARTIFACT_IMAGE_SUFFIX}",
    )


def _analyze_into_directory(
    source: Path,
    *,
    source_bytes: int,
    source_width: int,
    source_height: int,
    image_hash: str,
    analysis_id: str,
    resource_fingerprint: str,
    temporary: Path,
    directory: Path,
    limits: ReferenceImageResourceLimits,
    started: float,
    budget: ReferenceAnalysisTimeBudget | None,
) -> ReferenceImageAnalysis:
    target_size = _analysis_size(
        source_width,
        source_height,
        limits.max_analysis_pixels,
    )
    crops_directory = temporary / "text-crops"
    crops_directory.mkdir(parents=True)
    canvas = _load_canvas(source, target_size)
    try:
        estimate: ReferenceScanEstimate | None = None

        def stopped(
            stopped_budget: ReferenceAnalysisTimeBudget,
        ) -> ReferenceImageAnalysis:
            return _stopped_analysis(
                source,
                source_bytes=source_bytes,
                source_width=source_width,
                source_height=source_height,
                image_hash=image_hash,
                analysis_id=analysis_id,
                resource_fingerprint=resource_fingerprint,
                canvas=canvas,
                directory=directory,
                budget=stopped_budget,
                estimate=estimate,
                started=started,
            )

        if budget is not None:
            # (a) 빠른 사전 게이트. 이 이미지에서 실제 스캔 코드를 표본만큼
            # 돌려 재고 전체를 외삽한다(자체 비용 실측 0.33~1.42초).
            estimate = estimate_scan_cost(canvas)
            if estimate.predicted_seconds > budget.seconds:
                budget.stop("predicted_over_deadline")
            if budget.stopped:
                return stopped(budget)
        # (b) 협조적 예산 백스톱. 예측이 빗나가 어느 지배 구간에서든 예산을 다
        # 썼으면 잘린 검출로 다음 단계를 돌리지 않고 그 자리에서 정직하게 끊는다.
        # 단계 경계마다 묻는 이유는 실측이다 — 스캔과 필터만 예산을 물었을 때
        # 전체 시간의 39~58%만 덮였고, 예산 4.0초짜리 호출이 벽시계 7.0초까지
        # 갔다(1400x1000 빗금). refine·detect_* 는 이제 자기 지배 루프 안에서도
        # 예산을 묻는다.
        segments = detect_visible_segments(canvas, budget=budget)
        if budget is not None and budget.stopped:
            return stopped(budget)
        pixel_objects = detect_objects(canvas, segments, budget=budget)
        if budget is not None and budget.stopped:
            return stopped(budget)
        pixel_gaps = detect_protected_gaps(canvas, pixel_objects, budget=budget)
        if budget is not None and budget.stopped:
            return stopped(budget)
        segments = exclude_gap_crossing_segments(segments, pixel_gaps)
        pixel_texts = detect_text_regions(canvas, segments, budget=budget)
        # (c) 마지막 체크. 이 뒤로는 계약 변환과 산출물 쓰기뿐이고, 그 고정비는
        # 8MP에서 0.28~0.74초로 실측됐다 — 쪼갤 이유가 없는 대신 잘린 검출을
        # 디스크에 남기지 않도록 여기서 한 번 더 묻는다.
        if budget is not None and budget.exhausted():
            return stopped(budget)
        objects = compile_objects(canvas, ANALYZER_VERSION, image_hash, pixel_objects)
        segment_contracts = compile_segments(
            canvas,
            ANALYZER_VERSION,
            image_hash,
            segments,
        )
        gaps = compile_gaps(
            canvas,
            ANALYZER_VERSION,
            image_hash,
            pixel_gaps,
            objects,
        )
        temporary_texts = compile_texts(
            canvas,
            ANALYZER_VERSION,
            image_hash,
            pixel_texts,
            directory=crops_directory,
        )
        temporary_tiles = save_tiles(canvas, directory=temporary)
        temporary_contact_sheets = save_contact_sheets(
            temporary_tiles,
            directory=temporary,
        )
        temporary_overlay = temporary / f"evidence-overlay{ARTIFACT_IMAGE_SUFFIX}"
        save_overlay(
            canvas,
            objects=pixel_objects,
            text_regions=pixel_texts,
            gaps=pixel_gaps,
            segments=segments,
            path=temporary_overlay,
        )
        texts = tuple(
            item.model_copy(
                update={
                    "crop_path": _final_path(
                        item.crop_path,
                        temporary,
                        directory,
                    )
                }
            )
            for item in temporary_texts
        )
        tiles = tuple(
            item.model_copy(
                update={"path": _final_path(item.path, temporary, directory)}
            )
            for item in temporary_tiles
        )
        contact_sheets = tuple(
            _final_path(path, temporary, directory)
            for path in temporary_contact_sheets
        )
        result = ReferenceImageAnalysis(
            analysis_id=analysis_id,
            analyzer_version=ANALYZER_VERSION,
            image_hash=image_hash,
            source_image=source,
            image_width=source_width,
            image_height=source_height,
            source_bytes=source_bytes,
            analysis_width=canvas.width,
            analysis_height=canvas.height,
            analysis_downsampled=(canvas.width, canvas.height)
            != (source_width, source_height),
            resource_fingerprint=resource_fingerprint,
            analysis_time_ms=(time.perf_counter() - started) * 1000,
            cache_hit=False,
            objects=objects,
            text_regions=texts,
            protected_gaps=gaps,
            visible_segments=segment_contracts,
            breakpoint_candidates=compile_breakpoints(
                analysis_id,
                objects,
                gaps,
                segment_contracts,
            ),
            tiles=tiles,
            contact_sheet_paths=contact_sheets,
            overlay_path=_final_path(
                temporary_overlay,
                temporary,
                directory,
            ),
        )
        temporary_result = temporary / "result.json.tmp"
        _ = temporary_result.write_text(
            result.model_dump_json(indent=2),
            encoding="utf-8",
        )
        _ = temporary_result.replace(temporary / "result.json")
        if directory_size(temporary) > limits.max_analysis_artifact_bytes:
            raise ValueError("reference image analysis artifact limit exceeded")
        return result
    finally:
        canvas.close()


def analyze_reference_image(
    source_image: Path,
    *,
    artifact_root: Path | None = None,
    limits: ReferenceImageResourceLimits = DEFAULT_REFERENCE_IMAGE_LIMITS,
    full_scan: bool = False,
    time_budget_seconds: float | None = None,
) -> ReferenceImageAnalysis:
    """레퍼런스 이미지를 분석한다.

    기본값은 시간 예산 안에서 돌고, 마감을 넘길 것이 확실하면 몇 초 안에
    미완 표시가 붙은 결과로 돌아온다. full_scan=True 는 예산과 사전 게이트를
    모두 끄고 오늘까지와 똑같은 완전 스캔을 돌린다 — 명시적 좌표 증거가 꼭
    필요한 호출자를 위한 문이며, 그 경우 240초 작업자 마감은 그대로 적용된다.
    """
    started = time.perf_counter()
    budget = (
        None
        if full_scan
        else ReferenceAnalysisTimeBudget(
            DEFAULT_ANALYSIS_TIME_BUDGET_SECONDS
            if time_budget_seconds is None
            else time_budget_seconds
        )
    )
    source = source_image.expanduser().resolve()
    if not source.is_file():
        raise ValueError(f"reference image does not exist: {source}")
    source_bytes, source_width, source_height = _source_metadata(source, limits)
    image_hash = _file_hash(source)
    resource_fingerprint = limits.analysis_fingerprint
    analysis_key = hashlib.sha256(
        (
            f"{ANALYZER_VERSION}|{image_hash}|{resource_fingerprint}"
        ).encode("ascii")
    ).hexdigest()
    analysis_id = f"ria-{analysis_key[:16]}"
    root = (artifact_root or default_artifact_root()).resolve()
    root.mkdir(parents=True, exist_ok=True)
    prune_reference_image_cache(root, limits)
    directory = root / analysis_id
    result_path = directory / "result.json"
    lock = acquire_reference_image_lock(root, analysis_id, limits)
    temporary: Path | None = None
    try:
        cached = _load_complete_analysis(
            result_path,
            source=source,
            image_hash=image_hash,
            analysis_id=analysis_id,
            resource_fingerprint=resource_fingerprint,
        )
        if cached is not None:
            result = cached
        else:
            if directory.exists():
                remove_reference_cache_directory(root, directory)
            temporary = Path(
                tempfile.mkdtemp(
                    prefix=f".partial-{analysis_id}-",
                    dir=root,
                )
            ).resolve()
            result = _analyze_into_directory(
                source,
                source_bytes=source_bytes,
                source_width=source_width,
                source_height=source_height,
                image_hash=image_hash,
                analysis_id=analysis_id,
                resource_fingerprint=resource_fingerprint,
                temporary=temporary,
                directory=directory,
                limits=limits,
                started=started,
                budget=budget,
            )
            # 미완 결과는 캐시에 남기지 않는다. 남기면 다음 호출과 g03 의
            # analysis_id 고정이 빈 증거를 완전한 것으로 착각한다.
            if result.analysis_complete:
                _ = temporary.replace(directory)
                temporary = None
    finally:
        if temporary is not None and temporary.exists():
            remove_reference_cache_directory(root, temporary)
        lock.close()
    prune_reference_image_cache(
        root,
        limits,
        protected_analysis_ids=frozenset((analysis_id,)),
    )
    return result
