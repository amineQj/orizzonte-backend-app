"""ORM-aware orchestration for the pricing engine.

The pure maths live in :mod:`missions.pricing`; this module loads the active
configuration, persists quotes, applies boss overrides and bridges the API
serializers to the ``Mission`` model.
"""
from __future__ import annotations

import json
from datetime import date, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from django.db import transaction
from django.utils import timezone

from .models import (
    Mission,
    PricingAuditLog,
    PricingConfig,
    PricingOverride,
    Quote,
)
from .pricing import (
    ALGIERS_TZ,
    CONFIDENCE_LOW,
    CURRENCY,
    QUOTE_TTL_MINUTES,
    Configuration,
    PricingError,
    commission_split,
    compute_estimate,
    legacy_size_for_vehicle_class,
    make_pickup_at,
    resolve_distance,
    resolve_duration,
    resolve_vehicle_class,
    to_decimal_string,
    validate_estimate_inputs,
)
from .services import create_mission

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "data" / "pricing_v1.json"

DEFAULT_CONFIG_NOTE = (
    "Placeholder parameters seeded from missions/data/pricing_v1.json. "
    "Not market-calibrated: calibrated must stay false until the boss fits "
    "them to real quotes."
)


# ---------------------------------------------------------------------------
# Configuration access
# ---------------------------------------------------------------------------


def load_default_parameters() -> dict[str, Any]:
    """Read the bundled placeholder configuration."""
    with DEFAULT_CONFIG_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


def config_from_model(config_model: PricingConfig) -> Configuration:
    """Parse a database configuration, keeping its version authoritative."""
    return Configuration.from_mapping(
        {**config_model.parameters, "version": config_model.version}
    )


def seed_default_config(
    *,
    version: int | None = None,
    parameters: dict[str, Any] | None = None,
    activate: bool = True,
    force: bool = False,
) -> PricingConfig:
    """Create (or optionally refresh) a configuration from the bundled JSON."""
    data = parameters if parameters is not None else load_default_parameters()
    config_version = int(version or data.get("version", 1))
    data = {**data, "version": config_version}

    # Validate before touching the database.
    Configuration.from_mapping(data)

    with transaction.atomic():
        existing = PricingConfig.objects.filter(version=config_version).first()
        if existing is not None and not force:
            config_model = existing
        else:
            config_model, _created = PricingConfig.objects.update_or_create(
                version=config_version,
                defaults={
                    "parameters": data,
                    "calibrated": bool(data.get("calibrated", False)),
                    "notes": DEFAULT_CONFIG_NOTE,
                },
            )
        if activate:
            config_model.is_active = True
            config_model.save()

    return config_model


def get_active_config() -> PricingConfig:
    """Return the active configuration, seeding placeholders on a fresh DB."""
    config_model = (
        PricingConfig.objects.filter(is_active=True).order_by("-version").first()
    )
    if config_model is not None:
        return config_model

    config_model = PricingConfig.objects.order_by("-version").first()
    if config_model is not None:
        return config_model

    return seed_default_config(activate=True)


def public_rules(config_model: PricingConfig) -> dict[str, Any]:
    """Public tariff/negotiation rules. Never exposes the commission rate."""
    config = config_from_model(config_model)
    return {
        "currency": CURRENCY,
        "config_version": config.version,
        "calibrated": config.calibrated,
        "rounding_step": config.rounding_step,
        "estimate_band": to_decimal_string(config.estimate_band),
        "low_confidence_extra_band": to_decimal_string(
            config.low_confidence_extra_band
        ),
        "vehicle_classes": {
            name: {
                "base": vehicle.base,
                "per_km": vehicle.per_km,
                "per_min": vehicle.per_min,
                "min_fare": vehicle.min_fare,
            }
            for name, vehicle in config.vehicle_classes.items()
        },
        "extras": {
            "helper_hourly": config.extras.helper_hourly,
            "per_floor_fee": config.extras.per_floor_fee,
            "max_helpers": config.extras.max_helpers,
            "max_floors": config.extras.max_floors,
        },
        "time_multipliers": {
            "peak": to_decimal_string(config.peak),
            "night": to_decimal_string(config.night),
            "weekend": to_decimal_string(config.weekend),
        },
        "peak_hours": [list(window) for window in config.peak_hours],
        "night_hours": [list(window) for window in config.night_hours],
        "multiplier_cap": to_decimal_string(config.multiplier_cap),
        "distance_bounds": {
            "min_ratio_to_haversine": to_decimal_string(
                config.distance_bounds.min_ratio
            ),
            "max_ratio_to_haversine": to_decimal_string(
                config.distance_bounds.max_ratio
            ),
        },
        "fallback_detour_factor": to_decimal_string(
            config.fallback_detour_factor
        ),
        "fallback_speed_kmh": to_decimal_string(config.fallback_speed_kmh),
        "negotiation": {
            "tolerance": to_decimal_string(config.negotiation.tolerance),
            "max_offers": config.negotiation.max_offers,
            "min_step": config.negotiation.min_step,
            "offer_ttl_minutes": config.negotiation.offer_ttl_minutes,
            "total_ttl_minutes": config.negotiation.total_ttl_minutes,
        },
        "quote_ttl_minutes": QUOTE_TTL_MINUTES,
    }


