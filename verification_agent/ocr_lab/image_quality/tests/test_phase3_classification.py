from __future__ import annotations

import json

import pytest

import image_quality.document_classification as dc
from image_quality.output_schema import (
    STATUS_ACCEPTABLE,
    STATUS_POOR,
    STATUS_RECAPTURE,
)

ID_CARD_TEXT = (
    "REPUBLIQUE ALGERIENNE DEMOCRATIQUE ET POPULAIRE\n"
    "CARTE NATIONALE D'IDENTITE\n"
    "SPECIMEN / TEST"
)
DRIVING_LICENSE_TEXT = (
    "REPUBLIQUE ALGERIENNE\n"
    "PERMIS DE CONDUIRE\n"
    "SPECIMEN / TEST"
)
VEHICLE_REGISTRATION_TEXT = (
    "CERTIFICAT D'IMMATRICULATION\n"
    "CARTE GRISE\n"
    "SPECIMEN / TEST"
)
ARABIC_ID_CARD_TEXT = (
    "\u200fالجمهورية الجزائرية الديمقراطية الشعبية\u200e\n"
    "\u200fبطاقة التعريف الوطنية\u200e"
)
ARABIC_DRIVING_LICENSE_TEXT = (
    "\u200fالجمهورية الجزائرية\u200e\n"
    "\u200fرخصة السياقة\u200e"
)
ARABIC_VEHICLE_REGISTRATION_TEXT = (
    "\u200fشهادة تسجيل\u200e\n"
    "\u200fالبطاقة الرمادية\u200e"
)


@pytest.mark.parametrize(
    ("slot", "text", "expected_type"),
    [
        ("id_card_front", ID_CARD_TEXT, "id_card"),
        ("id_card_back", ID_CARD_TEXT, "id_card"),
        ("driving_license", DRIVING_LICENSE_TEXT, "driving_license"),
        ("vehicle_registration", VEHICLE_REGISTRATION_TEXT, "vehicle_registration"),
    ],
)
def test_correct_keywords_match_declared_slot(
    slot: str,
    text: str,
    expected_type: str,
) -> None:
    result = dc.classify_document(slot, ocr_text=text)

    assert result.expected_type == expected_type
    assert result.detected_type == expected_type
    assert result.consistency == dc.CONSISTENCY_MATCH
    assert result.decision == dc.DECISION_MATCH
    assert result.manual_review_required is False
    assert result.confidence >= 0.6 - 1e-9


@pytest.mark.parametrize(
    ("slot", "text", "expected_type"),
    [
        ("id_card_front", ARABIC_ID_CARD_TEXT, "id_card"),
        ("driving_license", ARABIC_DRIVING_LICENSE_TEXT, "driving_license"),
        (
            "vehicle_registration",
            ARABIC_VEHICLE_REGISTRATION_TEXT,
            "vehicle_registration",
        ),
    ],
)
def test_arabic_keywords_match_declared_slot(
    slot: str,
    text: str,
    expected_type: str,
) -> None:
    result = dc.classify_document(slot, ocr_text=text)

    assert result.expected_type == expected_type
    assert result.detected_type == expected_type
    assert result.consistency == dc.CONSISTENCY_MATCH
    assert result.decision == dc.DECISION_MATCH
    assert result.manual_review_required is False


def test_accent_and_punctuation_normalization() -> None:
    text = (
        "RÉPUBLIQUE ALGÉRIENNE DÉMOCRATIQUE ET POPULAIRE — "
        "CARTE NATIONALE D’IDENTITÉ"
    )
    result = dc.classify_document("id_card_front", ocr_text=text)

    assert result.detected_type == "id_card"
    assert result.decision == dc.DECISION_MATCH


def test_slot_mismatch_requires_manual_review_not_rejection() -> None:
    result = dc.classify_document("driving_license", ocr_text=ID_CARD_TEXT)

    assert result.detected_type == "id_card"
    assert result.consistency == dc.CONSISTENCY_MISMATCH
    assert result.decision == dc.DECISION_NEEDS_REVIEW
    assert result.manual_review_required is True
    assert result.reason_code == dc.REASON_SLOT_MISMATCH
    assert result.decision != "REJECTED"


def test_low_confidence_keywords_return_needs_review() -> None:
    config = dc.load_document_layouts()
    result = dc.classify_document(
        "id_card_front",
        ocr_text="REPUBLIQUE ALGERIENNE DEMOCRATIQUE ET POPULAIRE",
        config=config,
    )

    assert result.detected_type == "id_card"
    assert result.confidence < config.min_confidence
    assert result.consistency == dc.CONSISTENCY_UNKNOWN
    assert result.decision == dc.DECISION_NEEDS_REVIEW
    assert result.manual_review_required is True
    assert result.reason_code == dc.REASON_LOW_CONFIDENCE


