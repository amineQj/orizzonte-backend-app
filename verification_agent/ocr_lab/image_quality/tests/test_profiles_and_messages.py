from __future__ import annotations

import json
from pathlib import Path

import pytest

from image_quality.errors import ProfileError
from image_quality.output_schema import REASON_CODES
from image_quality.profiles import (
    DEFAULT_LAYOUTS,
    PROFILE_LAYOUTS,
    get_profile_config,
    load_thresholds,
)


def test_thresholds_load_all_profiles_and_layouts() -> None:
    data = load_thresholds()
    assert data["schema_version"] == "1.0.0"
    assert data["calibration_status"] == "UNCALIBRATED"
    for profile, layouts in PROFILE_LAYOUTS.items():
        for layout in layouts:
            cfg = get_profile_config(profile, layout)
            assert cfg.profile == profile
            assert cfg.layout == layout
            assert cfg.data["calibration_status"] == "UNCALIBRATED"
            assert cfg.thresholds_calibrated is False
            assert cfg.data["max_working_side"] == 1600
            assert cfg.data["max_megapixels"] == 40.0


def test_default_layouts() -> None:
    assert DEFAULT_LAYOUTS == {
        "id_card": "default",
        "driving_license": "card",
        "vehicle_registration": "paper",
        "selfie": "default",
    }
    assert get_profile_config("driving_license").layout == "card"
    assert get_profile_config("driving_license", "paper").layout == "paper"


def test_selfie_is_quality_only() -> None:
    cfg = get_profile_config("selfie")
    assert cfg.document_analysis is False
    assert "boundary" not in cfg.data
    assert "glare" not in cfg.data
    assert "rotation" not in cfg.data
    assert "resolution_document" not in cfg.data


def test_glare_guard_threshold_is_loaded_for_all_document_layouts() -> None:
    document_layouts: list[tuple[str, str]] = []
    for profile, layouts in PROFILE_LAYOUTS.items():
        for layout in layouts:
            cfg = get_profile_config(profile, layout)
            if not cfg.document_analysis:
                assert "glare" not in cfg.data
                continue
            guard = cfg.data["glare"]["max_near_saturated_fraction"]
            assert isinstance(guard, (int, float)) and not isinstance(guard, bool)
            assert 0.0 < float(guard) <= 1.0
            document_layouts.append((profile, layout))
    assert len(document_layouts) == 4


@pytest.mark.parametrize("bad_value", [0.0, 1.5, -0.1, "0.30", True, None])
def test_glare_guard_threshold_rejects_invalid_configuration(
    tmp_path: Path,
    bad_value: object,
) -> None:
    data = load_thresholds()
    data["profiles"]["id_card"]["layouts"]["default"]["glare"][
        "max_near_saturated_fraction"
    ] = bad_value
    path = tmp_path / "thresholds.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ProfileError) as exc_info:
        load_thresholds(path)
    assert exc_info.value.category == "invalid_configuration"


def test_paper_layout_has_no_aspect_ratio_range() -> None:
    cfg = get_profile_config("vehicle_registration")
    assert cfg.data["boundary"]["aspect_ratio_range"] is None


def test_invalid_profile_and_layout() -> None:
    with pytest.raises(ProfileError) as exc_info:
        get_profile_config("passport")
    assert exc_info.value.category == "invalid_profile"

    with pytest.raises(ProfileError) as exc_info:
        get_profile_config("id_card", "paper")
    assert exc_info.value.category == "invalid_layout"


def test_overrides_do_not_mutate_loaded_config() -> None:
    cfg = get_profile_config("id_card")
    changed = cfg.with_overrides(max_side=800, max_megapixels=5.0)
    assert changed.data["max_working_side"] == 800
    assert changed.data["max_megapixels"] == 5.0
    assert cfg.data["max_working_side"] == 1600
    assert cfg.data["max_megapixels"] == 40.0

    with pytest.raises(ProfileError) as exc_info:
        cfg.with_overrides(max_side=0)
    assert exc_info.value.category == "invalid_configuration"

    with pytest.raises(ProfileError) as exc_info:
        cfg.with_overrides(max_megapixels=-1.0)
    assert exc_info.value.category == "invalid_configuration"


def test_messages_catalog_is_complete() -> None:
    path = Path(__file__).resolve().parents[1] / "messages.json"
    with path.open("r", encoding="utf-8") as handle:
        messages = json.load(handle)
    assert messages["schema_version"] == "1.0.0"
    assert set(messages) == {"schema_version"} | set(REASON_CODES)
    for code in REASON_CODES:
        entry = messages[code]
        assert isinstance(entry, dict)
        assert set(entry) == {"en", "fr", "ar"}
        assert entry["en"].strip()
        assert entry["fr"].strip()
        assert entry["ar"] == ""
        assert "TODO" not in entry["en"].upper()
        assert "TODO" not in entry["fr"].upper()
        assert not any(character.isdigit() for character in entry["en"])
        assert not any(character.isdigit() for character in entry["fr"])


def test_every_profile_has_uncertainty_marked_in_thresholds() -> None:
    data = load_thresholds()
    for profile, profile_data in data["profiles"].items():
        for layout, layout_data in profile_data["layouts"].items():
            assert layout_data["calibration_status"] == "UNCALIBRATED", (profile, layout)
