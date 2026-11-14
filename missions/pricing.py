"""Pure pricing engine for ORIZZONTE missions.

This module deliberately has **no Django imports, no ORM access and no
network calls**. Every function is deterministic and receives its clock /
pickup time explicitly, so tests can pin time without freezing the system
clock.

Rules implemented here (see ``missions/data/pricing_v1.json``):

    subtotal = max(min_fare, base + km * per_km + min * per_min + extras)
    extras   = helpers * helper_hourly * (handling_minutes / 60)
             + floors * per_floor_fee
    estimate = round_to_step(subtotal * multiplier)
    low/high = estimate * (1 -/+ band)

All monetary amounts are integers in DZD. Ratios are ``Decimal`` values
parsed with ``Decimal(str(value))`` so a JSON number never enters float
arithmetic. The placeholder configuration is **not** market-calibrated.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import date, datetime, time as dtime
from decimal import (
    Decimal,
    InvalidOperation,
    ROUND_CEILING,
    ROUND_FLOOR,
    ROUND_HALF_UP,
)
from typing import Any, Mapping
from zoneinfo import ZoneInfo

CURRENCY = "DZD"

ALGIERS_TZ = ZoneInfo("Africa/Algiers")

# A quote stays confirmable for this long. Not part of the versioned JSON so a
# boss editing prices cannot accidentally change the quote lifetime.
QUOTE_TTL_MINUTES = 30

# Hard safety caps on request inputs. They are not pricing parameters.
MAX_HANDLING_MINUTES = 1440
MAX_DURATION_MINUTES = 7 * 24 * 60
MAX_LEGACY_WORKERS = 5

MIN_STRAIGHT_LINE_KM = Decimal("0.05")

VEHICLE_CLASSES = ("standard", "van", "truck")

CONFIDENCE_NORMAL = "normal"
CONFIDENCE_LOW = "low"

# Fields that switch a request onto the new pricing path. Presence of any one
# of them (with a non-null value) is what makes pricing opt-in.
NEW_PRICING_FIELDS = (
    "vehicle_class",
    "helpers",
    "floors",
    "handling_minutes",
    "duration_min",
)

_ONE = Decimal("1")
_ZERO = Decimal("0")


class PricingError(ValueError):
    """A pricing/validation failure carrying a stable machine-readable code."""

    def __init__(self, code: str, message: str, *, field: str | None = None):
        self.code = code
        self.message = message
        self.field = field
        super().__init__(message)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VehicleClass:
    base: int
    per_km: int
    per_min: int
    min_fare: int


@dataclass(frozen=True)
class ExtrasConfig:
    helper_hourly: int
    per_floor_fee: int
    max_helpers: int
    max_floors: int


@dataclass(frozen=True)
class NegotiationConfig:
    tolerance: Decimal
    max_offers: int
    offer_ttl_minutes: int
    total_ttl_minutes: int
    min_step: int


@dataclass(frozen=True)
class DistanceBounds:
    min_ratio: Decimal
    max_ratio: Decimal


@dataclass(frozen=True)
class Configuration:
    version: int
    calibrated: bool
    rounding_step: int
    vehicle_classes: dict[str, VehicleClass]
    extras: ExtrasConfig
    peak: Decimal
    night: Decimal
    weekend: Decimal
    peak_hours: tuple[tuple[int, int], ...]
    night_hours: tuple[tuple[int, int], ...]
    multiplier_cap: Decimal
    estimate_band: Decimal
    low_confidence_extra_band: Decimal
    distance_bounds: DistanceBounds
    fallback_detour_factor: Decimal
    fallback_speed_kmh: Decimal
    commission_rate: Decimal
    negotiation: NegotiationConfig

    # -- parsing ---------------------------------------------------------

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "Configuration":
        """Build and validate a configuration from the JSON document."""
        if not isinstance(data, Mapping):
            raise PricingError(
                "invalid_pricing_config", "Pricing config must be a JSON object."
            )

        version = _as_int(data.get("version", 1), "version", minimum=1)
        calibrated = bool(data.get("calibrated", False))
        step = _as_int(data.get("rounding_step"), "rounding_step", minimum=1)

        classes_raw = _mapping(data.get("vehicle_classes"), "vehicle_classes")
        classes: dict[str, VehicleClass] = {}
        for raw_name, raw in classes_raw.items():
            name = str(raw_name).strip().lower()
            if name not in VEHICLE_CLASSES:
                raise PricingError(
                    "invalid_pricing_config",
                    f"Unknown vehicle class '{raw_name}'.",
                )
            item = _mapping(raw, f"vehicle_classes.{name}")
            classes[name] = VehicleClass(
                base=_as_int(
                    item.get("base"), f"vehicle_classes.{name}.base", minimum=0
                ),
                per_km=_as_int(
                    item.get("per_km"), f"vehicle_classes.{name}.per_km", minimum=0
                ),
                per_min=_as_int(
                    item.get("per_min"), f"vehicle_classes.{name}.per_min", minimum=0
                ),
                min_fare=_as_int(
                    item.get("min_fare"),
                    f"vehicle_classes.{name}.min_fare",
                    minimum=1,
                ),
            )
        missing = [name for name in VEHICLE_CLASSES if name not in classes]
        if missing:
            raise PricingError(
                "invalid_pricing_config",
                f"Missing vehicle class(es): {', '.join(missing)}.",
            )

        extras_raw = _mapping(data.get("extras"), "extras")
        extras = ExtrasConfig(
            helper_hourly=_as_int(
                extras_raw.get("helper_hourly"), "extras.helper_hourly", minimum=0
            ),
            per_floor_fee=_as_int(
                extras_raw.get("per_floor_fee"), "extras.per_floor_fee", minimum=0
            ),
            max_helpers=_as_int(
                extras_raw.get("max_helpers"), "extras.max_helpers", minimum=0
            ),
            max_floors=_as_int(
                extras_raw.get("max_floors"), "extras.max_floors", minimum=0
            ),
        )

        multipliers = _mapping(data.get("time_multipliers"), "time_multipliers")
        peak = _as_decimal(
            multipliers.get("peak"), "time_multipliers.peak", minimum=_ONE
        )
        night = _as_decimal(
            multipliers.get("night"), "time_multipliers.night", minimum=_ONE
        )
        weekend = _as_decimal(
            multipliers.get("weekend"), "time_multipliers.weekend", minimum=_ONE
        )

        cap = _as_decimal(
            data.get("multiplier_cap"), "multiplier_cap", minimum=_ONE
        )
        band = _as_decimal(
            data.get("estimate_band"),
            "estimate_band",
            minimum=_ZERO,
            maximum=Decimal("0.99"),
        )
        low_band = _as_decimal(
            data.get("low_confidence_extra_band"),
            "low_confidence_extra_band",
            minimum=_ZERO,
            maximum=Decimal("0.99"),
        )
        if band + low_band >= _ONE:
            raise PricingError(
                "invalid_pricing_config",
                "estimate_band + low_confidence_extra_band must stay below 1.",
            )

        bounds_raw = _mapping(data.get("distance_bounds"), "distance_bounds")
        min_ratio = _as_decimal(
            bounds_raw.get("min_ratio_to_haversine"),
            "distance_bounds.min_ratio_to_haversine",
            minimum=_ZERO,
        )
        max_ratio = _as_decimal(
            bounds_raw.get("max_ratio_to_haversine"),
            "distance_bounds.max_ratio_to_haversine",
            minimum=_ZERO,
        )
        if max_ratio <= min_ratio:
            raise PricingError(
                "invalid_pricing_config",
                "distance_bounds.max_ratio_to_haversine must be greater than the minimum.",
            )

        detour = _as_decimal(
            data.get("fallback_detour_factor"),
            "fallback_detour_factor",
            minimum=_ONE,
        )
        speed = _as_decimal(
            data.get("fallback_speed_kmh"),
            "fallback_speed_kmh",
            minimum=Decimal("0.01"),
        )
        commission = _as_decimal(
            data.get("commission_rate"),
            "commission_rate",
            minimum=_ZERO,
            maximum=Decimal("0.99"),
        )

        negotiation_raw = _mapping(data.get("negotiation"), "negotiation")
        tolerance = _as_decimal(
            negotiation_raw.get("tolerance"),
            "negotiation.tolerance",
            minimum=_ZERO,
            maximum=Decimal("0.99"),
        )
        negotiation = NegotiationConfig(
            tolerance=tolerance,
            max_offers=_as_int(
                negotiation_raw.get("max_offers"),
                "negotiation.max_offers",
                minimum=1,
            ),
            offer_ttl_minutes=_as_int(
                negotiation_raw.get("offer_ttl_minutes"),
                "negotiation.offer_ttl_minutes",
                minimum=1,
            ),
            total_ttl_minutes=_as_int(
                negotiation_raw.get("total_ttl_minutes"),
                "negotiation.total_ttl_minutes",
                minimum=1,
            ),
            min_step=_as_int(
                negotiation_raw.get("min_step"),
                "negotiation.min_step",
                minimum=1,
            ),
        )
        if negotiation.offer_ttl_minutes > negotiation.total_ttl_minutes:
            raise PricingError(
                "invalid_pricing_config",
                "negotiation.offer_ttl_minutes cannot exceed total_ttl_minutes.",
            )

        return cls(
            version=version,
            calibrated=calibrated,
            rounding_step=step,
            vehicle_classes=classes,
            extras=extras,
            peak=peak,
            night=night,
            weekend=weekend,
            peak_hours=_windows(data.get("peak_hours"), "peak_hours"),
            night_hours=_windows(data.get("night_hours"), "night_hours"),
            multiplier_cap=cap,
            estimate_band=band,
            low_confidence_extra_band=low_band,
            distance_bounds=DistanceBounds(min_ratio=min_ratio, max_ratio=max_ratio),
            fallback_detour_factor=detour,
            fallback_speed_kmh=speed,
            commission_rate=commission,
            negotiation=negotiation,
        )

    def with_version(self, version: int) -> "Configuration":
        """Return a copy carrying the database's authoritative version."""
        return replace(self, version=int(version))


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PricingError(
            "invalid_pricing_config", f"{field} must be a JSON object."
        )
    return value


