"""Hand-computed tests for the pure pricing engine (no database)."""
from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from django.test import SimpleTestCase

from missions import pricing as p

CONFIG_PATH = Path(__file__).resolve().parents[1] / "data" / "pricing_v1.json"


def load_config() -> p.Configuration:
    with CONFIG_PATH.open(encoding="utf-8") as handle:
        return p.Configuration.from_mapping(json.load(handle))


def monday(hour: int = 10, minute: int = 0) -> datetime:
    # 2026-11-16 is a Monday (no weekend multiplier).
    return datetime(2026, 11, 16, hour, minute)


def friday(hour: int = 8, minute: int = 0) -> datetime:
    # 2026-11-20 is a Friday (Algerian weekend).
    return datetime(2026, 11, 20, hour, minute)


def sunday(hour: int = 10) -> datetime:
    return datetime(2026, 11, 22, hour, 0)


class EstimateMathTests(SimpleTestCase):
    def setUp(self):
        self.config = load_config()

    def estimate(self, **overrides):
        params = {
            "vehicle_class": "standard",
            "distance_km": Decimal("10"),
            "duration_min": 20,
            "helpers": 0,
            "floors": 0,
            "handling_minutes": 0,
            "pickup_at": monday(),
        }
        params.update(overrides)
        return p.compute_estimate(self.config, **params)

    def test_standard_estimate_is_hand_computed(self):
        result = self.estimate()
        # 150 base + 10 × 50 + 20 × 5 = 750
        self.assertEqual(result.amount, 750)
        self.assertEqual(result.low, 670)   # floor(675 / 10) × 10
        self.assertEqual(result.high, 830)  # ceil(825 / 10) × 10
        self.assertEqual(result.components["subtotal"], 750)
        self.assertFalse(result.components["min_fare_applied"])

    def test_van_estimate(self):
        result = self.estimate(vehicle_class="van", distance_km=Decimal("10"))
        # 400 + 900 + 160 = 1460
        self.assertEqual(result.amount, 1460)
        self.assertEqual(result.low, 1310)
        self.assertEqual(result.high, 1610)

    def test_truck_min_fare_applies(self):
        result = self.estimate(
            vehicle_class="truck",
            distance_km=Decimal("5"),
            duration_min=10,
        )
        # 800 + 700 + 120 = 1620 < 2500
        self.assertEqual(result.amount, 2500)
        self.assertTrue(result.components["min_fare_applied"])
        self.assertEqual(result.low, 2500)
        self.assertEqual(result.high, 2750)

    def test_helpers_and_floors(self):
        result = self.estimate(
            helpers=2,
            floors=2,
            handling_minutes=90,
        )
        # helpers: 2 × 600 × 1.5 = 1800; floors: 2 × 150 = 300
        self.assertEqual(result.components["helpers"], 1800)
        self.assertEqual(result.components["floors"], 300)
        self.assertEqual(result.components["extras"], 2100)
        self.assertEqual(result.amount, 2850)

    def test_weekend_peak_multiplier_multiplies_then_rounds(self):
        result = self.estimate(
            helpers=2,
            floors=2,
            handling_minutes=90,
            pickup_at=friday(8),
        )
        # 1.05 (weekend) × 1.15 (peak) = 1.2075
        self.assertEqual(result.components["multiplier"], "1.2075")
        self.assertEqual(
            result.components["multiplier_labels"], ["weekend", "peak"]
        )
        # 2850 × 1.2075 = 3441.375 → 3440
        self.assertEqual(result.amount, 3440)
        self.assertEqual(result.low, 3090)
        self.assertEqual(result.high, 3790)

    def test_weekend_night_multiplier(self):
        result = self.estimate(
            helpers=2,
            floors=2,
            handling_minutes=90,
            pickup_at=friday(23),
        )
        # 1.05 × 1.10 = 1.155; 2850 × 1.155 = 3291.75 → 3290
        self.assertEqual(result.components["multiplier"], "1.155")
        self.assertEqual(
            result.components["multiplier_labels"], ["weekend", "night"]
        )
        self.assertEqual(result.amount, 3290)

    def test_manual_multiplier_is_capped(self):
        result = self.estimate(
            helpers=2,
            floors=2,
            handling_minutes=90,
            manual_multiplier=Decimal("2.0"),
        )
        self.assertEqual(result.components["multiplier"], "1.5")
        self.assertTrue(result.components["multiplier_capped"])
        self.assertEqual(result.amount, 4280)  # 2850 × 1.5 = 4275 → 4280

    def test_normal_band_and_low_confidence_band(self):
        normal = self.estimate()
        self.assertEqual((normal.low, normal.high), (670, 830))
        self.assertEqual(normal.components["band"], "0.1")

        low = self.estimate(confidence=p.CONFIDENCE_LOW)
        self.assertEqual(low.components["band"], "0.15")
        self.assertEqual(low.low, 630)   # floor(637.5 / 10)
        self.assertEqual(low.high, 870)  # ceil(862.5 / 10)

    def test_calibrated_flag_comes_from_config(self):
        self.assertFalse(self.estimate().calibrated)
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        data["calibrated"] = True
        calibrated = p.Configuration.from_mapping(data)
        result = p.compute_estimate(
            calibrated,
            vehicle_class="standard",
            distance_km=Decimal("10"),
            duration_min=20,
            helpers=0,
            floors=0,
            handling_minutes=0,
            pickup_at=monday(),
        )
        self.assertTrue(result.calibrated)

    def test_estimate_is_deterministic(self):
        self.assertEqual(self.estimate(), self.estimate())


