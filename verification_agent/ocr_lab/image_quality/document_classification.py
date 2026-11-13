"""Phase 3: document-type classification after the image-quality gate.

The module does not bundle an OCR engine.  A later OCR stage supplies text to
``classify_after_quality``; the tests use synthetic text only.  Classification
is based exclusively on configurable public document titles and, for selfies,
an optional OpenCV Haar face-presence check.  It never uses colour, image
appearance, biometrics, or identity data.
"""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from .output_schema import STATUS_ACCEPTABLE, STATUS_POOR

QUALITY_GATE_USABLE = frozenset({STATUS_ACCEPTABLE, STATUS_POOR})

DECISION_MATCH = "MATCH"
DECISION_NEEDS_REVIEW = "NEEDS_REVIEW"
DECISION_QUALITY_GATE_BLOCKED = "QUALITY_GATE_BLOCKED"

CONSISTENCY_MATCH = "MATCH"
CONSISTENCY_MISMATCH = "MISMATCH"
CONSISTENCY_UNKNOWN = "UNKNOWN"

REASON_KEYWORD_MATCH = "KEYWORD_MATCH"
REASON_SLOT_MISMATCH = "SLOT_MISMATCH"
REASON_LOW_CONFIDENCE = "LOW_CONFIDENCE"
REASON_AMBIGUOUS_KEYWORDS = "AMBIGUOUS_KEYWORDS"
REASON_NO_KEYWORD = "NO_KEYWORD"
REASON_NO_TEXT = "NO_TEXT"
REASON_FACE_DETECTED = "FACE_DETECTED"
REASON_FACE_NOT_DETECTED = "FACE_NOT_DETECTED"
REASON_FACE_DETECTOR_UNAVAILABLE = "FACE_DETECTOR_UNAVAILABLE"
REASON_QUALITY_GATE_BLOCKED = "QUALITY_GATE_BLOCKED"

SLOT_MODES = ("text", "face")

class ClassificationError(Exception):
    """An invalid slot, configuration, or classification input.

    The category is machine-readable and never contains OCR text, a path, or
    identity data.
    """

    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


@dataclass(frozen=True)
class KeywordDefinition:
    text: str
    weight: float
    normalized: str


@dataclass(frozen=True)
class TypeDefinition:
    name: str
    keywords: tuple[KeywordDefinition, ...]

    @property
    def total_weight(self) -> float:
        return sum(keyword.weight for keyword in self.keywords)


@dataclass(frozen=True)
class SlotDefinition:
    name: str
    expected_type: str
    mode: str


@dataclass(frozen=True)
class ClassificationConfig:
    min_confidence: float
    min_confidence_margin: float
    slots: Mapping[str, SlotDefinition]
    types: Mapping[str, TypeDefinition]


@dataclass(frozen=True)
class ClassificationResult:
    declared_slot: str
    expected_type: str | None
    detected_type: str | None
    confidence: float
    consistency: str
    decision: str
    manual_review_required: bool
    reason_code: str
    matched_keywords: tuple[str, ...] = ()
    face_detected: bool | None = None
    quality_gate_blocked: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "declared_slot": self.declared_slot,
            "expected_type": self.expected_type,
            "detected_type": self.detected_type,
            "confidence": round(float(self.confidence), 4),
            "consistency": self.consistency,
            "decision": self.decision,
            "manual_review_required": bool(self.manual_review_required),
            "reason_code": self.reason_code,
            "matched_keywords": list(self.matched_keywords),
            "face_detected": self.face_detected,
            "quality_gate_blocked": bool(self.quality_gate_blocked),
        }


def normalize_classification_text(value: str) -> str:
    """Normalize OCR text for keyword matching without preserving identity text."""

    decomposed = unicodedata.normalize("NFKD", value)
    without_accents = "".join(
        character for character in decomposed if not unicodedata.combining(character)
    )
    normalized = "".join(
        character if character.isalnum() else " "
        for character in without_accents
    )
    return " ".join(normalized.upper().split())


def _contains_keyword(normalized_text: str, normalized_keyword: str) -> bool:
    if not normalized_keyword:
        return False
    return f" {normalized_keyword} " in f" {normalized_text} "


def _keyword_group(value: str) -> str:
    """Group keywords by script so each language is scored independently."""

    if any("\u0600" <= character <= "\u06FF" for character in value):
        return "arabic"
    return "latin"


def _document_layouts_path() -> Path:
    return Path(__file__).with_name("document_layouts.json")


def document_layouts_path() -> Path:
    """Return the default on-disk classification layout file."""

    return _document_layouts_path()