def _as_int(
    value: Any,
    field: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
    code: str = "invalid_pricing_config",
) -> int:
    if value is None or isinstance(value, bool):
        raise PricingError(code, f"{field} must be an integer.")
    if isinstance(value, float):
        if not value.is_integer():
            raise PricingError(code, f"{field} must be an integer.")
        value = int(value)
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise PricingError(code, f"{field} must be an integer.") from None
    if minimum is not None and number < minimum:
        raise PricingError(code, f"{field} must be >= {minimum}.")
    if maximum is not None and number > maximum:
        raise PricingError(code, f"{field} must be <= {maximum}.")
    return number


def _as_decimal(
    value: Any,
    field: str,
    *,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
    code: str = "invalid_pricing_config",
) -> Decimal:
    if value is None or isinstance(value, bool):
        raise PricingError(code, f"{field} must be a number.")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise PricingError(code, f"{field} must be a number.") from None
    if not number.is_finite():
        raise PricingError(code, f"{field} must be a finite number.")
    if minimum is not None and number < minimum:
        raise PricingError(code, f"{field} must be >= {minimum}.")
    if maximum is not None and number > maximum:
        raise PricingError(code, f"{field} must be <= {maximum}.")
    return number


def _windows(value: Any, field: str) -> tuple[tuple[int, int], ...]:
    if not isinstance(value, (list, tuple)):
        raise PricingError(
            "invalid_pricing_config", f"{field} must be a list of [start, end]."
        )
    windows: list[tuple[int, int]] = []
    for item in value:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise PricingError(
                "invalid_pricing_config",
                f"{field} entries must be [start_hour, end_hour].",
            )
        start = _as_int(item[0], field, minimum=0, maximum=23)
        end = _as_int(item[1], field, minimum=0, maximum=24)
        if start == end:
            raise PricingError(
                "invalid_pricing_config", f"{field} entries cannot be empty."
            )
        windows.append((start, end))
    return tuple(windows)


