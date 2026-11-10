"""Versioned output contract and lightweight validation helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SCHEMA_VERSION = "1.0.0"
ALGORITHM_VERSION = "1.0.0"

STATUS_ACCEPTABLE = "ACCEPTABLE_QUALITY"
STATUS_POOR = "POOR_QUALITY"
STATUS_RECAPTURE = "NEEDS_RECAPTURE"

STATUSES = (STATUS_ACCEPTABLE, STATUS_POOR, STATUS_RECAPTURE)

REASON_CODES = (
    "LOW_RES",
    "TOO_SMALL",
    "BLURRY",
    "DARK",
    "OVEREXPOSED",
    "UNEVEN_LIGHT",
    "LOW_CONTRAST",
    "GLARE",
    "CROPPED",
    "ROTATED",
    "PERSPECTIVE",
    "NO_DOCUMENT_FOUND",
    "UNREADABLE_IMAGE",
)

REASON_PRIORITY = {
    "UNREADABLE_IMAGE": 0,
    "TOO_SMALL": 1,
    "LOW_RES": 2,
    "CROPPED": 3,
    "NO_DOCUMENT_FOUND": 4,
    "BLURRY": 5,
    "DARK": 6,
    "OVEREXPOSED": 7,
    "UNEVEN_LIGHT": 8,
    "LOW_CONTRAST": 9,
    "GLARE": 10,
    "PERSPECTIVE": 11,
    "ROTATED": 12,
}

NATURES = ("measured", "heuristic", "unknown")

REQUIRED_TOP_LEVEL = (
    "schema_version",
    "algorithm_version",
    "profile",
    "status",
    "confidence",
    "thresholds_calibrated",
    "processing_time_ms",
    "image",
    "metrics",
    "reason_codes",
)

REQUIRED_METRIC_KEYS = ("value", "nature", "confidence", "metadata")


@dataclass
class MetricResult:
    """A single measured, heuristic, or unknown quality metric."""

    value: Any
    nature: str = "measured"
    confidence: float = 1.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "nature": self.nature,
            "confidence": round(float(self.confidence), 4),
            "metadata": self.metadata,
        }


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_output(result: dict[str, Any]) -> list[str]:
    """Return schema-validation errors; an empty list means valid.

    This is intentionally dependency-free.  It validates the public contract,
    not internal metric semantics.
    """

    errors: list[str] = []
    for key in REQUIRED_TOP_LEVEL:
        if key not in result:
            errors.append(f"missing top-level key: {key}")

    if errors:
        return errors

    if result["schema_version"] != SCHEMA_VERSION:
        errors.append("schema_version mismatch")
    if result["algorithm_version"] != ALGORITHM_VERSION:
        errors.append("algorithm_version mismatch")
    if not isinstance(result["profile"], str) or not result["profile"]:
        errors.append("profile must be a non-empty string")
    if result["status"] not in STATUSES:
        errors.append("invalid status")
    if not _is_number(result["confidence"]) or not 0.0 <= float(result["confidence"]) <= 1.0:
        errors.append("confidence must be a number in [0, 1]")
    if not isinstance(result["thresholds_calibrated"], bool):
        errors.append("thresholds_calibrated must be a boolean")
    if not _is_number(result["processing_time_ms"]) or result["processing_time_ms"] < 0:
        errors.append("processing_time_ms must be a non-negative number")
    if result["image"] is not None and not isinstance(result["image"], dict):
        errors.append("image must be an object or null")
    if not isinstance(result["metrics"], dict):
        errors.append("metrics must be an object")

    reasons = result["reason_codes"]
    if not isinstance(reasons, list):
        errors.append("reason_codes must be a list")
    else:
        for code in reasons:
            if code not in REASON_CODES:
                errors.append(f"unknown reason code: {code}")
        if len(reasons) != len(set(reasons)):
            errors.append("reason_codes contains duplicates")

    metrics = result["metrics"]
    if isinstance(metrics, dict):
        for name, metric in metrics.items():
            if not isinstance(metric, dict):
                errors.append(f"metric {name} must be an object")
                continue
            for key in REQUIRED_METRIC_KEYS:
                if key not in metric:
                    errors.append(f"metric {name} missing {key}")
            if metric.get("nature") not in NATURES:
                errors.append(f"metric {name} has invalid nature")
            conf = metric.get("confidence")
            if not _is_number(conf) or not 0.0 <= float(conf) <= 1.0:
                errors.append(f"metric {name} has invalid confidence")

    error_category = result.get("error_category")
    if error_category is not None and not isinstance(error_category, str):
        errors.append("error_category must be a string or null")

    trace = result.get("decision_trace", [])
    if not isinstance(trace, list):
        errors.append("decision_trace must be a list")
    else:
        for item in trace:
            if not isinstance(item, dict):
                errors.append("decision_trace entries must be objects")
                continue
            if item.get("reason_code") not in REASON_CODES:
                errors.append("decision_trace has unknown reason_code")
            if item.get("severity") not in ("critical", "moderate"):
                errors.append("decision_trace has invalid severity")
            if not isinstance(item.get("metrics"), list):
                errors.append("decision_trace metrics must be a list")

    return errors
