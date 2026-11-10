"""Working-copy normalisation.

Metrics are computed on a bounded working copy so that a 48 MP camera image
and a 2 MP image are judged on comparable pixel statistics.  The original
resolution is kept separately for resolution checks.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from .image_loader import LoadedImage


@dataclass(frozen=True)
class WorkingImage:
    array: np.ndarray
    original_width: int
    original_height: int
    oriented_width: int
    oriented_height: int
    working_width: int
    working_height: int
    scale_x: float
    scale_y: float
    resized: bool
    max_side: int


def normalize_image(image: LoadedImage, max_side: int) -> WorkingImage:
    """Downscale only; never upscale, so blur cannot be hidden by resampling."""

    height, width = image.array.shape[:2]
    side = max(1, int(max_side))
    longest = max(width, height)

    if longest <= side:
        working = image.array
        working_width, working_height = width, height
        resized = False
    else:
        scale = side / float(longest)
        working_width = max(1, int(round(width * scale)))
        working_height = max(1, int(round(height * scale)))
        working = cv2.resize(
            image.array,
            (working_width, working_height),
            interpolation=cv2.INTER_AREA,
        )
        resized = True

    return WorkingImage(
        array=working,
        original_width=image.original_width,
        original_height=image.original_height,
        oriented_width=image.oriented_width,
        oriented_height=image.oriented_height,
        working_width=working_width,
        working_height=working_height,
        scale_x=working_width / float(image.oriented_width),
        scale_y=working_height / float(image.oriented_height),
        resized=resized,
        max_side=side,
    )
