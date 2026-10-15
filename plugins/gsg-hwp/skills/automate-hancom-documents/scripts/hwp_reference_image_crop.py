from __future__ import annotations

import hashlib
import math
import shutil
import uuid
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps
from pydantic import Field

from hwp_live_values import ContractModel
from hwp_reference_image_analyzer import analyze_reference_image, default_artifact_root
from hwp_reference_image_artifacts import ARTIFACT_IMAGE_SUFFIX, save_artifact_image
from hwp_reference_image_cache import (
    DEFAULT_REFERENCE_IMAGE_LIMITS,
    ReferenceImageResourceLimits,
    prune_reference_image_cache,
)
from hwp_reference_image_contract import NormalizedBox
from hwp_reference_image_crop_boundary import resolve_crop_boundary
from hwp_reference_image_crop_contract import (
    CropAnalysisMethod,
    CropBoundaryAnalysis,
    CropBoundaryMode,
    ReferenceImagePixelBox,
)


class ReferenceImageCropRequest(ContractModel):
    name: str = Field(
        min_length=1,
        max_length=120,
        description="Model-chosen semantic asset name.",
    )
    boundary_mode: CropBoundaryMode = Field(
        description=(
            "Use tight_raster for one rectangular photo or continuous raster "
            "panel so adjacent labels and decorations are removed. Use "
            "annotated_group for a map, drawing, logo, or motif whose detached "
            "labels, callouts, or legend belong to the asset."
        ),
    )
    bbox: NormalizedBox = Field(
        description=(
            "Rough box around the complete semantic raster asset. Include attached "
            "labels, callouts, and legends only for annotated_group. Pixel analysis "
            "resolves the final boundary; do not submit guessed tight coordinates."
        ),
    )


class PreparedReferenceImageCrop(ContractModel):
    name: str
    boundary_mode: CropBoundaryMode
    requested_bbox: NormalizedBox
    bbox: NormalizedBox
    requested_pixel_box: ReferenceImagePixelBox
    pixel_box: ReferenceImagePixelBox
    width_px: int = Field(ge=1)
    height_px: int = Field(ge=1)
    refined: bool
    analysis_method: CropAnalysisMethod
    confidence: float = Field(ge=0, le=1)
    needs_review: bool
    component_count: int = Field(ge=0)
    image_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    path: Path


class PreparedReferenceImageCrops(ContractModel):
    source_path: Path
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_width_px: int = Field(ge=1)
    source_height_px: int = Field(ge=1)
    analysis_id: str = Field(pattern=r"^ria-[0-9a-f]{16}$")
    analysis_overlay_path: Path
    contact_sheet_paths: tuple[Path, ...]
    crop_overlay_path: Path
    crops: tuple[PreparedReferenceImageCrop, ...] = Field(min_length=1)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _pixel_box(bbox: NormalizedBox, width: int, height: int) -> ReferenceImagePixelBox:
    left = min(width - 1, max(0, math.floor(bbox.left * width)))
    top = min(height - 1, max(0, math.floor(bbox.top * height)))
    right = min(width, max(left + 1, math.ceil(bbox.right * width)))
    bottom = min(height, max(top + 1, math.ceil(bbox.bottom * height)))
    return ReferenceImagePixelBox(left=left, top=top, right=right, bottom=bottom)


def _normalized_box(box: ReferenceImagePixelBox, width: int, height: int) -> NormalizedBox:
    return NormalizedBox(
        left=box.left / width,
        top=box.top / height,
        right=box.right / width,
        bottom=box.bottom / height,
    )


def _save_boundary_overlay(
    image: Image.Image,
    prepared: list[
        tuple[
            ReferenceImageCropRequest,
            ReferenceImagePixelBox,
            ReferenceImagePixelBox,
            CropBoundaryAnalysis,
            str,
        ]
    ],
    path: Path,
) -> None:
    with image.convert("RGBA") as overlay:
        draw = ImageDraw.Draw(overlay)
        width = max(2, round(min(image.size) * 0.0025))
        for index, (_, requested, refined, _, _) in enumerate(prepared, start=1):
            draw.rectangle(
                (requested.left, requested.top, requested.right - 1, requested.bottom - 1),
                outline=(255, 166, 0, 255),
                width=width,
            )
            draw.rectangle(
                (refined.left, refined.top, refined.right - 1, refined.bottom - 1),
                outline=(0, 180, 80, 255),
                width=width,
            )
            draw.text(
                (refined.left + width, refined.top + width),
                str(index),
                fill=(0, 100, 30, 255),
                stroke_width=1,
                stroke_fill=(255, 255, 255, 255),
            )
        save_artifact_image(overlay, path)


