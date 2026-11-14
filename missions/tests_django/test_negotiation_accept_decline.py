"""Acceptance, decline, idempotency and visibility."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from drivers.models import DriverProfile
from missions.models import (
    Mission,
    MissionStatus,
    Negotiation,
    PricingAuditLog,
)

from .negotiation_base import (
    MISSION_PRICE,
    NegotiationAPITestMixin,
    accept_url,
    decline_url,
    make_mission,
    make_offer_payload,
    negotiation_url,
    offer_url,
)


class AcceptNegotiationTests(NegotiationAPITestMixin, TestCase):
    def setUp(self):
        super().setUp()
        response = self.open_negotiation(800, "accept-open")
        self.assertEqual(response.status_code, 201, response.data)

    def test_client_accepts_the_driver_offer(self):
        response = self.client_api.post(accept_url(self.mission))
        self.assertEqual(response.status_code, 200, response.data)

        negotiation_data = response.data["negotiation"]
        self.assertEqual(negotiation_data["status"], "accepted")
        self.assertEqual(negotiation_data["accepted_amount"], 800)
        self.assertEqual(negotiation_data["current_amount"], 800)
        self.assertFalse(negotiation_data["is_final"])
        # Client audience: total only.
        for field in ("commission_rate", "commission_amount", "driver_net"):
            self.assertNotIn(field, negotiation_data)

        mission_data = response.data["mission"]
        self.assertEqual(mission_data["status"], MissionStatus.ACCEPTED)
        self.assertEqual(Decimal(mission_data["price"]), Decimal("800"))

        self.mission.refresh_from_db()
        self.assertEqual(self.mission.status, MissionStatus.ACCEPTED)
        self.assertEqual(self.mission.assigned_employee_id, self.driver.pk)
        self.assertEqual(int(self.mission.price), 800)
        self.assertEqual(self.mission.commission_rate, Decimal("0.1"))
        self.assertEqual(self.mission.commission_amount, 80)
        self.assertEqual(self.mission.driver_net, 720)
        self.assertEqual(self.mission.pricing_config_version, 1)
        self.assertIsNotNone(self.mission.price_locked_at)
        self.assertIsNone(self.mission.held_by_id)
        self.assertIsNone(self.mission.hold_expires_at)

        negotiation = Negotiation.objects.get()
        self.assertEqual(negotiation.status, Negotiation.Status.ACCEPTED)
        self.assertEqual(negotiation.accepted_offer.sequence, 1)
        self.assertEqual(negotiation.accepted_amount, 800)
        self.assertEqual(negotiation.commission_amount, 80)
        self.assertEqual(negotiation.driver_net, 720)
        self.assertIsNotNone(negotiation.accepted_at)
        self.assertIsNotNone(negotiation.closed_at)

        audit = PricingAuditLog.objects.get(action="negotiation_accepted")
        self.assertEqual(audit.actor_id, self.client_user.pk)
        self.assertEqual(audit.payload["accepted_offer_id"], negotiation.accepted_offer_id)
        for forbidden in ("username", "email", "phone", "phone_number"):
            self.assertNotIn(forbidden, audit.payload)

    def test_driver_accepts_the_client_counter(self):
        self.assertEqual(
            self.counter(self.client_api, 780, "accept-counter").status_code,
            200,
        )
        response = self.driver_api.post(accept_url(self.mission))
        self.assertEqual(response.status_code, 200, response.data)

        self.mission.refresh_from_db()
        self.assertEqual(int(self.mission.price), 780)
        self.assertEqual(self.mission.commission_amount, 78)
        self.assertEqual(self.mission.driver_net, 702)
        self.assertEqual(self.mission.assigned_employee_id, self.driver.pk)

    def test_author_cannot_accept_their_own_offer(self):
        response = self.driver_api.post(accept_url(self.mission))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["code"], "not_your_turn")

        self.counter(self.client_api, 780, "own-counter")
        response = self.client_api.post(accept_url(self.mission))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["code"], "not_your_turn")

    def test_non_participants_get_404(self):
        for api in (self.other_driver_api, self.other_client_api):
            with self.subTest(api=api):
                response = api.post(accept_url(self.mission))
                self.assertEqual(response.status_code, 404)
                self.assertEqual(response.data["code"], "negotiation_not_found")

    def test_accept_is_idempotent(self):
        self.assertEqual(
            self.client_api.post(accept_url(self.mission)).status_code, 200
        )
        response = self.client_api.post(accept_url(self.mission))
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["negotiation"]["status"], "accepted")
        self.mission.refresh_from_db()
        self.assertEqual(self.mission.assigned_employee_id, self.driver.pk)
        self.assertEqual(
            PricingAuditLog.objects.filter(action="negotiation_accepted").count(),
            1,
        )

    def test_accept_expired_negotiation(self):
        Mission.objects.filter(pk=self.mission.pk).update(
            hold_expires_at=timezone.now() - timedelta(minutes=1)
        )
        response = self.client_api.post(accept_url(self.mission))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "negotiation_expired")

        self.mission.refresh_from_db()
        self.assertEqual(self.mission.status, MissionStatus.PENDING)
        self.assertIsNone(self.mission.assigned_employee_id)
        self.assertEqual(
            Negotiation.objects.get().status, Negotiation.Status.EXPIRED
        )

    def test_accept_after_decline_is_closed(self):
        self.assertEqual(
            self.client_api.post(decline_url(self.mission)).status_code, 200
        )
        response = self.client_api.post(accept_url(self.mission))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "negotiation_closed")

    def test_accept_refuses_a_driver_that_is_no_longer_approved(self):
        DriverProfile.objects.filter(user=self.driver).update(
            status=DriverProfile.Status.REJECTED
        )
        response = self.client_api.post(accept_url(self.mission))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "driver_not_approved")
        self.assertEqual(response.data["profile_status"], "rejected")

        self.mission.refresh_from_db()
        self.assertEqual(self.mission.status, MissionStatus.PENDING)
        self.assertEqual(
            Negotiation.objects.get().status, Negotiation.Status.OPEN
        )

    def test_accept_without_a_negotiation_returns_404(self):
        mission = make_mission(self.client_user)
        response = self.client_api.post(accept_url(mission))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data["code"], "negotiation_not_found")

    def test_accept_missing_mission_returns_404(self):
        from django.urls import reverse

        response = self.client_api.post(
            reverse("missions:mission-negotiation-accept", args=[999999])
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data["code"], "mission_not_found")


class DeclineNegotiationTests(NegotiationAPITestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.assertEqual(
            self.open_negotiation(800, "decline-open").status_code, 201
        )

    def test_client_declines_and_mission_stays_open(self):
        response = self.client_api.post(decline_url(self.mission))
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["status"], "declined")
        self.assertIsNotNone(response.data["closed_at"])
        self.assertIsNone(response.data["hold_expires_at"])

        negotiation = Negotiation.objects.get()
        self.assertEqual(negotiation.status, Negotiation.Status.DECLINED)
        self.assertIsNotNone(negotiation.closed_at)
        self.assertIsNone(negotiation.accepted_offer_id)

        self.mission.refresh_from_db()
        self.assertEqual(self.mission.status, MissionStatus.PENDING)
        self.assertEqual(int(self.mission.price), MISSION_PRICE)
        self.assertIsNone(self.mission.assigned_employee_id)
        self.assertIsNone(self.mission.held_by_id)
        self.assertIsNone(self.mission.hold_expires_at)

        self.assertTrue(
            PricingAuditLog.objects.filter(action="negotiation_declined").exists()
        )

    def test_driver_can_decline_a_client_counter(self):
        self.counter(self.client_api, 780, "decline-counter")
        response = self.driver_api.post(decline_url(self.mission))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "declined")

    def test_author_cannot_decline_their_own_offer(self):
        response = self.driver_api.post(decline_url(self.mission))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["code"], "not_your_turn")

        self.counter(self.client_api, 780, "decline-own")
        response = self.client_api.post(decline_url(self.mission))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["code"], "not_your_turn")

    def test_decline_twice_is_closed(self):
        self.assertEqual(
            self.client_api.post(decline_url(self.mission)).status_code, 200
        )
        response = self.client_api.post(decline_url(self.mission))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "negotiation_closed")

    def test_another_driver_can_open_after_a_decline(self):
        self.assertEqual(
            self.client_api.post(decline_url(self.mission)).status_code, 200
        )
        response = self.other_driver_api.post(
            offer_url(self.mission),
            make_offer_payload(780, "after-decline"),
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)

        self.assertEqual(Negotiation.objects.count(), 2)
        self.assertEqual(
            Negotiation.objects.filter(status=Negotiation.Status.DECLINED).count(),
            1,
        )
        open_negotiation = Negotiation.objects.get(status=Negotiation.Status.OPEN)
        self.assertEqual(open_negotiation.driver_id, self.other_driver.pk)
        self.assertEqual(open_negotiation.offers.get().amount, 780)

    def test_retrying_the_declined_first_offer_replays_history(self):
        self.assertEqual(
            self.client_api.post(decline_url(self.mission)).status_code, 200
        )
        response = self.driver_api.post(
            offer_url(self.mission),
            make_offer_payload(800, "decline-open"),
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["status"], "declined")
        self.assertEqual(Negotiation.objects.count(), 1)

    def test_decline_missing_mission_returns_404(self):
        from django.urls import reverse

        response = self.client_api.post(
            reverse("missions:mission-negotiation-decline", args=[999999])
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data["code"], "mission_not_found")


class NegotiationVisibilityTests(NegotiationAPITestMixin, TestCase):
    def test_get_without_a_negotiation_returns_404(self):
        for api in (self.client_api, self.driver_api):
            with self.subTest(api=api):
                response = api.get(negotiation_url(self.mission))
                self.assertEqual(response.status_code, 404)
                self.assertEqual(
                    response.data["code"], "negotiation_not_found"
                )

    def test_get_by_non_participants_returns_404(self):
        self.assertEqual(self.open_negotiation(800).status_code, 201)
        for api in (self.other_driver_api, self.other_client_api):
            with self.subTest(api=api):
                response = api.get(negotiation_url(self.mission))
                self.assertEqual(response.status_code, 404)
                self.assertEqual(
                    response.data["code"], "negotiation_not_found"
                )

    def test_boss_cannot_read_a_negotiation(self):
        self.assertEqual(self.open_negotiation(800).status_code, 201)
        response = self.boss_api.get(negotiation_url(self.mission))
        self.assertEqual(response.status_code, 403)

    def test_both_participants_can_read_their_own_view(self):
        self.assertEqual(self.open_negotiation(800).status_code, 201)

        client_view = self.client_api.get(negotiation_url(self.mission))
        self.assertEqual(client_view.status_code, 200)
        self.assertNotIn("commission_rate", client_view.data)
        self.assertNotIn("commission_amount", client_view.data)
        self.assertNotIn("driver_net", client_view.data)

        driver_view = self.driver_api.get(negotiation_url(self.mission))
        self.assertEqual(driver_view.status_code, 200)
        self.assertEqual(driver_view.data["commission_rate"], "0.1")
        self.assertEqual(driver_view.data["commission_amount"], 80)
        self.assertEqual(driver_view.data["driver_net"], 720)

    def test_get_lazily_expires_an_abandoned_negotiation(self):
        self.assertEqual(self.open_negotiation(800).status_code, 201)
        Mission.objects.filter(pk=self.mission.pk).update(
            hold_expires_at=timezone.now() - timedelta(minutes=1)
        )

        response = self.client_api.get(negotiation_url(self.mission))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "expired")
        self.assertIsNone(response.data["hold_expires_at"])

        self.mission.refresh_from_db()
        self.assertIsNone(self.mission.held_by_id)
        self.assertIsNone(self.mission.hold_expires_at)

    def test_negotiation_serialization_has_no_personal_data(self):
        self.assertEqual(self.open_negotiation(800).status_code, 201)
        data = self.driver_api.get(negotiation_url(self.mission)).data
        for forbidden in (
            "driver",
            "client",
            "author",
            "username",
            "email",
            "phone",
            "phone_number",
        ):
            self.assertNotIn(forbidden, data)
            for offer in data["offers"]:
                self.assertNotIn(forbidden, offer)
