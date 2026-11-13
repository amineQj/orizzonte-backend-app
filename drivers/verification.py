"""Bridge from Django to the standalone verification_agent module.

The backend virtual environment intentionally does not contain OpenCV or numpy.
The image-quality/classification module therefore runs in its own existing
virtual environment through a JSON stdin/stdout bridge.  No uploaded filename,
path, OCR text, or image content is written to disk or logged here.
"""

from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path

from django.conf import settings

QUALITY_ACCEPTABLE = "ACCEPTABLE_QUALITY"
QUALITY_RECAPTURE = "NEEDS_RECAPTURE"

FINAL_ACCEPT = "ACCEPT"
FINAL_MANUAL_REVIEW = "MANUAL_REVIEW"
FINAL_REJECT = "REJECT"

CLASSIFICATION_MATCH = "MATCH"


class VerificationUnavailable(Exception):
    """Raised when the isolated verification process cannot be executed."""

    def __init__(self, category: str = "verification_unavailable") -> None:
        super().__init__(category)
        self.category = category


def _verification_python() -> Path:
    configured = getattr(settings, "VERIFICATION_AGENT_PYTHON", None)
    if configured:
        candidate = Path(str(configured))
        if candidate.is_file():
            return candidate
        raise VerificationUnavailable("verification_python_missing")

    root = Path(settings.BASE_DIR) / "verification_agent"
    candidates = (
        root / ".venv" / "Scripts" / "python.exe",
        root / ".venv" / "bin" / "python",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise VerificationUnavailable("verification_python_missing")


def _bridge_working_directory() -> Path:
    directory = Path(settings.BASE_DIR) / "verification_agent" / "ocr_lab"
    if not (directory / "image_quality" / "backend_bridge.py").is_file():
        raise VerificationUnavailable("verification_bridge_missing")
    return directory


def _timeout_seconds() -> float:
    configured = getattr(settings, "VERIFICATION_AGENT_TIMEOUT_SECONDS", 30)
    try:
        value = float(configured)
    except (TypeError, ValueError):
        return 30.0
    return value if value > 0 else 30.0


def run_verification(
    image_bytes: bytes,
    slot: str,
    *,
    ocr_text: str | None = None,
    debug_ocr: bool = False,
) -> dict:
    """Run the quality gate and classification in the verification environment.

    ``ocr_text`` is reserved for trusted internal callers; the HTTP endpoint
    never accepts OCR text from an uploader. ``debug_ocr`` returns the raw
    Tesseract text only when the caller has explicitly enabled debug output.
    """

    if not image_bytes:
        raise VerificationUnavailable("empty_image")

    python = _verification_python()
    working_directory = _bridge_working_directory()
    payload: dict[str, object] = {
        "image_base64": base64.b64encode(image_bytes).decode("ascii"),
        "slot": slot,
        "allow_ocr": not bool(
            getattr(settings, "VERIFICATION_AGENT_DISABLE_OCR", False)
        ),
        "debug_ocr": bool(debug_ocr),
    }
    if ocr_text is not None:
        payload["ocr_text"] = ocr_text

    try:
        completed = subprocess.run(
            [str(python), "-m", "image_quality.backend_bridge"],
            cwd=str(working_directory),
            input=json.dumps(payload).encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=_timeout_seconds(),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise VerificationUnavailable("verification_process_failed") from exc

    if completed.returncode != 0:
        raise VerificationUnavailable("verification_process_failed")

    try:
        result = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise VerificationUnavailable("verification_response_invalid") from exc

    if not isinstance(result, dict) or result.get("error"):
        raise VerificationUnavailable("verification_response_invalid")
    if not isinstance(result.get("quality"), dict):
        raise VerificationUnavailable("verification_response_invalid")
    if not isinstance(result.get("classification"), dict):
        raise VerificationUnavailable("verification_response_invalid")
    if not isinstance(result.get("ocr"), dict):
        raise VerificationUnavailable("verification_response_invalid")
    return result


def final_decision(result: dict) -> str:
    """Map quality and classification to the safe backend decision."""

    quality = result.get("quality") or {}
    classification = result.get("classification") or {}
    quality_status = quality.get("status")
    classification_decision = classification.get("decision")

    if quality_status == QUALITY_RECAPTURE:
        return FINAL_REJECT
    if (
        quality_status == QUALITY_ACCEPTABLE
        and classification_decision == CLASSIFICATION_MATCH
    ):
        return FINAL_ACCEPT
    return FINAL_MANUAL_REVIEW


def build_response(
    slot: str,
    result: dict,
    *,
    include_debug: bool = False,
) -> dict:
    """Flatten the fields expected by the API contract without leaking text."""

    quality = result["quality"]
    classification = result["classification"]
    response = {
        "slot": slot,
        "quality_status": quality.get("status"),
        "reason_codes": list(quality.get("reason_codes") or []),
        "quality": quality,
        "classification": classification,
        "consistency": classification.get("consistency"),
        "ocr": result["ocr"],
        "final_decision": final_decision(result),
    }
    if include_debug and "ocr_debug_text" in result:
        response["ocr_debug_text"] = result["ocr_debug_text"]
    return response