# ---------------------------------------------------------------------------
# Overrides and audit
# ---------------------------------------------------------------------------


def resolve_manual_override(
    *,
    client,
    mission: Mission | None = None,
    now=None,
) -> PricingOverride | None:
    """Return the unexpired boss override that applies, if any.

    A mission-specific override wins over a client-level one; quotes and
    estimates use the client-level override.
    """
    now = now or timezone.now()
    base = PricingOverride.objects.filter(expires_at__gt=now)

    if mission is not None:
        specific = (
            base.filter(mission=mission).order_by("-created_at").first()
        )
        if specific is not None:
            return specific
        return (
            base.filter(client=mission.client, mission__isnull=True)
            .order_by("-created_at")
            .first()
        )

    return (
        base.filter(client=client, mission__isnull=True)
        .order_by("-created_at")
        .first()
    )


def record_pricing_audit(
    *,
    actor,
    action: str,
    mission: Mission | None = None,
    quote: Quote | None = None,
    override: PricingOverride | None = None,
    payload: dict[str, Any] | None = None,
) -> PricingAuditLog:
    """Append an immutable pricing audit row (never store PII or GPS)."""
    return PricingAuditLog.objects.create(
        actor=actor,
        action=action,
        mission=mission,
        quote=quote,
        override=override,
        payload=payload or {},
    )


# ---------------------------------------------------------------------------
# Estimation
# ---------------------------------------------------------------------------


def _pickup_at(data: dict[str, Any], now):
    pickup_date = data.get("date")
    pickup_time = data.get("time")
    if pickup_date is not None and pickup_time is not None:
        return make_pickup_at(pickup_date, pickup_time)
    if pickup_date is not None:
        return make_pickup_at(pickup_date, time(0, 0))
    return now.astimezone(ALGIERS_TZ)


def _resolve_inputs(
    config: Configuration,
    data: dict[str, Any],
    *,
    enforce_bounds: bool,
) -> dict[str, Any]:
    """Resolve vehicle class, distance and duration for pricing."""
    explicit_helpers = data.get("helpers") is not None
    helpers = int(data["helpers"]) if explicit_helpers else int(data.get("workers") or 0)
    floors = int(data.get("floors") or 0)

    handling = data.get("handling_minutes")
    if handling is None:
        # Agreed Stage 0 default: one hour of handling when helpers are used.
        handling = 60 if helpers > 0 else 0
    handling = int(handling)

    vehicle_class = resolve_vehicle_class(
        data.get("vehicle_class"), data.get("size"), data.get("capacity")
    )

    validate_estimate_inputs(
        config,
        vehicle_class=vehicle_class,
        distance_km=data.get("distance"),
        duration_min=data.get("duration_min"),
        helpers=helpers,
        floors=floors,
        handling_minutes=handling,
        helpers_from_legacy=not explicit_helpers,
    )

    distance_km, distance_confidence = resolve_distance(
        config,
        provided_km=data.get("distance"),
        departure_lat=data["departure_latitude"],
        departure_lon=data["departure_longitude"],
        arrival_lat=data["arrival_latitude"],
        arrival_lon=data["arrival_longitude"],
        enforce_bounds=enforce_bounds,
    )
    duration_min, duration_confidence = resolve_duration(
        config, provided_min=data.get("duration_min"), distance_km=distance_km
    )

    confidence = (
        CONFIDENCE_LOW
        if CONFIDENCE_LOW in (distance_confidence, duration_confidence)
        else "normal"
    )

    return {
        "vehicle_class": vehicle_class,
        "distance_km": distance_km,
        "duration_min": duration_min,
        "helpers": helpers,
        "floors": floors,
        "handling_minutes": handling,
        "confidence": confidence,
    }


