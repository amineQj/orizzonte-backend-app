"""Standalone, offline quality gate and document-slot classifier.

The package contains no OCR engine, no biometrics, no identity matching, and no
fraud decision.  It first decides whether an image is technically suitable for
a later OCR stage, then optionally checks whether caller-supplied OCR text is
consistent with the declared upload slot.  Selfie classification may use an
optional OpenCV Haar face-presence check when the cascade is available.
"""

from .analyzer import analyze_bytes, analyze_path
from .document_classification import (
    CONSISTENCY_MATCH,
    CONSISTENCY_MISMATCH,
    CONSISTENCY_UNKNOWN,
    DECISION_MATCH,
    DECISION_NEEDS_REVIEW,
    DECISION_QUALITY_GATE_BLOCKED,
    ClassificationError,
    ClassificationResult,
    classify_after_quality,
    classify_document,
    load_document_layouts,
)
from .output_schema import ALGORITHM_VERSION, SCHEMA_VERSION

__all__ = [
    "ALGORITHM_VERSION",
    "CONSISTENCY_MATCH",
    "CONSISTENCY_MISMATCH",
    "CONSISTENCY_UNKNOWN",
    "DECISION_MATCH",
    "DECISION_NEEDS_REVIEW",
    "DECISION_QUALITY_GATE_BLOCKED",
    "SCHEMA_VERSION",
    "ClassificationError",
    "ClassificationResult",
    "analyze_bytes",
    "analyze_path",
    "classify_after_quality",
    "classify_document",
    "load_document_layouts",
]
__version__ = "1.1.0"
