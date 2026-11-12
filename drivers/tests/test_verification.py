"""
drivers/tests/test_verification.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Integration tests for ``POST /api/verification/check/``.

All images are synthetic SPECIMEN / TEST layouts.  They contain no real
identity data, and the endpoint never receives or returns OCR text.
"""

from __future__ import annotations

import io
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, override_settings
from django.urls import reverse
from PIL import Image, ImageDraw, ImageFont
from rest_framework.test import APIClient

from drivers.tests.base import DriverAPITestCase, auth_client, make_user
from drivers.verification import (
    VerificationUnavailable,
    final_decision,
    run_verification,
)

User = get_user_model()

MATCHING_ID_TEXT = "CARTE NATIONALE D'IDENTITE"


# ─────────────────────────────────────────────────────────────
#  Synthetic SPECIMEN images (Pillow only; no numpy/OpenCV)
# ─────────────────────────────────────────────────────────────

def _font(size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.truetype("arial.ttf", max(8, int(size)))
    except OSError:
        return ImageFont.load_default()


def _to_png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def specimen_document_bytes(width: int = 1600, height: int = 1000) -> bytes:
    """A generic card layout for the image-quality gate."""

    image = Image.new("RGB", (width, height), (38, 42, 48))
    draw = ImageDraw.Draw(image)
    margin_x = int(width * 0.10)
    margin_y = int(height * 0.10)
    x0, y0, x1, y1 = margin_x, margin_y, width - margin_x, height - margin_y
    card_width = x1 - x0
    card_height = y1 - y0

    paper = (226, 226, 226)
    ink = (50, 50, 50)
    outline = (70, 70, 70)
    header_fill = (185, 185, 185)

    draw.rectangle(
        (x0, y0, x1, y1),
        fill=paper,
        outline=outline,
        width=max(2, card_width // 350),
    )
    header_height = max(18, int(card_height * 0.17))
    draw.rectangle(
        [x0 + 2, y0 + 2, x1 - 2, y0 + header_height],
        fill=header_fill,
    )
    draw.text(
        (x0 + int(card_width * 0.06), y0 + int(header_height * 0.22)),
        "SPECIMEN / TEST",
        fill=ink,
        font=_font(max(10, int(card_height * 0.065))),
    )
    draw.text(
        (
            x0 + int(card_width * 0.06),
            y0 + header_height + int(card_height * 0.035),
        ),
        "FICTIONAL DOCUMENT - NOT A REAL ID",
        fill=ink,
        font=_font(max(8, int(card_height * 0.042))),
    )

    line_top = y0 + header_height + int(card_height * 0.20)
    line_height = max(2, int(card_height * 0.025))
    for index in range(5):
        y = line_top + index * int(card_height * 0.105)
        if y + line_height >= y1:
            break
        draw.rectangle(
            [
                x0 + int(card_width * 0.06),
                y,
                x0 + int(card_width * 0.48),
                y + line_height,
            ],
            fill=ink,
        )
        draw.rectangle(
            [
                x0 + int(card_width * 0.06),
                y + line_height + 2,
                x0 + int(card_width * 0.36),
                y + line_height + 2,
            ],
            fill=outline,
        )

    photo_x0 = x0 + int(card_width * 0.62)
    photo_y0 = y0 + int(card_height * 0.30)
    photo_x1 = x0 + int(card_width * 0.92)
    photo_y1 = y0 + int(card_height * 0.82)
    draw.rectangle(
        [photo_x0, photo_y0, photo_x1, photo_y1],
        outline=outline,
        width=max(2, card_width // 500),
    )
    draw.line([photo_x0, photo_y0, photo_x1, photo_y1], fill=outline, width=2)
    draw.line([photo_x1, photo_y0, photo_x0, photo_y1], fill=outline, width=2)
    return _to_png(image)


def specimen_selfie_bytes(width: int = 1200, height: int = 900) -> bytes:
    """A generic portrait-shaped image with no face and SPECIMEN text."""

    image = Image.new("RGB", (width, height), (120, 130, 140))
    draw = ImageDraw.Draw(image)
    draw.rectangle(
        [
            int(width * 0.08),
            int(height * 0.08),
            int(width * 0.92),
            int(height * 0.92),
        ],
        fill=(185, 170, 155),
        outline=(40, 40, 40),
        width=3,
    )
    draw.text(
        (int(width * 0.18), int(height * 0.16)),
        "SPECIMEN / TEST",
        fill=(20, 20, 20),
        font=_font(max(18, int(height * 0.06))),
    )
    draw.text(
        (int(width * 0.18), int(height * 0.25)),
        "NO FACE - QUALITY TEST ONLY",
        fill=(30, 30, 30),
        font=_font(max(12, int(height * 0.04))),
    )
    for index in range(16):
        y = int(height * 0.38) + index * int(height * 0.028)
        draw.rectangle(
            [
                int(width * 0.18),
                y,
                int(width * (0.55 + 0.18 * (index % 3))),
                y + max(2, int(height * 0.008)),
            ],
            fill=(55, 65, 75),
        )
    return _to_png(image)


def upload(name: str, data: bytes) -> SimpleUploadedFile:
    return SimpleUploadedFile(name, data, content_type="image/png")


def _fake_result(
    *,
    quality_status: str = "ACCEPTABLE_QUALITY",
    classification_decision: str = "MATCH",
    consistency: str = "MATCH",
) -> dict:
    return {
        "quality": {
            "status": quality_status,
            "reason_codes": [] if quality_status == "ACCEPTABLE_QUALITY" else ["BLURRY"],
        },
        "classification": {
            "decision": classification_decision,
            "consistency": consistency,
            "reason_code": (
                "KEYWORD_MATCH"
                if classification_decision == "MATCH"
                else "LOW_CONFIDENCE"
            ),
        },
        "ocr": {
            "used": True,
            "available": True,
            "source": "tesseract",
            "reason": "OCR_USED",
        },
    }


# ─────────────────────────────────────────────────────────────
#  Service-level integration with the real quality gate
# ─────────────────────────────────────────────────────────────

class VerificationServiceTests(SimpleTestCase):
    @override_settings(VERIFICATION_AGENT_DISABLE_OCR=True)
    def test_document_without_ocr_returns_needs_review(self):
        result = run_verification(specimen_document_bytes(), "id_card_front")

        self.assertIn(
            result["quality"]["status"],
            {"ACCEPTABLE_QUALITY", "POOR_QUALITY"},
        )
        self.assertEqual(result["classification"]["decision"], "NEEDS_REVIEW")
        self.assertEqual(result["classification"]["reason_code"], "NO_OCR")
        self.assertEqual(result["ocr"]["reason"], "NO_OCR")
        self.assertEqual(final_decision(result), "MANUAL_REVIEW")

    @override_settings(VERIFICATION_AGENT_DISABLE_OCR=True)
    def test_synthetic_ocr_text_reaches_classification(self):
        result = run_verification(
            specimen_document_bytes(),
            "id_card_front",
            ocr_text=MATCHING_ID_TEXT,
        )

        self.assertEqual(result["classification"]["decision"], "MATCH")
        self.assertEqual(result["classification"]["consistency"], "MATCH")
        self.assertEqual(final_decision(result), "ACCEPT")

    @override_settings(VERIFICATION_AGENT_DISABLE_OCR=True)
    def test_slot_mismatch_never_auto_accepts(self):
        result = run_verification(
            specimen_document_bytes(),
            "driving_license",
            ocr_text=MATCHING_ID_TEXT,
        )

        self.assertEqual(result["classification"]["decision"], "NEEDS_REVIEW")
        self.assertEqual(result["classification"]["consistency"], "MISMATCH")
        self.assertEqual(result["classification"]["reason_code"], "SLOT_MISMATCH")
        self.assertEqual(final_decision(result), "MANUAL_REVIEW")

    def test_selfie_without_detector_or_face_needs_review(self):
        result = run_verification(specimen_selfie_bytes(), "selfie")

        self.assertIn(
            result["quality"]["status"],
            {"ACCEPTABLE_QUALITY", "POOR_QUALITY"},
        )
        self.assertEqual(result["classification"]["decision"], "NEEDS_REVIEW")
        self.assertIn(
            result["classification"]["reason_code"],
            {"FACE_DETECTOR_UNAVAILABLE", "FACE_NOT_DETECTED"},
        )
        self.assertEqual(final_decision(result), "MANUAL_REVIEW")


# ─────────────────────────────────────────────────────────────
#  HTTP endpoint
# ─────────────────────────────────────────────────────────────

class VerificationCheckEndpointTests(DriverAPITestCase):
    def setUp(self):
        super().setUp()
        self.driver = make_user(User.Role.DRIVER, username="verification_driver")
        self.client = auth_client(self.driver)
        self.url = reverse("verification-check")

    def _post(self, *, slot="id_card_front", data=None, name="specimen.png"):
        return self.client.post(
            self.url,
            {"slot": slot, "file": upload(name, data or specimen_document_bytes())},
            format="multipart",
        )

    def test_anonymous_is_unauthorized(self):
        response = APIClient().post(
            self.url,
            {
                "slot": "id_card_front",
                "file": upload("specimen.png", specimen_document_bytes()),
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, 401)

    def test_missing_file_is_rejected(self):
        response = self.client.post(
            self.url,
            {"slot": "id_card_front"},
            format="multipart",
        )
        self.assertEqual(response.status_code, 400)

    def test_unknown_slot_is_rejected(self):
        response = self._post(slot="passport")
        self.assertEqual(response.status_code, 400)

    def test_corrupt_image_is_rejected_before_the_bridge(self):
        response = self._post(data=b"this is not an image")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "invalid_document")

    def test_tiny_image_returns_recapture_and_reject(self):
        tiny = Image.new("RGB", (16, 16), (240, 240, 240))
        response = self._post(data=_to_png(tiny))

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["quality_status"], "NEEDS_RECAPTURE")
        self.assertEqual(body["classification"]["decision"], "QUALITY_GATE_BLOCKED")
        self.assertEqual(body["final_decision"], "REJECT")

    @override_settings(VERIFICATION_AGENT_DISABLE_OCR=True)
    def test_good_document_without_ocr_returns_manual_review(self):
        response = self._post()

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn(
            body["quality_status"],
            {"ACCEPTABLE_QUALITY", "POOR_QUALITY"},
        )
        self.assertEqual(body["ocr"]["reason"], "NO_OCR")
        self.assertEqual(body["classification"]["decision"], "NEEDS_REVIEW")
        self.assertEqual(body["classification"]["reason_code"], "NO_OCR")
        self.assertEqual(body["consistency"], "UNKNOWN")
        self.assertEqual(body["final_decision"], "MANUAL_REVIEW")

        # The endpoint must not echo the uploader's filename or image content.
        self.assertNotIn(b"specimen.png", response.content)

    @patch("drivers.verification_views.run_verification")
    def test_accept_only_when_quality_is_acceptable_and_type_matches(self, run):
        run.return_value = _fake_result()
        response = self._post()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["final_decision"], "ACCEPT")
        self.assertEqual(response.json()["quality_status"], "ACCEPTABLE_QUALITY")
        self.assertEqual(response.json()["consistency"], "MATCH")

    @patch("drivers.verification_views.run_verification")
    def test_poor_quality_match_still_needs_manual_review(self, run):
        run.return_value = _fake_result(quality_status="POOR_QUALITY")
        response = self._post()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["final_decision"], "MANUAL_REVIEW")

    @patch("drivers.verification_views.run_verification")
    def test_mismatch_is_manual_review_not_automatic_rejection(self, run):
        run.return_value = _fake_result(
            classification_decision="NEEDS_REVIEW",
            consistency="MISMATCH",
        )
        response = self._post()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["final_decision"], "MANUAL_REVIEW")
        self.assertEqual(response.json()["consistency"], "MISMATCH")

    @patch("drivers.verification_views.run_verification")
    def test_quality_recapture_is_reject(self, run):
        run.return_value = _fake_result(quality_status="NEEDS_RECAPTURE")
        response = self._post()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["final_decision"], "REJECT")

    @patch("drivers.verification_views.run_verification")
    def test_bridge_unavailable_returns_503(self, run):
        run.side_effect = VerificationUnavailable("verification_python_missing")
        response = self._post()

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "verification_python_missing")