class TimeWindowTests(SimpleTestCase):
    def setUp(self):
        self.config = load_config()

    def labels(self, moment):
        _multiplier, labels = p.apply_time_multiplier(self.config, moment)
        return labels

    def test_peak_window_is_start_inclusive_end_exclusive(self):
        self.assertIn("peak", self.labels(monday(7, 0)))
        self.assertNotIn("peak", self.labels(monday(9, 0)))
        self.assertIn("peak", self.labels(monday(16, 0)))
        self.assertNotIn("peak", self.labels(monday(19, 0)))
        self.assertNotIn("peak", self.labels(monday(12, 0)))

    def test_night_window_wraps_midnight(self):
        self.assertIn("night", self.labels(monday(22, 0)))
        self.assertIn("night", self.labels(monday(23, 59)))
        self.assertIn("night", self.labels(monday(4, 59)))
        self.assertNotIn("night", self.labels(monday(5, 0)))
        self.assertNotIn("night", self.labels(monday(12, 0)))

    def test_weekend_is_friday_and_saturday(self):
        self.assertIn("weekend", self.labels(friday(12)))
        self.assertIn("weekend", self.labels(datetime(2026, 11, 21, 12, 0)))
        self.assertNotIn("weekend", self.labels(sunday(12)))


class InputResolutionTests(SimpleTestCase):
    def setUp(self):
        self.config = load_config()

    def test_vehicle_class_precedence(self):
        self.assertEqual(p.resolve_vehicle_class("truck", "small", 1), "truck")
        self.assertEqual(p.resolve_vehicle_class(None, "big", 1), "truck")
        self.assertEqual(p.resolve_vehicle_class(None, "medium", 1), "van")
        self.assertEqual(p.resolve_vehicle_class(None, "small", 9), "standard")

    def test_capacity_only_used_without_size(self):
        self.assertEqual(p.resolve_vehicle_class(None, None, "1.0"), "standard")
        self.assertEqual(p.resolve_vehicle_class(None, None, "2.0"), "van")
        self.assertEqual(p.resolve_vehicle_class(None, None, "10"), "truck")
        with self.assertRaises(p.PricingError):
            p.resolve_vehicle_class(None, None, None)

    def test_distance_bounds_are_enforced(self):
        straight = p.haversine_km("36.7538", "3.0588", "36.4700", "2.8300")
        valid, confidence = p.resolve_distance(
            self.config,
            provided_km=(straight * Decimal("1.5")).quantize(Decimal("0.01")),
            departure_lat="36.7538",
            departure_lon="3.0588",
            arrival_lat="36.4700",
            arrival_lon="2.8300",
        )
        self.assertEqual(confidence, p.CONFIDENCE_NORMAL)
        self.assertGreater(valid, straight)

        for factor in ("0.5", "3.5"):
            with self.assertRaises(p.PricingError) as ctx:
                p.resolve_distance(
                    self.config,
                    provided_km=(straight * Decimal(factor)).quantize(
                        Decimal("0.01")
                    ),
                    departure_lat="36.7538",
                    departure_lon="3.0588",
                    arrival_lat="36.4700",
                    arrival_lon="2.8300",
                )
            self.assertEqual(ctx.exception.code, "invalid_distance")

    def test_distance_fallback_is_low_confidence(self):
        straight = p.haversine_km("36.7538", "3.0588", "36.4700", "2.8300")
        distance, confidence = p.resolve_distance(
            self.config,
            provided_km=None,
            departure_lat="36.7538",
            departure_lon="3.0588",
            arrival_lat="36.4700",
            arrival_lon="2.8300",
        )
        self.assertEqual(confidence, p.CONFIDENCE_LOW)
        self.assertEqual(
            distance, (straight * self.config.fallback_detour_factor).quantize(Decimal("0.01"))
        )

    def test_duration_fallback_uses_placeholder_speed(self):
        minutes, confidence = p.resolve_duration(
            self.config, provided_min=None, distance_km=Decimal("10")
        )
        self.assertEqual(minutes, 20)  # 10 / 30 × 60
        self.assertEqual(confidence, p.CONFIDENCE_LOW)

    def test_helpers_cap(self):
        p.validate_estimate_inputs(
            self.config,
            vehicle_class="standard",
            distance_km=Decimal("10"),
            duration_min=20,
            helpers=4,
            floors=0,
            handling_minutes=0,
        )
        with self.assertRaises(p.PricingError) as ctx:
            p.validate_estimate_inputs(
                self.config,
                vehicle_class="standard",
                distance_km=Decimal("10"),
                duration_min=20,
                helpers=5,
                floors=0,
                handling_minutes=0,
            )
        self.assertEqual(ctx.exception.code, "invalid_helpers")

    def test_legacy_helpers_allow_five_workers(self):
        p.validate_estimate_inputs(
            self.config,
            vehicle_class="standard",
            distance_km=Decimal("10"),
            duration_min=20,
            helpers=5,
            floors=0,
            handling_minutes=0,
            helpers_from_legacy=True,
        )

    def test_invalid_configuration_is_rejected(self):
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        del data["vehicle_classes"]
        with self.assertRaises(p.PricingError) as ctx:
            p.Configuration.from_mapping(data)
        self.assertEqual(ctx.exception.code, "invalid_pricing_config")


class MoneyHelperTests(SimpleTestCase):
    def test_round_to_step_half_up(self):
        self.assertEqual(p.round_to_step(Decimal("3445"), 10), 3450)
        self.assertEqual(p.round_to_step(Decimal("3444"), 10), 3440)
        self.assertEqual(p.floor_to_step(Decimal("3449.99"), 10), 3440)
        self.assertEqual(p.ceil_to_step(Decimal("3440.01"), 10), 3450)

    def test_commission_never_loses_a_dinar(self):
        self.assertEqual(p.commission_split(750, Decimal("0.10")), (75, 675))
        self.assertEqual(p.commission_split(755, Decimal("0.10")), (76, 679))
        for price in (0, 1, 999, 1000, 12345):
            commission, net = p.commission_split(price, Decimal("0.10"))
            self.assertEqual(commission + net, price)

    def test_float_ratios_never_enter_money_math(self):
        # 0.1-style artefacts must not appear: all money is int.
        result = p.commission_split(3, Decimal("0.1"))
        self.assertEqual(result, (0, 3))
        self.assertIsInstance(result[0], int)