def round_to_step(value: int | Decimal, step: int) -> int:
    """Round a positive value to the nearest multiple of ``step`` (half-up)."""
    if step <= 0:
        raise PricingError("invalid_pricing_config", "rounding_step must be > 0.")
    quotient = (_as_decimal(value, "value") / step).quantize(
        _ONE, rounding=ROUND_HALF_UP
    )
    return int(quotient) * step


def floor_to_step(value: int | Decimal, step: int) -> int:
    if step <= 0:
        raise PricingError("invalid_pricing_config", "rounding_step must be > 0.")
    quotient = (_as_decimal(value, "value") / step).quantize(
        _ONE, rounding=ROUND_FLOOR
    )
    return int(quotient) * step


def ceil_to_step(value: int | Decimal, step: int) -> int:
    if step <= 0:
        raise PricingError("invalid_pricing_config", "rounding_step must be > 0.")
    quotient = (_as_decimal(value, "value") / step).quantize(
        _ONE, rounding=ROUND_CEILING
    )
    return int(quotient) * step


def commission_split(price: int | Decimal, rate: Decimal) -> tuple[int, int]:
    """Split a price into ``(commission, driver_net)`` with no lost dinar.

    The commission is rounded half-up; the driver's net is the remainder, so
    ``commission + driver_net == price`` always holds.
    """
    total = _as_int(price, "price", minimum=0, code="invalid_price")
    value = (_as_decimal(total, "price") * rate).quantize(
        _ONE, rounding=ROUND_HALF_UP
    )
    commission = min(max(int(value), 0), total)
    return commission, total - commission


