"""Standalone, offline image-quality gate for ORIZZONTE documents.

This package deliberately contains no OCR, no face detection, no identity
matching, and no fraud decision.  It answers only whether an image is
technically suitable for a later OCR stage.
"""

from .analyzer import analyze_bytes, analyze_path
from .output_schema import ALGORITHM_VERSION, SCHEMA_VERSION

__all__ = [
    "ALGORITHM_VERSION",
    "SCHEMA_VERSION",
    "analyze_bytes",
    "analyze_path",
]
__version__ = "1.0.0"
