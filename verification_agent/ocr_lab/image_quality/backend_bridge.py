"""JSON bridge used by the Django backend.

The backend runs this module with the verification_agent virtual environment so
the backend process does not need OpenCV or numpy.  It reads one JSON object
from stdin and writes one JSON object to stdout.  No filename, path, image
content, or OCR text is returned.
"""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
import sys
from typing import Any

from .analyzer import analyze_bytes
from .document_classification import (
    CONSISTENCY_UNKNOWN,
    DECISION_NEEDS_REVIEW,
    ClassificationConfig,
    ClassificationResult,
    classify_after_quality,
    load_document_layouts,
)
from .output_schema import STATUS_ACCEPTABLE, STATUS_POOR

SLOT_PROFILES = {
    "selfie": "selfie",
    "id_card_front": "id_card",
    "id_card_back": "id_card",
    "driving_license": "driving_license",
    "vehicle_registration": "vehicle_registration",
}

QUALITY_GATE_USABLE = frozenset({STATUS_ACCEPTABLE, STATUS_POOR})
MAX_INPUT_BYTES = 16 * 1024 * 1024
OCR_TIMEOUT_SECONDS = 20


class BridgeError(Exception):
    """A machine-readable bridge failure; details never reach the caller."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _no_ocr_result(
    slot: str,
    config: ClassificationConfig,
) -> ClassificationResult:
    slot_definition = config.slots[slot]
    return ClassificationResult(
        declared_slot=slot,
        expected_type=slot_definition.expected_type,
        detected_type=None,
        confidence=0.0,
        consistency=CONSISTENCY_UNKNOWN,
        decision=DECISION_NEEDS_REVIEW,
        manual_review_required=True,
        reason_code="NO_OCR",
    )


def _tesseract_available() -> bool:
    return shutil.which("tesseract") is not None


def _run_tesseract(image_bytes: bytes) -> tuple[str | None, str]:
    binary = shutil.which("tesseract")
    if binary is None:
        return None, "UNAVAILABLE"
    try:
        completed = subprocess.run(
            [binary, "stdin", "stdout", "-l", "eng", "--psm", "3"],
            input=image_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=OCR_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, "FAILED"
    if completed.returncode != 0:
        return None, "FAILED"
    text = completed.stdout.decode("utf-8", errors="replace").strip()
    if not text:
        return None, "EMPTY"
    return text, "USED"


def _ocr_metadata(
    *,
    used: bool,
    available: bool,
    source: str,
    reason: str,
) -> dict[str, Any]:
    return {
        "used": bool(used),
        "available": bool(available),
        "source": source,
        "reason": reason,
    }


def _classify(
    *,
    image_bytes: bytes,
    slot: str,
    quality: dict[str, Any],
    ocr_text: str | None,
    allow_ocr: bool,
    config: ClassificationConfig,
) -> tuple[dict[str, Any], dict[str, Any]]:
    status = quality.get("status")
    if status not in QUALITY_GATE_USABLE:
        classification = classify_after_quality(
            quality,
            slot,
            config=config,
        )
        return (
            classification.to_dict(),
            _ocr_metadata(
                used=False,
                available=_tesseract_available(),
                source="none",
                reason="NOT_REQUIRED",
            ),
        )

    slot_definition = config.slots[slot]
    if slot_definition.mode == "face":
        classification = classify_after_quality(
            quality,
            slot,
            image_bytes=image_bytes,
            config=config,
        )
        return (
            classification.to_dict(),
            _ocr_metadata(
                used=False,
                available=_tesseract_available(),
                source="none",
                reason="NOT_REQUIRED",
            ),
        )

    if ocr_text is not None and ocr_text.strip():
        classification = classify_after_quality(
            quality,
            slot,
            ocr_text=ocr_text,
            config=config,
        )
        return (
            classification.to_dict(),
            _ocr_metadata(
                used=True,
                available=True,
                source="provided",
                reason="OCR_USED",
            ),
        )

    if not allow_ocr:
        classification = _no_ocr_result(slot, config)
        return (
            classification.to_dict(),
            _ocr_metadata(
                used=False,
                available=False,
                source="none",
                reason="NO_OCR",
            ),
        )

    text, ocr_status = _run_tesseract(image_bytes)
    if ocr_status == "USED" and text is not None:
        classification = classify_after_quality(
            quality,
            slot,
            ocr_text=text,
            config=config,
        )
        return (
            classification.to_dict(),
            _ocr_metadata(
                used=True,
                available=True,
                source="tesseract",
                reason="OCR_USED",
            ),
        )

    classification = _no_ocr_result(slot, config)
    available = ocr_status != "UNAVAILABLE"
    reason = "NO_OCR" if not available else "OCR_FAILED"
    return (
        classification.to_dict(),
        _ocr_metadata(
            used=False,
            available=available,
            source="none",
            reason=reason,
        ),
    )


def handle(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise BridgeError("INVALID_REQUEST")

    encoded = payload.get("image_base64")
    slot = payload.get("slot")
    ocr_text = payload.get("ocr_text")
    allow_ocr = payload.get("allow_ocr", True)

    if not isinstance(encoded, str) or not encoded:
        raise BridgeError("INVALID_REQUEST")
    if not isinstance(slot, str) or slot not in SLOT_PROFILES:
        raise BridgeError("INVALID_SLOT")
    if ocr_text is not None and not isinstance(ocr_text, str):
        raise BridgeError("INVALID_REQUEST")
    if not isinstance(allow_ocr, bool):
        raise BridgeError("INVALID_REQUEST")

    try:
        image_bytes = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise BridgeError("INVALID_IMAGE") from exc
    if not image_bytes or len(image_bytes) > MAX_INPUT_BYTES:
        raise BridgeError("INVALID_IMAGE")

    config = load_document_layouts()
    quality = analyze_bytes(
        image_bytes,
        SLOT_PROFILES[slot],
        layout=None,
    )
    classification, ocr = _classify(
        image_bytes=image_bytes,
        slot=slot,
        quality=quality,
        ocr_text=ocr_text,
        allow_ocr=allow_ocr,
        config=config,
    )
    return {
        "quality": quality,
        "classification": classification,
        "ocr": ocr,
    }


def main() -> int:
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        if len(raw) > MAX_INPUT_BYTES:
            raise BridgeError("REQUEST_TOO_LARGE")
        payload = json.loads(raw.decode("utf-8"))
        result = handle(payload)
    except BridgeError as exc:
        print(json.dumps({"error": {"code": exc.code}}, ensure_ascii=True))
        return 2
    except Exception:
        print(json.dumps({"error": {"code": "BRIDGE_FAILED"}}, ensure_ascii=True))
        return 2

    print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
