"""Pure price-negotiation rules.

This module deliberately has **no Django imports and no ORM access**. It
contains only deterministic helpers used by :mod:`missions.negotiation_services`
and by the tests, so the negotiation policy can be reasoned about (and pinned
in time) without a database.

Rules
-----
* offers are integers in DZD;
* an offer must be a multiple of ``min_step``;
* an offer must stay inside ``[ceil(0.85·E), floor(1.15·E)]``, rounded to
  ``min_step``, where ``E`` is the mission price when the negotiation opened;
* an offer must not fall below ``min(config class min_fare, E)``;
* the client may only lower the previous amount and the driver may only raise
  it, by at least ``min_step``;
* the driver moves first and turns alternate driver → client → driver.

The values come from the versioned pricing configuration, which is still a
placeholder (``calibrated=false``).
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from .pricing import ceil_to_step, floor_to_step

ROLE_CLIENT = "client"
ROLE_DRIVER = "driver"

STATUS_OPEN = "open"
STATUS_ACCEPTED = "accepted"
STATUS_DECLINED = "declined"
STATUS_EXPIRED = "expired"
STATUS_CLOSED = "closed"

MAX_AMOUNT = 2_147_483_647  # PositiveIntegerField upper bound (SQLite/Postgres)
MAX_IDEMPOTENCY_KEY_LENGTH = 128


class NegotiationError(ValueError):
    """A negotiation failure carrying a stable machine-readable code."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        field: str | None = None,
        http_status: int = 400,
        extra: dict[str, Any] | None = None,
    ):
        self.code = code
        self.message = message
        self.field = field
        self.http_status = http_status
        self.extra = extra or {}
        super().__init__(message)


# ---------------------------------------------------------------------------
# Input parsing
# ---------------------------------------------------------------------------


def parse_amount(value: Any) -> int:
    """Parse an offer amount, accepting only integral DZD values.

    JSON numbers arrive as ``int``/``float``; form-encoded payloads arrive as
    strings. ``Decimal`` is accepted for direct service callers. Booleans are
    explicitly rejected (``True`` is an ``int`` subclass in Python).
    """
    if isinstance(value, bool) or value is None:
        raise NegotiationError(
            "invalid_offer_amount",
            "Offer amount must be an integer in DZD.",
            field="amount",
        )

    if isinstance(value, int):
        amount = value
    else:
        if isinstance(value, float):
            raw = str(value)
        elif isinstance(value, Decimal):
            raw = str(value)
        elif isinstance(value, str):
            raw = value.strip()
            if not raw:
                raise NegotiationError(
                    "invalid_offer_amount",
                    "Offer amount must be an integer in DZD.",
                    field="amount",
                )
        else:
            raise NegotiationError(
                "invalid_offer_amount",
                "Offer amount must be an integer in DZD.",
                field="amount",
            )

        try:
            decimal = Decimal(raw)
        except (InvalidOperation, ValueError):
            raise NegotiationError(
                "invalid_offer_amount",
                "Offer amount must be an integer in DZD.",
                field="amount",
            ) from None

        if not decimal.is_finite() or decimal != decimal.to_integral_value():
            raise NegotiationError(
                "invalid_offer_amount",
                "Offer amount must be a whole number of DZD.",
                field="amount",
            )
        amount = int(decimal)

    if amount <= 0 or amount > MAX_AMOUNT:
        raise NegotiationError(
            "invalid_offer_amount",
            "Offer amount must be a positive integer in DZD.",
            field="amount",
        )
    return amount


def parse_idempotency_key(value: Any) -> str:
    """Validate the opaque idempotency key supplied with an offer."""
    if not isinstance(value, str) or not value.strip():
        raise NegotiationError(
            "invalid_idempotency_key",
            "idempotency_key is required.",
            field="idempotency_key",
        )
    if len(value) > MAX_IDEMPOTENCY_KEY_LENGTH:
        raise NegotiationError(
            "invalid_idempotency_key",
            f"idempotency_key must be at most {MAX_IDEMPOTENCY_KEY_LENGTH} characters.",
            field="idempotency_key",
        )
    return value


# ---------------------------------------------------------------------------
# Bounds
# ---------------------------------------------------------------------------


def allowed_range(
    initial_price: int, tolerance: Decimal, min_step: int
) -> tuple[int, int]:
    """Return the inclusive ``(low, high)`` offer range for a mission price."""
    if initial_price <= 0:
        raise NegotiationError(
            "mission_not_negotiable",
            "This mission has no negotiable price.",
            http_status=409,
        )
    if min_step <= 0:
        raise NegotiationError(
            "invalid_negotiation_config",
            "min_step must be greater than zero.",
            http_status=500,
        )
    base = Decimal(int(initial_price))
    low = ceil_to_step(base * (Decimal("1") - tolerance), min_step)
    high = floor_to_step(base * (Decimal("1") + tolerance), min_step)
    return low, high


def effective_min_fare(initial_price: int, class_min_fare: int) -> int:
    """Never demand more than the mission's own price as a minimum fare."""
    return min(int(class_min_fare), int(initial_price))


def expected_role(last_author_role: str | None) -> str:
    """The driver moves first; then roles alternate."""
    if last_author_role == ROLE_DRIVER:
        return ROLE_CLIENT
    return ROLE_DRIVER


def validate_offer_amount(
    amount: int,
    *,
    initial_price: int,
    tolerance: Decimal,
    min_step: int,
    min_fare: int,
    last_amount: int | None = None,
    last_author_role: str | None = None,
    requester_role: str,
) -> tuple[int, int]:
    """Validate an offer against the frozen negotiation rules.

    Returns the inclusive ``(low, high)`` bounds for error messages. Raises
    :class:`NegotiationError` with a stable code on the first failure.
    """
    low, high = allowed_range(initial_price, tolerance, min_step)

    if amount % min_step != 0:
        raise NegotiationError(
            "offer_not_aligned",
            f"Offer must be a multiple of {min_step} DZD.",
            field="amount",
        )

    if amount < low or amount > high:
        raise NegotiationError(
            "offer_out_of_range",
            f"Offer must be between {low} and {high} DZD.",
            field="amount",
            extra={"min_amount": low, "max_amount": high},
        )

    if amount < min_fare:
        raise NegotiationError(
            "offer_below_min_fare",
            f"Offer must be at least {min_fare} DZD.",
            field="amount",
            extra={"min_fare": min_fare},
        )

    if last_amount is not None:
        if requester_role == ROLE_CLIENT:
            if amount >= last_amount or last_amount - amount < min_step:
                raise NegotiationError(
                    "offer_not_monotonic",
                    f"A client counter-offer must be at least {min_step} DZD "
                    "below the previous offer.",
                    field="amount",
                )
        else:
            if amount <= last_amount or amount - last_amount < min_step:
                raise NegotiationError(
                    "offer_not_monotonic",
                    f"A driver counter-offer must be at least {min_step} DZD "
                    "above the previous offer.",
                    field="amount",
                )

    return low, high


# ---------------------------------------------------------------------------
# Time rules
# ---------------------------------------------------------------------------


def is_expired(*, expires_at, hold_expires_at, now) -> bool:
    """True when the total TTL or the current turn's hold has elapsed."""
    if expires_at <= now:
        return True
    if hold_expires_at is not None and hold_expires_at <= now:
        return True
    return False


def next_hold_deadline(now, *, offer_ttl_minutes: int, expires_at):
    """The earlier of ``now + offer_ttl`` and the absolute deadline."""
    from datetime import timedelta

    return min(now + timedelta(minutes=int(offer_ttl_minutes)), expires_at)
