"""Robust contrast metric based on a percentile range and RMS spread."""

from __future__ import annotations

from typing import Any

import cv2
import numpy as np

from ..output_schema import MetricResult
from .region import EvaluationRegion


def _gray(region: EvaluationRegion) -> np.ndarray:
    if region.array.ndim == 2:
        return region.array
    return cv2.cvtColor(region.array, cv2.COLOR_RGB2GRAY)


def compute_contrast_metric(
    region: EvaluationRegion,
    cfg: dict[str, Any],
) -> dict[str, MetricResult]:
    gray = _gray(region)
    values = gray.astype(np.float64)
    p5 = float(np.percentile(values, 5))
    p95 = float(np.percentile(values, 95))
    percentile_range = p95 - p5
    rms = float(np.std(values))
    return {
        "contrast": MetricResult(
            value={
                "p5_p95_range": round(percentile_range, 3),
                "rms_contrast": round(rms, 3),
                "p5": round(p5, 2),
                "p95": round(p95, 2),
            },
            nature="measured",
            confidence=0.9,
            metadata={
                "region_source": region.source,
                "region_bbox_working": list(region.bbox),
                "p5_p95_min_range": cfg["contrast"]["p5_p95_min_range"],
                "rms_min": cfg["contrast"]["rms_min"],
                "method": "percentile_range_and_rms",
                "note": "A percentile range is robust to a small number of extreme pixels.",
            },
        )
    }
