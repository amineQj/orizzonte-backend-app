from __future__ import annotations

from copy import deepcopy

from image_quality.decision_engine import decide
from image_quality.output_schema import (
    STATUS_ACCEPTABLE,
    STATUS_POOR,
    STATUS_RECAPTURE,
    MetricResult,
)
from image_quality.profiles import get_profile_config


def _m(value, nature: str = "measured", confidence: float = 0.9) -> MetricResult:
    return MetricResult(value=value, nature=nature, confidence=confidence)


def _base_metrics() -> dict[str, MetricResult]:
    return {
        "resolution_original": _m(
            {"width_px": 1600, "height_px": 1000, "megapixels": 1.6}
        ),
        "resolution_document": _m(
            {"width_px": 1200, "height_px": 750, "megapixels": 0.9}
        ),
        "sharpness_laplacian": _m(800.0),
        "sharpness_tenengrad": _m(12000.0),
        "brightness": _m(180.0),
        "clipping": _m({"dark_fraction": 0.0, "bright_fraction": 0.0}),
        "contrast": _m(
            {"p5_p95_range": 180.0, "rms_contrast": 50.0, "p5": 220.0}
        ),
        "uneven_illumination": _m({"ratio": 0.2}, nature="heuristic", confidence=0.65),
        "glare": _m(
            {
                "suspected_fraction": 0.0,
                "largest_component_fraction": 0.0,
                "near_saturated_fraction": 0.0,
            },
            nature="heuristic",
            confidence=0.55,
        ),
        "document_boundary": _m(
            {
                "valid": True,
                "cropping_confirmed": False,
                "no_document_signals": 0,
            },
            nature="heuristic",
            confidence=0.9,
        ),
        "rotation": _m({"angle_deg": 0.0}, nature="heuristic", confidence=0.9),
        "perspective": _m({"ratio": 0.01}, nature="heuristic", confidence=0.9),
    }


def _cfg():
    return get_profile_config("id_card").data


def _glare_metric(
    near_saturated: object,
    *,
    include_near_saturated: bool = True,
) -> MetricResult:
    value: dict[str, object] = {
        "suspected_fraction": 0.1,
        "largest_component_fraction": 0.05,
    }
    if include_near_saturated:
        value["near_saturated_fraction"] = near_saturated
    return _m(value, nature="heuristic", confidence=0.8)


def test_clean_metrics_are_acceptable() -> None:
    decision = decide(_base_metrics(), _cfg(), True)
    assert decision.status == STATUS_ACCEPTABLE
    assert decision.reason_codes == []
    assert decision.trace == []
    assert decision.confidence > 0.9


def test_both_severe_sharpness_indicators_are_needs_recapture() -> None:
    metrics = _base_metrics()
    metrics["sharpness_laplacian"] = _m(5.0)
    metrics["sharpness_tenengrad"] = _m(500.0)
    decision = decide(metrics, _cfg(), True)
    assert decision.status == STATUS_RECAPTURE
    assert decision.reason_codes == ["BLURRY"]
    assert decision.trace[0]["severity"] == "critical"


def test_one_soft_sharpness_indicator_alone_is_acceptable() -> None:
    metrics = _base_metrics()
    metrics["sharpness_laplacian"] = _m(32.0)
    metrics["sharpness_tenengrad"] = _m(5000.0)
    decision = decide(metrics, _cfg(), True)
    assert decision.status == STATUS_ACCEPTABLE
    assert decision.reason_codes == []


def test_one_severe_indicator_alone_needs_recapture() -> None:
    metrics = _base_metrics()
    metrics["sharpness_laplacian"] = _m(5.0)
    metrics["sharpness_tenengrad"] = _m(5000.0)
    decision = decide(metrics, _cfg(), True)
    assert decision.status == STATUS_RECAPTURE
    assert decision.reason_codes == ["BLURRY"]
    assert decision.trace[0]["severity"] == "critical"


def test_soft_blur_band_is_poor_quality() -> None:
    metrics = _base_metrics()
    metrics["sharpness_laplacian"] = _m(32.0)
    metrics["sharpness_tenengrad"] = _m(2400.0)
    decision = decide(metrics, _cfg(), True)
    assert decision.status == STATUS_POOR
    assert decision.reason_codes == ["BLURRY"]
    assert decision.trace[0]["severity"] == "moderate"


