"""Database-level guarantee: at most one open negotiation per mission."""
from __future__ import annotations

import threading

from django.db import IntegrityError, connection, transaction
from django.test import TestCase, TransactionTestCase

from missions.models import Negotiation, NegotiationOffer
from missions.negotiation import allowed_range
from missions.pricing_services import config_from_model, get_active_config, seed_default_config

from .base import api_client, make_client, make_driver
from .negotiation_base import (
    MISSION_PRICE,
    MIN_STEP,
    make_mission,
    make_offer_payload,
    offer_url,
)


def _create_open_negotiation(mission, driver, *, key: str) -> Negotiation:
    config = config_from_model(get_active_config())
    low, high = allowed_range(
        MISSION_PRICE, config.negotiation.tolerance, MIN_STEP
    )
    from datetime import timedelta

    from django.utils import timezone

    now = timezone.now()
    negotiation = Negotiation.objects.create(
        mission=mission,
        client=mission.client,
        driver=driver,
        status=Negotiation.Status.OPEN,
        config_version=config.version,
        calibrated=config.calibrated,
        initial_price=MISSION_PRICE,
        min_amount=low,
        max_amount=high,
        min_fare=min(config.vehicle_classes["standard"].min_fare, MISSION_PRICE),
        tolerance=config.negotiation.tolerance,
        max_offers=config.negotiation.max_offers,
        min_step=config.negotiation.min_step,
        offer_ttl_minutes=config.negotiation.offer_ttl_minutes,
        total_ttl_minutes=config.negotiation.total_ttl_minutes,
        commission_rate=config.commission_rate,
        expires_at=now + timedelta(minutes=config.negotiation.total_ttl_minutes),
    )
    NegotiationOffer.objects.create(
        negotiation=negotiation,
        author=driver,
        author_role="driver",
        sequence=1,
        amount=800,
        idempotency_key=key,
    )
    return negotiation


class OpenNegotiationConstraintTests(TestCase):
    def setUp(self):
        seed_default_config()
        self.client_user = make_client("constraint-client")
        self.driver = make_driver("constraint-driver")
        self.other_driver = make_driver("constraint-other")
        self.mission = make_mission(self.client_user)

    def test_second_open_negotiation_is_rejected(self):
        _create_open_negotiation(self.mission, self.driver, key="first")

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                _create_open_negotiation(
                    self.mission, self.other_driver, key="second"
                )

        self.assertEqual(
            Negotiation.objects.filter(
                mission=self.mission, status=Negotiation.Status.OPEN
            ).count(),
            1,
        )

    def test_closed_negotiation_does_not_block_a_new_one(self):
        first = _create_open_negotiation(self.mission, self.driver, key="first")
        first.status = Negotiation.Status.DECLINED
        first.save(update_fields=["status"])

        second = _create_open_negotiation(
            self.mission, self.other_driver, key="second"
        )
        self.assertEqual(second.status, Negotiation.Status.OPEN)
        self.assertEqual(
            Negotiation.objects.filter(
                mission=self.mission, status=Negotiation.Status.OPEN
            ).count(),
            1,
        )


class ConcurrentOpenTests(TransactionTestCase):
    """Two drivers race to open the same mission.

    SQLite serialises writes and treats ``select_for_update`` as a no-op, so
    this test asserts the *invariant* (at most one open negotiation) rather
    than a specific winner. On PostgreSQL the row lock makes the loser wait and
    then fail cleanly with ``negotiation_already_open``.
    """

    reset_sequences = True

    def setUp(self):
        seed_default_config()
        self.client_user = make_client("race-client")
        self.driver = make_driver("race-driver")
        self.other_driver = make_driver("race-other")
        self.mission = make_mission(self.client_user)

    def test_concurrent_first_offers_leave_at_most_one_open(self):
        barrier = threading.Barrier(2)
        results: list[tuple[str, object]] = []

        def attempt(user, key):
            try:
                barrier.wait(timeout=10)
                api = api_client(user)
                response = api.post(
                    offer_url(self.mission),
                    make_offer_payload(800, key),
                    format="json",
                )
                results.append(("response", response.status_code))
            except Exception as exc:  # pragma: no cover - SQLite lock paths
                results.append(("exception", exc))
            finally:
                connection.close()

        threads = [
            threading.Thread(target=attempt, args=(self.driver, "race-a")),
            threading.Thread(
                target=attempt, args=(self.other_driver, "race-b")
            ),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
            self.assertFalse(thread.is_alive(), "worker thread did not finish")

        self.assertEqual(len(results), 2)
        successes = [
            value
            for kind, value in results
            if kind == "response" and value in (200, 201)
        ]
        self.assertLessEqual(len(successes), 1)
        self.assertLessEqual(
            Negotiation.objects.filter(
                mission=self.mission, status=Negotiation.Status.OPEN
            ).count(),
            1,
        )
