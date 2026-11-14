from __future__ import annotations

import base64
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from image_quality import backend_bridge
from image_quality.document_classification import (
    CONSISTENCY_MATCH,
    DECISION_MATCH,
    DECISION_NEEDS_REVIEW,
)

from . import synthetic as s

ARABIC_ID_TEXT = (
    "الجمهورية الجزائرية الديمقراطية الشعبية\n"
    "بطاقة التعريف الوطنية\n"
    "SPECIMEN / TEST"
)
FRENCH_ID_TEXT = (
    "REPUBLIQUE ALGERIENNE DEMOCRATIQUE ET POPULAIRE\n"
    "CARTE NATIONALE D'IDENTITE\n"
    "SPECIMEN / TEST"
)
ARABIC_GENERIC_TEXT = (
    "الجمهورية الجزائرية الديمقراطية الشعبية\n"
    "SPECIMEN / TEST"
)
ARABIC_DRIVING_TEXT = (
    "الجمهورية الجزائرية\n"
    "رخصة السياقة\n"
    "SPECIMEN / TEST"
)


def _payload(
    image_bytes: bytes,
    slot: str = "id_card_front",
) -> dict[str, str | bool]:
    return {
        "image_base64": base64.b64encode(image_bytes).decode("ascii"),
        "slot": slot,
        "allow_ocr": True,
    }


def test_easyocr_server_url_uses_environment_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EASYOCR_SERVER_URL", "http://example.test:9000/")

    assert backend_bridge._easyocr_server_url() == "http://example.test:9000"


def test_stale_easyocr_temp_dirs_are_removed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    stale = tmp_path / f"{backend_bridge.EASYOCR_TEMP_PREFIX}stale"
    fresh = tmp_path / f"{backend_bridge.EASYOCR_TEMP_PREFIX}fresh"
    stale.mkdir()
    fresh.mkdir()
    old_timestamp = (
        time.time() - backend_bridge.EASYOCR_STALE_TEMP_SECONDS - 10
    )
    os.utime(stale, (old_timestamp, old_timestamp))
    monkeypatch.setattr(
        backend_bridge.tempfile,
        "gettempdir",
        lambda: str(tmp_path),
    )

    backend_bridge._cleanup_stale_easyocr_temp_dirs()

    assert not stale.exists()
    assert fresh.exists()


def _tesseract_completed(
    command: list[str],
    text: str,
    returncode: int = 0,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(
        args=command,
        returncode=returncode,
        stdout=text.encode("utf-8"),
        stderr=b"",
    )


def test_easyocr_http_json_classifies_arabic_id_as_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backend_bridge, "_easyocr_available", lambda: True)
    calls: list[bytes] = []

    def fake_http(image_bytes: bytes) -> dict[str, object]:
        calls.append(image_bytes)
        return {"status": "USED" if ARABIC_ID_TEXT else "EMPTY", "text": ARABIC_ID_TEXT}

    monkeypatch.setattr(backend_bridge, "_easyocr_http_request", fake_http)

    result = backend_bridge.handle(_payload(s.arabic_id_card_bytes()))

    assert result["ocr"] == {
        "used": True,
        "available": True,
        "source": "easyocr",
        "reason": "OCR_USED",
    }
    classification = result["classification"]
    assert classification["detected_type"] == "id_card"
    assert classification["consistency"] == CONSISTENCY_MATCH
    assert classification["decision"] == DECISION_MATCH
    assert classification["manual_review_required"] is False
    assert len(classification["matched_keywords"]) == 2

    assert len(calls) == 1
    assert calls[0] == s.arabic_id_card_bytes() or len(calls[0]) > 0