def test_easy_readable_specimen_metrics_are_acceptable() -> None:
    # Regression for id_card_test_easy.jpeg: Laplacian 35.6451 is just above
    # the narrowed soft upper edge (35) and Tenengrad 2480.5907 is soft.  Only
    # one indicator is soft, so the AND rule accepts the image.
    metrics = _base_metrics()
    metrics["sharpness_laplacian"] = _m(35.6451)
    metrics["sharpness_tenengrad"] = _m(2480.5907)
    decision = decide(metrics, _cfg(), True)
    assert decision.status == STATUS_ACCEPTABLE
    assert decision.reason_codes == []


def test_soft_blur_boundary_is_inclusive() -> None:
    metrics = _base_metrics()
    metrics["sharpness_laplacian"] = _m(30.0)
    metrics["sharpness_tenengrad"] = _m(2000.0)
    decision = decide(metrics, _cfg(), True)
    assert decision.status == STATUS_POOR
    assert decision.trace[0]["severity"] == "moderate"


def test_two_hard_blur_indicators_below_soft_band_need_recapture() -> None:
    metrics = _base_metrics()
    metrics["sharpness_laplacian"] = _m(29.9)
    metrics["sharpness_tenengrad"] = _m(1999.9)
    decision = decide(metrics, _cfg(), True)
    assert decision.status == STATUS_RECAPTURE
    assert decision.reason_codes == ["BLURRY"]
    assert decision.trace[0]["severity"] == "critical"


def test_tenengrad_hard_indicator_alone_needs_recapture() -> None:
    metrics = _base_metrics()
    metrics["sharpness_laplacian"] = _m(50.0)
    metrics["sharpness_tenengrad"] = _m(1999.9)
    decision = decide(metrics, _cfg(), True)
    assert decision.status == STATUS_RECAPTURE
    assert decision.reason_codes == ["BLURRY"]
    assert decision.trace[0]["severity"] == "critical"
    assert decision.trace[0]["metrics"] == ["sharpness_tenengrad"]


def test_low_contrast_suppresses_critical_blur() -> None:
    metrics = _base_metrics()
    metrics["sharpness_laplacian"] = _m(20.0)
    metrics["sharpness_tenengrad"] = _m(1000.0)
    metrics["contrast"] = _m({"p5_p95_range": 40.0, "rms_contrast": 10.0})
    decision = decide(metrics, _cfg(), True)
    assert decision.status == STATUS_POOR
    assert "LOW_CONTRAST" in decision.reason_codes
    blur = next(
        entry for entry in decision.trace if entry["reason_code"] == "BLURRY"
    )
    assert blur["severity"] == "moderate"


def test_p5_secondary_contrast_signal_is_moderate() -> None:
    metrics = _base_metrics()
    metrics["contrast"] = _m(
        {"p5": 35.0, "p5_p95_range": 180.0, "rms_contrast": 50.0}
    )
    decision = decide(metrics, _cfg(), True)
    assert decision.status == STATUS_POOR
    assert decision.reason_codes == ["LOW_CONTRAST"]
    assert decision.trace[0]["severity"] == "moderate"


def test_rms_secondary_contrast_signal_is_moderate() -> None:
    cfg = deepcopy(_cfg())
    cfg["contrast"]["p5_p95_min_range"] = 10.0
    cfg["contrast"]["rms_min"] = 10.0
    metrics = _base_metrics()
    metrics["contrast"] = _m(
        {"p5": 80.0, "p5_p95_range": 180.0, "rms_contrast": 19.0}
    )
    decision = decide(metrics, cfg, True)
    assert decision.status == STATUS_POOR
    assert decision.reason_codes == ["LOW_CONTRAST"]
    assert decision.trace[0]["severity"] == "moderate"


def test_secondary_contrast_thresholds_are_strict() -> None:
    cfg = deepcopy(_cfg())
    cfg["contrast"]["p5_p95_min_range"] = 10.0
    cfg["contrast"]["rms_min"] = 10.0
    metrics = _base_metrics()
    metrics["contrast"] = _m(
        {"p5": 40.0, "p5_p95_range": 180.0, "rms_contrast": 20.0}
    )
    decision = decide(metrics, cfg, True)
    assert decision.status == STATUS_ACCEPTABLE
    assert decision.reason_codes == []


def test_sharpness_above_soft_band_is_acceptable() -> None:
    metrics = _base_metrics()
    metrics["sharpness_laplacian"] = _m(35.1)
    metrics["sharpness_tenengrad"] = _m(2500.1)
    decision = decide(metrics, _cfg(), True)
    assert decision.status == STATUS_ACCEPTABLE
    assert decision.reason_codes == []


