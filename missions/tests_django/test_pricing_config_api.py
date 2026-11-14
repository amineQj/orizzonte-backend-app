# -*- coding: utf-8 -*-
"""Tests for the boss-only pricing configuration management API."""
from __future__ import annotations

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from rest_framework import status

from missions.models import PricingAuditLog, PricingConfig
from missions.pricing_services import get_active_config, seed_default_config

from .base import api_client, make_boss, make_client, make_driver, make_user, PASSWORD


CONFIG_URL = reverse("missions:pricing-config")
CONFIG_UPDATE_URL = reverse("missions:pricing-config-update")


class PricingConfigAPITests(TestCase):
    def setUp(self):
        cache.clear()
        self.boss = make_boss("config-boss")
        self.client_user = make_client("config-client")
        self.driver = make_driver("config-driver")
        self.boss_api = api_client(self.boss)
        self.client_api = api_client(self.client_user)
        self.driver_api = api_client(self.driver)
        seed_default_config()

    def test_get_config_requires_boss(self):
        self.assertEqual(self.client_api.get(CONFIG_URL).status_code, 403)
        self.assertEqual(self.driver_api.get(CONFIG_URL).status_code, 403)
        self.assertEqual(self.boss_api.get(CONFIG_URL).status_code, 200)

    def test_get_config_returns_active_config(self):
        response = self.boss_api.get(CONFIG_URL)
        self.assertEqual(response.status_code, 200)
        self.assertIn("version", response.data)
        self.assertIn("calibrated", response.data)
        self.assertIn("parameters", response.data)
        self.assertIn("created_at", response.data)
        self.assertIn("updated_at", response.data)
        self.assertFalse(response.data["calibrated"])

    def test_update_requires_boss(self):
        payload = {"password": PASSWORD, "parameters": {"commission_rate": 0.2}}
        self.assertEqual(self.client_api.post(CONFIG_UPDATE_URL, payload, format="json").status_code, 403)
        self.assertEqual(self.driver_api.post(CONFIG_UPDATE_URL, payload, format="json").status_code, 403)

    def test_update_requires_authentication(self):
        from rest_framework.test import APIClient
        response = APIClient().post(CONFIG_UPDATE_URL, {"password": PASSWORD, "parameters": {}}, format="json")
        self.assertEqual(response.status_code, 401)

    def test_wrong_password_returns_403(self):
        payload = {"password": "WrongPassword!", "parameters": {"commission_rate": 0.2}}
        response = self.boss_api.post(CONFIG_UPDATE_URL, payload, format="json")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["code"], "invalid_password")

    def test_correct_password_creates_new_version(self):
        old_version = get_active_config().version
        payload = {"password": PASSWORD, "parameters": {"commission_rate": 0.2}}
        response = self.boss_api.post(CONFIG_UPDATE_URL, payload, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["version"], old_version + 1)
        self.assertFalse(response.data["calibrated"])
        # Old config deactivated
        self.assertEqual(PricingConfig.objects.filter(is_active=True).count(), 1)
        self.assertEqual(get_active_config().version, old_version + 1)

    def test_update_creates_audit_log(self):
        payload = {"password": PASSWORD, "parameters": {"commission_rate": 0.2}}
        self.boss_api.post(CONFIG_UPDATE_URL, payload, format="json")
        audit = PricingAuditLog.objects.filter(action="config_updated").first()
        self.assertIsNotNone(audit)
        self.assertEqual(audit.actor_id, self.boss.pk)
        self.assertEqual(audit.payload["old_config_version"], 1)
        self.assertEqual(audit.payload["new_config_version"], 2)
        self.assertIn("changes", audit.payload)

    def test_partial_update_merges(self):
        payload = {"password": PASSWORD, "parameters": {"commission_rate": 0.2}}
        response = self.boss_api.post(CONFIG_UPDATE_URL, payload, format="json")
        self.assertEqual(response.status_code, 200)
        # Other params preserved
        self.assertEqual(response.data["parameters"]["rounding_step"], 10)
        self.assertEqual(response.data["parameters"]["commission_rate"], 0.2)

    def test_invalid_multiplier_cap(self):
        payload = {"password": PASSWORD, "parameters": {"multiplier_cap": 5.0}}
        response = self.boss_api.post(CONFIG_UPDATE_URL, payload, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("multiplier_cap", response.data)

    def test_invalid_commission_rate(self):
        payload = {"password": PASSWORD, "parameters": {"commission_rate": 0.9}}
        response = self.boss_api.post(CONFIG_UPDATE_URL, payload, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("commission_rate", response.data)

    def test_invalid_estimate_band(self):
        payload = {"password": PASSWORD, "parameters": {"estimate_band": 0.9}}
        response = self.boss_api.post(CONFIG_UPDATE_URL, payload, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("estimate_band", response.data)

    def test_invalid_negotiation_tolerance(self):
        payload = {"password": PASSWORD, "parameters": {"negotiation": {"tolerance": 0.9}}}
        response = self.boss_api.post(CONFIG_UPDATE_URL, payload, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("negotiation", response.data)

    def test_invalid_max_offers(self):
        payload = {"password": PASSWORD, "parameters": {"negotiation": {"max_offers": 10}}}
        response = self.boss_api.post(CONFIG_UPDATE_URL, payload, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("negotiation", response.data)

    def test_invalid_offer_ttl(self):
        payload = {"password": PASSWORD, "parameters": {"negotiation": {"offer_ttl_minutes": 2}}}
        response = self.boss_api.post(CONFIG_UPDATE_URL, payload, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("negotiation", response.data)

    def test_invalid_total_ttl(self):
        payload = {"password": PASSWORD, "parameters": {"negotiation": {"total_ttl_minutes": 5}}}
        response = self.boss_api.post(CONFIG_UPDATE_URL, payload, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("negotiation", response.data)

    def test_invalid_vehicle_class_base(self):
        payload = {"password": PASSWORD, "parameters": {"vehicle_classes": {"standard": {"base": -5}}}}
        response = self.boss_api.post(CONFIG_UPDATE_URL, payload, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("vehicle_classes", response.data)

    def test_invalid_structural_config(self):
        payload = {"password": PASSWORD, "parameters": {"rounding_step": 0}}
        response = self.boss_api.post(CONFIG_UPDATE_URL, payload, format="json")
        self.assertEqual(response.status_code, 400)

    def test_rate_limiting(self):
        payload = {"password": PASSWORD, "parameters": {"commission_rate": 0.1}}
        for i in range(5):
            resp = self.boss_api.post(CONFIG_UPDATE_URL, payload, format="json")
            self.assertIn(resp.status_code, (200, 400))
        resp = self.boss_api.post(CONFIG_UPDATE_URL, payload, format="json")
        self.assertEqual(resp.status_code, 429)

    def test_password_not_in_audit_log(self):
        payload = {"password": PASSWORD, "parameters": {"commission_rate": 0.2}}
        self.boss_api.post(CONFIG_UPDATE_URL, payload, format="json")
        audit = PricingAuditLog.objects.filter(action="config_updated").first()
        self.assertIsNotNone(audit)
        self.assertNotIn("password", str(audit.payload))
        self.assertNotIn(PASSWORD, str(audit.payload))

    def test_password_not_in_response(self):
        payload = {"password": PASSWORD, "parameters": {"commission_rate": 0.2}}
        response = self.boss_api.post(CONFIG_UPDATE_URL, payload, format="json")
        self.assertNotIn("password", response.data)
