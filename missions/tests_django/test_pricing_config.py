"""Configuration lifecycle tests: seeding, activation, validation, rules."""
from __future__ import annotations

import copy
import json
from decimal import Decimal
from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase

from missions.models import PricingConfig
from missions.pricing_services import (
    config_from_model,
    load_default_parameters,
    public_rules,
    seed_default_config,
)

CONFIG_PATH = Path(__file__).resolve().parents[1] / "data" / "pricing_v1.json"


class SeedPricingCommandTests(TestCase):
    def test_seed_is_idempotent_and_not_calibrated(self):
        call_command("seed_pricing", verbosity=0)
        self.assertEqual(PricingConfig.objects.count(), 1)
        config = PricingConfig.objects.get()
        self.assertTrue(config.is_active)
        self.assertFalse(config.calibrated)

        call_command("seed_pricing", verbosity=0)
        self.assertEqual(PricingConfig.objects.count(), 1)
        self.assertEqual(config_from_model(config).version, 1)

    def test_seed_does_not_overwrite_boss_edits_without_force(self):
        config = seed_default_config()
        parameters = copy.deepcopy(config.parameters)
        parameters["commission_rate"] = 0.25
        config.parameters = parameters
        config.save()

        seed_default_config()  # no force
        config.refresh_from_db()
        self.assertEqual(Decimal(str(config.parameters["commission_rate"])), Decimal("0.25"))

        seed_default_config(force=True)
        config.refresh_from_db()
        self.assertEqual(Decimal(str(config.parameters["commission_rate"])), Decimal("0.10"))

    def test_seeding_a_new_version_activates_it(self):
        seed_default_config(version=1)
        call_command("seed_pricing", config_version=2, activate=True, verbosity=0)

        self.assertEqual(PricingConfig.objects.count(), 2)
        active = PricingConfig.objects.get(is_active=True)
        self.assertEqual(active.version, 2)
        self.assertFalse(PricingConfig.objects.get(version=1).is_active)

    def test_activation_deactivates_previous_rows(self):
        parameters = load_default_parameters()
        first = PricingConfig.objects.create(
            version=10, parameters=parameters, is_active=True
        )
        PricingConfig.objects.create(version=11, parameters=parameters, is_active=True)

        first.refresh_from_db()
        self.assertFalse(first.is_active)
        self.assertEqual(
            PricingConfig.objects.filter(is_active=True).count(), 1
        )

    def test_invalid_parameters_fail_validation(self):
        config = PricingConfig(version=99, parameters={"version": 99})
        with self.assertRaises(ValidationError):
            config.full_clean()

    def test_model_version_is_authoritative(self):
        parameters = load_default_parameters()
        parameters["version"] = 999
        config = PricingConfig.objects.create(
            version=7, parameters=parameters, is_active=True
        )
        self.assertEqual(config_from_model(config).version, 7)


class BundledConfigTests(TestCase):
    def test_placeholder_config_is_explicitly_not_calibrated(self):
        with CONFIG_PATH.open(encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertFalse(data["calibrated"])
        self.assertIn("fallback_speed_kmh", data)
        self.assertEqual(data.get("currency", "DZD"), "DZD")


class PublicRulesTests(TestCase):
    def setUp(self):
        self.config = seed_default_config()

    def test_rules_expose_negotiation_and_rounding(self):
        rules = public_rules(self.config)
        self.assertFalse(rules["calibrated"])
        self.assertEqual(rules["rounding_step"], 10)
        self.assertEqual(rules["currency"], "DZD")
        self.assertEqual(rules["negotiation"]["tolerance"], "0.15")
        self.assertEqual(rules["negotiation"]["max_offers"], 3)
        self.assertEqual(rules["negotiation"]["min_step"], 10)
        self.assertEqual(rules["quote_ttl_minutes"], 30)

    def test_rules_never_expose_commission_rate(self):
        rules = public_rules(self.config)
        self.assertNotIn("commission_rate", rules)
        self.assertNotIn("commission_amount", rules)
        # The rate must not leak inside nested objects either.
        self.assertNotIn("commission_rate", json.dumps(rules))

    def test_rules_are_serialised_as_strings(self):
        # Decimal values are serialised as strings, not floats.
        rules = public_rules(self.config)
        self.assertEqual(rules["time_multipliers"]["peak"], "1.15")
        self.assertEqual(rules["multiplier_cap"], "1.5")