# ---------------------------------------------------------------------------
# Geography and distance / duration integrity
# ---------------------------------------------------------------------------


def _validate_coordinates(lat: Any, lon: Any) -> tuple[Decimal, Decimal]:
    latitude = _as_decimal(lat, "latitude", code="invalid_coordinates")
    longitude = _as_decimal(lon, "longitude", code="invalid_coordinates")
    if not (Decimal("-90") <= latitude <= Decimal("90")):
        raise PricingError(
            "invalid_coordinates", "Latitude must be between -90 and 90."
        )
    if not (Decimal("-180") <= longitude <= Decimal("180")):
        raise PricingError(
            "invalid_coordinates", "Longitude must be between -180 and 180."
        )
    return latitude, longitude


def haversine_km(
    departure_lat: Any,
    departure_lon: Any,
    arrival_lat: Any,
    arrival_lon: Any,
) -> Decimal:
    """Great-circle distance in kilometres (straight line)."""
    lat1, lon1 = _validate_coordinates(departure_lat, departure_lon)
    lat2, lon2 = _validate_coordinates(arrival_lat, arrival_lon)

    phi1 = math.radians(float(lat1))
    phi2 = math.radians(float(lat2))
    dphi = math.radians(float(lat2 - lat1))
    dlambda = math.radians(float(lon2 - lon1))

    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    a = min(1.0, max(0.0, a))
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return Decimal(str(round(6371.0088 * c, 4)))


