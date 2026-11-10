from __future__ import annotations

from types import SimpleNamespace

import numpy as np

from image_quality import analyze_bytes
from image_quality.metrics.boundaries import (
    compute_perspective_metric,
    compute_rotation_metric,
)
from image_quality.profiles import PROFILE_LAYOUTS, get_profile_config

from . import synthetic as s


def _metric(result: dict, name: str) -> dict:
    return result["metrics"][name]


def _document_configs():
    for profile, layouts in PROFILE_LAYOUTS.items():
        for layout in layouts:
            cfg = get_profile_config(profile, layout)
            if cfg.document_analysis:
                yield profile, layout, cfg.data


def test_sharpness_moves_in_the_right_direction() -> None:
    good = analyze_bytes(s.good_document_bytes(), "id_card")
    blurry = analyze_bytes(s.blurry_document_bytes(), "id_card")
    good_lap = _metric(good, "sharpness_laplacian")["value"]
    blur_lap = _metric(blurry, "sharpness_laplacian")["value"]
    good_ten = _metric(good, "sharpness_tenengrad")["value"]
    blur_ten = _metric(blurry, "sharpness_tenengrad")["value"]
    assert good_lap > blur_lap
    assert good_ten > blur_ten
    assert _metric(good, "sharpness_laplacian")["nature"] == "measured"
    assert _metric(good, "sharpness_tenengrad")["nature"] == "measured"


def test_exposure_and_uneven_light_move_in_the_right_direction() -> None:
    good = analyze_bytes(s.good_document_bytes(), "id_card")
    dark = analyze_bytes(s.dark_document_bytes(), "id_card")
    over = analyze_bytes(s.overexposed_document_bytes(), "id_card")
    uneven = analyze_bytes(s.uneven_light_document_bytes(), "id_card")

    assert _metric(dark, "brightness")["value"] < _metric(good, "brightness")["value"]
    assert _metric(over, "brightness")["value"] > _metric(good, "brightness")["value"]
    assert (
        _metric(over, "clipping")["value"]["bright_fraction"]
        > _metric(good, "clipping")["value"]["bright_fraction"]
    )
    assert (
        _metric(uneven, "uneven_illumination")["value"]["ratio"]
        > _metric(good, "uneven_illumination")["value"]["ratio"]
    )
    assert "DARK" in dark["reason_codes"]
    assert "OVEREXPOSED" in over["reason_codes"]
    assert "UNEVEN_LIGHT" in uneven["reason_codes"]


def test_contrast_moves_in_the_right_direction() -> None:
    good = analyze_bytes(s.good_document_bytes(), "id_card")
    low = analyze_bytes(s.low_contrast_document_bytes(), "id_card")
    good_range = _metric(good, "contrast")["value"]["p5_p95_range"]
    low_range = _metric(low, "contrast")["value"]["p5_p95_range"]
    good_rms = _metric(good, "contrast")["value"]["rms_contrast"]
    low_rms = _metric(low, "contrast")["value"]["rms_contrast"]
    assert low_range < good_range
    assert low_rms < good_rms
    assert "LOW_CONTRAST" in low["reason_codes"]


def test_glare_moves_in_the_right_direction() -> None:
    good = analyze_bytes(s.good_document_bytes(), "id_card")
    glare = analyze_bytes(s.glare_document_bytes(), "id_card")
    good_fraction = _metric(good, "glare")["value"]["suspected_fraction"]
    glare_fraction = _metric(glare, "glare")["value"]["suspected_fraction"]
    assert good_fraction == 0.0
    assert glare_fraction > good_fraction
    assert _metric(glare, "glare")["value"]["component_count"] >= 1
    assert _metric(glare, "glare")["nature"] == "heuristic"
    assert "GLARE" in glare["reason_codes"]


def test_glare_below_configured_near_saturation_threshold_triggers() -> None:
    image = s.glare_document_bytes()
    for profile, layout, cfg in _document_configs():
        result = analyze_bytes(image, profile, layout)
        glare = _metric(result, "glare")["value"]
        threshold = cfg["glare"]["max_near_saturated_fraction"]
        assert glare["near_saturated_fraction"] <= threshold, (profile, layout)
        assert "GLARE" in result["reason_codes"], (profile, layout)


