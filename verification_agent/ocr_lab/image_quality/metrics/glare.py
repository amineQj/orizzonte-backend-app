"""Heuristic glare / near-saturated reflection detection."""

from __future__ import annotations

from typing import Any

import cv2
import numpy as np

from ..output_schema import MetricResult
from .region import EvaluationRegion


def compute_glare_metric(
    region: EvaluationRegion,
    cfg: dict[str, Any],
) -> dict[str, MetricResult]:
    if region.array.ndim == 2:
        rgb = cv2.cvtColor(region.array, cv2.COLOR_GRAY2RGB)
    else:
        rgb = region.array
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    saturation = hsv[:, :, 1]
    value = hsv[:, :, 2]

    near_saturated = value >= int(cfg["glare"]["near_saturated_threshold"])
    low_saturation = saturation <= int(cfg["glare"]["low_saturation_threshold"])
    near_saturated_fraction = float(np.mean(near_saturated))
    low_saturation_fraction = float(np.mean(low_saturation))

    # A large near-saturated area can be the white paper itself.  Keep it as a
    # candidate component and discard components that are so large they are
    # almost certainly the document background rather than a local reflection.
    mask = near_saturated & low_saturation
    mask_u8 = (mask.astype(np.uint8) * 255)
    mask_u8 = cv2.morphologyEx(
        mask_u8,
        cv2.MORPH_OPEN,
        np.ones((3, 3), np.uint8),
    )

    component_count = 0
    largest_fraction = 0.0
    suspected_fraction = 0.0
    region_area = float(gray.size)
    if region_area > 0 and np.count_nonzero(mask_u8) > 0:
        count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(
            mask_u8,
            connectivity=8,
        )
        minimum_area = int(cfg["glare"]["component_min_area_px"])
        maximum_background_area = region_area * float(cfg["glare"]["max_background_fraction"])
        areas = [
            int(stats[index, cv2.CC_STAT_AREA])
            for index in range(1, count)
            if minimum_area
            <= int(stats[index, cv2.CC_STAT_AREA])
            <= maximum_background_area
        ]
        component_count = len(areas)
        if areas:
            largest_fraction = max(areas) / region_area
            suspected_fraction = sum(areas) / region_area

    maximum_component = max(float(cfg["glare"]["max_component_fraction"]), 1e-9)
    if component_count == 0:
        confidence = 0.55
    else:
        confidence = min(0.9, 0.55 + 0.35 * min(1.0, largest_fraction / maximum_component))

    return {
        "glare": MetricResult(
            value={
                "suspected_fraction": round(float(suspected_fraction), 5),
                "largest_component_fraction": round(float(largest_fraction), 5),
                "component_count": int(component_count),
                "near_saturated_fraction": round(near_saturated_fraction, 5),
                "low_saturation_fraction": round(low_saturation_fraction, 5),
            },
            nature="heuristic",
            confidence=round(float(confidence), 4),
            metadata={
                "region_source": region.source,
                "region_bbox_working": list(region.bbox),
                "max_suspected_fraction": cfg["glare"]["max_suspected_fraction"],
                "max_component_fraction": cfg["glare"]["max_component_fraction"],
                "max_background_fraction": cfg["glare"]["max_background_fraction"],
                "method": "near_saturated_low_saturation_components",
                "false_positive_note": (
                    "Plastic sleeves, holograms, white paper, and printed highlights "
                    "can look like glare. A fully white document can hide a reflection."
                ),
            },
        )
    }
