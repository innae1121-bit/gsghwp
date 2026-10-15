from __future__ import annotations

import hashlib
from pathlib import Path

from hwp_reference_image_artifacts import ARTIFACT_IMAGE_SUFFIX, save_text_crop
from hwp_reference_image_contract import (
    AnalysisAxis,
    BreakpointSource,
    NormalizedBox,
    ReferenceBreakpointCandidate,
    ReferenceImageObject,
    ReferenceImageSegment,
    ReferenceProtectedGap,
    ReferenceTextRegion,
)
from hwp_reference_image_pixels import (
    PixelBox,
    PixelCanvas,
    PixelGap,
    PixelObject,
    PixelSegment,
)


def _stable_id(prefix: str, version: str, image_hash: str, *parts: object) -> str:
    payload = "|".join((version, image_hash, *(str(part) for part in parts)))
    return f"{prefix}-{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:12]}"


def _normalized(bbox: PixelBox, canvas: PixelCanvas) -> NormalizedBox:
    return NormalizedBox(
        left=bbox.left / canvas.width,
        top=bbox.top / canvas.height,
        right=bbox.right / canvas.width,
        bottom=bbox.bottom / canvas.height,
    )


def compile_objects(
    canvas: PixelCanvas,
    version: str,
    image_hash: str,
    objects: tuple[PixelObject, ...],
) -> tuple[ReferenceImageObject, ...]:
    return tuple(
        ReferenceImageObject(
            region_id=_stable_id(
                "obj",
                version,
                image_hash,
                item.bbox,
                item.fill,
                item.edges,
            ),
            kind="filled_region" if item.fill is not None else "outlined_box",
            bbox=_normalized(item.bbox, canvas),
            fill=item.fill,
            edges=item.edges,
        )
        for item in objects
    )


def compile_segments(
    canvas: PixelCanvas,
    version: str,
    image_hash: str,
    segments: tuple[PixelSegment, ...],
) -> tuple[ReferenceImageSegment, ...]:
    result: list[ReferenceImageSegment] = []
    for item in segments:
        segment_id = _stable_id(
            "seg",
            version,
            image_hash,
            item.orientation,
            item.start,
            item.end,
            item.position,
        )
        if item.orientation == "horizontal":
            result.append(
                ReferenceImageSegment(
                    segment_id=segment_id,
                    orientation="horizontal",
                    start_x=item.start / canvas.width,
                    start_y=item.position / canvas.height,
                    end_x=item.end / canvas.width,
                    end_y=item.position / canvas.height,
                    width_px=item.width,
                    color=item.color,
                )
            )
        else:
            result.append(
                ReferenceImageSegment(
                    segment_id=segment_id,
                    orientation="vertical",
                    start_x=item.position / canvas.width,
                    start_y=item.start / canvas.height,
                    end_x=item.position / canvas.width,
                    end_y=item.end / canvas.height,
                    width_px=item.width,
                    color=item.color,
                )
            )
    return tuple(result)


def compile_texts(
    canvas: PixelCanvas,
    version: str,
    image_hash: str,
    regions: tuple[PixelBox, ...],
    *,
    directory: Path,
) -> tuple[ReferenceTextRegion, ...]:
    result: list[ReferenceTextRegion] = []
    for bbox in regions:
        region_id = _stable_id("txt", version, image_hash, bbox)
        crop_path = directory / f"{region_id}{ARTIFACT_IMAGE_SUFFIX}"
        crop_hash = save_text_crop(canvas, bbox=bbox, path=crop_path)
        result.append(
            ReferenceTextRegion(
                region_id=region_id,
                bbox=_normalized(bbox, canvas),
                line_count=1,
                crop_hash=crop_hash,
                crop_path=crop_path,
            )
        )
    return tuple(result)


def compile_gaps(
    canvas: PixelCanvas,
    version: str,
    image_hash: str,
    gaps: tuple[PixelGap, ...],
    objects: tuple[ReferenceImageObject, ...],
) -> tuple[ReferenceProtectedGap, ...]:
    result: list[ReferenceProtectedGap] = []
    for gap in gaps:
        left_index, right_index = gap.bounded_by
        gap_id = _stable_id("gap", version, image_hash, gap.orientation, gap.bbox)
        result.append(
            ReferenceProtectedGap(
                gap_id=gap_id,
                orientation=gap.orientation,
                bbox=_normalized(gap.bbox, canvas),
                minimum_size_px=gap.minimum_size,
                bounded_by=(
                    objects[left_index].region_id,
                    objects[right_index].region_id,
                ),
            )
        )
    return tuple(result)


def compile_breakpoints(
    analysis_id: str,
    objects: tuple[ReferenceImageObject, ...],
    gaps: tuple[ReferenceProtectedGap, ...],
    segments: tuple[ReferenceImageSegment, ...],
) -> tuple[ReferenceBreakpointCandidate, ...]:
    values: dict[
        tuple[AnalysisAxis, float],
        tuple[int, BreakpointSource, str],
    ] = {}
    gap_values: set[tuple[AnalysisAxis, float, BreakpointSource, str]] = set()

    def add(
        axis: AnalysisAxis,
        position: float,
        priority: int,
        source: BreakpointSource,
        source_id: str,
    ) -> None:
        key = (axis, round(position, 8))
        current = values.get(key)
        if current is None or priority < current[0]:
            values[key] = (priority, source, source_id)

    add("column", 0.0, 0, "frame", analysis_id)
    add("column", 1.0, 0, "frame", analysis_id)
    add("row", 0.0, 0, "frame", analysis_id)
    add("row", 1.0, 0, "frame", analysis_id)
    for item in objects:
        add("column", item.bbox.left, 2, "object", item.region_id)
        add("column", item.bbox.right, 2, "object", item.region_id)
        add("row", item.bbox.top, 2, "object", item.region_id)
        add("row", item.bbox.bottom, 2, "object", item.region_id)
    for item in gaps:
        axis: AnalysisAxis = (
            "column" if item.orientation == "vertical" else "row"
        )
        start = item.bbox.left if axis == "column" else item.bbox.top
        end = item.bbox.right if axis == "column" else item.bbox.bottom
        gap_values.add((axis, round(start, 8), "gap", item.gap_id))
        gap_values.add((axis, round(end, 8), "gap", item.gap_id))
    for item in segments:
        if item.orientation == "horizontal":
            add("row", item.start_y, 3, "segment", item.segment_id)
            add("column", item.start_x, 3, "segment", item.segment_id)
            add("column", item.end_x, 3, "segment", item.segment_id)
        else:
            add("column", item.start_x, 3, "segment", item.segment_id)
            add("row", item.start_y, 3, "segment", item.segment_id)
            add("row", item.end_y, 3, "segment", item.segment_id)
    candidates: list[
        tuple[AnalysisAxis, float, BreakpointSource, str]
    ] = [
        (axis, position, source, source_id)
        for (axis, position), (_, source, source_id) in values.items()
    ]
    candidates.extend(gap_values)
    return tuple(
        ReferenceBreakpointCandidate(
            axis=axis,
            position=position,
            source=source,
            source_id=source_id,
        )
        for axis, position, source, source_id in sorted(candidates)
    )
