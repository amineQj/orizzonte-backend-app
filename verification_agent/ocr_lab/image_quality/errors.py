"""Structured errors for the image-quality module.

Errors deliberately carry only machine-readable categories.  They never carry
file paths, image contents, or human-readable diagnostic messages.
"""

from __future__ import annotations


class ImageQualityError(Exception):
    """An expected, safely reportable image-processing failure."""

    def __init__(self, category: str, reason_code: str = "UNREADABLE_IMAGE") -> None:
        super().__init__(category)
        self.category = category
        self.reason_code = reason_code


class ProfileError(Exception):
    """An invalid profile, layout, or threshold configuration."""

    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category