def test_no_keyword_returns_needs_review() -> None:
    result = dc.classify_document("id_card_front", ocr_text="SPECIMEN / TEST")

    assert result.detected_type is None
    assert result.confidence == 0.0
    assert result.consistency == dc.CONSISTENCY_UNKNOWN
    assert result.decision == dc.DECISION_NEEDS_REVIEW
    assert result.manual_review_required is True
    assert result.reason_code == dc.REASON_NO_KEYWORD


def test_missing_text_returns_needs_review() -> None:
    result = dc.classify_document("id_card_front")

    assert result.detected_type is None
    assert result.decision == dc.DECISION_NEEDS_REVIEW
    assert result.reason_code == dc.REASON_NO_TEXT


def test_conflicting_document_keywords_return_needs_review() -> None:
    result = dc.classify_document(
        "id_card_front",
        ocr_text=ID_CARD_TEXT + "\n" + DRIVING_LICENSE_TEXT,
    )

    assert result.detected_type is None
    assert result.consistency == dc.CONSISTENCY_UNKNOWN
    assert result.decision == dc.DECISION_NEEDS_REVIEW
    assert result.reason_code == dc.REASON_AMBIGUOUS_KEYWORDS


def test_selfie_with_face_present_matches() -> None:
    result = dc.classify_document("selfie", face_detected=True)

    assert result.expected_type == "selfie"
    assert result.detected_type == "selfie"
    assert result.face_detected is True
    assert result.consistency == dc.CONSISTENCY_MATCH
    assert result.decision == dc.DECISION_MATCH


def test_selfie_without_face_returns_needs_review() -> None:
    result = dc.classify_document("selfie", face_detected=False)

    assert result.detected_type is None
    assert result.face_detected is False
    assert result.decision == dc.DECISION_NEEDS_REVIEW
    assert result.reason_code == dc.REASON_FACE_NOT_DETECTED


def test_selfie_without_face_detector_returns_needs_review() -> None:
    result = dc.classify_document("selfie")

    assert result.detected_type is None
    assert result.face_detected is None
    assert result.decision == dc.DECISION_NEEDS_REVIEW
    assert result.reason_code == dc.REASON_FACE_DETECTOR_UNAVAILABLE


def test_selfie_can_use_an_injected_face_detector() -> None:
    result = dc.classify_document(
        "selfie",
        image_bytes=b"synthetic-face-detector-input",
        face_detector=lambda _: True,
    )

    assert result.detected_type == "selfie"
    assert result.decision == dc.DECISION_MATCH


@pytest.mark.parametrize("quality_status", [STATUS_ACCEPTABLE, STATUS_POOR])
def test_classification_runs_for_usable_quality(quality_status: str) -> None:
    result = dc.classify_after_quality(
        {"status": quality_status},
        "id_card_front",
        ocr_text=ID_CARD_TEXT,
    )

    assert result.quality_gate_blocked is False
    assert result.decision == dc.DECISION_MATCH


def test_classification_is_blocked_by_failed_quality_gate(monkeypatch) -> None:
    def fail_if_called(*args, **kwargs):
        raise AssertionError("classification must not run after a failed quality gate")

    monkeypatch.setattr(dc, "classify_document", fail_if_called)
    result = dc.classify_after_quality(
        {"status": STATUS_RECAPTURE},
        "id_card_front",
        ocr_text=ID_CARD_TEXT,
    )

    assert result.quality_gate_blocked is True
    assert result.detected_type is None
    assert result.decision == dc.DECISION_QUALITY_GATE_BLOCKED
    assert result.manual_review_required is False
    assert result.reason_code == dc.REASON_QUALITY_GATE_BLOCKED


def test_keywords_are_configurable(tmp_path) -> None:
    layout = {
        "min_confidence": 0.5,
        "min_confidence_margin": 0.1,
        "slots": {
            "specimen_slot": {
                "expected_type": "specimen_card",
                "mode": "text",
            }
        },
        "types": {
            "specimen_card": {
                "keywords": [
                    {"text": "SPECIMEN CARD", "weight": 1.0},
                ]
            }
        },
    }
    path = tmp_path / "document_layouts.json"
    path.write_text(json.dumps(layout), encoding="utf-8")
    config = dc.load_document_layouts(path)

    result = dc.classify_document(
        "specimen_slot",
        ocr_text="SPECIMEN CARD",
        config=config,
    )

    assert result.detected_type == "specimen_card"
    assert result.consistency == dc.CONSISTENCY_MATCH
    assert result.decision == dc.DECISION_MATCH


def test_unknown_slot_is_a_structured_error() -> None:
    with pytest.raises(dc.ClassificationError):
        dc.classify_document("not_a_slot", ocr_text=ID_CARD_TEXT)


def test_result_does_not_echo_raw_ocr_text() -> None:
    private_marker = "UNIQUE_PRIVATE_TOKEN_123"
    result = dc.classify_document(
        "id_card_front",
        ocr_text=ID_CARD_TEXT + "\n" + private_marker,
    )

    serialized = json.dumps(result.to_dict(), ensure_ascii=False)
    assert private_marker not in serialized
    assert "SPECIMEN / TEST" not in serialized