def test_easyocr_server_failure_falls_back_to_tesseract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backend_bridge, "_easyocr_available", lambda: True)
    monkeypatch.setattr(backend_bridge, "_tesseract_binary", lambda: "tesseract")
    calls: list[object] = []

    def fake_http(_image_bytes: bytes) -> dict[str, object]:
        calls.append("http")
        raise OSError("simulated EasyOCR server failure")

    def fake_run(
        command: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        calls.append(command)
        joined = " ".join(str(part) for part in command)
        if "tesseract" in joined or command[:1] == ["tesseract"]:
            return _tesseract_completed(command, FRENCH_ID_TEXT)
        return _tesseract_completed(command, FRENCH_ID_TEXT)

    monkeypatch.setattr(backend_bridge, "_easyocr_http_request", fake_http)
    monkeypatch.setattr(backend_bridge.subprocess, "run", fake_run)

    result = backend_bridge.handle(_payload(s.arabic_id_card_bytes()))

    assert result["ocr"] == {
        "used": True,
        "available": True,
        "source": "tesseract",
        "reason": "OCR_USED",
    }
    classification = result["classification"]
    assert classification["detected_type"] == "id_card"
    assert classification["decision"] == DECISION_MATCH
    assert len(classification["matched_keywords"]) == 2

    assert "http" in calls
    assert any(isinstance(call, list) for call in calls)


def test_easyocr_list_response_is_joined(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backend_bridge, "_easyocr_available", lambda: True)
    monkeypatch.setattr(
        backend_bridge,
        "_easyocr_http_request",
        lambda _image_bytes: {
            "status": "USED",
            "text": ["بطاقة التعريف الوطنية", "SPECIMEN / TEST"],
        },
    )

    text, status = backend_bridge._run_easyocr(b"image")

    assert status == "USED"
    assert text == "بطاقة التعريف الوطنية\nSPECIMEN / TEST"


def test_easyocr_and_tesseract_failure_returns_needs_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backend_bridge, "_easyocr_available", lambda: True)
    monkeypatch.setattr(backend_bridge, "_tesseract_binary", lambda: "tesseract")

    def fake_http(_image_bytes: bytes) -> dict[str, object]:
        raise OSError("simulated EasyOCR server failure")

    def fake_run(
        _command: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        raise OSError("simulated OCR subprocess failure")

    monkeypatch.setattr(backend_bridge, "_easyocr_http_request", fake_http)
    monkeypatch.setattr(backend_bridge.subprocess, "run", fake_run)

    result = backend_bridge.handle(_payload(s.arabic_id_card_bytes()))

    assert result["ocr"] == {
        "used": False,
        "available": True,
        "source": "none",
        "reason": "OCR_FAILED",
    }
    assert result["classification"]["decision"] == DECISION_NEEDS_REVIEW
    assert result["classification"]["reason_code"] == "NO_OCR"
    assert result["classification"]["manual_review_required"] is True


def test_easyocr_generic_only_does_not_fall_back_to_tesseract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backend_bridge, "_easyocr_available", lambda: True)
    monkeypatch.setattr(backend_bridge, "_tesseract_binary", lambda: "tesseract")
    tesseract_calls: list[object] = []

    def fake_http(_image_bytes: bytes) -> dict[str, object]:
        return {"status": "USED", "text": ARABIC_GENERIC_TEXT}

    def fake_run(
        command: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        tesseract_calls.append(command)
        return _tesseract_completed(command, ARABIC_DRIVING_TEXT)

    monkeypatch.setattr(backend_bridge, "_easyocr_http_request", fake_http)
    monkeypatch.setattr(backend_bridge.subprocess, "run", fake_run)

    result = backend_bridge.handle(
        _payload(
            s.ocr_document_bytes(ARABIC_GENERIC_TEXT),
            slot="driving_license",
        )
    )

    assert result["ocr"] == {
        "used": True,
        "available": True,
        "source": "easyocr",
        "reason": "OCR_USED",
    }
    classification = result["classification"]
    assert classification["detected_type"] is None
    assert classification["decision"] == DECISION_NEEDS_REVIEW
    assert classification["reason_code"] == "AMBIGUOUS_KEYWORDS"
    assert classification["manual_review_required"] is True
    assert tesseract_calls == []


def test_easyocr_and_tesseract_generic_only_returns_ambiguous_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backend_bridge, "_easyocr_available", lambda: True)
    monkeypatch.setattr(backend_bridge, "_tesseract_binary", lambda: "tesseract")

    def fake_http(_image_bytes: bytes) -> dict[str, object]:
        return {"status": "USED", "text": ARABIC_GENERIC_TEXT}

    def fake_run(
        command: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        return _tesseract_completed(command, ARABIC_GENERIC_TEXT)

    monkeypatch.setattr(backend_bridge, "_easyocr_http_request", fake_http)
    monkeypatch.setattr(backend_bridge.subprocess, "run", fake_run)

    result = backend_bridge.handle(
        _payload(
            s.ocr_document_bytes(ARABIC_GENERIC_TEXT),
            slot="driving_license",
        )
    )

    assert result["ocr"] == {
        "used": True,
        "available": True,
        "source": "easyocr",
        "reason": "OCR_USED",
    }
    classification = result["classification"]
    assert classification["detected_type"] is None
    assert classification["decision"] == DECISION_NEEDS_REVIEW
    assert classification["reason_code"] == "AMBIGUOUS_KEYWORDS"
    assert classification["manual_review_required"] is True