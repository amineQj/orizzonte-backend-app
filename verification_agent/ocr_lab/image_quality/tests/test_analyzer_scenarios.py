from __future__ import annotations

import pytest

from image_quality import analyze_bytes
from image_quality.output_schema import (
    REASON_CODES,
    STATUS_ACCEPTABLE,
    STATUS_POOR,
    STATUS_RECAPTURE,
    validate_output,
)

from . import synthetic as s


@pytest.mark.parametrize(
    ("name", "factory", "profile", "expected_status", "expected_reason"),
    [
        ("good", s.good_document_bytes, "id_card", STATUS_ACCEPTABLE, None),
        ("blurry", s.blurry_document_bytes, "id_card", STATUS_RECAPTURE, "BLURRY"),
        (
            "low_resolution",
            s.low_resolution_document_bytes,
            "id_card",
            STATUS_RECAPTURE,
            "TOO_SMALL",
        ),
        ("dark", s.dark_document_bytes, "id_card", STATUS_POOR, "DARK"),
        (
            "overexposed",
            s.overexposed_document_bytes,
            "id_card",
            STATUS_POOR,
            "OVEREXPOSED",
        ),
        (
            "low_contrast",
            s.low_contrast_document_bytes,
            "id_card",
            STATUS_POOR,
            "LOW_CONTRAST",
        ),
        ("glare", s.glare_document_bytes, "id_card", STATUS_POOR, "GLARE"),
        ("cropped", s.cropped_document_bytes, "id_card", STATUS_RECAPTURE, "CROPPED"),
        ("rotated", s.rotated_document_bytes, "id_card", STATUS_POOR, "ROTATED"),
        (
            "no_document",
            s.no_document_bytes,
            "id_card",
            STATUS_RECAPTURE,
            "NO_DOCUMENT_FOUND",
        ),
        ("uneven", s.uneven_light_document_bytes, "id_card", STATUS_POOR, "UNEVEN_LIGHT"),
        ("tiny", s.tiny_image_bytes, "id_card", STATUS_RECAPTURE, "TOO_SMALL"),
        ("selfie_good", s.selfie_image_bytes, "selfie", STATUS_ACCEPTABLE, None),
        (
            "selfie_blurry",
            lambda: s.selfie_image_bytes(blur=6),
            "selfie",
            STATUS_RECAPTURE,
            "BLURRY",
        ),
    ],
)
def test_synthetic_scenarios(
    name: str,
    factory,
    profile: str,
    expected_status: str,
    expected_reason: str | None,
) -> None:
    result = analyze_bytes(factory(), profile)
    assert result["status"] == expected_status, name
    if expected_reason is None:
        assert result["reason_codes"] == [], name
    else:
        assert expected_reason in result["reason_codes"], name
    assert result["error_category"] is None
    assert result["thresholds_calibrated"] is False
    assert validate_output(result) == []
    assert set(result["reason_codes"]).issubset(set(REASON_CODES))


def test_all_profiles_and_layouts_produce_valid_schema() -> None:
    cases = [
        ("id_card", "default", s.good_document_bytes()),
        ("driving_license", "card", s.good_document_bytes()),
        ("driving_license", "paper", s.good_document_bytes(1600, 1100)),
        ("vehicle_registration", "paper", s.good_document_bytes(1600, 1100)),
        ("selfie", "default", s.selfie_image_bytes()),
    ]
    for profile, layout, data in cases:
        result = analyze_bytes(data, profile, layout)
        assert result["profile"] == profile
        assert result["layout"] == layout
        assert validate_output(result) == []
        assert result["status"] == STATUS_ACCEPTABLE


def test_vehicle_registration_paper_is_supported() -> None:
    result = analyze_bytes(s.good_document_bytes(1600, 1100), "vehicle_registration")
    assert result["status"] == STATUS_ACCEPTABLE
    assert result["metrics"]["document_boundary"]["value"]["aspect_ratio_in_range"] is None


def test_selfie_has_only_quality_metrics() -> None:
    result = analyze_bytes(s.selfie_image_bytes(), "selfie")
    forbidden = {
        "document_boundary",
        "resolution_document",
        "glare",
        "rotation",
        "perspective",
    }
    assert not (set(result["metrics"]) & forbidden)
    assert set(result["reason_codes"]) == set()


def test_selfie_never_reports_document_reasons() -> None:
    result = analyze_bytes(s.selfie_image_bytes(blur=6), "selfie")
    assert "NO_DOCUMENT_FOUND" not in result["reason_codes"]
    assert "CROPPED" not in result["reason_codes"]
    assert "ROTATED" not in result["reason_codes"]
    assert "PERSPECTIVE" not in result["reason_codes"]
    assert "GLARE" not in result["reason_codes"]


def test_webp_saved_with_jpg_extension_is_read_by_content() -> None:
    result = analyze_bytes(s.webp_named_jpg_bytes(), "id_card")
    assert result["image"]["format"] == "WEBP"
    assert result["image"]["original_width_px"] == 1600
    assert result["status"] == STATUS_ACCEPTABLE


def test_exif_rotated_image_uses_oriented_dimensions() -> None:
    result = analyze_bytes(s.exif_rotated_jpeg_bytes(), "id_card")
    assert result["image"]["exif_orientation"] == 6
    assert result["image"]["exif_orientation_applied"] is True
    assert result["image"]["original_width_px"] == 1600
    assert result["image"]["original_height_px"] == 1000
    assert result["image"]["oriented_width_px"] == 1000
    assert result["image"]["oriented_height_px"] == 1600
    assert result["status"] == STATUS_ACCEPTABLE


def test_grayscale_image_is_supported() -> None:
    result = analyze_bytes(s.grayscale_document_bytes(), "id_card")
    assert result["image"]["is_grayscale"] is True
    assert result["status"] == STATUS_ACCEPTABLE


def test_corrupt_empty_and_huge_images_are_structured_errors() -> None:
    corrupt = analyze_bytes(s.corrupt_bytes(), "id_card")
    assert corrupt["status"] == STATUS_RECAPTURE
    assert corrupt["reason_codes"] == ["UNREADABLE_IMAGE"]
    assert corrupt["error_category"] == "corrupt_image"
    assert corrupt["image"] is None
    assert validate_output(corrupt) == []

    empty = analyze_bytes(b"", "id_card")
    assert empty["reason_codes"] == ["UNREADABLE_IMAGE"]
    assert empty["error_category"] == "empty_input"

    huge = analyze_bytes(s.huge_image_bytes(), "id_card", max_megapixels=5.0)
    assert huge["reason_codes"] == ["UNREADABLE_IMAGE"]
    assert huge["error_category"] == "megapixel_limit"


def test_invalid_profile_is_structured_error() -> None:
    result = analyze_bytes(s.good_document_bytes(), "passport")
    assert result["status"] == STATUS_RECAPTURE
    assert result["error_category"] == "invalid_profile"
    assert result["reason_codes"] == []
    assert result["image"] is None
    assert validate_output(result) == []


def test_output_contains_versions_time_and_confidence() -> None:
    result = analyze_bytes(s.good_document_bytes(), "id_card")
    assert result["schema_version"] == "1.0.0"
    assert result["algorithm_version"] == "1.0.0"
    assert isinstance(result["processing_time_ms"], float)
    assert result["processing_time_ms"] >= 0.0
    assert 0.0 <= result["confidence"] <= 1.0
    assert result["thresholds_calibrated"] is False
