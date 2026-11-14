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
import urllib.error
import urllib.request
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
EASYOCR_SERVER_URL = os.environ.get(
    "EASYOCR_SERVER_URL", "http://127.0.0.1:8001"
).rstrip("/")
EASYOCR_SLOTS = frozenset({"id_card_front", "id_card_back", "driving_license"})
EASYOCR_TIMEOUT_SECONDS = 30
EASYOCR_TEMP_PREFIX = "orizzonte_easyocr_"
EASYOCR_STALE_TEMP_SECONDS = 3600
EASYOCR_DIAG_PREFIX = "EASYOCR_DIAG"


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


def _easyocr_server_url() -> str:
    configured = os.environ.get("EASYOCR_SERVER_URL", "").strip()
    return (configured or EASYOCR_SERVER_URL).rstrip("/")


def _easyocr_available() -> bool:
    """Check whether the persistent EasyOCR server is reachable."""

    try:
        with urllib.request.urlopen(
            _easyocr_server_url() + "/health",
            timeout=2,
        ) as response:
            return getattr(response, "status", 200) == 200
    except (OSError, urllib.error.URLError, TimeoutError):
        return False


def _cleanup_stale_easyocr_temp_dirs() -> None:
    """Remove temp directories left behind by older subprocess-based runs."""

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


def _easyocr_diag(message: str, *args: object) -> None:
    """Write privacy-safe EasyOCR diagnostics to stderr.

    EasyOCR text itself is never logged.  Only response shape, byte counts,
    status codes, and exception details are emitted.  The prefix lets the
    Django bridge forward these lines without exposing unrelated stderr noise.
    """

    rendered = message % args if args else message
    print(f"{EASYOCR_DIAG_PREFIX} {rendered}", file=sys.stderr, flush=True)


def _coerce_easyocr_text(value: Any) -> str:
    """Accept a string or a list of EasyOCR lines/results."""

    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, list):
        return ""

    parts: list[str] = []
    for item in value:
        if isinstance(item, str):
            candidate = item
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            candidate = item[1] if isinstance(item[1], str) else ""
        elif isinstance(item, dict):
            candidate = item.get("text") if isinstance(item.get("text"), str) else ""
        else:
            candidate = ""
        if candidate.strip():
            parts.append(candidate.strip())
    return "\n".join(parts).strip()


def _extract_easyocr_text(payload: dict[str, Any]) -> str:
    for key in ("text", "lines", "results"):
        text = _coerce_easyocr_text(payload.get(key))
        if text:
            return text
    return ""


def _easyocr_http_request(image_bytes: bytes) -> dict[str, Any]:
    """Send one image to the persistent EasyOCR server."""

    request = urllib.request.Request(
        _easyocr_server_url() + "/ocr",
        data=image_bytes,
        headers={"Content-Type": "application/octet-stream"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            request,
            timeout=EASYOCR_TIMEOUT_SECONDS,
        ) as response:
            status_code = getattr(response, "status", None)
            if status_code is None:
                status_code = response.getcode()
            raw = response.read()
    except urllib.error.HTTPError as exc:
        _easyocr_diag(
            "http_error status=%s reason=%s",
            exc.code,
            exc.reason,
        )
        raise
    except Exception as exc:
        _easyocr_diag(
            "request_exception type=%s error=%s",
            type(exc).__name__,
            exc,
        )
        raise

    _easyocr_diag(
        "http_status=%s raw_bytes=%d",
        status_code,
        len(raw),
    )
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _easyocr_diag(
            "json_error type=%s raw_bytes=%d",
            type(exc).__name__,
            len(raw),
        )
        raise
    if not isinstance(payload, dict):
        _easyocr_diag("json_type=%s", type(payload).__name__)
        raise ValueError("invalid EasyOCR response")

    text = _extract_easyocr_text(payload)
    _easyocr_diag(
        "json_keys=%s has_text=%s text_length=%d",
        sorted(str(key) for key in payload),
        "text" in payload,
        len(text),
    )
    return payload


def _run_easyocr(image_bytes: bytes) -> tuple[str | None, str]:
    """Run EasyOCR through the persistent HTTP server.

    The server keeps the model warm, so this avoids the previous subprocess
    cold start.  Tesseract remains the fallback only when EasyOCR returns an
    error or produces no text at all.
    """

    if not _easyocr_available():
        _easyocr_diag("server_unavailable url=%s", _easyocr_server_url())
        return None, "UNAVAILABLE"

    try:
        payload = _easyocr_http_request(image_bytes)
    except Exception as exc:
        _easyocr_diag(
            "request_failed type=%s error=%s",
            type(exc).__name__,
            exc,
        )
        return None, "FAILED"

    text = _extract_easyocr_text(payload)
    if text:
        _easyocr_diag("text_found chars=%d", len(text))
        return text, "USED"

    status = payload.get("status")
    _easyocr_diag(
        "no_text status=%s text_type=%s",
        status,
        type(payload.get("text")).__name__,
    )
    if status == "FAILED":
        return None, "FAILED"
    return None, "EMPTY"


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
            # EasyOCR produced text, so never fall back to Tesseract.  If no
            # document keyword matched, the classification is NEEDS_REVIEW and
            # the caller receives the EasyOCR text for manual review.
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
    if ocr_status == "USED" and text is not None:
        tesseract_classification = classify_after_quality(
            quality,
            slot,
            ocr_text=text,
            config=config,
        )
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
