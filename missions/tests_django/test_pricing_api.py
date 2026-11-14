"""API-level tests for the pricing opt-in, quotes and confirmations."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from missions.models import Mission, PricingAuditLog, PricingOverride, Quote
from missions.pricing_services import seed_default_config

from .base import (
    COMMISSION_10_PCT_850,
    DRIVER_NET_850,
    STANDARD_12KM_20MIN,
    api_client,
    confirm_payload,
    legacy_estimate_payload,
    make_boss,
    make_client,
    make_driver,
    new_confirm_payload,
    new_estimate_payload,
    quote_confirm_payload,
)

ESTIMATE_URL = reverse("missions:mission-estimate")
CONFIRM_URL = reverse("missions:mission-confirm")
RULES_URL = reverse("missions:pricing-rules")

LEGACY_PRICE = 67500


class EstimateAPITests(TestCase):
    def setUp(self):
        self.client_user = make_client()
        self.other_client = make_client("pricing-client-2")
        self.driver = make_driver()
        self.boss = make_boss()
        self.api = api_client(self.client_user)
        seed_default_config()

    def test_legacy_estimate_keeps_amount_and_shape(self):
        response = self.api.post(
            ESTIMATE_URL, legacy_estimate_payload(), format="json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["estimated_price"], LEGACY_PRICE)
        self.assertEqual(
            response.data["breakdown"],
            {
                "distance_km": Decimal("300.00"),
                "workers": 2,
                "size": "medium",
                "price_per_km": 200,
                "fixed_fuel_cost": 2000,
                "price_per_worker": 1500,
                "size_surcharge": 2500,
            },
        )
        estimate = response.data["estimate"]
        self.assertFalse(estimate["calibrated"])
        self.assertIsNone(estimate["quote_id"])
        self.assertEqual(estimate["confidence"], "low")  # duration fallback
        self.assertEqual(estimate["amount"], 33400)
        self.assertEqual(Quote.objects.count(), 0)  # legacy = no DB write

    def test_new_estimate_uses_engine_and_issues_a_quote(self):
        response = self.api.post(ESTIMATE_URL, new_estimate_payload(), format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["estimated_price"], STANDARD_12KM_20MIN)

        estimate = response.data["estimate"]
        self.assertEqual(estimate["amount"], STANDARD_12KM_20MIN)
        self.assertEqual(estimate["confidence"], "normal")
        self.assertFalse(estimate["calibrated"])
        self.assertEqual(estimate["config_version"], 1)
        self.assertIsNotNone(estimate["quote_id"])
        self.assertIsNotNone(estimate["expires_at"])

        self.assertEqual(Quote.objects.count(), 1)
        quote = Quote.objects.get()
        self.assertEqual(quote.amount, STANDARD_12KM_20MIN)
        self.assertFalse(quote.calibrated)
        self.assertNotIn("departure_latitude", quote.inputs)
        self.assertNotIn("arrival_latitude", quote.inputs)
        self.assertNotIn("departure_longitude", quote.inputs)
        self.assertNotIn("arrival_longitude", quote.inputs)

    def test_new_estimate_falls_back_to_haversine_with_low_confidence(self):
        payload = new_estimate_payload()
        payload.pop("distance")
        payload.pop("duration_min")

        response = self.api.post(ESTIMATE_URL, payload, format="json")
        self.assertEqual(response.status_code, 200)

        estimate = response.data["estimate"]
        self.assertEqual(estimate["confidence"], "low")
        self.assertGreater(estimate["amount"], 0)
        self.assertLess(estimate["low"], estimate["amount"])
        self.assertGreater(estimate["high"], estimate["amount"])

    def test_out_of_bounds_distance_is_rejected(self):
        response = self.api.post(
            ESTIMATE_URL, new_estimate_payload(distance="80.00"), format="json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "invalid_distance")

    def test_legacy_payload_is_not_bounds_checked(self):
        # 300 km is below the 317.8 km straight line, but legacy is opt-out.
        response = self.api.post(
            ESTIMATE_URL, legacy_estimate_payload(), format="json"
        )
        self.assertEqual(response.status_code, 200)

    def test_explicit_helpers_above_cap_are_rejected(self):
        response = self.api.post(
            ESTIMATE_URL, new_estimate_payload(helpers=5), format="json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "invalid_helpers")

    def test_legacy_workers_above_cap_still_work(self):
        response = self.api.post(
            ESTIMATE_URL, legacy_estimate_payload(workers=5), format="json"
        )
        self.assertEqual(response.status_code, 200)

    def test_invalid_coordinates_are_rejected(self):
        response = self.api.post(
            ESTIMATE_URL,
            new_estimate_payload(departure_latitude="91.000000"),
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("departure_latitude", response.data)

    def test_vehicle_class_wins_over_size(self):
        response = self.api.post(
            ESTIMATE_URL,
            new_estimate_payload(size="big", vehicle_class="standard"),
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["estimated_price"], STANDARD_12KM_20MIN)

    def test_estimate_requires_authentication(self):
        response = APIClient().post(ESTIMATE_URL, new_estimate_payload(), format="json")
        self.assertEqual(response.status_code, 401)

    def test_estimate_is_client_only(self):
        response = api_client(self.driver).post(
            ESTIMATE_URL, new_estimate_payload(), format="json"
        )
        self.assertEqual(response.status_code, 403)

    def test_rules_endpoint_is_public_to_authenticated_users(self):
        response = self.api.get(RULES_URL)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["calibrated"])
        self.assertEqual(response.data["negotiation"]["tolerance"], "0.15")
        self.assertEqual(response.data["negotiation"]["max_offers"], 3)
        self.assertNotIn("commission_rate", response.data)

    def test_rules_endpoint_requires_authentication(self):
        self.assertEqual(APIClient().get(RULES_URL).status_code, 401)

    def test_active_manual_override_applies_and_is_audited(self):
        PricingOverride.objects.create(
            client=self.client_user,
            multiplier=Decimal("1.2"),
            expires_at=timezone.now() + timedelta(hours=1),
            created_by=self.boss,
            reason="VIP client",
        )
        response = self.api.post(ESTIMATE_URL, new_estimate_payload(), format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["estimate"]["amount"], 1020)  # 850 × 1.2
        self.assertTrue(
            PricingAuditLog.objects.filter(action="override_applied").exists()
        )

    def test_expired_manual_override_is_ignored(self):
        PricingOverride.objects.create(
            client=self.client_user,
            multiplier=Decimal("2.0"),
            expires_at=timezone.now() - timedelta(minutes=1),
            created_by=self.boss,
        )
        response = self.api.post(ESTIMATE_URL, new_estimate_payload(), format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["estimate"]["amount"], STANDARD_12KM_20MIN)
        self.assertFalse(
            PricingAuditLog.objects.filter(action="override_applied").exists()
        )


class ConfirmAPITests(TestCase):
    def setUp(self):
        self.client_user = make_client("pricing-confirm-client")
        self.other_client = make_client("pricing-confirm-other")
        self.driver = make_driver("pricing-confirm-driver")
        self.boss = make_boss("pricing-confirm-boss")
        self.api = api_client(self.client_user)
        seed_default_config()

    def issue_quote(self) -> Quote:
        response = self.api.post(ESTIMATE_URL, new_estimate_payload(), format="json")
        self.assertEqual(response.status_code, 200)
        return Quote.objects.get(public_id=response.data["estimate"]["quote_id"])

    def test_legacy_confirm_is_byte_compatible(self):
        response = self.api.post(CONFIRM_URL, confirm_payload(), format="json")
        self.assertEqual(response.status_code, 201, response.data)

        mission = Mission.objects.get()
        self.assertEqual(int(mission.price), LEGACY_PRICE)
        self.assertFalse(mission.negotiable)
        self.assertIsNone(mission.quote_id)
        self.assertIsNone(mission.pricing_config_version)
        self.assertIsNone(mission.commission_rate)
        self.assertIsNone(mission.commission_amount)
        self.assertIsNone(mission.driver_net)
        self.assertIsNone(mission.price_locked_at)
        self.assertEqual(Quote.objects.count(), 0)
        self.assertEqual(PricingAuditLog.objects.count(), 0)

    def test_legacy_confirm_still_requires_distance_and_size(self):
        payload = confirm_payload()
        payload.pop("distance")
        response = self.api.post(CONFIRM_URL, payload, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("distance", response.data)

        payload = confirm_payload()
        payload.pop("size")
        response = self.api.post(CONFIRM_URL, payload, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("size", response.data)

    def test_confirm_with_quote_uses_quoted_price_and_snapshot(self):
        quote = self.issue_quote()
        response = self.api.post(
            CONFIRM_URL, quote_confirm_payload(quote.public_id), format="json"
        )
        self.assertEqual(response.status_code, 201, response.data)

        mission = Mission.objects.get()
        self.assertEqual(int(mission.price), STANDARD_12KM_20MIN)
        self.assertEqual(mission.size, "small")  # derived from standard
        self.assertEqual(mission.quote_id, quote.pk)
        self.assertEqual(mission.pricing_config_version, 1)
        self.assertEqual(mission.commission_rate, Decimal("0.1000"))
        self.assertEqual(mission.commission_amount, COMMISSION_10_PCT_850)
        self.assertEqual(mission.driver_net, DRIVER_NET_850)
        self.assertIsNotNone(mission.price_locked_at)
        self.assertEqual(mission.price_components, quote.components)
        self.assertFalse(mission.negotiable)

        quote.refresh_from_db()
        self.assertIsNotNone(quote.used_at)
        self.assertEqual(quote.mission_id, mission.pk)

        audit = PricingAuditLog.objects.get(action="quote_confirmed")
        self.assertEqual(audit.mission_id, mission.pk)
        self.assertEqual(audit.actor_id, self.client_user.pk)
        self.assertEqual(audit.payload["amount"], STANDARD_12KM_20MIN)

    def test_negotiable_flag_is_persisted(self):
        quote = self.issue_quote()
        response = self.api.post(
            CONFIRM_URL,
            quote_confirm_payload(quote.public_id, negotiable=True),
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertTrue(Mission.objects.get().negotiable)

    def test_quote_cannot_be_reused(self):
        quote = self.issue_quote()
        payload = quote_confirm_payload(quote.public_id)
        self.assertEqual(
            self.api.post(CONFIRM_URL, payload, format="json").status_code, 201
        )
        response = self.api.post(CONFIRM_URL, payload, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "quote_already_used")

    def test_expired_quote_is_rejected(self):
        quote = self.issue_quote()
        Quote.objects.filter(pk=quote.pk).update(
            expires_at=timezone.now() - timedelta(minutes=1)
        )
        response = self.api.post(
            CONFIRM_URL, quote_confirm_payload(quote.public_id), format="json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "quote_expired")

    def test_other_client_cannot_use_the_quote(self):
        quote = self.issue_quote()
        response = api_client(self.other_client).post(
            CONFIRM_URL, quote_confirm_payload(quote.public_id), format="json"
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data["code"], "quote_not_found")
        self.assertEqual(Mission.objects.count(), 0)

    def test_quote_inputs_must_match(self):
        quote = self.issue_quote()
        response = self.api.post(
            CONFIRM_URL,
            quote_confirm_payload(quote.public_id, distance="15.00"),
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "quote_mismatch")

    def test_route_is_revalidated_against_the_quote(self):
        quote = self.issue_quote()
        response = self.api.post(
            CONFIRM_URL,
            quote_confirm_payload(quote.public_id, arrival_latitude="20.000000"),
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "invalid_distance")

    def test_new_confirm_without_quote_stores_a_snapshot(self):
        response = self.api.post(CONFIRM_URL, new_confirm_payload(), format="json")
        self.assertEqual(response.status_code, 201, response.data)

        mission = Mission.objects.get()
        self.assertEqual(int(mission.price), STANDARD_12KM_20MIN)
        self.assertIsNone(mission.quote_id)
        self.assertEqual(mission.pricing_config_version, 1)
        self.assertEqual(mission.commission_amount, COMMISSION_10_PCT_850)
        self.assertEqual(mission.driver_net, DRIVER_NET_850)
        self.assertIsNotNone(mission.price_components)
        self.assertIsNotNone(mission.price_locked_at)
        self.assertEqual(Quote.objects.count(), 0)
        self.assertTrue(
            PricingAuditLog.objects.filter(action="price_locked").exists()
        )

    def test_confirm_is_client_only(self):
        response = api_client(self.driver).post(
            CONFIRM_URL, new_confirm_payload(), format="json"
        )
        self.assertEqual(response.status_code, 403)
