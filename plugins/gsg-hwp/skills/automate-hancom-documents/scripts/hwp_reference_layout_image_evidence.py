from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from statistics import median

from PIL import Image, ImageOps, UnidentifiedImageError


@dataclass(frozen=True, slots=True)
class ProjectionLine:
    position: float
    strength: float


# detect_layout_lines_from_image 의 결과 모양 — (행 후보, 열 후보).
LayoutLines = tuple[tuple[ProjectionLine, ...], tuple[ProjectionLine, ...]]


def _otsu_threshold(histogram: list[int], total: int) -> int:
    weighted_sum = sum(index * count for index, count in enumerate(histogram))
    background_weight = 0
    background_sum = 0
    best_threshold = 127
    best_variance = -1.0
    for threshold, count in enumerate(histogram):
        background_weight += count
        if background_weight == 0:
            continue
        foreground_weight = total - background_weight
        if foreground_weight == 0:
            break
        background_sum += threshold * count
        background_mean = background_sum / background_weight
        foreground_mean = (weighted_sum - background_sum) / foreground_weight
        variance = (
            background_weight
            * foreground_weight
            * (background_mean - foreground_mean) ** 2
        )
        if variance > best_variance:
            best_variance = variance
            best_threshold = threshold
    return min(best_threshold, 220)


def _dark_projections(image: Image.Image) -> tuple[list[float], list[float]]:
    gray = ImageOps.autocontrast(image.convert("L"))
    width, height = gray.size
    if width < 2 or height < 2:
        raise ValueError("reference image must be at least 2×2 pixels")
    threshold = _otsu_threshold(gray.histogram(), width * height)
    dark = gray.point([255 if value <= threshold else 0 for value in range(256)])
    pixels = dark.load()
    if pixels is None:
        raise ValueError("reference image pixels are unavailable")
    column_runs = [0] * width
    column_longest = [0] * width
    rows: list[float] = []
    for y_position in range(height):
        row_run = 0
        row_longest = 0
        for x_position in range(width):
            if pixels[x_position, y_position] == 0:
                row_run = 0
                column_runs[x_position] = 0
                continue
            row_run += 1
            row_longest = max(row_longest, row_run)
            column_runs[x_position] += 1
            column_longest[x_position] = max(
                column_longest[x_position],
                column_runs[x_position],
            )
        rows.append(row_longest / width)
    columns = [run / height for run in column_longest]
    return columns, rows


def _load_dark_projections(path: Path) -> tuple[list[float], list[float]]:
    try:
        with Image.open(path) as opened:
            return _dark_projections(opened)
    except (OSError, UnidentifiedImageError) as error:
        raise ValueError(f"reference image cannot be decoded: {path}") from error


def _line_candidates(projection: list[float]) -> tuple[ProjectionLine, ...]:
    maximum = max(projection, default=0.0)
    if maximum <= 0:
        return ()
    baseline = median(projection)
    cutoff = max(0.06, baseline * 2.5, min(maximum * 0.48, 0.12))
    candidates: list[ProjectionLine] = []
    start: int | None = None
    for index, strength in enumerate((*projection, 0.0)):
        if strength >= cutoff and start is None:
            start = index
        if strength < cutoff and start is not None:
            end = index
            weights = projection[start:end]
            weight_sum = sum(weights)
            center = (
                sum((start + offset) * value for offset, value in enumerate(weights))
                / weight_sum
            )
            candidates.append(
                ProjectionLine(
                    position=center / max(1, len(projection) - 1),
                    strength=max(weights),
                )
            )
            start = None
    return tuple(candidates)


def detect_layout_lines(
    source_image: Path,
) -> tuple[tuple[ProjectionLine, ...], tuple[ProjectionLine, ...]]:
    if not source_image.is_file():
        raise ValueError(f"reference image does not exist: {source_image}")
    column_projection, row_projection = _load_dark_projections(source_image)
    return _line_candidates(row_projection), _line_candidates(column_projection)


def detect_layout_lines_from_image(
    source_image: Image.Image,
) -> tuple[tuple[ProjectionLine, ...], tuple[ProjectionLine, ...]]:
    column_projection, row_projection = _dark_projections(source_image)
    return _line_candidates(row_projection), _line_candidates(column_projection)


def _snap_breakpoints(
    breakpoints: tuple[float, ...],
    candidates: tuple[ProjectionLine, ...],
) -> tuple[float, ...]:
    if len(breakpoints) <= 2 or not candidates:
        return breakpoints
    snapped = list(breakpoints)
    for index in range(1, len(breakpoints) - 1):
        original = breakpoints[index]
        local_spacing = min(
            original - breakpoints[index - 1],
            breakpoints[index + 1] - original,
        )
        tolerance = min(0.06, local_spacing * 0.35)
        nearby = [
            candidate
            for candidate in candidates
            if abs(candidate.position - original) <= tolerance
        ]
        if not nearby:
            continue
        chosen = min(
            nearby,
            key=lambda candidate: (
                abs(candidate.position - original),
                -candidate.strength,
            ),
        )
        lower = snapped[index - 1] + 1e-6
        upper = breakpoints[index + 1] - 1e-6
        snapped[index] = min(upper, max(lower, chosen.position))
    return tuple(snapped)


def snap_layout_breakpoints(
    source_image: Path,
    row_breakpoints: tuple[float, ...],
    column_breakpoints: tuple[float, ...],
) -> tuple[tuple[float, ...], tuple[float, ...]]:
    row_lines, column_lines = detect_layout_lines(source_image)
    return (
        _snap_breakpoints(row_breakpoints, row_lines),
        _snap_breakpoints(column_breakpoints, column_lines),
    )


def snap_layout_breakpoints_from_image(
    source_image: Image.Image,
    row_breakpoints: tuple[float, ...],
    column_breakpoints: tuple[float, ...],
    layout_lines: LayoutLines | None = None,
) -> tuple[tuple[float, ...], tuple[float, ...]]:
    # layout_lines 는 detect_layout_lines_from_image 의 결과를 그대로 받는
    # 선택 인자다. 같은 이미지로 이 함수와 snap_column_gap_breakpoints 를
    # 잇달아 부르는 호출자(analyze_layout_image)가 _dark_projections 전 픽셀
    # 루프를 두 번 돌지 않게 한 번 계산한 값을 넘긴다. 생략하면 예전처럼
    # 스스로 계산하므로 기존 호출자의 관측은 바뀌지 않는다.
    row_lines, column_lines = (
        detect_layout_lines_from_image(source_image)
        if layout_lines is None
        else layout_lines
    )
    return (
        _snap_breakpoints(row_breakpoints, row_lines),
        _snap_breakpoints(column_breakpoints, column_lines),
    )
