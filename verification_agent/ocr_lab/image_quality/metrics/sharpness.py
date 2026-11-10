"""Two independent sharpness indicators."""

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


def laplacian_variance(gray: np.ndarray) -> float:
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def tenengrad_energy(gray: np.ndarray) -> float:
    gradient_x = cv2.Sobel(gray, cv2.CV_64F, 1, 0, ksize=3)
    gradient_y = cv2.Sobel(gray, cv2.CV_64F, 0, 1, ksize=3)
    return float(np.mean(gradient_x * gradient_x + gradient_y * gradient_y))


def compute_sharpness_metrics(
    region: EvaluationRegion,
    cfg: dict[str, Any],
) -> dict[str, MetricResult]:
    gray = _gray(region)
    lap_value = laplacian_variance(gray)
    ten_value = tenengrad_energy(gray)
    common_metadata = {
        "region_source": region.source,
        "region_bbox_working": list(region.bbox),
        "note": "Computed on the normalised working copy; not corrected by deconvolution.",
    }
    return {
        "sharpness_laplacian": MetricResult(
            value=round(lap_value, 4),
            nature="measured",
            confidence=0.95,
            metadata={
                **common_metadata,
                "method": "variance_of_laplacian",
                "poor_threshold": cfg["blur"]["laplacian_variance_poor"],
                "severe_threshold": cfg["blur"]["laplacian_variance_severe"],
            },
        ),
        "sharpness_tenengrad": MetricResult(
            value=round(ten_value, 4),
            nature="measured",
            confidence=0.9,
            metadata={
                **common_metadata,
                "method": "tenengrad_gradient_energy",
                "poor_threshold": cfg["blur"]["tenengrad_poor"],
                "severe_threshold": cfg["blur"]["tenengrad_severe"],
            },
        ),
    }