def resolve_distance(
    config: Configuration,
    *,
    provided_km: Any | None,
    departure_lat: Any,
    departure_lon: Any,
    arrival_lat: Any,
    arrival_lon: Any,
    enforce_bounds: bool = True,
) -> tuple[Decimal, str]:
    """Return ``(distance_km, confidence)`` after integrity checks.

    * client-provided distance: must be within the configured ratio bounds of
      the straight-line distance (unless ``enforce_bounds`` is disabled for a
      legacy, opt-out request);
    * missing distance: straight line × ``fallback_detour_factor`` and
      ``confidence="low"``.
    """
    straight = haversine_km(
        departure_lat, departure_lon, arrival_lat, arrival_lon
    )
    if straight < MIN_STRAIGHT_LINE_KM:
        raise PricingError(
            "invalid_distance",
            "Departure and arrival are too close to price this trip.",
            field="distance",
        )

    if provided_km is not None:
        distance = _as_decimal(
            provided_km, "distance", minimum=Decimal("0.01"), code="invalid_distance"
        )
        if not enforce_bounds:
            return distance.quantize(Decimal("0.01")), CONFIDENCE_NORMAL
        ratio = distance / straight
        bounds = config.distance_bounds
        if ratio < bounds.min_ratio or ratio > bounds.max_ratio:
            raise PricingError(
                "invalid_distance",
                (
                    f"distance is {ratio:.2f}x the straight-line distance; "
                    f"allowed range is [{bounds.min_ratio}, {bounds.max_ratio}]."
                ),
                field="distance",
            )
        return distance.quantize(Decimal("0.01")), CONFIDENCE_NORMAL

    fallback = (straight * config.fallback_detour_factor).quantize(
        Decimal("0.01")
    )
    return fallback, CONFIDENCE_LOW


def resolve_duration(
    config: Configuration,
    *,
    provided_min: Any | None,
    distance_km: Decimal,
) -> tuple[int, str]:
    """Return ``(duration_min, confidence)``.

    A missing duration is derived from the distance and the placeholder
    ``fallback_speed_kmh`` and flagged as low confidence.
    """
    if provided_min is not None:
        minutes = _as_int(
            provided_min,
            "duration_min",
            minimum=1,
            maximum=MAX_DURATION_MINUTES,
            code="invalid_duration",
        )
        return minutes, CONFIDENCE_NORMAL

    raw = distance_km / config.fallback_speed_kmh * Decimal(60)
    minutes = int(raw.to_integral_value(rounding=ROUND_CEILING))
    return max(1, minutes), CONFIDENCE_LOW


# ---------------------------------------------------------------------------
# Input validation + time multiplier
# ---------------------------------------------------------------------------


def resolve_vehicle_class(
    explicit: Any | None,
    size: Any | None,
    capacity: Any | None,
) -> str:
    """Resolve the pricing class.

    Precedence (agreed in Stage 0):
      1. explicit ``vehicle_class``;
      2. ``size``: big → truck, medium → van, small → standard;
      3. ``capacity`` (tons) is only used when ``size`` is absent:
         ≤ 1.5 → standard, ≤ 3.5 → van, > 3.5 → truck.
    """
    if explicit is not None and str(explicit).strip():
        name = str(explicit).strip().lower()
        if name not in VEHICLE_CLASSES:
            raise PricingError(
                "invalid_vehicle_class",
                f"vehicle_class must be one of {', '.join(VEHICLE_CLASSES)}.",
                field="vehicle_class",
            )
        return name

    normalised_size = str(size).strip().lower() if size is not None else ""
    if normalised_size == "big":
        return "truck"
    if normalised_size == "medium":
        return "van"
    if normalised_size == "small":
        return "standard"

    if capacity is not None:
        tons = _as_decimal(capacity, "capacity", minimum=_ZERO, code="invalid_capacity")
        if tons <= Decimal("1.5"):
            return "standard"
        if tons <= Decimal("3.5"):
            return "van"
        return "truck"

    raise PricingError(
        "invalid_vehicle_class",
        "Cannot determine the vehicle class: send size, capacity or vehicle_class.",
        field="vehicle_class",
    )


def legacy_size_for_vehicle_class(vehicle_class: str) -> str:
    """Map the pricing class back to the legacy ``Mission.size`` value."""
    return {"standard": "small", "van": "medium", "truck": "big"}.get(
        vehicle_class, "small"
    )


