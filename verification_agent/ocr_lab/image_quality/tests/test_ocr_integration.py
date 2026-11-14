from __future__ import annotations

import base64

import pytest

from image_quality import backend_bridge
from image_quality.document_classification import (
    CONSISTENCY_MATCH,
    CONSISTENCY_MISMATCH,
    DECISION_MATCH,
    DECISION_NEEDS_REVIEW,
)
from image_quality.output_schema import STATUS_ACCEPTABLE, STATUS_POOR

from . import synthetic as s

pytestmark = pytest.mark.skipif(
    not backend_bridge._tesseract_available(),
    reason="Tesseract OCR is not installed",
)


@pytest.fixture(autouse=True)
def _force_tesseract(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep this file focused on the Tesseract path.

    EasyOCR has its own mocked regression tests in test_easyocr_bridge.py.
    """

    monkeypatch.setattr(backend_bridge, "_easyocr_available", lambda: False)


ID_CARD_OCR_TEXT = (
    "REPUBLIQUE ALGERIENNE DEMOCRATIQUE ET POPULAIRE\n"
    "CARTE NATIONALE D'IDENTITE\n"
    "SPECIMEN / TEST"
)
WRONG_DOCUMENT_OCR_TEXT = (
    "REPUBLIQUE ALGERIENNE\n"
    "PERMIS DE CONDUIRE\n"
    "SPECIMEN / TEST"
)
FRENCH_ID_OCR_TEXT = (
    "REPUBLIQUE ALGERIENNE\n"
    "CARTE NATIONALE D'IDENTITE\n"
    "SPECIMEN / TEST"
)


def _payload(image_bytes: bytes, slot: str = "id_card_front") -> dict[str, str | bool]:
    return {
        "image_base64": base64.b64encode(image_bytes).decode("ascii"),
        "slot": slot,
        "allow_ocr": True,
    }


def test_tesseract_reads_republic_phrase() -> None:
    image_bytes = s.ocr_document_bytes(
        "REPUBLIQUE ALGERIENNE DEMOCRATIQUE ET POPULAIRE"
    )

    text, status = backend_bridge._run_tesseract(image_bytes)

    assert status == "USED"
    assert text is not None
    normalized = " ".join(text.upper().split())
    assert "REPUBLIQUE ALGERIENNE DEMOCRATIQUE ET POPULAIRE" in normalized


def test_known_id_text_classifies_as_match() -> None:
    result = backend_bridge.handle(
        _payload(s.ocr_document_bytes(ID_CARD_OCR_TEXT))
    )

    assert result["quality"]["status"] in {STATUS_ACCEPTABLE, STATUS_POOR}
    assert result["ocr"] == {
        "used": True,
        "available": True,
        "source": "tesseract",
        "reason": "OCR_USED",
    }
    classification = result["classification"]
    assert classification["detected_type"] == "id_card"
    assert classification["consistency"] == CONSISTENCY_MATCH
    assert classification["decision"] == DECISION_MATCH
    assert classification["manual_review_required"] is False
    assert (
        "REPUBLIQUE ALGERIENNE DEMOCRATIQUE ET POPULAIRE"
        in classification["matched_keywords"]
    )
    assert "CARTE NATIONALE D'IDENTITE" in classification["matched_keywords"]


def test_french_id_image_still_classifies_as_match() -> None:
    result = backend_bridge.handle(
        _payload(s.ocr_document_bytes(FRENCH_ID_OCR_TEXT))
    )

    assert result["quality"]["status"] in {STATUS_ACCEPTABLE, STATUS_POOR}
    assert result["ocr"]["used"] is True
    assert result["ocr"]["source"] == "tesseract"
    classification = result["classification"]
    assert classification["detected_type"] == "id_card"
    assert classification["consistency"] == CONSISTENCY_MATCH
    assert classification["decision"] == DECISION_MATCH
    assert classification["manual_review_required"] is False


def test_arabic_id_image_classifies_as_match() -> None:
    result = backend_bridge.handle(_payload(s.arabic_id_card_bytes()))

    assert result["quality"]["status"] in {STATUS_ACCEPTABLE, STATUS_POOR}
    assert result["ocr"]["used"] is True
    assert result["ocr"]["source"] == "tesseract"
    classification = result["classification"]
    assert classification["detected_type"] == "id_card"
    assert classification["consistency"] == CONSISTENCY_MATCH
    assert classification["decision"] == DECISION_MATCH
    assert classification["manual_review_required"] is False
    assert (
        "الجمهورية الجزائرية الديمقراطية الشعبية"
        in classification["matched_keywords"]
    )
    assert "بطاقة التعريف الوطنية" in classification["matched_keywords"]


def test_wrong_document_text_classifies_as_mismatch() -> None:
    result = backend_bridge.handle(
        _payload(s.ocr_document_bytes(WRONG_DOCUMENT_OCR_TEXT))
    )

    assert result["quality"]["status"] in {STATUS_ACCEPTABLE, STATUS_POOR}
    assert result["ocr"]["used"] is True
    assert result["ocr"]["source"] == "tesseract"
    classification = result["classification"]
    assert classification["detected_type"] == "driving_license"
    assert classification["consistency"] == CONSISTENCY_MISMATCH
    assert classification["decision"] == DECISION_NEEDS_REVIEW
    assert classification["manual_review_required"] is True
    assert classification["reason_code"] == "SLOT_MISMATCH"
    assert "PERMIS DE CONDUIRE" in classification["matched_keywords"]


def test_debug_ocr_is_absent_by_default() -> None:
    result = backend_bridge.handle(
        _payload(s.ocr_document_bytes(ID_CARD_OCR_TEXT))
    )

    assert "ocr_debug_text" not in result


def test_debug_ocr_returns_raw_tesseract_text() -> None:
    result = backend_bridge.handle(
        {
            **_payload(s.ocr_document_bytes(ID_CARD_OCR_TEXT)),
            "debug_ocr": True,
        }
    )

    assert "ocr_debug_text" in result
    debug_text = result["ocr_debug_text"]
    assert debug_text is not None
    assert "REPUBLIQUE ALGERIENNE DEMOCRATIQUE ET POPULAIRE" in " ".join(
        debug_text.upper().split()
    )


def test_debug_ocr_returns_null_when_ocr_is_disabled() -> None:
    result = backend_bridge.handle(
        {
            **_payload(s.ocr_document_bytes(ID_CARD_OCR_TEXT)),
            "debug_ocr": True,
            "allow_ocr": False,
        }
    )

    assert "ocr_debug_text" in result
    assert result["ocr_debug_text"] is None
