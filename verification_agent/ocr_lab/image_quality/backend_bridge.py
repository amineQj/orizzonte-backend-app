"""JSON bridge used by the Django backend.

The backend runs this module with the verification_agent virtual environment so
the backend process does not need OpenCV or numpy.  It reads one JSON object
from stdin and writes one JSON object to stdout.  No filename, path, image
content, or OCR text is returned unless ``debug_ocr`` is explicitly requested.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
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
TESSERACT_LANGUAGE = "fra+ara"
DEFAULT_EASYOCR_PYTHON_PATH = r"C:\orizzonte_easyocr_env\Scripts\python.exe"
EASYOCR_RUNNER_PATH = Path(__file__).resolve().parents[2] / "easyocr_runner.py"
EASYOCR_SLOTS = frozenset({"id_card_front", "id_card_back", "driving_license"})
EASYOCR_TIMEOUT_SECONDS = 120
EASYOCR_TEMP_PREFIX = "orizzonte_easyocr_"
EASYOCR_STALE_TEMP_SECONDS = 3600


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


def _tesseract_binary() -> str | None:
    configured = os.environ.get("TESSERACT_CMD", "").strip()
    if configured:
        candidate = Path(configured)
        if candidate.is_file():
            return str(candidate)

    found = shutil.which("tesseract")
    if found is not None:
        return found

    candidates: list[Path] = []
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        candidates.append(
            Path(local_app_data) / "Programs" / "Tesseract-OCR" / "tesseract.exe"
        )
    candidates.extend(
        (
            Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe"),
            Path(r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"),
        )
    )
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def _tessdata_prefix(binary: str) -> str | None:
    configured = os.environ.get("TESSDATA_PREFIX", "").strip()
    if configured:
        candidate = Path(configured)
        if candidate.is_dir():
            return str(candidate)

    candidate = Path(binary).resolve().parent / "tessdata"
    if candidate.is_dir():
        return str(candidate)
    return None


def _tesseract_available() -> bool:
    return _tesseract_binary() is not None


def _easyocr_python() -> str:
    configured = os.environ.get("EASYOCR_PYTHON_PATH", "").strip()
    return configured or DEFAULT_EASYOCR_PYTHON_PATH


def _easyocr_available() -> bool:
    return Path(_easyocr_python()).is_file() and EASYOCR_RUNNER_PATH.is_file()


def _cleanup_stale_easyocr_temp_dirs() -> None:
    """Remove temp directories left behind if an earlier process was killed."""

    root = Path(tempfile.gettempdir())
    now = time.time()
    for entry in root.glob(f"{EASYOCR_TEMP_PREFIX}*"):
        try:
            if not entry.is_dir():
                continue
            if now - entry.stat().st_mtime < EASYOCR_STALE_TEMP_SECONDS:
                continue
            shutil.rmtree(entry, ignore_errors=True)
        except OSError:
            continue


def _run_easyocr(image_bytes: bytes) -> tuple[str | None, str]:
    """Run EasyOCR in its isolated environment through a subprocess.

    The image is written to a short-lived temporary file because EasyOCR reads
    from a path.  Neither the path nor the recognized text is logged.
    """

    _cleanup_stale_easyocr_temp_dirs()
    if not _easyocr_available():
        return None, "UNAVAILABLE"

    try:
        with tempfile.TemporaryDirectory(prefix=EASYOCR_TEMP_PREFIX) as temp_dir:
            image_path = Path(temp_dir) / "input.png"
            image_path.write_bytes(image_bytes)
            completed = subprocess.run(
                [
                    _easyocr_python(),
                    str(EASYOCR_RUNNER_PATH),
                    str(image_path),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=EASYOCR_TIMEOUT_SECONDS,
                check=False,
            )
    except (OSError, subprocess.TimeoutExpired):
        return None, "FAILED"

    if completed.returncode != 0:
        return None, "FAILED"

    try:
        payload = json.loads(completed.stdout.decode("utf-8", errors="replace"))
    except (UnicodeDecodeError, ValueError):
        return None, "FAILED"
    if not isinstance(payload, dict):
        return None, "FAILED"

    status = payload.get("status")
    text = payload.get("text")
    if status == "USED" and isinstance(text, str) and text.strip():
        return text.strip(), "USED"
    if status == "EMPTY":
        return None, "EMPTY"
    return None, "FAILED"


def _run_tesseract(image_bytes: bytes) -> tuple[str | None, str]:
    binary = _tesseract_binary()
    if binary is None:
        return None, "UNAVAILABLE"
    language = (
        os.environ.get("TESSERACT_LANGUAGE", TESSERACT_LANGUAGE).strip()
        or TESSERACT_LANGUAGE
    )
    env = os.environ.copy()
    tessdata = _tessdata_prefix(binary)
    if tessdata is not None:
        env["TESSDATA_PREFIX"] = tessdata
    else:
        env.pop("TESSDATA_PREFIX", None)
    try:
        completed = subprocess.run(
            [binary, "stdin", "stdout", "-l", language, "--psm", "3"],
            input=image_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=OCR_TIMEOUT_SECONDS,
            check=False,
            env=env,
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
) -> tuple[dict[str, Any], dict[str, Any], str | None]:
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
            None,
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
            None,
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
            None,
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
            None,
        )

    easyocr_classification: ClassificationResult | None = None
    easyocr_text: str | None = None
    easyocr_status = "UNAVAILABLE"

    if slot in EASYOCR_SLOTS:
        easyocr_text, easyocr_status = _run_easyocr(image_bytes)
        if easyocr_status == "USED" and easyocr_text is not None:
            easyocr_classification = classify_after_quality(
                quality,
                slot,
                ocr_text=easyocr_text,
                config=config,
            )
            if easyocr_classification.detected_type is not None:
                return (
                    easyocr_classification.to_dict(),
                    _ocr_metadata(
                        used=True,
                        available=True,
                        source="easyocr",
                        reason="OCR_USED",
                    ),
                    easyocr_text,
                )

    text, ocr_status = _run_tesseract(image_bytes)
    tesseract_classification: ClassificationResult | None = None
    if ocr_status == "USED" and text is not None:
        tesseract_classification = classify_after_quality(
            quality,
            slot,
            ocr_text=text,
            config=config,
        )
        if tesseract_classification.detected_type is not None:
            return (
                tesseract_classification.to_dict(),
                _ocr_metadata(
                    used=True,
                    available=True,
                    source="tesseract",
                    reason="OCR_USED",
                ),
                text,
            )

    if easyocr_classification is not None:
        return (
            easyocr_classification.to_dict(),
            _ocr_metadata(
                used=True,
                available=True,
                source="easyocr",
                reason="OCR_USED",
            ),
            easyocr_text,
        )

    if tesseract_classification is not None:
        return (
            tesseract_classification.to_dict(),
            _ocr_metadata(
                used=True,
                available=True,
                source="tesseract",
                reason="OCR_USED",
            ),
            text,
        )

    classification = _no_ocr_result(slot, config)
    available = (
        ocr_status != "UNAVAILABLE" or easyocr_status != "UNAVAILABLE"
    )
    reason = "NO_OCR" if not available else "OCR_FAILED"
    return (
        classification.to_dict(),
        _ocr_metadata(
            used=False,
            available=available,
            source="none",
            reason=reason,
        ),
        None,
    )


def handle(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise BridgeError("INVALID_REQUEST")

    encoded = payload.get("image_base64")
    slot = payload.get("slot")
    ocr_text = payload.get("ocr_text")
    allow_ocr = payload.get("allow_ocr", True)
    debug_ocr = payload.get("debug_ocr", False)

    if not isinstance(encoded, str) or not encoded:
        raise BridgeError("INVALID_REQUEST")
    if not isinstance(slot, str) or slot not in SLOT_PROFILES:
        raise BridgeError("INVALID_SLOT")
    if ocr_text is not None and not isinstance(ocr_text, str):
        raise BridgeError("INVALID_REQUEST")
    if not isinstance(allow_ocr, bool):
        raise BridgeError("INVALID_REQUEST")
    if not isinstance(debug_ocr, bool):
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
    classification, ocr, ocr_debug_text = _classify(
        image_bytes=image_bytes,
        slot=slot,
        quality=quality,
        ocr_text=ocr_text,
        allow_ocr=allow_ocr,
        config=config,
    )
    response: dict[str, Any] = {
        "quality": quality,
        "classification": classification,
        "ocr": ocr,
    }
    if debug_ocr:
        response["ocr_debug_text"] = ocr_debug_text
    return response


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