def validate_estimate_inputs(
    config: Configuration,
    *,
    vehicle_class: str,
    distance_km: Any | None,
    duration_min: Any | None,
    helpers: int,
    floors: int,
    handling_minutes: int,
    helpers_from_legacy: bool = False,
) -> None:
    """Validate request-side inputs against the active configuration."""
    if vehicle_class not in config.vehicle_classes:
        raise PricingError(
            "invalid_vehicle_class", "Unknown vehicle class.", field="vehicle_class"
        )
    if distance_km is not None:
        _as_decimal(
            distance_km,
            "distance",
            minimum=Decimal("0.01"),
            code="invalid_distance",
        )
    if duration_min is not None:
        _as_int(
            duration_min,
            "duration_min",
            minimum=1,
            maximum=MAX_DURATION_MINUTES,
            code="invalid_duration",
        )
    if helpers < 0:
        raise PricingError("invalid_helpers", "helpers must be >= 0.", field="helpers")
    max_helpers = config.extras.max_helpers
    if helpers_from_legacy:
        # Legacy requests used ``workers`` (0–5). Keep accepting them even when
        # the new configuration caps explicit helpers lower.
        max_helpers = max(max_helpers, MAX_LEGACY_WORKERS)
    if helpers > max_helpers:
        raise PricingError(
            "invalid_helpers",
            f"helpers must be <= {max_helpers}.",
            field="helpers",
        )
    if floors < 0 or floors > config.extras.max_floors:
        raise PricingError(
            "invalid_floors",
            f"floors must be between 0 and {config.extras.max_floors}.",
            field="floors",
        )
    if handling_minutes < 0 or handling_minutes > MAX_HANDLING_MINUTES:
        raise PricingError(
            "invalid_handling_minutes",
            f"handling_minutes must be between 0 and {MAX_HANDLING_MINUTES}.",
            field="handling_minutes",
        )


def _hour_in_window(hour: int, start: int, end: int) -> bool:
    if start < end:
        return start <= hour < end
    # Window wraps midnight, e.g. [22, 5) → 22, 23, 0, 1, 2, 3, 4.
    return hour >= start or hour < end


def apply_time_multiplier(
    config: Configuration, pickup_at: datetime
) -> tuple[Decimal, tuple[str, ...]]:
    """Return ``(multiplier, labels)`` from the *pickup* time.

    Naive datetimes are interpreted in Africa/Algiers; aware datetimes are
    converted. Multipliers are multiplied together, then the caller applies
    the cap.
    """
    if pickup_at.tzinfo is None:
        local = pickup_at.replace(tzinfo=ALGIERS_TZ)
    else:
        local = pickup_at.astimezone(ALGIERS_TZ)

    multiplier = _ONE
    labels: list[str] = []

    # Friday + Saturday (Algerian weekend).
    if local.weekday() in (4, 5):
        multiplier *= config.weekend
        labels.append("weekend")

    if any(
        _hour_in_window(local.hour, start, end)
        for start, end in config.peak_hours
    ):
        multiplier *= config.peak
        labels.append("peak")

    if any(
        _hour_in_window(local.hour, start, end)
        for start, end in config.night_hours
    ):
        multiplier *= config.night
        labels.append("night")

    return multiplier, tuple(labels)


def effective_multiplier(
    config: Configuration,
    pickup_at: datetime,
    manual_multiplier: Decimal | None = None,
) -> tuple[Decimal, tuple[str, ...], bool]:
    """Combine time multipliers and an optional boss override, then cap."""
    multiplier, labels = apply_time_multiplier(config, pickup_at)
    if manual_multiplier is not None:
        multiplier *= _as_decimal(
            manual_multiplier,
            "manual_multiplier",
            minimum=Decimal("0.01"),
            code="invalid_multiplier",
        )
        labels = labels + ("manual",)

    capped = multiplier > config.multiplier_cap
    if capped:
        multiplier = config.multiplier_cap
    return multiplier, labels, capped


def make_pickup_at(pickup_date: date, pickup_time: dtime) -> datetime:
    """Build an aware Africa/Algiers datetime from the mission schedule."""
    return datetime.combine(pickup_date, pickup_time, tzinfo=ALGIERS_TZ)


# ---------------------------------------------------------------------------
# Estimation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EstimateResult:
    amount: int
    low: int
    high: int
    components: dict[str, Any]
    multiplier: Decimal
    confidence: str
    calibrated: bool
    currency: str = CURRENCY


