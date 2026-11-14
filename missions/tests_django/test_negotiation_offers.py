"""Counter-offers, turns, limits and TTL behaviour."""
from __future__ import annotations

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from missions.models import MissionStatus, Negotiation, NegotiationOffer, PricingAuditLog
from missions.services import assign_mission, cancel_mission

from .negotiation_base import (
    MAX_OFFERS,
    NegotiationAPITestMixin,
    make_offer_payload,
    negotiation_url,
    offer_url,
)


class CounterOfferTests(NegotiationAPITestMixin, TestCase):
    def setUp(self):
        super().setUp()
        response = self.open_negotiation(800, "open-key")
        self.assertEqual(response.status_code, 201, response.data)

    def test_client_counters_lower_and_driver_sees_net(self):
        response = self.counter(self.client_api, 780, "client-key-1")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["offer_count"], 2)
        self.assertEqual(response.data["current_amount"], 780)
        self.assertFalse(response.data["is_final"])
        self.assertFalse(response.data["your_turn"])  # client just offered

        driver_view = self.driver_api.get(negotiation_url(self.mission))
        self.assertEqual(driver_view.status_code, 200)
        self.assertTrue(driver_view.data["your_turn"])

        # Client audience: total only, no commission fields.
        for field in ("commission_rate", "commission_amount", "driver_net"):
            self.assertNotIn(field, response.data)
        for offer in response.data["offers"]:
            self.assertNotIn("commission_amount", offer)
            self.assertNotIn("driver_net", offer)

        offers = list(NegotiationOffer.objects.order_by("sequence"))
        self.assertEqual([o.amount for o in offers], [800, 780])
        self.assertEqual([o.author_role for o in offers], ["driver", "client"])
        self.assertEqual([o.sequence for o in offers], [1, 2])

        self.mission.refresh_from_db()
        self.assertEqual(self.mission.held_by_id, self.driver.pk)
        self.assertTrue(
            PricingAuditLog.objects.filter(action="negotiation_offer").exists()
        )

    def test_driver_sees_net_on_every_offer(self):
        self.counter(self.client_api, 780, "client-key-2")
        response = self.counter(self.driver_api, 790, "driver-key-2")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["commission_rate"], "0.1")
        self.assertEqual(response.data["commission_amount"], 79)
        self.assertEqual(response.data["driver_net"], 711)
        for offer in response.data["offers"]:
            self.assertIn("commission_amount", offer)
            self.assertIn("driver_net", offer)

    def test_third_offer_is_final_and_a_fourth_is_rejected(self):
        self.counter(self.client_api, 780, "c-final-1")
        response = self.counter(self.driver_api, 790, "d-final-2")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["offer_count"], MAX_OFFERS)
        self.assertTrue(response.data["is_final"])
        self.assertTrue(NegotiationOffer.objects.order_by("-sequence").first().is_final)

        response = self.counter(self.client_api, 780, "c-final-3")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "max_offers_reached")
        self.assertEqual(NegotiationOffer.objects.count(), MAX_OFFERS)

    def test_driver_cannot_offer_twice_in_a_row(self):
        response = self.counter(self.driver_api, 810, "driver-double")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["code"], "not_your_turn")
        self.assertEqual(NegotiationOffer.objects.count(), 1)

    def test_client_must_lower_the_previous_amount(self):
        for amount in (800, 820):
            with self.subTest(amount=amount):
                response = self.counter(self.client_api, amount, f"c-high-{amount}")
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.data["code"], "offer_not_monotonic")

    def test_client_cannot_counter_outside_the_range(self):
        response = self.counter(self.client_api, 700, "c-low")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "offer_out_of_range")

    def test_driver_must_raise_the_previous_amount(self):
        self.counter(self.client_api, 780, "c-for-driver")
        for amount in (780, 770):
            with self.subTest(amount=amount):
                response = self.counter(self.driver_api, amount, f"d-low-{amount}")
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.data["code"], "offer_not_monotonic")

    def test_turn_retry_is_idempotent_after_the_turn_flips(self):
        self.counter(self.client_api, 780, "c-flip")
        # Replay the driver's first offer with its original key/amount.
        response = self.driver_api.post(
            offer_url(self.mission),
            make_offer_payload(800, "open-key"),
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["current_amount"], 780)
        self.assertEqual(NegotiationOffer.objects.count(), 2)

    def test_client_idempotency_conflict(self):
        self.assertEqual(
            self.counter(self.client_api, 780, "c-idem").status_code, 200
        )
        response = self.counter(self.client_api, 770, "c-idem")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "idempotency_conflict")
        self.assertEqual(NegotiationOffer.objects.count(), 2)

    def test_missing_idempotency_key_is_rejected(self):
        response = self.client_api.post(
            offer_url(self.mission), {"amount": 780}, format="json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "invalid_idempotency_key")

    def test_missing_amount_is_rejected(self):
        response = self.client_api.post(
            offer_url(self.mission),
            {"idempotency_key": "no-amount"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "invalid_offer_amount")

    def test_total_ttl_expiry_is_lazy(self):
        Negotiation.objects.filter(mission=self.mission).update(
            expires_at=timezone.now() - timedelta(minutes=1)
        )

        response = self.counter(self.client_api, 780, "after-total-ttl")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "negotiation_expired")

        negotiation = Negotiation.objects.get()
        self.assertEqual(negotiation.status, Negotiation.Status.EXPIRED)
        self.assertIsNotNone(negotiation.closed_at)
        self.mission.refresh_from_db()
        self.assertIsNone(self.mission.held_by_id)
        self.assertIsNone(self.mission.hold_expires_at)

    def test_cancelled_mission_closes_the_negotiation(self):
        cancel_mission(self.mission.pk)
        negotiation = Negotiation.objects.get()
        self.assertEqual(negotiation.status, Negotiation.Status.CLOSED)
        self.mission.refresh_from_db()
        self.assertIsNone(self.mission.held_by_id)

        response = self.counter(self.client_api, 780, "after-cancel")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "negotiation_closed")

    def test_boss_assignment_overrides_the_hold_and_closes_the_negotiation(self):
        mission = assign_mission(self.mission.pk, self.other_driver.pk)
        self.assertEqual(mission.status, MissionStatus.ACCEPTED)
        self.assertEqual(mission.assigned_employee_id, self.other_driver.pk)
        self.assertIsNone(mission.held_by_id)
        self.assertIsNone(mission.hold_expires_at)
        self.assertEqual(
            Negotiation.objects.get().status, Negotiation.Status.CLOSED
        )

        response = self.counter(self.client_api, 780, "after-boss-assign")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "negotiation_closed")