def _number(value: Any, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ClassificationError("invalid_configuration")
    number = float(value)
    if not 0.0 <= number <= 1.0:
        raise ClassificationError("invalid_configuration")
    return number


def load_document_layouts(path: str | Path | None = None) -> ClassificationConfig:
    """Load and validate the configurable slot/type keyword layout."""

    source = Path(path) if path is not None else _document_layouts_path()
    try:
        with source.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        raise ClassificationError("invalid_configuration") from exc

    if not isinstance(data, dict):
        raise ClassificationError("invalid_configuration")

    min_confidence = _number(data.get("min_confidence"), name="min_confidence")
    min_confidence_margin = _number(
        data.get("min_confidence_margin"), name="min_confidence_margin"
    )

    raw_types = data.get("types")
    raw_slots = data.get("slots")
    if not isinstance(raw_types, dict) or not isinstance(raw_slots, dict):
        raise ClassificationError("invalid_configuration")

    types: dict[str, TypeDefinition] = {}
    for type_name, type_data in raw_types.items():
        if not isinstance(type_name, str) or not type_name:
            raise ClassificationError("invalid_configuration")
        if not isinstance(type_data, dict):
            raise ClassificationError("invalid_configuration")
        raw_keywords = type_data.get("keywords")
        if not isinstance(raw_keywords, list):
            raise ClassificationError("invalid_configuration")

        keywords: list[KeywordDefinition] = []
        for keyword_data in raw_keywords:
            if not isinstance(keyword_data, dict):
                raise ClassificationError("invalid_configuration")
            text = keyword_data.get("text")
            weight = keyword_data.get("weight")
            if not isinstance(text, str) or not text.strip():
                raise ClassificationError("invalid_configuration")
            if isinstance(weight, bool) or not isinstance(weight, (int, float)):
                raise ClassificationError("invalid_configuration")
            numeric_weight = float(weight)
            if numeric_weight <= 0.0:
                raise ClassificationError("invalid_configuration")
            normalized = normalize_classification_text(text)
            if not normalized:
                raise ClassificationError("invalid_configuration")
            keywords.append(
                KeywordDefinition(
                    text=text.strip(),
                    weight=numeric_weight,
                    normalized=normalized,
                )
            )
        types[type_name] = TypeDefinition(
            name=type_name,
            keywords=tuple(keywords),
        )

    slots: dict[str, SlotDefinition] = {}
    for slot_name, slot_data in raw_slots.items():
        if not isinstance(slot_name, str) or not slot_name:
            raise ClassificationError("invalid_configuration")
        if not isinstance(slot_data, dict):
            raise ClassificationError("invalid_configuration")
        expected_type = slot_data.get("expected_type")
        mode = slot_data.get("mode", "text")
        if not isinstance(expected_type, str) or expected_type not in types:
            raise ClassificationError("invalid_configuration")
        if mode not in SLOT_MODES:
            raise ClassificationError("invalid_configuration")
        if mode == "text" and not types[expected_type].keywords:
            raise ClassificationError("invalid_configuration")
        slots[slot_name] = SlotDefinition(
            name=slot_name,
            expected_type=expected_type,
            mode=mode,
        )

    if not slots or not types:
        raise ClassificationError("invalid_configuration")

    return ClassificationConfig(
        min_confidence=min_confidence,
        min_confidence_margin=min_confidence_margin,
        slots=slots,
        types=types,
    )


def detect_face(image_bytes: bytes | bytearray) -> bool | None:
    """Return face presence when OpenCV's Haar cascade is available.

    ``None`` means the detector is unavailable or the image could not be
    evaluated.  Callers must turn that into ``NEEDS_REVIEW`` rather than an
    automatic rejection.
    """

    try:
        cascade_root = getattr(getattr(cv2, "data", None), "haarcascades", None)
        if not cascade_root:
            return None
        cascade_path = Path(cascade_root) / "haarcascade_frontalface_default.xml"
        if not cascade_path.is_file():
            return None
        encoded = np.frombuffer(bytes(image_bytes), dtype=np.uint8)
        grayscale = cv2.imdecode(encoded, cv2.IMREAD_GRAYSCALE)
        if grayscale is None:
            return None
        classifier = cv2.CascadeClassifier(str(cascade_path))
        if classifier.empty():
            return None
        faces = classifier.detectMultiScale(
            grayscale,
            scaleFactor=1.1,
            minNeighbors=5,
            minSize=(30, 30),
        )
        return len(faces) > 0
    except Exception:
        return None


def _resolve_slot(
    config: ClassificationConfig,
    declared_slot: str,
) -> SlotDefinition:
    if not isinstance(declared_slot, str):
        raise ClassificationError("invalid_slot")
    slot = config.slots.get(declared_slot)
    if slot is None:
        raise ClassificationError("invalid_slot")
    return slot


def _result(
    slot: SlotDefinition,
    *,
    detected_type: str | None,
    confidence: float,
    consistency: str,
    decision: str,
    manual_review_required: bool,
    reason_code: str,
    matched_keywords: tuple[str, ...] = (),
    face_detected: bool | None = None,
    quality_gate_blocked: bool = False,
) -> ClassificationResult:
    return ClassificationResult(
        declared_slot=slot.name,
        expected_type=slot.expected_type,
        detected_type=detected_type,
        confidence=confidence,
        consistency=consistency,
        decision=decision,
        manual_review_required=manual_review_required,
        reason_code=reason_code,
        matched_keywords=matched_keywords,
        face_detected=face_detected,
        quality_gate_blocked=quality_gate_blocked,
    )


def _classify_face(
    slot: SlotDefinition,
    *,
    face_detected: bool | None,
    image_bytes: bytes | bytearray | None,
    face_detector: Callable[[bytes | bytearray], bool | None] | None,
) -> ClassificationResult:
    if face_detected is not None and not isinstance(face_detected, bool):
        raise ClassificationError("invalid_face_result")

    observed = face_detected
    if observed is None and image_bytes is not None:
        detector = face_detector if face_detector is not None else detect_face
        observed = detector(image_bytes)
        if observed is not None and not isinstance(observed, bool):
            raise ClassificationError("invalid_face_result")

    if observed is True:
        return _result(
            slot,
            detected_type=slot.expected_type,
            confidence=1.0,
            consistency=CONSISTENCY_MATCH,
            decision=DECISION_MATCH,
            manual_review_required=False,
            reason_code=REASON_FACE_DETECTED,
            face_detected=True,
        )
    if observed is False:
        return _result(
            slot,
            detected_type=None,
            confidence=0.0,
            consistency=CONSISTENCY_UNKNOWN,
            decision=DECISION_NEEDS_REVIEW,
            manual_review_required=True,
            reason_code=REASON_FACE_NOT_DETECTED,
            face_detected=False,
        )
    return _result(
        slot,
        detected_type=None,
        confidence=0.0,
        consistency=CONSISTENCY_UNKNOWN,
        decision=DECISION_NEEDS_REVIEW,
        manual_review_required=True,
        reason_code=REASON_FACE_DETECTOR_UNAVAILABLE,
        face_detected=None,
    )


def _classify_text(
    slot: SlotDefinition,
    config: ClassificationConfig,
    ocr_text: str | None,
) -> ClassificationResult:
    normalized_text = normalize_classification_text(ocr_text) if ocr_text else ""
    if not normalized_text:
        return _result(
            slot,
            detected_type=None,
            confidence=0.0,
            consistency=CONSISTENCY_UNKNOWN,
            decision=DECISION_NEEDS_REVIEW,
            manual_review_required=True,
            reason_code=REASON_NO_TEXT,
        )

    scored: list[tuple[float, str, tuple[str, ...]]] = []
    for type_name, type_definition in config.types.items():
        groups: dict[str, list[Any]] = {}
        for keyword in type_definition.keywords:
            if not _contains_keyword(normalized_text, keyword.normalized):
                continue
            group = _keyword_group(keyword.text)
            score, matched = groups.setdefault(group, [0.0, []])
            groups[group] = [score + keyword.weight, [*matched, keyword.text]]

        if not groups:
            continue

        best_confidence = 0.0
        best_matched: tuple[str, ...] = ()
        for group, (score, matched) in groups.items():
            group_total = sum(
                keyword.weight
                for keyword in type_definition.keywords
                if _keyword_group(keyword.text) == group
            )
            if group_total <= 0.0:
                continue
            confidence = min(1.0, score / group_total)
            if confidence > best_confidence:
                best_confidence = confidence
                best_matched = tuple(matched)
        if best_matched:
            scored.append((best_confidence, type_name, best_matched))

    if not scored:
        return _result(
            slot,
            detected_type=None,
            confidence=0.0,
            consistency=CONSISTENCY_UNKNOWN,
            decision=DECISION_NEEDS_REVIEW,
            manual_review_required=True,
            reason_code=REASON_NO_KEYWORD,
        )

    scored.sort(key=lambda item: (-item[0], item[1]))
    top_confidence, top_type, top_keywords = scored[0]
    second_confidence = scored[1][0] if len(scored) > 1 else 0.0

    if top_confidence + 1e-9 < config.min_confidence:
        return _result(
            slot,
            detected_type=top_type,
            confidence=top_confidence,
            consistency=CONSISTENCY_UNKNOWN,
            decision=DECISION_NEEDS_REVIEW,
            manual_review_required=True,
            reason_code=REASON_LOW_CONFIDENCE,
            matched_keywords=top_keywords,
        )

    if (
        len(scored) > 1
        and top_confidence - second_confidence + 1e-9 < config.min_confidence_margin
    ):
        return _result(
            slot,
            detected_type=None,
            confidence=top_confidence,
            consistency=CONSISTENCY_UNKNOWN,
            decision=DECISION_NEEDS_REVIEW,
            manual_review_required=True,
            reason_code=REASON_AMBIGUOUS_KEYWORDS,
            matched_keywords=top_keywords,
        )

    if top_type == slot.expected_type:
        return _result(
            slot,
            detected_type=top_type,
            confidence=top_confidence,
            consistency=CONSISTENCY_MATCH,
            decision=DECISION_MATCH,
            manual_review_required=False,
            reason_code=REASON_KEYWORD_MATCH,
            matched_keywords=top_keywords,
        )

    return _result(
        slot,
        detected_type=top_type,
        confidence=top_confidence,
        consistency=CONSISTENCY_MISMATCH,
        decision=DECISION_NEEDS_REVIEW,
        manual_review_required=True,
        reason_code=REASON_SLOT_MISMATCH,
        matched_keywords=top_keywords,
    )


def classify_document(
    declared_slot: str,
    *,
    ocr_text: str | None = None,
    face_detected: bool | None = None,
    image_bytes: bytes | bytearray | None = None,
    config: ClassificationConfig | None = None,
    face_detector: Callable[[bytes | bytearray], bool | None] | None = None,
) -> ClassificationResult:
    """Classify the detected document type for one declared upload slot.

    Text slots require ``ocr_text``.  A selfie slot uses only face presence; an
    unavailable detector or an uncertain result returns ``NEEDS_REVIEW``.
    """

    selected_config = config if config is not None else load_document_layouts()
    slot = _resolve_slot(selected_config, declared_slot)

    if slot.mode == "face":
        return _classify_face(
            slot,
            face_detected=face_detected,
            image_bytes=image_bytes,
            face_detector=face_detector,
        )
    return _classify_text(slot, selected_config, ocr_text)


def classify_after_quality(
    quality_result: Mapping[str, Any],
    declared_slot: str,
    *,
    ocr_text: str | None = None,
    face_detected: bool | None = None,
    image_bytes: bytes | bytearray | None = None,
    config: ClassificationConfig | None = None,
    face_detector: Callable[[bytes | bytearray], bool | None] | None = None,
) -> ClassificationResult:
    """Run classification only when the quality gate returned a usable image.

    A failed quality gate blocks classification.  The result is explicit and
    does not call the keyword or face stages.
    """

    selected_config = config if config is not None else load_document_layouts()
    slot = _resolve_slot(selected_config, declared_slot)

    if not isinstance(quality_result, Mapping):
        raise ClassificationError("invalid_quality_result")
    status = quality_result.get("status")
    if status not in QUALITY_GATE_USABLE:
        return _result(
            slot,
            detected_type=None,
            confidence=0.0,
            consistency=CONSISTENCY_UNKNOWN,
            decision=DECISION_QUALITY_GATE_BLOCKED,
            manual_review_required=False,
            reason_code=REASON_QUALITY_GATE_BLOCKED,
            quality_gate_blocked=True,
        )

    return classify_document(
        declared_slot,
        ocr_text=ocr_text,
        face_detected=face_detected,
        image_bytes=image_bytes,
        config=selected_config,
        face_detector=face_detector,
    )


__all__ = [
    "CONSISTENCY_MATCH",
    "CONSISTENCY_MISMATCH",
    "CONSISTENCY_UNKNOWN",
    "ClassificationConfig",
    "ClassificationError",
    "ClassificationResult",
    "DECISION_MATCH",
    "DECISION_NEEDS_REVIEW",
    "DECISION_QUALITY_GATE_BLOCKED",
    "QUALITY_GATE_USABLE",
    "classify_after_quality",
    "classify_document",
    "detect_face",
    "document_layouts_path",
    "load_document_layouts",
    "normalize_classification_text",
]