def compute_estimate(
    config: Configuration,
    *,
    vehicle_class: str,
    distance_km: Decimal,
    duration_min: int,
    helpers: int,
    floors: int,
    handling_minutes: int,
    pickup_at: datetime,
    confidence: str = CONFIDENCE_NORMAL,
    manual_multiplier: Decimal | None = None,
) -> EstimateResult:
    """Compute a complete, JSON-ready estimate.

    Range validation of the inputs is the caller's job
    (``validate_estimate_inputs``); this function only guards against
    negative counts so it can never produce a nonsensical amount.
    """
    if vehicle_class not in config.vehicle_classes:
        raise PricingError(
            "invalid_vehicle_class", "Unknown vehicle class.", field="vehicle_class"
        )
    if confidence not in (CONFIDENCE_NORMAL, CONFIDENCE_LOW):
        raise PricingError("invalid_confidence", "Unknown confidence level.")
    if helpers < 0 or floors < 0 or handling_minutes < 0 or duration_min < 1:
        raise PricingError(
            "invalid_pricing_input", "Counts and durations must be positive."
        )

    vehicle = config.vehicle_classes[vehicle_class]
    distance_component = int(
        (Decimal(distance_km) * vehicle.per_km).quantize(
            _ONE, rounding=ROUND_HALF_UP
        )
    )
    duration_component = int(duration_min) * vehicle.per_min

    helper_component = int(
        (
            Decimal(int(helpers))
            * config.extras.helper_hourly
            * Decimal(int(handling_minutes))
            / Decimal(60)
        ).quantize(_ONE, rounding=ROUND_HALF_UP)
    )
    floor_component = int(floors) * config.extras.per_floor_fee
    extras_total = helper_component + floor_component

    raw_subtotal = (
        vehicle.base + distance_component + duration_component + extras_total
    )
    subtotal = max(raw_subtotal, vehicle.min_fare)

    multiplier, labels, capped = effective_multiplier(
        config, pickup_at, manual_multiplier
    )
    pre_round = Decimal(subtotal) * multiplier
    amount = round_to_step(pre_round, config.rounding_step)

    band = config.estimate_band
    if confidence == CONFIDENCE_LOW:
        band += config.low_confidence_extra_band

    low = max(
        vehicle.min_fare,
        floor_to_step(Decimal(amount) * (_ONE - band), config.rounding_step),
    )
    high = max(
        amount,
        ceil_to_step(Decimal(amount) * (_ONE + band), config.rounding_step),
    )

    components = {
        "vehicle_class": vehicle_class,
        "base_rate": vehicle.base,
        "per_km": vehicle.per_km,
        "per_min": vehicle.per_min,
        "min_fare": vehicle.min_fare,
        "distance_km": _decimal_str(distance_km),
        "duration_min": int(duration_min),
        "helpers_count": int(helpers),
        "floors_count": int(floors),
        "handling_minutes": int(handling_minutes),
        "helper_hourly": config.extras.helper_hourly,
        "per_floor_fee": config.extras.per_floor_fee,
        "base": vehicle.base,
        "distance": distance_component,
        "duration": duration_component,
        "helpers": helper_component,
        "floors": floor_component,
        "extras": extras_total,
        "raw_subtotal": raw_subtotal,
        "min_fare_applied": raw_subtotal < vehicle.min_fare,
        "subtotal": subtotal,
        "pre_round": _decimal_str(pre_round),
        "rounding_step": config.rounding_step,
        "multiplier": _decimal_str(multiplier),
        "multiplier_labels": list(labels),
        "multiplier_capped": capped,
        "band": _decimal_str(band),
    }

    return EstimateResult(
        amount=amount,
        low=low,
        high=high,
        components=components,
        multiplier=multiplier,
        confidence=confidence,
        calibrated=config.calibrated,
    )


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def _decimal_str(value: Any) -> str:
    """Stable decimal string without float artefacts or trailing zeros."""
    number = _as_decimal(value, "value")
    text = format(number.normalize(), "f")
    return "0" if text in {"-0", ""} else text


def to_decimal_string(value: Any) -> str:
    """Public alias used by the pricing services / API layer."""
    return _decimal_str(value)