def prepare_reference_image_crops(
    source_image: Path,
    crops: tuple[ReferenceImageCropRequest, ...],
    *,
    artifact_root: Path | None = None,
    limits: ReferenceImageResourceLimits = DEFAULT_REFERENCE_IMAGE_LIMITS,
) -> PreparedReferenceImageCrops:
    if not crops:
        raise ValueError("at least one image crop is required")
    if len(crops) > 64:
        raise ValueError("at most 64 image crops can be prepared at once")

    source = source_image.expanduser().resolve()
    if not source.is_file():
        raise ValueError(f"reference image does not exist: {source}")
    source_bytes = source.stat().st_size
    if source_bytes > limits.max_source_bytes:
        raise ValueError("reference image exceeds the source byte limit")

    root = (artifact_root or default_artifact_root()).resolve()
    root.mkdir(parents=True, exist_ok=True)
    prune_reference_image_cache(root, limits)
    analysis = analyze_reference_image(
        source,
        artifact_root=root,
        limits=limits,
        full_scan=True,
    )
    token = uuid.uuid4().hex[:16]
    directory = root / f"crop-{token}"
    temporary = root / f".partial-crop-{token}"
    temporary.mkdir(parents=False, exist_ok=False)

    prepared: list[
        tuple[
            ReferenceImageCropRequest,
            ReferenceImagePixelBox,
            ReferenceImagePixelBox,
            CropBoundaryAnalysis,
            str,
        ]
    ] = []
    try:
        with Image.open(source) as opened:
            with ImageOps.exif_transpose(opened) as image:
                _ = image.load()
                width, height = image.size
                if width < 1 or height < 1:
                    raise ValueError("reference image has no visible pixels")
                if width * height > limits.max_source_pixels:
                    raise ValueError("reference image exceeds the source pixel limit")
                output_pixels = 0
                for index, request in enumerate(crops, start=1):
                    requested_box = _pixel_box(request.bbox, width, height)
                    boundary = resolve_crop_boundary(
                        image,
                        requested_box.as_pixel_box(),
                        mode=request.boundary_mode,
                    )
                    box = ReferenceImagePixelBox.from_pixel_box(boundary.refined)
                    crop_width = box.right - box.left
                    crop_height = box.bottom - box.top
                    output_pixels += crop_width * crop_height
                    if output_pixels > limits.max_source_pixels * 2:
                        raise ValueError("prepared image crops exceed the output pixel limit")
                    path = temporary / f"crop-{index:02d}{ARTIFACT_IMAGE_SUFFIX}"
                    with image.crop((box.left, box.top, box.right, box.bottom)) as cropped:
                        save_artifact_image(cropped, path)
                    prepared.append(
                        (request, requested_box, box, boundary, _sha256(path))
                    )
                _save_boundary_overlay(
                    image,
                    prepared,
                    temporary / f"crop-boundaries{ARTIFACT_IMAGE_SUFFIX}",
                )
        _ = temporary.replace(directory)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise

    results = tuple(
        PreparedReferenceImageCrop(
            name=request.name,
            boundary_mode=request.boundary_mode,
            requested_bbox=request.bbox,
            bbox=_normalized_box(box, width, height),
            requested_pixel_box=requested_box,
            pixel_box=box,
            width_px=box.right - box.left,
            height_px=box.bottom - box.top,
            refined=requested_box != box,
            analysis_method=boundary.method,
            confidence=boundary.confidence,
            needs_review=boundary.needs_review,
            component_count=boundary.component_count,
            image_sha256=image_hash,
            path=directory / f"crop-{index:02d}{ARTIFACT_IMAGE_SUFFIX}",
        )
        for index, (request, requested_box, box, boundary, image_hash) in enumerate(
            prepared,
            start=1,
        )
    )
    return PreparedReferenceImageCrops(
        source_path=source,
        source_sha256=_sha256(source),
        source_width_px=width,
        source_height_px=height,
        analysis_id=analysis.analysis_id,
        analysis_overlay_path=analysis.overlay_path,
        contact_sheet_paths=analysis.contact_sheet_paths,
        crop_overlay_path=directory / f"crop-boundaries{ARTIFACT_IMAGE_SUFFIX}",
        crops=results,
    )