def test_unknown_metric_lowers_confidence_but_does_not_reject_alone() -> None:
    known = decide(_base_metrics(), _cfg(), True)
    metrics = _base_metrics()
    metrics["contrast"] = _m(None, nature="unknown", confidence=0.0)
    unknown = decide(metrics, _cfg(), True)
    assert unknown.status == STATUS_ACCEPTABLE
    assert unknown.reason_codes == []
    assert unknown.confidence < known.confidence


def test_no_document_and_cropping_rules() -> None:
    no_document = _base_metrics()
    no_document["document_boundary"] = _m(
        {"valid": False, "cropping_confirmed": False, "no_document_signals": 2},
        nature="heuristic",
        confidence=0.3,
    )
    decision = decide(no_document, _cfg(), True)
    assert decision.status == STATUS_RECAPTURE
    assert decision.reason_codes == ["NO_DOCUMENT_FOUND"]

    cropped = _base_metrics()
    cropped["document_boundary"] = _m(
        {"valid": True, "cropping_confirmed": True, "no_document_signals": 0},
        nature="heuristic",
        confidence=0.9,
    )
    decision = decide(cropped, _cfg(), True)
    assert decision.status == STATUS_RECAPTURE
    assert decision.reason_codes == ["CROPPED"]


def test_resolution_rules() -> None:
    too_small = _base_metrics()
    too_small["resolution_original"] = _m(
        {"width_px": 320, "height_px": 240, "megapixels": 0.0768}
    )
    decision = decide(too_small, _cfg(), True)
    assert "TOO_SMALL" in decision.reason_codes
    assert decision.status == STATUS_RECAPTURE

    low_res = _base_metrics()
    low_res["resolution_document"] = _m(
        {"width_px": 200, "height_px": 150, "megapixels": 0.03}
    )
    decision = decide(low_res, _cfg(), True)
    assert "LOW_RES" in decision.reason_codes
    assert decision.status == STATUS_RECAPTURE


def test_exposure_contrast_glare_rotation_and_perspective_rules() -> None:
    dark = _base_metrics()
    dark["brightness"] = _m(40.0)
    assert "DARK" in decide(dark, _cfg(), True).reason_codes

    over = _base_metrics()
    over["brightness"] = _m(240.0)
    over["clipping"] = _m({"dark_fraction": 0.0, "bright_fraction": 0.8})
    over["contrast"] = _m({"p5_p95_range": 10.0, "rms_contrast": 5.0})
    over_decision = decide(over, _cfg(), True)
    assert "OVEREXPOSED" in over_decision.reason_codes
    assert "LOW_CONTRAST" in over_decision.reason_codes

    uneven = _base_metrics()
    uneven["uneven_illumination"] = _m(
        {"ratio": 0.7}, nature="heuristic", confidence=0.65
    )
    assert "UNEVEN_LIGHT" in decide(uneven, _cfg(), True).reason_codes

    glare = _base_metrics()
    glare["glare"] = _m(
        {
            "suspected_fraction": 0.1,
            "largest_component_fraction": 0.05,
            "near_saturated_fraction": 0.1,
        },
        nature="heuristic",
        confidence=0.8,
    )
    assert "GLARE" in decide(glare, _cfg(), True).reason_codes

    rotated = _base_metrics()
    rotated["rotation"] = _m({"angle_deg": 10.0}, nature="heuristic", confidence=0.9)
    assert "ROTATED" in decide(rotated, _cfg(), True).reason_codes

    perspective = _base_metrics()
    perspective["perspective"] = _m(
        {"ratio": 0.2}, nature="heuristic", confidence=0.9
    )
    assert "PERSPECTIVE" in decide(perspective, _cfg(), True).reason_codes


def test_glare_is_suppressed_for_overexposed_low_contrast_images() -> None:
    metrics = _base_metrics()
    metrics["brightness"] = _m(240.0)
    metrics["clipping"] = _m({"dark_fraction": 0.0, "bright_fraction": 0.8})
    metrics["contrast"] = _m({"p5_p95_range": 10.0, "rms_contrast": 5.0})
    metrics["glare"] = _m(
        {
            "suspected_fraction": 0.2,
            "largest_component_fraction": 0.05,
            "near_saturated_fraction": 0.8,
        },
        nature="heuristic",
        confidence=0.8,
    )
    decision = decide(metrics, _cfg(), True)
    assert "GLARE" not in decision.reason_codes


