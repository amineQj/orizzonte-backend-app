"""High-level, in-memory image-quality analysis."""

from __future__ import annotations

from pathlib import Path
from time import perf_counter
from typing import Any

from .decision_engine import decide
from .errors import ImageQualityError, ProfileError
from .image_loader import load_image
from .metrics.boundaries import (
    compute_perspective_metric,
    compute_rotation_metric,
    detect_document_boundary,
)
from .metrics.contrast import compute_contrast_metric
from .metrics.exposure import compute_exposure_metrics
from .metrics.glare import compute_glare_metric
from .metrics.region import select_evaluation_region
from .metrics.resolution import compute_resolution_metrics
from .metrics.sharpness import compute_sharpness_metrics
from .normalization import normalize_image
from .output_schema import (
    ALGORITHM_VERSION,
    SCHEMA_VERSION,
    STATUS_RECAPTURE,
    validate_output,
)
from .profiles import ProfileConfig, get_profile_config


def _elapsed_ms(start: float) -> float:
    return round((perf_counter() - start) * 1000.0, 3)


def _build_error_result(
    *,
    profile: str,
    layout: str | None,
    category: str,
    reason_codes: list[str],
    start: float,
) -> dict[str, Any]:
    trace = [
        {
            "reason_code": code,
            "severity": "critical",
            "metrics": [],
        }
        for code in reason_codes
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "algorithm_version": ALGORITHM_VERSION,
        "profile": profile,
        "layout": layout,
        "status": STATUS_RECAPTURE,
        "confidence": 0.0,
        "thresholds_calibrated": False,
        "processing_time_ms": _elapsed_ms(start),
        "image": None,
        "metrics": {},
        "reason_codes": reason_codes,
        "decision_trace": trace,
        "error_category": category,
    }


def _image_summary(loaded: Any, working: Any, cfg: ProfileConfig) -> dict[str, Any]:
    return {
        "format": loaded.format,
        "source_mode": loaded.source_mode,
        "is_grayscale": bool(loaded.is_grayscale),
        "original_width_px": int(loaded.original_width),
        "original_height_px": int(loaded.original_height),
        "original_megapixels": round(float(loaded.original_megapixels), 4),
        "oriented_width_px": int(loaded.oriented_width),
        "oriented_height_px": int(loaded.oriented_height),
        "working_width_px": int(working.working_width),
        "working_height_px": int(working.working_height),
        "resized_for_analysis": bool(working.resized),
        "working_side_limit_px": int(working.max_side),
        "max_megapixels": float(cfg.data["max_megapixels"]),
        "exif_orientation": loaded.exif_orientation,
        "exif_orientation_applied": bool(loaded.exif_applied),
    }


def _analyze_source(
    source: bytes | bytearray | str | Path,
    *,
    profile: str,
    layout: str | None,
    thresholds: dict[str, Any] | None,
    max_side: int | None,
    max_megapixels: float | None,
) -> dict[str, Any]:
    start = perf_counter()
    try:
        cfg = get_profile_config(profile, layout, thresholds)
        cfg = cfg.with_overrides(max_side=max_side, max_megapixels=max_megapixels)
        loaded = load_image(source, float(cfg.data["max_megapixels"]))
        working = normalize_image(loaded, int(cfg.data["max_working_side"]))

        boundary = None
        if cfg.document_analysis:
            boundary = detect_document_boundary(working, cfg.data["boundary"])

        region = select_evaluation_region(
            working,
            boundary,
            cfg.data,
            cfg.document_analysis,
        )

        metrics: dict[str, Any] = {}
        metrics.update(
            compute_resolution_metrics(
                loaded,
                working,
                boundary,
                cfg.data,
                cfg.document_analysis,
            )
        )
        metrics.update(compute_sharpness_metrics(region, cfg.data))
        metrics.update(compute_exposure_metrics(region, cfg.data))
        metrics.update(compute_contrast_metric(region, cfg.data))

        if cfg.document_analysis:
            if boundary is None:
                raise RuntimeError("document profile produced no boundary result")
            metrics.update(compute_glare_metric(region, cfg.data))
            metrics["document_boundary"] = boundary.to_metric(cfg.data["boundary"])
            metrics["rotation"] = compute_rotation_metric(boundary, cfg.data["rotation"])
            metrics["perspective"] = compute_perspective_metric(boundary, cfg.data["rotation"])

        decision = decide(metrics, cfg.data, cfg.document_analysis)
        error_category = None
        if (
            decision.status == STATUS_RECAPTURE
            and "NO_DOCUMENT_FOUND" in decision.reason_codes
        ):
            error_category = "no_document_found"
        result = {
            "schema_version": SCHEMA_VERSION,
            "algorithm_version": ALGORITHM_VERSION,
            "profile": cfg.profile,
            "layout": cfg.layout,
            "status": decision.status,
            "confidence": decision.confidence,
            "thresholds_calibrated": cfg.thresholds_calibrated,
            "processing_time_ms": _elapsed_ms(start),
            "image": _image_summary(loaded, working, cfg),
            "metrics": {name: metric.to_dict() for name, metric in metrics.items()},
            "reason_codes": decision.reason_codes,
            "decision_trace": decision.trace,
            "error_category": error_category,
        }
        validation_errors = validate_output(result)
        if validation_errors:
            raise RuntimeError("output schema validation failed: " + "; ".join(validation_errors))
        return result
    except ProfileError as exc:
        return _build_error_result(
            profile=profile,
            layout=layout,
            category=exc.category,
            reason_codes=[],
            start=start,
        )
    except ImageQualityError as exc:
        return _build_error_result(
            profile=profile,
            layout=layout,
            category=exc.category,
            reason_codes=[exc.reason_code],
            start=start,
        )


def analyze_bytes(
    data: bytes | bytearray,
    profile: str,
    layout: str | None = None,
    *,
    thresholds: dict[str, Any] | None = None,
    max_side: int | None = None,
    max_megapixels: float | None = None,
) -> dict[str, Any]:
    """Analyze image bytes without writing or modifying any file."""

    return _analyze_source(
        data,
        profile=profile,
        layout=layout,
        thresholds=thresholds,
        max_side=max_side,
        max_megapixels=max_megapixels,
    )


def analyze_path(
    path: str | Path,
    profile: str,
    layout: str | None = None,
    *,
    thresholds: dict[str, Any] | None = None,
    max_side: int | None = None,
    max_megapixels: float | None = None,
) -> dict[str, Any]:
    """Analyze an image path read-only; the file is never modified."""

    return _analyze_source(
        path,
        profile=profile,
        layout=layout,
        thresholds=thresholds,
        max_side=max_side,
        max_megapixels=max_megapixels,
    )