def test_glare_above_configured_near_saturation_threshold_is_suppressed() -> None:
    image = s.high_near_saturation_document_bytes()
    for profile, layout, cfg in _document_configs():
        result = analyze_bytes(image, profile, layout)
        glare = _metric(result, "glare")["value"]
        assert (
            glare["near_saturated_fraction"]
            > cfg["glare"]["max_near_saturated_fraction"]
        ), (profile, layout)
        assert (
            glare["suspected_fraction"] > cfg["glare"]["max_suspected_fraction"]
        ), (profile, layout)
        assert (
            glare["largest_component_fraction"]
            > cfg["glare"]["max_component_fraction"]
        ), (profile, layout)
        assert (
            _metric(result, "brightness")["value"]
            <= cfg["exposure"]["overexposed_mean"]
        ), (profile, layout)
        assert "GLARE" not in result["reason_codes"], (profile, layout)


def test_resolution_reports_original_and_document_pixels() -> None:
    good = analyze_bytes(s.good_document_bytes(), "id_card")
    low = analyze_bytes(s.low_resolution_document_bytes(), "id_card")
    no_document = analyze_bytes(s.no_document_bytes(), "id_card")

    original = _metric(good, "resolution_original")["value"]
    assert original["width_px"] == 1600
    assert original["height_px"] == 1000
    assert original["megapixels"] == 1.6
    document = _metric(good, "resolution_document")["value"]
    assert document["width_px"] >= 600
    assert document["height_px"] >= 400
    assert _metric(low, "resolution_document")["value"]["width_px"] < 600
    assert "TOO_SMALL" in low["reason_codes"]
    assert _metric(no_document, "resolution_document")["nature"] == "unknown"


def test_boundary_and_cropping_direction() -> None:
    good = analyze_bytes(s.good_document_bytes(), "id_card")
    cropped = analyze_bytes(s.cropped_document_bytes(), "id_card")
    good_boundary = _metric(good, "document_boundary")["value"]
    cropped_boundary = _metric(cropped, "document_boundary")["value"]

    assert good_boundary["valid"] is True
    assert good_boundary["area_ratio"] > 0.5
    assert good_boundary["border_sides_touching"] == 0
    assert good_boundary["cropping_confirmed"] is False
    assert good_boundary["aspect_ratio_in_range"] is True

    assert cropped_boundary["valid"] is True
    assert cropped_boundary["border_sides_touching"] >= 2
    assert cropped_boundary["cropping_confirmed"] is True
    assert "CROPPED" in cropped["reason_codes"]


def test_rotation_moves_and_90_180_is_unknown() -> None:
    rotated = analyze_bytes(s.rotated_document_bytes(), "id_card")
    rotation = _metric(rotated, "rotation")
    assert abs(rotation["value"]["angle_deg"]) > 5.0
    assert rotation["value"]["orientation_90_180"] == "unknown"
    assert "ROTATED" in rotated["reason_codes"]


def test_no_document_and_too_small_boundary_states() -> None:
    no_document = analyze_bytes(s.no_document_bytes(), "id_card")
    tiny = analyze_bytes(s.tiny_image_bytes(), "id_card")
    boundary = _metric(no_document, "document_boundary")["value"]
    assert boundary["valid"] is False
    assert boundary["no_document_signals"] >= 2
    assert "NO_DOCUMENT_FOUND" in no_document["reason_codes"]

    tiny_boundary = _metric(tiny, "document_boundary")
    assert tiny_boundary["value"]["valid"] is False
    assert tiny_boundary["value"]["no_document_signals"] == 0
    assert tiny_boundary["metadata"]["detector"] == "too_small_for_boundary"


def test_paper_layout_reports_unknown_aspect_ratio() -> None:
    result = analyze_bytes(s.good_document_bytes(1600, 1100), "vehicle_registration")
    assert _metric(result, "document_boundary")["value"]["aspect_ratio_in_range"] is None


def test_perspective_metric_direction() -> None:
    cfg = get_profile_config("id_card").data["rotation"]
    boundary = SimpleNamespace(
        valid=True,
        confidence=0.9,
        quad=np.array(
            [
                [0.0, 0.0],
                [1000.0, 20.0],
                [800.0, 520.0],
                [20.0, 500.0],
            ],
            dtype=np.float32,
        ),
    )
    metric = compute_perspective_metric(boundary, cfg)
    assert metric.value["ratio"] > 0.08
    assert metric.nature == "heuristic"
    assert metric.value["top_bottom_ratio"] > 0.0


def test_rotation_metric_unknown_without_boundary() -> None:
    cfg = get_profile_config("id_card").data["rotation"]
    boundary = SimpleNamespace(valid=False, confidence=0.0, quad=None)
    metric = compute_rotation_metric(boundary, cfg)
    assert metric.nature == "unknown"
    assert metric.value["angle_deg"] is None
    assert metric.value["orientation_90_180"] == "unknown"
