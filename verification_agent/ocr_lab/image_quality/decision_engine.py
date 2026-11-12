"""Rule-based quality decision.

The decision engine never decides whether a person or document is fraudulent.
It only turns quality metrics into one of three capture-quality statuses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .output_schema import (
    REASON_PRIORITY,
    STATUS_ACCEPTABLE,
    STATUS_POOR,
    STATUS_RECAPTURE,
    MetricResult,
)


@dataclass
class Decision:
    status: str
    confidence: float
    reason_codes: list[str] = field(default_factory=list)
    trace: list[dict[str, Any]] = field(default_factory=list)


def _known(metric: MetricResult | None) -> bool:
    return metric is not None and metric.nature != "unknown" and metric.value is not None


def _metric_value(metrics: dict[str, MetricResult], name: str) -> Any:
    metric = metrics.get(name)
    if metric is None:
        return None
    return metric.value


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _glare_guard_suppresses(glare: Any, cfg: dict[str, Any]) -> bool:
    """Return True when the configured near-saturation guard suppresses glare.

    A missing or invalid metric never suppresses the glare rule.  The guard is a
    false-positive control for images that are mostly white paper, not a reason
    to reject an image by itself.
    """

    if not isinstance(glare, dict):
        return False
    near_saturated = _number(glare.get("near_saturated_fraction"))
    if near_saturated is None:
        return False
    glare_cfg = cfg.get("glare")
    if not isinstance(glare_cfg, dict):
        return False
    threshold = _number(glare_cfg.get("max_near_saturated_fraction"))
    if threshold is None:
        return False
    return near_saturated > threshold


def decide(
    metrics: dict[str, MetricResult],
    cfg: dict[str, Any],
    document_analysis: bool,
) -> Decision:
    reasons: dict[str, dict[str, Any]] = {}

    def add(reason: str, severity: str, metric_names: list[str]) -> None:
        entry = reasons.get(reason)
        if entry is None:
            reasons[reason] = {
                "reason_code": reason,
                "severity": severity,
                "metrics": list(metric_names),
            }
            return
        if severity == "critical":
            entry["severity"] = "critical"
        for name in metric_names:
            if name not in entry["metrics"]:
                entry["metrics"].append(name)

    resolution_failed = False
    original = _metric_value(metrics, "resolution_original")
    if isinstance(original, dict) and _known(metrics.get("resolution_original")):
        width = _number(original.get("width_px"))
        height = _number(original.get("height_px"))
        megapixels = _number(original.get("megapixels"))
        if (
            width is not None
            and height is not None
            and megapixels is not None
            and (
                width < float(cfg["min_original_width"])
                or height < float(cfg["min_original_height"])
                or megapixels < float(cfg["min_original_megapixels"])
            )
        ):
            add("TOO_SMALL", "critical", ["resolution_original"])
            resolution_failed = True

    if document_analysis:
        document = _metric_value(metrics, "resolution_document")
        if isinstance(document, dict) and _known(metrics.get("resolution_document")):
            width = _number(document.get("width_px"))
            height = _number(document.get("height_px"))
            megapixels = _number(document.get("megapixels"))
            if (
                width is not None
                and height is not None
                and megapixels is not None
                and (
                    width < float(cfg["min_document_width"])
                    or height < float(cfg["min_document_height"])
                    or megapixels < float(cfg["min_document_megapixels"])
                )
            ):
                add("LOW_RES", "critical", ["resolution_document"])
                resolution_failed = True

    lap = metrics.get("sharpness_laplacian")
    ten = metrics.get("sharpness_tenengrad")
    lap_value = _number(lap.value) if _known(lap) else None
    ten_value = _number(ten.value) if _known(ten) else None
    # ``severe`` is the hard edge and is intentionally strict: values exactly
    # at the edge are part of the soft band.  ``poor`` is the upper edge of the
    # soft band; values at or below it are a moderate warning, not a hard
    # rejection.
    lap_severe = (
        lap_value is not None
        and lap_value < float(cfg["blur"]["laplacian_variance_severe"])
    )
    ten_severe = (
        ten_value is not None
        and ten_value < float(cfg["blur"]["tenengrad_severe"])
    )
    lap_poor = (
        lap_value is not None
        and lap_value <= float(cfg["blur"]["laplacian_variance_poor"])
    )
    ten_poor = (
        ten_value is not None
        and ten_value <= float(cfg["blur"]["tenengrad_poor"])
    )
    if document_analysis:
        boundary = _metric_value(metrics, "document_boundary")
        if isinstance(boundary, dict):
            if bool(boundary.get("cropping_confirmed")):
                add("CROPPED", "critical", ["document_boundary"])

    brightness = _metric_value(metrics, "brightness")
    brightness_value = _number(brightness)
    clipping = _metric_value(metrics, "clipping")
    dark_fraction = (
        _number(clipping.get("dark_fraction")) if isinstance(clipping, dict) else None
    )
    bright_fraction = (
        _number(clipping.get("bright_fraction")) if isinstance(clipping, dict) else None
    )

    dark = False
    dark_problem = False
    if brightness_value is not None:
        dark = brightness_value < float(cfg["exposure"]["dark_mean"])
        dark_clipped = (
            dark_fraction is not None
            and dark_fraction > float(cfg["exposure"]["dark_clip_fraction"])
            and brightness_value < 100.0
        )
        dark_problem = dark or dark_clipped
        if dark_problem:
            add("DARK", "moderate", ["brightness", "clipping"])

    contrast = _metric_value(metrics, "contrast")
    percentile_range = (
        _number(contrast.get("p5_p95_range")) if isinstance(contrast, dict) else None
    )
    rms_contrast = (
        _number(contrast.get("rms_contrast")) if isinstance(contrast, dict) else None
    )
    p5_value = _number(contrast.get("p5")) if isinstance(contrast, dict) else None
    contrast_cfg = cfg["contrast"]
    p5_min = float(contrast_cfg.get("p5_min", 40.0))
    rms_secondary_min = float(contrast_cfg.get("rms_secondary_min", 20.0))
    primary_low_contrast = (
        (
            percentile_range is not None
            and percentile_range < float(contrast_cfg["p5_p95_min_range"])
        )
        or (
            rms_contrast is not None
            and rms_contrast < float(contrast_cfg["rms_min"])
        )
    )
    secondary_low_contrast = (
        (p5_value is not None and p5_value < p5_min)
        or (rms_contrast is not None and rms_contrast < rms_secondary_min)
    )
    low_contrast = primary_low_contrast or secondary_low_contrast
    if low_contrast:
        add("LOW_CONTRAST", "moderate", ["contrast"])

    # Tenengrad is the primary indicator and can reject on its own.  Laplacian
    # is a secondary hard veto and otherwise only contributes to the soft
    # warning when both indicators are in their soft bands.  A single soft
    # indicator is deliberately accepted.
    blur_critical = lap_severe or ten_severe
    blur_soft_band = lap_poor and ten_poor
    blur_guard = document_analysis and (dark_problem or low_contrast)
    if blur_critical and not blur_guard:
        add(
            "BLURRY",
            "critical",
            [
                name
                for name, condition in (
                    ("sharpness_laplacian", lap_severe),
                    ("sharpness_tenengrad", ten_severe),
                )
                if condition
            ],
        )
    elif blur_soft_band or (blur_critical and blur_guard):
        add(
            "BLURRY",
            "moderate",
            [
                name
                for name, condition in (
                    ("sharpness_laplacian", lap_poor or lap_severe),
                    ("sharpness_tenengrad", ten_poor or ten_severe),
                )
                if condition
            ],
        )

    if document_analysis and isinstance(boundary, dict):
        if int(boundary.get("no_document_signals", 0)) >= 2:
            no_document_severity = "critical"
            if (
                str(cfg.get("boundary", {}).get("no_document_severity", "critical"))
                == "moderate"
                and not resolution_failed
                and not low_contrast
                and not blur_critical
                and not blur_soft_band
            ):
                no_document_severity = "moderate"
            add("NO_DOCUMENT_FOUND", no_document_severity, ["document_boundary"])

    if (
        brightness_value is not None
        and bright_fraction is not None
        and brightness_value > float(cfg["exposure"]["overexposed_mean"])
        and bright_fraction > float(cfg["exposure"]["bright_clip_fraction"])
        and low_contrast
    ):
        add("OVEREXPOSED", "moderate", ["brightness", "clipping", "contrast"])

    uneven = _metric_value(metrics, "uneven_illumination")
    uneven_ratio = _number(uneven.get("ratio")) if isinstance(uneven, dict) else None
    if (
        uneven_ratio is not None
        and uneven_ratio > float(cfg["exposure"]["uneven_illumination_ratio"])
    ):
        add("UNEVEN_LIGHT", "moderate", ["uneven_illumination"])

    if document_analysis:
        glare_allowed = not (
            brightness_value is not None
            and brightness_value > float(cfg["exposure"]["overexposed_mean"])
            and low_contrast
        )
        glare = _metric_value(metrics, "glare")
        if _glare_guard_suppresses(glare, cfg):
            glare_allowed = False
        if glare_allowed and isinstance(glare, dict):
            suspected = _number(glare.get("suspected_fraction"))
            largest = _number(glare.get("largest_component_fraction"))
            if (
                suspected is not None
                and largest is not None
                and suspected > float(cfg["glare"]["max_suspected_fraction"])
                and largest > float(cfg["glare"]["max_component_fraction"])
            ):
                add("GLARE", "moderate", ["glare"])

        rotation = _metric_value(metrics, "rotation")
        angle = _number(rotation.get("angle_deg")) if isinstance(rotation, dict) else None
        if angle is not None and abs(angle) > float(cfg["rotation"]["max_abs_angle_deg"]):
            add("ROTATED", "moderate", ["rotation"])

        perspective = _metric_value(metrics, "perspective")
        perspective_ratio = (
            _number(perspective.get("ratio")) if isinstance(perspective, dict) else None
        )
        if (
            perspective_ratio is not None
            and perspective_ratio > float(cfg["rotation"]["max_perspective_ratio"])
        ):
            add("PERSPECTIVE", "moderate", ["perspective"])

    reason_codes = sorted(reasons.keys(), key=lambda code: REASON_PRIORITY.get(code, 99))
    trace = [reasons[code] for code in reason_codes]

    if any(entry["severity"] == "critical" for entry in trace):
        status = STATUS_RECAPTURE
    elif trace:
        status = STATUS_POOR
    else:
        status = STATUS_ACCEPTABLE

    applicable = [
        "resolution_original",
        "sharpness_laplacian",
        "sharpness_tenengrad",
        "brightness",
        "clipping",
        "uneven_illumination",
        "contrast",
    ]
    if document_analysis:
        applicable.extend(
            [
                "resolution_document",
                "glare",
                "document_boundary",
                "rotation",
                "perspective",
            ]
        )
    known_count = sum(1 for name in applicable if _known(metrics.get(name)))
    confidence = min(0.99, 0.5 + 0.5 * (known_count / max(1, len(applicable))))
    return Decision(
        status=status,
        confidence=round(float(confidence), 4),
        reason_codes=reason_codes,
        trace=trace,
    )
