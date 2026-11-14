"""Opening a negotiation with the driver's first offer."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from missions.models import Mission, MissionStatus, Negotiation, NegotiationOffer, PricingAuditLog
from missions.negotiation import NegotiationError
from missions.negotiation_services import make_offer
from missions.pricing_services import seed_default_config

from .base import api_client, confirm_payload, make_client
from .negotiation_base import (
    MAX_OFFERS,
    MISSION_PRICE,
    MIN_STEP,
    OFFER_HIGH,
    OFFER_LOW,
    EFFECTIVE_MIN_FARE,
    NegotiationAPITestMixin,
    make_mission,
    make_offer_payload,
    offer_url,
)


class OpenNegotiationTests(NegotiationAPITestMixin, TestCase):
    def test_approved_driver_opens_with_first_offer(self):
        response = self.open_negotiation(800)
        self.assertEqual(response.status_code, 201, response.data)

        data = response.data
        self.assertEqual(data["status"], "open")
        self.assertEqual(data["initial_price"], MISSION_PRICE)
        self.assertEqual(data["current_amount"], 800)
        self.assertEqual(data["min_amount"], OFFER_LOW)
        self.assertEqual(data["max_amount"], OFFER_HIGH)
        self.assertEqual(data["min_fare"], EFFECTIVE_MIN_FARE)
        self.assertEqual(data["max_offers"], MAX_OFFERS)
        self.assertEqual(data["min_step"], MIN_STEP)
        self.assertEqual(data["offer_count"], 1)
        self.assertFalse(data["is_final"])
        self.assertFalse(data["calibrated"])
        self.assertEqual(data["currency"], "DZD")
        self.assertEqual(data["config_version"], 1)
        self.assertFalse(data["your_turn"])  # driver offered, client is next

        # Driver audience: net + rate.
        self.assertEqual(data["commission_rate"], "0.1")
        self.assertEqual(data["commission_amount"], 80)
        self.assertEqual(data["driver_net"], 720)

        negotiation = Negotiation.objects.get()
        self.assertEqual(negotiation.mission_id, self.mission.pk)
        self.assertEqual(negotiation.client_id, self.client_user.pk)
        self.assertEqual(negotiation.driver_id, self.driver.pk)
        self.assertEqual(negotiation.status, Negotiation.Status.OPEN)
        self.assertEqual(negotiation.initial_price, MISSION_PRICE)
        self.assertEqual(negotiation.commission_rate, Decimal("0.1"))

        offer = NegotiationOffer.objects.get()
        self.assertEqual(offer.negotiation_id, negotiation.pk)
        self.assertEqual(offer.sequence, 1)
        self.assertEqual(offer.amount, 800)
        self.assertEqual(offer.author_role, "driver")
        self.assertFalse(offer.is_final)

        self.mission.refresh_from_db()
        self.assertEqual(self.mission.held_by_id, self.driver.pk)
        self.assertEqual(self.mission.status, MissionStatus.PENDING)
        self.assertIsNotNone(self.mission.hold_expires_at)
        self.assertGreater(
            self.mission.hold_expires_at,
            timezone.now() + timedelta(minutes=14),
        )
        self.assertLessEqual(
            self.mission.hold_expires_at,
            timezone.now() + timedelta(minutes=16),
        )

        audit = PricingAuditLog.objects.get(action="negotiation_opened")
        self.assertEqual(audit.mission_id, self.mission.pk)
        self.assertEqual(audit.actor_id, self.driver.pk)
        self.assertEqual(audit.payload["amount"], 800)
        for forbidden in ("username", "email", "phone", "phone_number"):
            self.assertNotIn(forbidden, audit.payload)

        for forbidden in ("author", "username", "email", "phone", "phone_number"):
            self.assertNotIn(forbidden, data)
            self.assertNotIn(forbidden, data["offers"][0])

    def test_string_amount_is_accepted(self):
        response = self.driver_api.post(
            offer_url(self.mission),
            {"amount": "800", "idempotency_key": "string-amount"},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["current_amount"], 800)

    def test_mission_must_be_negotiable(self):
        self.mission.negotiable = False
        self.mission.save(update_fields=["negotiable"])

        response = self.open_negotiation()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "mission_not_negotiable")
        self.assertEqual(Negotiation.objects.count(), 0)
        self.mission.refresh_from_db()
        self.assertIsNone(self.mission.held_by_id)

    def test_mission_must_be_pending(self):
        self.mission.status = MissionStatus.ACCEPTED
        self.mission.save(update_fields=["status"])

        response = self.open_negotiation()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "mission_not_negotiable")
        self.assertEqual(Negotiation.objects.count(), 0)

    def test_client_cannot_open_a_negotiation(self):
        response = self.client_api.post(
            offer_url(self.mission), make_offer_payload(), format="json"
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data["code"], "negotiation_not_found")
        self.assertEqual(Negotiation.objects.count(), 0)

    def test_other_client_cannot_open_a_negotiation(self):
        response = self.other_client_api.post(
            offer_url(self.mission), make_offer_payload(), format="json"
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data["code"], "negotiation_not_found")

    def test_unapproved_driver_gets_403(self):
        response = self.unapproved_api.post(
            offer_url(self.mission), make_offer_payload(), format="json"
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["code"], "driver_not_approved")
        self.assertEqual(response.data["profile_status"], "incomplete")

    def test_boss_gets_403(self):
        response = self.boss_api.post(
            offer_url(self.mission), make_offer_payload(), format="json"
        )
        self.assertEqual(response.status_code, 403)

    def test_missing_mission_returns_404(self):
        response = self.driver_api.post(
            reverse("missions:mission-negotiation-offer", args=[999999]),
            make_offer_payload(),
            format="json",
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data["code"], "mission_not_found")

    def test_offer_below_the_allowed_range_is_rejected(self):
        response = self.open_negotiation(700)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "offer_out_of_range")
        self.assertEqual(response.data["min_amount"], OFFER_LOW)
        self.assertEqual(response.data["max_amount"], OFFER_HIGH)
        self.assertEqual(Negotiation.objects.count(), 0)
        self.mission.refresh_from_db()
        self.assertIsNone(self.mission.held_by_id)

    def test_offer_above_the_allowed_range_is_rejected(self):
        response = self.open_negotiation(980)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "offer_out_of_range")

    def test_unaligned_offer_is_rejected(self):
        response = self.open_negotiation(805)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "offer_not_aligned")

    def test_non_integer_offer_is_rejected(self):
        response = self.open_negotiation(800.5)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "invalid_offer_amount")

    def test_below_min_fare_is_rejected(self):
        mission = make_mission(self.client_user, price=Decimal("200"))
        response = self.driver_api.post(
            offer_url(mission),
            make_offer_payload(180, "min-fare"),
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "offer_below_min_fare")

    def test_same_key_and_amount_replays_without_duplicating(self):
        first = self.open_negotiation(800, "idem-1")
        self.assertEqual(first.status_code, 201)
        created_at = NegotiationOffer.objects.get().created_at

        second = self.open_negotiation(800, "idem-1")
        self.assertEqual(second.status_code, 200, second.data)
        self.assertEqual(second.data["current_amount"], 800)
        self.assertEqual(NegotiationOffer.objects.count(), 1)
        self.assertEqual(NegotiationOffer.objects.get().created_at, created_at)

    def test_same_key_different_amount_conflicts(self):
        self.assertEqual(self.open_negotiation(800, "idem-2").status_code, 201)

        response = self.open_negotiation(810, "idem-2")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "idempotency_conflict")
        self.assertEqual(NegotiationOffer.objects.count(), 1)

    def test_second_driver_is_hidden_while_negotiation_is_open(self):
        self.assertEqual(self.open_negotiation(800).status_code, 201)

        response = self.other_driver_api.post(
            offer_url(self.mission),
            make_offer_payload(800, "other-driver-key"),
            format="json",
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data["code"], "negotiation_not_found")
        self.assertEqual(Negotiation.objects.count(), 1)

    def test_other_driver_direct_accept_is_blocked_by_the_hold(self):
        self.assertEqual(self.open_negotiation(800).status_code, 201)

        response = self.other_driver_api.post(
            reverse("missions:mission-accept", args=[self.mission.pk])
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "mission_held")
        self.assertIsNotNone(response.data["hold_expires_at"])

        self.mission.refresh_from_db()
        self.assertEqual(self.mission.status, MissionStatus.PENDING)
        self.assertEqual(
            Negotiation.objects.get().status, Negotiation.Status.OPEN
        )

    def test_holder_direct_accept_closes_the_negotiation(self):
        self.assertEqual(self.open_negotiation(800).status_code, 201)

        response = self.driver_api.post(
            reverse("missions:mission-accept", args=[self.mission.pk])
        )
        self.assertEqual(response.status_code, 200, response.data)

        self.mission.refresh_from_db()
        self.assertEqual(self.mission.status, MissionStatus.ACCEPTED)
        self.assertEqual(self.mission.assigned_employee_id, self.driver.pk)
        self.assertEqual(int(self.mission.price), MISSION_PRICE)
        self.assertIsNone(self.mission.held_by_id)
        self.assertEqual(
            Negotiation.objects.get().status, Negotiation.Status.CLOSED
        )

    def test_stale_hold_is_released_for_another_driver(self):
        self.assertEqual(self.open_negotiation(800).status_code, 201)
        Mission.objects.filter(pk=self.mission.pk).update(
            hold_expires_at=timezone.now() - timedelta(minutes=1)
        )

        response = self.other_driver_api.post(
            offer_url(self.mission),
            make_offer_payload(800, "stale-hold-key"),
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        open_negotiation = Negotiation.objects.get(
            status=Negotiation.Status.OPEN
        )
        self.assertEqual(open_negotiation.driver_id, self.other_driver.pk)
        self.assertEqual(
            Negotiation.objects.filter(
                status=Negotiation.Status.EXPIRED
            ).count(),
            1,
        )

    def test_client_offer_after_expiry_returns_expired(self):
        self.assertEqual(self.open_negotiation(800).status_code, 201)
        Mission.objects.filter(pk=self.mission.pk).update(
            hold_expires_at=timezone.now() - timedelta(minutes=1)
        )

        response = self.counter(self.client_api, 780, "late-client")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "negotiation_expired")
        self.assertEqual(
            Negotiation.objects.get().status, Negotiation.Status.EXPIRED
        )


class ServiceGuardTests(TestCase):
    """Direct service calls must enforce the same approval rule."""

    def setUp(self):
        seed_default_config()
        self.client_user = make_client("guard-client")
        self.driver = make_client("guard-driver")  # role=client on purpose
        self.mission = make_mission(self.client_user)

    def test_service_refuses_a_non_driver_opener(self):
        with self.assertRaises(NegotiationError) as ctx:
            make_offer(
                self.mission.pk,
                self.driver,
                make_offer_payload(),
            )
        self.assertEqual(ctx.exception.code, "negotiation_not_found")
        self.assertEqual(ctx.exception.http_status, 404)


class LegacyNegotiableFlagTests(TestCase):
    def setUp(self):
        seed_default_config()
        self.client_user = make_client("legacy-neg-client")
        self.mission_api = api_client(self.client_user)

    def test_legacy_confirm_persists_negotiable_true(self):
        response = self.mission_api.post(
            reverse("missions:mission-confirm"),
            confirm_payload(negotiable=True),
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        mission = Mission.objects.get()
        self.assertTrue(mission.negotiable)

    def test_legacy_confirm_defaults_to_not_negotiable(self):
        response = self.mission_api.post(
            reverse("missions:mission-confirm"),
            confirm_payload(),
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertFalse(Mission.objects.get().negotiable)