def test_glare_is_suppressed_when_most_pixels_are_near_saturated() -> None:
    metrics = _base_metrics()
    metrics["glare"] = _m(
        {
            "suspected_fraction": 0.1,
            "largest_component_fraction": 0.05,
            "near_saturated_fraction": 0.8,
        },
        nature="heuristic",
        confidence=0.8,
    )
    decision = decide(metrics, _cfg(), True)
    assert "GLARE" not in decision.reason_codes


def test_glare_guard_below_configured_threshold_keeps_glare() -> None:
    cfg = _cfg()
    threshold = float(cfg["glare"]["max_near_saturated_fraction"])
    metrics = _base_metrics()
    metrics["glare"] = _glare_metric(threshold * 0.5)
    decision = decide(metrics, cfg, True)
    assert "GLARE" in decision.reason_codes
    assert decision.status == STATUS_POOR


def test_glare_guard_above_configured_threshold_suppresses_glare() -> None:
    cfg = _cfg()
    threshold = float(cfg["glare"]["max_near_saturated_fraction"])
    metrics = _base_metrics()
    metrics["glare"] = _glare_metric(min(0.999, threshold + 0.05))
    decision = decide(metrics, cfg, True)
    assert "GLARE" not in decision.reason_codes


def test_glare_guard_reads_configured_threshold_not_a_constant() -> None:
    metrics = _base_metrics()
    metrics["glare"] = _glare_metric(0.5)

    low_threshold_cfg = deepcopy(_cfg())
    low_threshold_cfg["glare"]["max_near_saturated_fraction"] = 0.4
    assert "GLARE" not in decide(metrics, low_threshold_cfg, True).reason_codes

    high_threshold_cfg = deepcopy(_cfg())
    high_threshold_cfg["glare"]["max_near_saturated_fraction"] = 0.6
    assert "GLARE" in decide(metrics, high_threshold_cfg, True).reason_codes


def test_glare_guard_handles_missing_or_invalid_near_saturation() -> None:
    cfg = _cfg()
    cases = (
        (None, True),
        (None, False),
        ("invalid", True),
        (True, True),
    )
    for value, include in cases:
        metrics = _base_metrics()
        metrics["glare"] = _glare_metric(value, include_near_saturated=include)
        decision = decide(metrics, cfg, True)
        assert "GLARE" in decision.reason_codes, (value, include)

    metrics = _base_metrics()
    del metrics["glare"]
    assert "GLARE" not in decide(metrics, cfg, True).reason_codes


def test_reason_priority_and_trace_structure() -> None:
    metrics = _base_metrics()
    metrics["resolution_original"] = _m(
        {"width_px": 320, "height_px": 240, "megapixels": 0.0768}
    )
    metrics["sharpness_laplacian"] = _m(5.0)
    metrics["sharpness_tenengrad"] = _m(500.0)
    decision = decide(metrics, _cfg(), True)
    assert decision.reason_codes == ["TOO_SMALL", "BLURRY"]
    assert [entry["reason_code"] for entry in decision.trace] == ["TOO_SMALL", "BLURRY"]
    for entry in decision.trace:
        assert entry["severity"] in ("critical", "moderate")
        assert isinstance(entry["metrics"], list)


def test_selfie_ignores_document_metrics() -> None:
    metrics = deepcopy(_base_metrics())
    metrics["document_boundary"] = _m(
        {"valid": False, "cropping_confirmed": True, "no_document_signals": 4},
        nature="heuristic",
        confidence=0.9,
    )
    decision = decide(metrics, get_profile_config("selfie").data, False)
    assert decision.status == STATUS_ACCEPTABLE
    assert decision.reason_codes == []


def test_driving_license_cut_borders_with_high_contrast_are_acceptable() -> None:
    metrics = _base_metrics()
    metrics["document_boundary"] = _m(
        {
            "valid": True,
            "cropping_confirmed": False,
            "no_document_signals": 0,
            "border_sides_touching": 3,
            "candidate_count": 0,
            "edge_density": 0.08,
            "warning": "border_cut_but_high_contrast",
        },
        nature="heuristic",
        confidence=0.9,
    )
    metrics["contrast"] = _m(
        {"p5": 35.0, "p5_p95_range": 120.0, "rms_contrast": 30.0}
    )
    decision = decide(
        metrics,
        get_profile_config("driving_license", "card").data,
        True,
    )
    assert decision.status == STATUS_ACCEPTABLE
    assert decision.reason_codes == []
