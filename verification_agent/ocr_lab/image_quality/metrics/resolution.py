"""Original and document-region resolution metrics."""

from __future__ import annotations

from typing import Any

from ..image_loader import LoadedImage
from ..normalization import WorkingImage
from ..output_schema import MetricResult
from .boundaries import BoundaryResult


def compute_resolution_metrics(
    loaded: LoadedImage,
    working: WorkingImage,
    boundary: BoundaryResult | None,
    cfg: dict[str, Any],
    document_analysis: bool,
) -> dict[str, MetricResult]:
    original_megapixels = (
        loaded.original_width * loaded.original_height
    ) / 1_000_000.0
    metrics: dict[str, MetricResult] = {
        "resolution_original": MetricResult(
            value={
                "width_px": int(loaded.original_width),
                "height_px": int(loaded.original_height),
                "megapixels": round(original_megapixels, 4),
                "oriented_width_px": int(loaded.oriented_width),
                "oriented_height_px": int(loaded.oriented_height),
                "working_width_px": int(working.working_width),
                "working_height_px": int(working.working_height),
                "resized_for_analysis": bool(working.resized),
                "exif_orientation_applied": bool(loaded.exif_applied),
            },
            nature="measured",
            confidence=1.0,
            metadata={
                "min_original_width": cfg["min_original_width"],
                "min_original_height": cfg["min_original_height"],
                "min_original_megapixels": cfg["min_original_megapixels"],
                "measurement_basis": "decoded_original_pixels",
                "upscaling_note": "Upscaling does not restore lost detail.",
            },
        )
    }

    if not document_analysis:
        return metrics

    if boundary is None or not boundary.valid or boundary.bbox is None:
        metrics["resolution_document"] = MetricResult(
            value={
                "width_px": None,
                "height_px": None,
                "megapixels": None,
                "measurement_basis": "oriented_pixels",
            },
            nature="unknown",
            confidence=0.0,
            metadata={
                "min_document_width": cfg["min_document_width"],
                "min_document_height": cfg["min_document_height"],
                "min_document_megapixels": cfg["min_document_megapixels"],
                "reason": "no_reliable_document_region",
            },
        )
        return metrics

    _x, _y, bbox_width, bbox_height = boundary.bbox
    width_px = int(round(bbox_width / max(working.scale_x, 1e-9)))
    height_px = int(round(bbox_height / max(working.scale_y, 1e-9)))
    megapixels = (width_px * height_px) / 1_000_000.0
    metrics["resolution_document"] = MetricResult(
        value={
            "width_px": width_px,
            "height_px": height_px,
            "megapixels": round(megapixels, 4),
            "measurement_basis": "oriented_pixels",
            "bounding_box_working": list(boundary.bbox),
        },
        nature="measured",
        confidence=min(0.95, max(0.5, boundary.confidence)),
        metadata={
            "min_document_width": cfg["min_document_width"],
            "min_document_height": cfg["min_document_height"],
            "min_document_megapixels": cfg["min_document_megapixels"],
            "note": "The bounding box is used; perspective can make this estimate conservative.",
        },
    )
    return metrics
