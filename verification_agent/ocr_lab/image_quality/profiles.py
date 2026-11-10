"""Profile and threshold loading for the image-quality module."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ProfileError

PROFILE_LAYOUTS: dict[str, tuple[str, ...]] = {
    "id_card": ("default",),
    "driving_license": ("card", "paper"),
    "vehicle_registration": ("paper",),
    "selfie": ("default",),
}

DEFAULT_LAYOUTS: dict[str, str] = {
    "id_card": "default",
    "driving_license": "card",
    "vehicle_registration": "paper",
    "selfie": "default",
}

DOCUMENT_PROFILES = frozenset({"id_card", "driving_license", "vehicle_registration"})

_COMMON_KEYS = (
    "calibration_status",
    "document_analysis",
    "max_megapixels",
    "max_working_side",
    "min_original_width",
    "min_original_height",
    "min_original_megapixels",
    "evaluation_region",
    "blur",
    "exposure",
    "contrast",
)

_DOCUMENT_KEYS = (
    "min_document_width",
    "min_document_height",
    "min_document_megapixels",
    "glare",
    "boundary",
    "rotation",
)


@dataclass(frozen=True)
class ProfileConfig:
    profile: str
    layout: str
    data: dict[str, Any]

    @property
    def document_analysis(self) -> bool:
        return bool(self.data["document_analysis"])

    @property
    def calibration_status(self) -> str:
        return str(self.data["calibration_status"])

    @property
    def thresholds_calibrated(self) -> bool:
        return self.calibration_status.upper() == "CALIBRATED"

    def with_overrides(
        self,
        max_side: int | None = None,
        max_megapixels: float | None = None,
    ) -> "ProfileConfig":
        data = copy.deepcopy(self.data)
        if max_side is not None:
            value = int(max_side)
            if value <= 0:
                raise ProfileError("invalid_configuration")
            data["max_working_side"] = value
        if max_megapixels is not None:
            megapixels = float(max_megapixels)
            if megapixels <= 0.0:
                raise ProfileError("invalid_configuration")
            data["max_megapixels"] = megapixels
        return ProfileConfig(profile=self.profile, layout=self.layout, data=data)


def _thresholds_path() -> Path:
    return Path(__file__).with_name("thresholds.json")


def _validate_glare_guard(data: dict[str, Any]) -> None:
    glare = data.get("glare")
    if not isinstance(glare, dict):
        raise ProfileError("invalid_configuration")
    guard = glare.get("max_near_saturated_fraction")
    if isinstance(guard, bool) or not isinstance(guard, (int, float)):
        raise ProfileError("invalid_configuration")
    if not 0.0 < float(guard) <= 1.0:
        raise ProfileError("invalid_configuration")


def _validate_layout(profile: str, layout: str, data: dict[str, Any]) -> None:
    if not isinstance(data, dict):
        raise ProfileError("invalid_configuration")
    required = list(_COMMON_KEYS)
    if data.get("document_analysis", False):
        required.extend(_DOCUMENT_KEYS)
    for key in required:
        if key not in data:
            raise ProfileError("invalid_configuration")
    if data["calibration_status"] not in ("UNCALIBRATED", "CALIBRATED"):
        raise ProfileError("invalid_configuration")
    if not isinstance(data["document_analysis"], bool):
        raise ProfileError("invalid_configuration")
    if data["document_analysis"] and profile == "selfie":
        raise ProfileError("invalid_configuration")
    if profile in DOCUMENT_PROFILES and not data["document_analysis"]:
        raise ProfileError("invalid_configuration")
    if not isinstance(data["evaluation_region"], dict):
        raise ProfileError("invalid_configuration")
    if "document_inset_fraction" not in data["evaluation_region"]:
        raise ProfileError("invalid_configuration")
    if "central_fraction" not in data["evaluation_region"]:
        raise ProfileError("invalid_configuration")
    if data["document_analysis"]:
        _validate_glare_guard(data)


def load_thresholds(path: str | Path | None = None) -> dict[str, Any]:
    source = Path(path) if path is not None else _thresholds_path()
    try:
        with source.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        raise ProfileError("invalid_configuration") from exc

    if not isinstance(data, dict) or "profiles" not in data:
        raise ProfileError("invalid_configuration")
    profiles = data["profiles"]
    if not isinstance(profiles, dict):
        raise ProfileError("invalid_configuration")

    for profile, expected_layouts in PROFILE_LAYOUTS.items():
        profile_data = profiles.get(profile)
        if not isinstance(profile_data, dict):
            raise ProfileError("invalid_configuration")
        layouts = profile_data.get("layouts")
        if not isinstance(layouts, dict):
            raise ProfileError("invalid_configuration")
        for layout in expected_layouts:
            if layout not in layouts:
                raise ProfileError("invalid_configuration")
            _validate_layout(profile, layout, layouts[layout])
    return data


def get_profile_config(
    profile: str,
    layout: str | None = None,
    thresholds: dict[str, Any] | None = None,
) -> ProfileConfig:
    if profile not in PROFILE_LAYOUTS:
        raise ProfileError("invalid_profile")

    selected_layout = layout or DEFAULT_LAYOUTS[profile]
    if selected_layout not in PROFILE_LAYOUTS[profile]:
        raise ProfileError("invalid_layout")

    data = thresholds if thresholds is not None else load_thresholds()
    try:
        layout_data = data["profiles"][profile]["layouts"][selected_layout]
    except (KeyError, TypeError) as exc:
        raise ProfileError("invalid_configuration") from exc

    return ProfileConfig(profile=profile, layout=selected_layout, data=layout_data)
