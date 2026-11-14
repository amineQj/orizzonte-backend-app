"""ORM orchestration for price negotiation.

The pure policy lives in :mod:`missions.negotiation`; this module loads and
locks rows, snapshots the pricing configuration when a negotiation opens, and
delegates the final assignment to the existing :func:`missions.services.assign_mission`
logic.

Lock order is always **mission → negotiation**. No personal data is written to
negotiation rows or to the audit payload (amounts, sequence numbers, internal
IDs and configuration versions only).
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from drivers.models import DriverProfile

from .models import Mission, MissionStatus, Negotiation, NegotiationOffer
from .negotiation import (
    ROLE_DRIVER,
    STATUS_ACCEPTED,
    STATUS_CLOSED,
    STATUS_DECLINED,
    STATUS_EXPIRED,
    STATUS_OPEN,
    NegotiationError,
    allowed_range,
    effective_min_fare,
    expected_role,
    is_expired,
    next_hold_deadline,
    parse_amount,
    parse_idempotency_key,
    validate_offer_amount,
)
from .pricing import commission_split, resolve_vehicle_class
from .pricing_services import config_from_model, get_active_config, record_pricing_audit
from .services import (
    DriverNotApprovedError,
    MissionAlreadyAcceptedError,
    assign_mission,
)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _mission_not_found() -> NegotiationError:
    return NegotiationError("mission_not_found", "Mission not found.", http_status=404)


def _negotiation_not_found() -> NegotiationError:
    # Deliberately the same response for "no negotiation" and "not a
    # participant" so the endpoint never leaks another user's negotiation.
    return NegotiationError(
        "negotiation_not_found", "Negotiation not found.", http_status=404
    )


def _status_error(negotiation: Negotiation) -> NegotiationError:
    if negotiation.status == STATUS_EXPIRED:
        return NegotiationError(
            "negotiation_expired",
            "This negotiation has expired.",
            http_status=409,
        )
    return NegotiationError(
        "negotiation_closed", "This negotiation is closed.", http_status=409
    )


def _require_participant(negotiation: Negotiation, requester) -> str:
    role = negotiation.requester_role(requester)
    if role is None:
        raise _negotiation_not_found()
    return role


def _require_approved_driver(requester) -> None:
    profile = getattr(requester, "driver_profile", None)
    status = profile.status if profile is not None else DriverProfile.Status.INCOMPLETE
    if status != DriverProfile.Status.APPROVED:
        raise NegotiationError(
            "driver_not_approved",
            "Your driver profile must be approved before you can negotiate.",
            http_status=403,
            extra={"profile_status": status},
        )


def _latest(mission: Mission, *, for_update: bool = False) -> Negotiation | None:
    # No select_related("mission"): the serializers read the *current* hold
    # fields, which may have just been cleared on a different mission object.
    qs = Negotiation.objects.filter(mission=mission)
    if for_update:
        qs = qs.select_for_update()
    return qs.order_by("-created_at").first()


def _audit_payload(negotiation: Negotiation, **extra: Any) -> dict[str, Any]:
    payload = {
        "negotiation_id": negotiation.pk,
        "initial_price": negotiation.initial_price,
        "config_version": negotiation.config_version,
    }
    payload.update(extra)
    return payload


def _clear_hold(mission: Mission, driver_id: int | None = None) -> None:
    if mission.held_by_id is None:
        return
    if driver_id is not None and mission.held_by_id != driver_id:
        return
    mission.held_by = None
    mission.hold_expires_at = None
    mission.save(update_fields=["held_by", "hold_expires_at", "updated_at"])


def _expire_if_needed(negotiation: Negotiation, mission: Mission, now) -> bool:
    """Lazily expire an open negotiation whose TTL/hold elapsed."""
    if negotiation.status != STATUS_OPEN:
        return False

    hold = (
        mission.hold_expires_at
        if mission.held_by_id == negotiation.driver_id
        else None
    )
    if not is_expired(
        expires_at=negotiation.expires_at,
        hold_expires_at=hold,
        now=now,
    ):
        return False

    negotiation.status = STATUS_EXPIRED
    negotiation.closed_at = now
    negotiation.save(update_fields=["status", "closed_at", "updated_at"])
    _clear_hold(mission, driver_id=negotiation.driver_id)
    record_pricing_audit(
        actor=None,
        action="negotiation_expired",
        mission=mission,
        payload=_audit_payload(
            negotiation,
            offer_count=negotiation.offers.count(),
            reason="ttl",
        ),
    )
    return True


def _close_open_negotiation(
    negotiation: Negotiation,
    mission: Mission,
    now,
    *,
    reason: str,
    actor=None,
    status: str = STATUS_CLOSED,
    action: str = "negotiation_closed",
    clear_hold: bool = True,
) -> None:
    negotiation.status = status
    negotiation.closed_at = now
    negotiation.save(update_fields=["status", "closed_at", "updated_at"])
    if clear_hold:
        _clear_hold(mission, driver_id=negotiation.driver_id)
    record_pricing_audit(
        actor=actor,
        action=action,
        mission=mission,
        payload=_audit_payload(
            negotiation,
            offer_count=negotiation.offers.count(),
            reason=reason,
        ),
    )


def close_open_negotiations(
    mission: Mission,
    *,
    reason: str,
    actor=None,
    now=None,
    clear_hold: bool = True,
) -> int:
    """Close every open negotiation for *mission* (direct accept/assign/cancel).

    Callers already hold the mission row lock inside a transaction.
    """
    now = now or timezone.now()
    closed = 0
    open_rows = (
        Negotiation.objects.select_for_update()
        .filter(mission=mission, status=STATUS_OPEN)
        .order_by("-created_at")
    )
    for negotiation in open_rows:
        _close_open_negotiation(
            negotiation,
            mission,
            now,
            reason=reason,
            actor=actor,
            clear_hold=False,
        )
        closed += 1
    if clear_hold:
        _clear_hold(mission)
    return closed


# ---------------------------------------------------------------------------
# Availability / configuration snapshot
# ---------------------------------------------------------------------------


def _snapshot_config(mission: Mission) -> dict[str, Any]:
    config_model = get_active_config()
    config = config_from_model(config_model)
    vehicle_class = resolve_vehicle_class(None, mission.size, mission.capacity)
    class_config = config.vehicle_classes[vehicle_class]
    initial_price = int(mission.price)
    min_fare = effective_min_fare(initial_price, class_config.min_fare)
    low, high = allowed_range(
        initial_price, config.negotiation.tolerance, config.negotiation.min_step
    )
    return {
        "config_model": config_model,
        "config": config,
        "vehicle_class": vehicle_class,
        "initial_price": initial_price,
        "min_fare": min_fare,
        "min_amount": low,
        "max_amount": high,
    }


def _assert_mission_available(mission: Mission, requester, now) -> None:
    if mission.status != MissionStatus.PENDING or not mission.negotiable:
        raise NegotiationError(
            "mission_not_negotiable",
            "This mission is not open to negotiation.",
            http_status=409,
        )

    if (
        mission.held_by_id is not None
        and mission.hold_expires_at is not None
        and mission.hold_expires_at > now
        and mission.held_by_id != requester.pk
    ):
        raise NegotiationError(
            "mission_held",
            "Another driver is currently negotiating this mission.",
            http_status=409,
            extra={"hold_expires_at": mission.hold_expires_at.isoformat()},
        )

    # A hold without an open negotiation is stale: release it.
    if mission.held_by_id is not None and (
        mission.hold_expires_at is None or mission.hold_expires_at <= now
    ):
        _clear_hold(mission)


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


def _prepare_mission(mission_id: int, now) -> None:
    """Commit lazy expiry / mission-state closure before the main operation.

    Doing this in its own short transaction matters: the main operation may
    raise a 409 (e.g. ``negotiation_expired``), and that rollback must not
    undo the expiry write or the hold release.
    """
    with transaction.atomic():
        try:
            mission = Mission.objects.select_for_update().get(pk=mission_id)
        except Mission.DoesNotExist:
            return
        negotiation = _latest(mission, for_update=True)
        if negotiation is None or negotiation.status != STATUS_OPEN:
            return
        if mission.status != MissionStatus.PENDING:
            _close_open_negotiation(
                negotiation,
                mission,
                now,
                reason="mission_not_pending",
                actor=None,
            )
            return
        _expire_if_needed(negotiation, mission, now)


def _cleanup_after_error(mission_id: int, now) -> None:
    """Best-effort consistency pass after a rolled-back negotiation request."""
    try:
        _prepare_mission(mission_id, now)
    except Exception:  # pragma: no cover - never mask the original error
        pass


def get_negotiation(mission_id: int, requester, *, now=None) -> Negotiation:
    """Return the latest negotiation visible to *requester*.

    Only the mission client and the latest negotiation's driver may read it;
    everybody else gets the same 404 as a mission without a negotiation.
    """
    now = now or timezone.now()
    try:
        mission = Mission.objects.get(pk=mission_id)
    except Mission.DoesNotExist:
        raise _mission_not_found() from None

    negotiation = _latest(mission)
    if negotiation is None:
        raise _negotiation_not_found()
    _require_participant(negotiation, requester)

    if negotiation.status == STATUS_OPEN:
        with transaction.atomic():
            try:
                locked_mission = Mission.objects.select_for_update().get(pk=mission_id)
                locked = (
                    Negotiation.objects.select_for_update()
                    .filter(mission=locked_mission)
                    .order_by("-created_at")
                    .first()
                )
            except Mission.DoesNotExist:
                raise _mission_not_found() from None
            if locked is not None:
                _require_participant(locked, requester)
                if locked.status == STATUS_OPEN:
                    _expire_if_needed(locked, locked_mission, now)
                negotiation = locked
    return negotiation


# ---------------------------------------------------------------------------
# Offers
# ---------------------------------------------------------------------------


def _new_negotiation(
    mission: Mission,
    requester,
    *,
    amount: int,
    idempotency_key: str,
    snapshot: dict[str, Any],
    now,
) -> tuple[Negotiation, bool]:
    config = snapshot["config"]
    negotiation_config = config.negotiation
    negotiation = Negotiation.objects.create(
        mission=mission,
        client=mission.client,
        driver=requester,
        status=STATUS_OPEN,
        config_version=config.version,
        calibrated=config.calibrated,
        initial_price=snapshot["initial_price"],
        min_amount=snapshot["min_amount"],
        max_amount=snapshot["max_amount"],
        min_fare=snapshot["min_fare"],
        tolerance=negotiation_config.tolerance,
        max_offers=negotiation_config.max_offers,
        min_step=negotiation_config.min_step,
        offer_ttl_minutes=negotiation_config.offer_ttl_minutes,
        total_ttl_minutes=negotiation_config.total_ttl_minutes,
        commission_rate=config.commission_rate,
        expires_at=now
        + timedelta(minutes=int(negotiation_config.total_ttl_minutes)),
    )

    offer = NegotiationOffer.objects.create(
        negotiation=negotiation,
        author=requester,
        author_role=ROLE_DRIVER,
        sequence=1,
        amount=amount,
        is_final=1 >= negotiation.max_offers,
        idempotency_key=idempotency_key,
    )

    mission.held_by = requester
    mission.hold_expires_at = next_hold_deadline(
        now,
        offer_ttl_minutes=negotiation.offer_ttl_minutes,
        expires_at=negotiation.expires_at,
    )
    mission.save(update_fields=["held_by", "hold_expires_at", "updated_at"])

    record_pricing_audit(
        actor=requester,
        action="negotiation_opened",
        mission=mission,
        payload=_audit_payload(
            negotiation,
            offer_id=offer.pk,
            amount=amount,
            sequence=offer.sequence,
            min_amount=negotiation.min_amount,
            max_amount=negotiation.max_amount,
            min_fare=negotiation.min_fare,
        ),
    )
    return negotiation, True


def _counter_offer(
    mission: Mission,
    negotiation: Negotiation,
    requester,
    role: str,
    data: dict[str, Any],
    idempotency_key: str,
    now,
) -> Negotiation:
    if mission.status != MissionStatus.PENDING:
        _close_open_negotiation(
            negotiation,
            mission,
            now,
            reason="mission_not_pending",
            actor=requester,
        )
        raise _status_error(negotiation)

    # Idempotent replay is checked before the turn so a retry of an offer that
    # the other side already answered still returns the stored result.
    existing = negotiation.offers.filter(idempotency_key=idempotency_key).first()
    if existing is not None:
        amount = parse_amount(data.get("amount"))
        if amount == existing.amount:
            return negotiation
        raise NegotiationError(
            "idempotency_conflict",
            "This idempotency_key was already used with a different amount.",
            http_status=409,
        )

    last = negotiation.offers.order_by("-sequence").first()
    if last is None:
        _close_open_negotiation(
            negotiation,
            mission,
            now,
            reason="empty_negotiation",
            actor=requester,
        )
        raise _status_error(negotiation)

    if role != expected_role(last.author_role):
        raise NegotiationError(
            "not_your_turn",
            "It is not your turn to make an offer.",
            http_status=403,
        )

    offer_count = negotiation.offers.count()
    if offer_count >= negotiation.max_offers:
        raise NegotiationError(
            "max_offers_reached",
            "Both sides already used all allowed offers.",
            http_status=409,
            extra={"max_offers": negotiation.max_offers},
        )

    amount = parse_amount(data.get("amount"))
    validate_offer_amount(
        amount,
        initial_price=negotiation.initial_price,
        tolerance=negotiation.tolerance,
        min_step=negotiation.min_step,
        min_fare=negotiation.min_fare,
        last_amount=last.amount,
        last_author_role=last.author_role,
        requester_role=role,
    )

    sequence = offer_count + 1
    offer = NegotiationOffer.objects.create(
        negotiation=negotiation,
        author=requester,
        author_role=role,
        sequence=sequence,
        amount=amount,
        is_final=sequence >= negotiation.max_offers,
        idempotency_key=idempotency_key,
    )

    # Every accepted turn extends the per-offer hold, capped by the absolute
    # deadline. The mission remains held by the negotiation's driver.
    mission.held_by = negotiation.driver
    mission.hold_expires_at = next_hold_deadline(
        now,
        offer_ttl_minutes=negotiation.offer_ttl_minutes,
        expires_at=negotiation.expires_at,
    )
    mission.save(update_fields=["held_by", "hold_expires_at", "updated_at"])

    record_pricing_audit(
        actor=requester,
        action="negotiation_offer",
        mission=mission,
        payload=_audit_payload(
            negotiation,
            offer_id=offer.pk,
            amount=amount,
            sequence=sequence,
            author_role=role,
        ),
    )
    return negotiation


def _make_offer_atomic(
    mission_id: int,
    requester,
    data: dict[str, Any],
    idempotency_key: str,
    now,
) -> tuple[Negotiation, bool]:
    with transaction.atomic():
        try:
            mission = Mission.objects.select_for_update().get(pk=mission_id)
        except Mission.DoesNotExist:
            raise _mission_not_found() from None

        latest = _latest(mission, for_update=True)
        if latest is not None and latest.status == STATUS_OPEN:
            _expire_if_needed(latest, mission, now)

        if latest is not None and latest.status == STATUS_OPEN:
            role = _require_participant(latest, requester)
            return (
                _counter_offer(
                    mission,
                    latest,
                    requester,
                    role,
                    data,
                    idempotency_key,
                    now,
                ),
                False,
            )

        # ---- Opening path -------------------------------------------------
        if getattr(requester, "role", None) != "driver":
            if latest is not None and latest.requester_role(requester) is not None:
                raise _status_error(latest)
            raise _negotiation_not_found()

        _require_approved_driver(requester)

        # A retry of an already-recorded first offer (including after a
        # decline/expiry) returns the stored negotiation instead of opening a
        # second one.
        historical = (
            NegotiationOffer.objects.select_related("negotiation")
            .filter(
                negotiation__mission=mission,
                negotiation__driver=requester,
                idempotency_key=idempotency_key,
            )
            .order_by("-created_at")
            .first()
        )
        if historical is not None:
            amount = parse_amount(data.get("amount"))
            if amount == historical.amount:
                return historical.negotiation, False
            raise NegotiationError(
                "idempotency_conflict",
                "This idempotency_key was already used with a different amount.",
                http_status=409,
            )

        _assert_mission_available(mission, requester, now)

        amount = parse_amount(data.get("amount"))
        snapshot = _snapshot_config(mission)
        validate_offer_amount(
            amount,
            initial_price=snapshot["initial_price"],
            tolerance=snapshot["config"].negotiation.tolerance,
            min_step=snapshot["config"].negotiation.min_step,
            min_fare=snapshot["min_fare"],
            requester_role=ROLE_DRIVER,
        )
        return _new_negotiation(
            mission,
            requester,
            amount=amount,
            idempotency_key=idempotency_key,
            snapshot=snapshot,
            now=now,
        )


def make_offer(
    mission_id: int,
    requester,
    data: dict[str, Any],
    *,
    now=None,
) -> tuple[Negotiation, bool]:
    """Open a negotiation or add the next offer.

    Returns ``(negotiation, created)`` where ``created`` is True only when the
    first offer created a new negotiation.
    """
    now = now or timezone.now()
    if not hasattr(data, "get"):
        raise NegotiationError(
            "invalid_payload",
            "Request body must be a JSON object.",
            http_status=400,
        )
    idempotency_key = parse_idempotency_key(data.get("idempotency_key"))
    _prepare_mission(mission_id, now)
    try:
        return _make_offer_atomic(mission_id, requester, data, idempotency_key, now)
    except IntegrityError as exc:
        # The partial unique constraint is the last line of defence against
        # two drivers opening a negotiation for the same mission.
        message = str(exc).lower()
        if (
            "uniq_open_negotiation_per_mission" in message
            or (
                "unique" in message
                and "negotiation" in message
                and "mission_id" in message
            )
        ):
            raise NegotiationError(
                "negotiation_already_open",
                "Another driver just opened a negotiation for this mission.",
                http_status=409,
            ) from exc
        raise
    except NegotiationError:
        _cleanup_after_error(mission_id, now)
        raise


# ---------------------------------------------------------------------------
# Accept / decline
# ---------------------------------------------------------------------------


def _accept_negotiation_atomic(
    mission_id: int, requester, now
) -> tuple[Negotiation, Mission, bool]:
    with transaction.atomic():
        try:
            mission = Mission.objects.select_for_update().get(pk=mission_id)
        except Mission.DoesNotExist:
            raise _mission_not_found() from None

        negotiation = _latest(mission, for_update=True)
        if negotiation is None:
            raise _negotiation_not_found()
        role = _require_participant(negotiation, requester)

        if negotiation.status == STATUS_ACCEPTED:
            return negotiation, mission, True

        if negotiation.status == STATUS_OPEN:
            _expire_if_needed(negotiation, mission, now)

        if negotiation.status != STATUS_OPEN:
            raise _status_error(negotiation)

        if mission.status != MissionStatus.PENDING:
            _close_open_negotiation(
                negotiation,
                mission,
                now,
                reason="mission_not_pending",
                actor=requester,
            )
            raise _status_error(negotiation)

        last = negotiation.offers.order_by("-sequence").first()
        if last is None:
            _close_open_negotiation(
                negotiation,
                mission,
                now,
                reason="empty_negotiation",
                actor=requester,
            )
            raise _status_error(negotiation)

        if last.author_role == role:
            raise NegotiationError(
                "not_your_turn",
                "You cannot accept your own offer.",
                http_status=403,
            )

        amount = int(last.amount)
        commission, driver_net = commission_split(
            amount, negotiation.commission_rate
        )

        try:
            mission = assign_mission(
                mission_id=mission.pk,
                employee_id=negotiation.driver_id,
                close_negotiations=False,
            )
        except DriverNotApprovedError as exc:
            raise NegotiationError(
                "driver_not_approved",
                str(exc),
                http_status=409,
                extra={"profile_status": exc.profile_status},
            ) from exc
        except MissionAlreadyAcceptedError:
            _close_open_negotiation(
                negotiation,
                mission,
                now,
                reason="mission_already_accepted",
                actor=requester,
            )
            raise _status_error(negotiation) from None
        except ValueError as exc:
            raise NegotiationError(
                "negotiation_closed", str(exc), http_status=409
            ) from exc

        mission.price = Decimal(amount)
        mission.commission_rate = negotiation.commission_rate
        mission.commission_amount = commission
        mission.driver_net = driver_net
        mission.pricing_config_version = negotiation.config_version
        mission.price_locked_at = now
        mission.held_by = None
        mission.hold_expires_at = None
        mission.save(
            update_fields=[
                "price",
                "commission_rate",
                "commission_amount",
                "driver_net",
                "pricing_config_version",
                "price_locked_at",
                "held_by",
                "hold_expires_at",
                "updated_at",
            ]
        )

        negotiation.status = STATUS_ACCEPTED
        negotiation.accepted_offer = last
        negotiation.accepted_amount = amount
        negotiation.commission_amount = commission
        negotiation.driver_net = driver_net
        negotiation.accepted_at = now
        negotiation.closed_at = now
        negotiation.save(
            update_fields=[
                "status",
                "accepted_offer",
                "accepted_amount",
                "commission_amount",
                "driver_net",
                "accepted_at",
                "closed_at",
                "updated_at",
            ]
        )

        record_pricing_audit(
            actor=requester,
            action="negotiation_accepted",
            mission=mission,
            payload=_audit_payload(
                negotiation,
                accepted_offer_id=last.pk,
                amount=amount,
                sequence=last.sequence,
                commission_amount=commission,
                driver_net=driver_net,
            ),
        )

    return negotiation, mission, False


def accept_negotiation(
    mission_id: int, requester, *, now=None
) -> tuple[Negotiation, Mission, bool]:
    """Accept the latest offer and assign its driver atomically.

    Returns ``(negotiation, mission, idempotent)``. A second accept by a
    participant returns the stored state without assigning again.
    """
    now = now or timezone.now()
    _prepare_mission(mission_id, now)
    try:
        return _accept_negotiation_atomic(mission_id, requester, now)
    except NegotiationError:
        _cleanup_after_error(mission_id, now)
        raise


def _decline_negotiation_atomic(mission_id: int, requester, now) -> Negotiation:
    with transaction.atomic():
        try:
            mission = Mission.objects.select_for_update().get(pk=mission_id)
        except Mission.DoesNotExist:
            raise _mission_not_found() from None

        negotiation = _latest(mission, for_update=True)
        if negotiation is None:
            raise _negotiation_not_found()
        role = _require_participant(negotiation, requester)

        if negotiation.status == STATUS_OPEN:
            _expire_if_needed(negotiation, mission, now)

        if negotiation.status != STATUS_OPEN:
            raise _status_error(negotiation)

        if mission.status != MissionStatus.PENDING:
            _close_open_negotiation(
                negotiation,
                mission,
                now,
                reason="mission_not_pending",
                actor=requester,
            )
            raise _status_error(negotiation)

        last = negotiation.offers.order_by("-sequence").first()
        if last is None:
            _close_open_negotiation(
                negotiation,
                mission,
                now,
                reason="empty_negotiation",
                actor=requester,
            )
            raise _status_error(negotiation)

        if last.author_role == role:
            raise NegotiationError(
                "not_your_turn",
                "You cannot decline your own offer.",
                http_status=403,
            )

        _close_open_negotiation(
            negotiation,
            mission,
            now,
            reason="declined",
            actor=requester,
            status=STATUS_DECLINED,
            action="negotiation_declined",
        )

    return negotiation


def decline_negotiation(mission_id: int, requester, *, now=None) -> Negotiation:
    """Reject the latest offer and leave the mission open for a new driver."""
    now = now or timezone.now()
    _prepare_mission(mission_id, now)
    try:
        return _decline_negotiation_atomic(mission_id, requester, now)
    except NegotiationError:
        _cleanup_after_error(mission_id, now)
        raise


# ---------------------------------------------------------------------------
# Expiry (lazy helper + management command)
# ---------------------------------------------------------------------------


def expire_stale_negotiations(*, now=None, dry_run: bool = False) -> dict[str, int]:
    """Expire open negotiations and release stale holds.

    Returns ``{"negotiations_expired": n, "holds_cleared": m}``.
    """
    now = now or timezone.now()

    stale_ids = list(
        Negotiation.objects.filter(status=STATUS_OPEN)
        .filter(
            Q(expires_at__lte=now)
            | Q(
                mission__held_by__isnull=False,
                mission__hold_expires_at__lte=now,
            )
        )
        .values_list("pk", flat=True)
    )

    expired = 0
    if dry_run:
        expired = len(stale_ids)
    else:
        for negotiation_id in stale_ids:
            with transaction.atomic():
                try:
                    negotiation = (
                        Negotiation.objects.select_for_update()
                        .select_related("mission")
                        .get(pk=negotiation_id)
                    )
                except Negotiation.DoesNotExist:
                    continue
                if negotiation.status != STATUS_OPEN:
                    continue
                if _expire_if_needed(negotiation, negotiation.mission, now):
                    expired += 1

    orphan_qs = (
        Mission.objects.filter(held_by__isnull=False, hold_expires_at__lte=now)
        .exclude(negotiations__status=STATUS_OPEN)
        .distinct()
    )
    if dry_run:
        holds_cleared = orphan_qs.count()
    else:
        holds_cleared = 0
        for mission in orphan_qs:
            _clear_hold(mission)
            holds_cleared += 1

    return {
        "negotiations_expired": expired,
        "holds_cleared": holds_cleared,
    }
