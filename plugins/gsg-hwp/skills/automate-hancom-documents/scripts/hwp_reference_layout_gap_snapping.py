from __future__ import annotations

from PIL import Image

from hwp_reference_layout_contract import ReferenceLayoutBlock
from hwp_reference_layout_gap_pixels import (
    estimate_background_color,
    quantile,
)
from hwp_reference_layout_gap_snap_pixels import (
    refine_blank_run,
    strip_background_distance,
)
from hwp_reference_layout_gap_topology import (
    edge_covers,
    text_blocked_cells,
)
from hwp_reference_layout_image_evidence import (
    LayoutLines,
    ProjectionLine,
    detect_layout_lines_from_image,
)


def _candidate_pairs(
    candidates: tuple[ProjectionLine, ...],
) -> tuple[tuple[float, float], ...]:
    positions = sorted(candidate.position for candidate in candidates)
    return tuple(zip(positions, positions[1:], strict=False))


def preserve_occupied_narrow_columns(
    block: ReferenceLayoutBlock,
    column_breakpoints: tuple[float, ...],
) -> tuple[float, ...]:
    proposed = block.column_breakpoints
    widths = [
        right - left
        for left, right in zip(proposed, proposed[1:], strict=False)
    ]
    narrow = quantile(widths, 0.4)
    occupied = {column for _, column in text_blocked_cells(block)}
    preserved = list(column_breakpoints)
    for column, width in enumerate(widths):
        if column in occupied and width <= narrow:
            left_shift = abs(column_breakpoints[column] - proposed[column])
            right_shift = abs(
                column_breakpoints[column + 1]
                - proposed[column + 1]
            )
            significant_shift = width * 0.1
            if (
                left_shift > significant_shift
                and right_shift <= significant_shift
            ):
                translated_right = column_breakpoints[column] + width
                if (
                    column + 2 < len(column_breakpoints)
                    and translated_right < column_breakpoints[column + 2]
                ):
                    preserved[column + 1] = translated_right
            elif (
                right_shift > significant_shift
                and left_shift <= significant_shift
            ):
                translated_left = column_breakpoints[column + 1] - width
                if (
                    column > 0
                    and translated_left > column_breakpoints[column - 1]
                ):
                    preserved[column] = translated_left
                elif column == 0:
                    preserved[column + 1] = proposed[column + 1]
            elif (
                column_breakpoints[column + 1]
                - column_breakpoints[column]
                < width * 0.9
            ):
                preserved[column] = proposed[column]
                preserved[column + 1] = proposed[column + 1]
    return tuple(preserved)


def preserve_occupied_narrow_rows(
    block: ReferenceLayoutBlock,
    row_breakpoints: tuple[float, ...],
) -> tuple[float, ...]:
    proposed = block.row_breakpoints
    heights = [
        bottom - top
        for top, bottom in zip(proposed, proposed[1:], strict=False)
    ]
    narrow = quantile(heights, 0.4)
    occupied = {row for row, _ in text_blocked_cells(block)}
    preserved = list(row_breakpoints)
    for row, height in enumerate(heights):
        if row in occupied and height <= narrow:
            preserved[row] = proposed[row]
            preserved[row + 1] = proposed[row + 1]
    return tuple(preserved)


def snap_column_gap_breakpoints(
    image: Image.Image,
    block: ReferenceLayoutBlock,
    row_breakpoints: tuple[float, ...],
    column_breakpoints: tuple[float, ...],
    layout_lines: LayoutLines | None = None,
) -> tuple[float, ...]:
    # layout_lines 를 받으면 같은 이미지의 투영을 다시 돌지 않는다 —
    # snap_layout_breakpoints_from_image 의 같은 인자와 짝이다.
    _, candidates = (
        detect_layout_lines_from_image(image)
        if layout_lines is None
        else layout_lines
    )
    pairs = _candidate_pairs(candidates)
    if not pairs:
        return column_breakpoints
    background = estimate_background_color(image)
    blocked = text_blocked_cells(block)
    snapped = list(column_breakpoints)
    widths = [
        right - left
        for left, right in zip(
            column_breakpoints,
            column_breakpoints[1:],
            strict=False,
        )
    ]
    narrow = quantile(widths, 0.4)
    row_count = len(row_breakpoints) - 1
    column_count = len(column_breakpoints) - 1
    for column, target_width in enumerate(widths):
        if (
            column == 0
            or column == column_count - 1
            or target_width > narrow
        ):
            continue
        covered_rows = tuple(
            row
            for row in range(row_count)
            if edge_covers(block, "vertical", column, row)
            and edge_covers(block, "vertical", column + 1, row)
        )
        if not covered_rows or any(
            (row, column) in blocked for row in covered_rows
        ):
            continue
        target_midpoint = (
            column_breakpoints[column]
            + column_breakpoints[column + 1]
        ) / 2
        search_radius = max(0.06, target_width * 3)
        choices: list[tuple[float, float, float, float, float]] = []
        for left, right in pairs:
            width = right - left
            midpoint = (left + right) / 2
            if (
                width < target_width * 0.5
                or width > target_width * 1.75
                or abs(midpoint - target_midpoint) > search_radius
                or left <= snapped[column - 1]
                or right >= column_breakpoints[column + 2]
            ):
                continue
            distance = strip_background_distance(
                image,
                background,
                left,
                right,
                covered_rows,
                row_breakpoints,
            )
            choices.append(
                (
                    distance,
                    abs(midpoint - target_midpoint),
                    abs(width - target_width),
                    left,
                    right,
                )
            )
        if not choices:
            continue
        distance, _, _, left, right = min(choices)
        if distance > 0.08:
            continue
        left, right = refine_blank_run(
            image,
            background,
            left,
            right,
            covered_rows,
            row_breakpoints,
        )
        snapped[column] = left
        snapped[column + 1] = right
        if column + 2 < column_count:
            neighbor_width = (
                column_breakpoints[column + 2]
                - column_breakpoints[column + 1]
            )
            translated_neighbor = right + neighbor_width
            if translated_neighbor < column_breakpoints[column + 3]:
                snapped[column + 2] = translated_neighbor
    return tuple(snapped)