def _estimate_payload(
    result, *, quote: Quote | None, config: Configuration
) -> dict[str, Any]:
    return {
        "amount": result.amount,
        "low": result.low,
        "high": result.high,
        "currency": result.currency,
        "components": result.components,
        "multiplier": to_decimal_string(result.multiplier),
        "confidence": result.confidence,
        "calibrated": result.calibrated,
        "config_version": config.version,
        "quote_id": str(quote.public_id) if quote is not None else None,
        "expires_at": quote.expires_at.isoformat() if quote is not None else None,
    }


def build_estimate(
    validated_data: dict[str, Any],
    *,
    client,
    persist_quote: bool,
    enforce_bounds: bool = True,
    audit_override: bool = True,
    now=None,
) -> tuple[dict[str, Any], Quote | None, dict[str, Any]]:
    """Compute an estimate and, when opted in, persist a confirmable quote."""
    now = now or timezone.now()
    config_model = get_active_config()
    config = config_from_model(config_model)
    resolved = _resolve_inputs(config, validated_data, enforce_bounds=enforce_bounds)

    override = resolve_manual_override(client=client, now=now)
    result = compute_estimate(
        config,
        pickup_at=_pickup_at(validated_data, now),
        manual_multiplier=override.multiplier if override is not None else None,
        **{
            key: resolved[key]
            for key in (
                "vehicle_class",
                "distance_km",
                "duration_min",
                "helpers",
                "floors",
                "handling_minutes",
                "confidence",
            )
        },
    )

    quote = None
    if persist_quote:
        pickup_at = _pickup_at(validated_data, now)
        quote = Quote.objects.create(
            client=client,
            config=config_model,
            config_version=config.version,
            inputs={
                "vehicle_class": resolved["vehicle_class"],
                "distance_km": to_decimal_string(resolved["distance_km"]),
                "duration_min": resolved["duration_min"],
                "helpers": resolved["helpers"],
                "floors": resolved["floors"],
                "handling_minutes": resolved["handling_minutes"],
                "pickup_date": pickup_at.date().isoformat(),
                "pickup_time": pickup_at.time().isoformat(),
                "confidence": resolved["confidence"],
                "distance_provided": validated_data.get("distance") is not None,
                "duration_provided": validated_data.get("duration_min") is not None,
            },
            components=result.components,
            amount=result.amount,
            low=result.low,
            high=result.high,
            currency=result.currency,
            confidence=result.confidence,
            calibrated=result.calibrated,
            expires_at=now + timedelta(minutes=QUOTE_TTL_MINUTES),
        )

    if override is not None and audit_override:
        record_pricing_audit(
            actor=None,
            action="override_applied",
            quote=quote,
            override=override,
            payload={
                "multiplier": to_decimal_string(override.multiplier),
                "config_version": config.version,
                "source": "estimate",
            },
        )

    resolved["manual_override"] = override
    # Reused by the quote-less confirmation path so the config cannot change
    # between the estimate and the mission snapshot.
    resolved["config_model"] = config_model
    resolved["config"] = config
    return _estimate_payload(result, quote=quote, config=config), quote, resolved


# ---------------------------------------------------------------------------
# Confirmation
# ---------------------------------------------------------------------------


def _quote_mismatch(field: str) -> PricingError:
    return PricingError(
        "quote_mismatch",
        f"{field} does not match the price quote.",
        field=field,
    )


def _merge_quote_inputs(
    data: dict[str, Any], quote: Quote, config: Configuration
) -> dict[str, Any]:
    """Fill missing confirmation fields from the quote and reject conflicts."""
    merged = dict(data)
    inputs = quote.inputs

    quote_distance = Decimal(str(inputs["distance_km"]))
    if merged.get("distance") is None:
        merged["distance"] = quote_distance
    elif Decimal(str(merged["distance"])) != quote_distance:
        raise _quote_mismatch("distance")

    quote_date = date.fromisoformat(inputs["pickup_date"])
    quote_time = time.fromisoformat(inputs["pickup_time"])
    if merged.get("date") is None:
        merged["date"] = quote_date
    elif merged["date"] != quote_date:
        raise _quote_mismatch("date")
    if merged.get("time") is None:
        merged["time"] = quote_time
    elif merged["time"] != quote_time:
        raise _quote_mismatch("time")

    for field in ("duration_min", "helpers", "floors", "handling_minutes"):
        quote_value = int(inputs[field])
        if merged.get(field) is None:
            merged[field] = quote_value
        elif int(merged[field]) != quote_value:
            raise _quote_mismatch(field)

    quote_class = str(inputs["vehicle_class"])
    if merged.get("vehicle_class") is None:
        merged["vehicle_class"] = quote_class
    elif str(merged["vehicle_class"]) != quote_class:
        raise _quote_mismatch("vehicle_class")

    if not merged.get("size"):
        merged["size"] = legacy_size_for_vehicle_class(quote_class)

    # Keep the legacy column meaningful for the new pricing flow.
    merged["workers"] = int(inputs["helpers"])
    merged["pricing_new_flow"] = True

    # Re-validate the submitted route against the quote's distance. Nothing
    # GPS-related is stored on the quote.
    resolve_distance(
        config,
        provided_km=quote_distance,
        departure_lat=merged["departure_latitude"],
        departure_lon=merged["departure_longitude"],
        arrival_lat=merged["arrival_latitude"],
        arrival_lon=merged["arrival_longitude"],
        enforce_bounds=True,
    )
    return merged


