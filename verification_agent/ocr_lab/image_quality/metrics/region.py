"""Select the area used for sharpness, exposure, contrast, and glare metrics."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import cv2
import numpy as np

from ..normalization import WorkingImage
from .boundaries import BoundaryResult


@dataclass(frozen=True)
class EvaluationRegion:
    array: np.ndarray
    bbox: tuple[int, int, int, int]
    source: str


def _warp_document(
    working: WorkingImage,
    boundary: BoundaryResult,
    inset: float,
) -> EvaluationRegion | None:
    if boundary.quad is None:
        return None
    quad = boundary.quad.astype(np.float32)
    top = float(np.linalg.norm(quad[1] - quad[0]))
    right = float(np.linalg.norm(quad[2] - quad[1]))
    bottom = float(np.linalg.norm(quad[3] - quad[2]))
    left = float(np.linalg.norm(quad[0] - quad[3]))
    document_width = int(round(max(top, bottom)))
    document_height = int(round(max(left, right)))
    if document_width < 8 or document_height < 8:
        return None

    destination = np.array(
        [
            [0, 0],
            [document_width - 1, 0],
            [document_width - 1, document_height - 1],
            [0, document_height - 1],
        ],
        dtype=np.float32,
    )
    try:
        matrix = cv2.getPerspectiveTransform(quad, destination)
        warped = cv2.warpPerspective(
            working.array,
            matrix,
            (document_width, document_height),
            flags=cv2.INTER_LINEAR,
        )
    except (cv2.error, ValueError, MemoryError):
        return None
    if warped.size == 0:
        return None
    inset_x = int(round(document_width * inset))
    inset_y = int(round(document_height * inset))
    x0 = max(0, inset_x)
    y0 = max(0, inset_y)
    x1 = min(document_width, document_width - inset_x)
    y1 = min(document_height, document_height - inset_y)
    if x1 - x0 < 8 or y1 - y0 < 8:
        return None
    return EvaluationRegion(
        array=warped[y0:y1, x0:x1],
        bbox=(x0, y0, x1 - x0, y1 - y0),
        source="document_warped",
    )


def select_evaluation_region(
    working: WorkingImage,
    boundary: BoundaryResult | None,
    cfg: dict[str, Any],
    document_analysis: bool,
) -> EvaluationRegion:
    """Use the rectified document interior, else a configurable central crop."""

    height, width = working.array.shape[:2]
    if document_analysis and boundary is not None and boundary.valid:
        inset = float(cfg["evaluation_region"]["document_inset_fraction"])
        warped_region = _warp_document(working, boundary, inset)
        if warped_region is not None:
            return warped_region

        if boundary.bbox is not None:
            x, y, box_width, box_height = boundary.bbox
            inset_x = int(round(box_width * inset))
            inset_y = int(round(box_height * inset))
            x0 = max(0, x + inset_x)
            y0 = max(0, y + inset_y)
            x1 = min(width, x + box_width - inset_x)
            y1 = min(height, y + box_height - inset_y)
            if x1 - x0 >= 8 and y1 - y0 >= 8:
                return EvaluationRegion(
                    array=working.array[y0:y1, x0:x1],
                    bbox=(x0, y0, x1 - x0, y1 - y0),
                    source="document_bounding_box",
                )

    fraction = float(np.clip(cfg["evaluation_region"]["central_fraction"], 0.05, 1.0))
    crop_width = max(1, min(width, int(round(width * fraction))))
    crop_height = max(1, min(height, int(round(height * fraction))))
    x0 = max(0, (width - crop_width) // 2)
    y0 = max(0, (height - crop_height) // 2)
    return EvaluationRegion(
        array=working.array[y0 : y0 + crop_height, x0 : x0 + crop_width],
        bbox=(x0, y0, crop_width, crop_height),
        source="central",
    )
