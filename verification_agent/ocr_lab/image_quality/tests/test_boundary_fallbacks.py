from __future__ import annotations

from collections import Counter

import cv2
import numpy as np

from image_quality import analyze_bytes
from image_quality.metrics.boundaries import _adaptive_threshold_rectangle_candidate
from image_quality.output_schema import STATUS_ACCEPTABLE, STATUS_RECAPTURE

from . import synthetic as s


def _boundary(result: dict) -> dict:
    return result["metrics"]["document_boundary"]["value"]


def test_uniform_background_card_is_detected() -> None:
    result = analyze_bytes(s.uniform_background_document_bytes(), "id_card")
    boundary = _boundary(result)

    assert boundary["valid"] is True
    assert boundary["no_document_signals"] == 0
    assert boundary["detector_used"] in {
        "contour_quadrilateral",
        "hough_lines",
        "full_frame",
    }
    assert result["status"] == STATUS_ACCEPTABLE


def test_hough_fallback_recovers_dashed_card_border() -> None:
    result = analyze_bytes(s.dashed_border_document_bytes(), "id_card")
    boundary = _boundary(result)

    assert boundary["valid"] is True
    assert boundary["detector_used"] == "hough_lines"
    assert boundary["no_document_signals"] == 0
    assert boundary["border_sides_touching"] == 0
    assert boundary["cropping_confirmed"] is False
    assert result["status"] == STATUS_ACCEPTABLE


def test_center_crop_fallback_for_featureless_outside() -> None:
    result = analyze_bytes(s.central_text_only_bytes(), "id_card")
    boundary = _boundary(result)

    assert boundary["valid"] is True
    assert boundary["detector_used"] == "center_crop_fallback"
    assert boundary["area_ratio"] == 0.64
    assert boundary["no_document_signals"] == 0
    assert result["status"] == STATUS_ACCEPTABLE


def test_full_frame_fallback_for_card_filling_most_of_frame() -> None:
    result = analyze_bytes(s.card_fills_frame_bytes(), "id_card")
    boundary = _boundary(result)

    assert boundary["valid"] is True
    assert boundary["detector_used"] == "full_frame"
    assert boundary["area_ratio"] == 1.0
    assert boundary["border_sides_touching"] == 0
    assert boundary["cropping_confirmed"] is False
    assert "CROPPED" not in result["reason_codes"]


def test_shadowed_card_remains_detected() -> None:
    result = analyze_bytes(s.shadowed_document_bytes(), "id_card")
    boundary = _boundary(result)

    assert boundary["valid"] is True
    assert boundary["no_document_signals"] == 0
    assert "NO_DOCUMENT_FOUND" not in result["reason_codes"]


def test_no_document_gradient_does_not_get_center_crop_fallback() -> None:
    result = analyze_bytes(s.no_document_bytes(), "id_card")
    boundary = _boundary(result)

    assert boundary["valid"] is False
    assert boundary["detector_used"] == "no_candidate"
    assert boundary["no_document_signals"] >= 2
    assert result["status"] == STATUS_RECAPTURE
    assert "NO_DOCUMENT_FOUND" in result["reason_codes"]


def test_adaptive_threshold_rectangle_uses_card_shaped_contours(monkeypatch) -> None:
    threshold_mask = np.zeros((200, 300), dtype=np.uint8)
    cv2.rectangle(threshold_mask, (40, 40), (260, 160), 255, thickness=-1)
    threshold_args = []

    def adaptive_threshold(gray, *args):
        threshold_args.append(args)
        return threshold_mask

    monkeypatch.setattr(cv2, "adaptiveThreshold", adaptive_threshold)
    candidate = _adaptive_threshold_rectangle_candidate(
        np.zeros((200, 300), dtype=np.uint8),
        width=300,
        height=200,
        edge_density=0.1,
        cfg={"fallback_confidence": 0.72},
        tolerance=2.0,
        rejection_counts=Counter(),
    )

    assert threshold_args == [
        (255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY, 17, 1)
    ]
    assert candidate is not None
    assert candidate["method"] == "adaptive_threshold_rectangle"
    assert candidate["candidate_count"] == 1
    assert candidate["solidity"] > 0.85
    _x, _y, box_width, box_height = candidate["bbox"]
    assert 1.3 <= max(box_width, box_height) / min(box_width, box_height) <= 2.0


def test_driving_license_cut_border_high_contrast_uses_full_frame_warning() -> None:
    result = analyze_bytes(
        s.driving_license_cut_border_high_contrast_bytes(),
        "driving_license",
        "card",
    )
    boundary = _boundary(result)
    contrast = result["metrics"]["contrast"]["value"]

    assert result["status"] == STATUS_ACCEPTABLE
    assert result["reason_codes"] == []
    assert boundary["valid"] is True
    assert boundary["detector_used"] == "full_frame"
    assert boundary["warning"] == "border_cut_but_high_contrast"
    assert boundary["candidate_count"] == 0
    assert boundary["edge_density"] > 0.068
    assert boundary["cropping_confirmed"] is False
    assert contrast["p5"] > 30.0
    assert contrast["rms_contrast"] > 25.0
    assert "CROPPED" not in result["reason_codes"]


def test_driving_license_wide_aspect_is_not_rejected() -> None:
    result = analyze_bytes(
        s.driving_license_wide_aspect_bytes(),
        "driving_license",
        "card",
    )
    boundary = _boundary(result)

    assert result["status"] == STATUS_ACCEPTABLE
    assert result["reason_codes"] == []
    assert boundary["valid"] is True
    assert boundary["aspect_ratio"] is not None
    assert 1.90 <= boundary["aspect_ratio"] <= 2.00
    assert boundary["aspect_ratio_in_range"] is True
    assert "CROPPED" not in result["reason_codes"]