def confirm_with_quote(client, data: dict[str, Any]) -> Mission:
    """Consume a quote and create the mission at the quoted price (atomic)."""
    quote_id = data["quote_id"]

    with transaction.atomic():
        try:
            quote = (
                Quote.objects.select_for_update()
                .select_related("config")
                .get(public_id=quote_id)
            )
        except Quote.DoesNotExist:
            raise PricingError(
                "quote_not_found", "Quote not found.", field="quote_id"
            ) from None

        if quote.client_id != client.pk:
            # Do not leak the existence of another client's quote.
            raise PricingError(
                "quote_not_found", "Quote not found.", field="quote_id"
            )

        now = timezone.now()
        if quote.used_at is not None:
            raise PricingError(
                "quote_already_used",
                "This quote has already been used.",
                field="quote_id",
            )
        if quote.expires_at <= now:
            raise PricingError(
                "quote_expired", "This quote has expired.", field="quote_id"
            )

        config = config_from_model(quote.config)
        merged = _merge_quote_inputs(data, quote, config)
        commission, driver_net = commission_split(
            quote.amount, config.commission_rate
        )

        mission = create_mission(
            client,
            merged,
            price_override=quote.amount,
            pricing_meta={
                "quote": quote,
                "negotiable": bool(data.get("negotiable", False)),
                "pricing_config_version": quote.config_version,
                "price_components": quote.components,
                "commission_rate": config.commission_rate,
                "commission_amount": commission,
                "driver_net": driver_net,
                "price_locked_at": now,
            },
        )

        quote.used_at = now
        quote.mission = mission
        quote.save(update_fields=["used_at", "mission"])

        record_pricing_audit(
            actor=client,
            action="quote_confirmed",
            mission=mission,
            quote=quote,
            payload={
                "amount": quote.amount,
                "config_version": quote.config_version,
                "confidence": quote.confidence,
            },
        )

    return mission


def confirm_new_without_quote(client, data: dict[str, Any]) -> Mission:
    """Confirm on the new engine without a pre-issued quote."""
    estimate, _quote, resolved = build_estimate(
        data,
        client=client,
        persist_quote=False,
        enforce_bounds=True,
        audit_override=False,
    )
    config = resolved["config"]
    amount = int(estimate["amount"])
    commission, driver_net = commission_split(amount, config.commission_rate)

    merged = dict(data)
    if not merged.get("size"):
        merged["size"] = legacy_size_for_vehicle_class(resolved["vehicle_class"])
    merged["workers"] = int(resolved["helpers"])

    now = timezone.now()
    mission = create_mission(
        client,
        merged,
        price_override=amount,
        pricing_meta={
            "negotiable": bool(data.get("negotiable", False)),
            "pricing_config_version": config.version,
            "price_components": estimate["components"],
            "commission_rate": config.commission_rate,
            "commission_amount": commission,
            "driver_net": driver_net,
            "price_locked_at": now,
        },
    )

    record_pricing_audit(
        actor=client,
        action="price_locked",
        mission=mission,
        payload={
            "amount": amount,
            "config_version": config.version,
            "confidence": estimate["confidence"],
            "source": "confirm",
        },
    )
    override = resolved.get("manual_override")
    if override is not None:
        record_pricing_audit(
            actor=None,
            action="override_applied",
            mission=mission,
            override=override,
            payload={
                "multiplier": to_decimal_string(override.multiplier),
                "config_version": config.version,
                "source": "confirm",
            },
        )

    return mission


def create_mission_from_request(client, data: dict[str, Any]) -> Mission:
    """Route a confirmed request to the legacy or the new pricing path."""
    if data.get("quote_id"):
        return confirm_with_quote(client, data)
    if data.get("pricing_new_flow"):
        return confirm_new_without_quote(client, data)
    return create_mission(client, data)
