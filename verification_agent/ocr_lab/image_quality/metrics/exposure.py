"""Brightness, clipping, and uneven-illumination metrics."""

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


def compute_exposure_metrics(
    region: EvaluationRegion,
    cfg: dict[str, Any],
) -> dict[str, MetricResult]:
    gray = _gray(region)
    values = gray.astype(np.float32)
    mean = float(np.mean(values))
    dark_level = int(cfg["exposure"]["dark_clip_level"])
    bright_level = int(cfg["exposure"]["bright_clip_level"])
    dark_fraction = float(np.mean(gray <= dark_level))
    bright_fraction = float(np.mean(gray >= bright_level))

    grid = max(2, int(cfg["exposure"]["block_grid"]))
    block_means = cv2.resize(gray, (grid, grid), interpolation=cv2.INTER_AREA).astype(np.float64)
    max_block = float(np.max(block_means))
    min_block = float(np.min(block_means))
    uneven_ratio = (max_block - min_block) / 255.0

    common = {
        "region_source": region.source,
        "region_bbox_working": list(region.bbox),
    }
    return {
        "brightness": MetricResult(
            value=round(mean, 3),
            nature="measured",
            confidence=0.95,
            metadata={
                **common,
                "method": "mean_gray_level",
                "dark_mean_threshold": cfg["exposure"]["dark_mean"],
                "overexposed_mean_threshold": cfg["exposure"]["overexposed_mean"],
                "scale": "0-255",
            },
        ),
        "clipping": MetricResult(
            value={
                "dark_fraction": round(dark_fraction, 5),
                "bright_fraction": round(bright_fraction, 5),
                "dark_clip_level": dark_level,
                "bright_clip_level": bright_level,
            },
            nature="measured",
            confidence=0.95,
            metadata={
                **common,
                "dark_clip_fraction_threshold": cfg["exposure"]["dark_clip_fraction"],
                "bright_clip_fraction_threshold": cfg["exposure"]["bright_clip_fraction"],
                "note": "Black text can create a large dark fraction on a well-lit white document.",
            },
        ),
        "uneven_illumination": MetricResult(
            value={
                "ratio": round(float(uneven_ratio), 4),
                "max_block_mean": round(max_block, 2),
                "min_block_mean": round(min_block, 2),
                "block_grid": grid,
            },
            nature="heuristic",
            confidence=0.65,
            metadata={
                **common,
                "threshold": cfg["exposure"]["uneven_illumination_ratio"],
                "method": "block_mean_range_over_255",
                "note": "Text layout can mimic uneven lighting; this is a heuristic.",
            },
        ),
    }
