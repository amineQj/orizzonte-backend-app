"""Pure negotiation rules (no database orchestration)."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from missions.negotiation import (
    MAX_IDEMPOTENCY_KEY_LENGTH,
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


class ParseAmountTests(TestCase):
    def test_accepts_integer_like_values(self):
        self.assertEqual(parse_amount(800), 800)
        self.assertEqual(parse_amount("800"), 800)
        self.assertEqual(parse_amount(" 800 "), 800)
        self.assertEqual(parse_amount("800.0"), 800)
        self.assertEqual(parse_amount(Decimal("800")), 800)
        self.assertEqual(parse_amount(800.0), 800)

    def test_rejects_non_integral_values(self):
        for value in (800.5, "800.5", Decimal("800.5")):
            with self.subTest(value=value):
                with self.assertRaises(NegotiationError) as ctx:
                    parse_amount(value)
                self.assertEqual(ctx.exception.code, "invalid_offer_amount")
                self.assertEqual(ctx.exception.http_status, 400)

    def test_rejects_booleans_and_missing_values(self):
        for value in (True, False, None, "", "abc", [], {}):
            with self.subTest(value=value):
                with self.assertRaises(NegotiationError) as ctx:
                    parse_amount(value)
                self.assertEqual(ctx.exception.code, "invalid_offer_amount")

    def test_rejects_non_positive_and_oversized_values(self):
        for value in (0, -10, 2_147_483_648):
            with self.subTest(value=value):
                with self.assertRaises(NegotiationError) as ctx:
                    parse_amount(value)
                self.assertEqual(ctx.exception.code, "invalid_offer_amount")


class ParseIdempotencyKeyTests(TestCase):
    def test_accepts_opaque_strings(self):
        self.assertEqual(parse_idempotency_key("abc-123"), "abc-123")
        self.assertEqual(
            parse_idempotency_key("x" * MAX_IDEMPOTENCY_KEY_LENGTH),
            "x" * MAX_IDEMPOTENCY_KEY_LENGTH,
        )

    def test_rejects_missing_or_invalid_keys(self):
        for value in (None, "", "   ", 123, [], {}):
            with self.subTest(value=value):
                with self.assertRaises(NegotiationError) as ctx:
                    parse_idempotency_key(value)
                self.assertEqual(ctx.exception.code, "invalid_idempotency_key")

    def test_rejects_too_long_keys(self):
        with self.assertRaises(NegotiationError) as ctx:
            parse_idempotency_key("x" * (MAX_IDEMPOTENCY_KEY_LENGTH + 1))
        self.assertEqual(ctx.exception.code, "invalid_idempotency_key")


class BoundsTests(TestCase):
    def test_allowed_range_is_rounded_inwards(self):
        low, high = allowed_range(850, Decimal("0.15"), 10)
        self.assertEqual((low, high), (730, 970))

    def test_allowed_range_exact_values(self):
        low, high = allowed_range(1000, Decimal("0.15"), 10)
        self.assertEqual((low, high), (850, 1150))

    def test_allowed_range_small_price(self):
        low, high = allowed_range(200, Decimal("0.15"), 10)
        self.assertEqual((low, high), (170, 230))

    def test_allowed_range_rejects_non_positive_price(self):
        with self.assertRaises(NegotiationError) as ctx:
            allowed_range(0, Decimal("0.15"), 10)
        self.assertEqual(ctx.exception.code, "mission_not_negotiable")
        self.assertEqual(ctx.exception.http_status, 409)

    def test_effective_min_fare_is_capped_by_mission_price(self):
        self.assertEqual(effective_min_fare(850, 300), 300)
        self.assertEqual(effective_min_fare(200, 300), 200)


class TurnTests(TestCase):
    def test_driver_moves_first_then_alternates(self):
        self.assertEqual(expected_role(None), "driver")
        self.assertEqual(expected_role("driver"), "client")
        self.assertEqual(expected_role("client"), "driver")


class ValidateOfferTests(TestCase):
    def validate(self, amount, **overrides):
        params = {
            "initial_price": 850,
            "tolerance": Decimal("0.15"),
            "min_step": 10,
            "min_fare": 300,
            "requester_role": "driver",
        }
        params.update(overrides)
        return validate_offer_amount(amount, **params)

    def test_first_offer_only_needs_range_and_step(self):
        self.assertEqual(self.validate(800), (730, 970))

    def test_step_alignment_is_checked(self):
        with self.assertRaises(NegotiationError) as ctx:
            self.validate(805)
        self.assertEqual(ctx.exception.code, "offer_not_aligned")

    def test_range_is_inclusive(self):
        self.validate(730)
        self.validate(970)
        for amount in (720, 980):
            with self.subTest(amount=amount):
                with self.assertRaises(NegotiationError) as ctx:
                    self.validate(amount)
                self.assertEqual(ctx.exception.code, "offer_out_of_range")

    def test_min_fare_is_enforced(self):
        with self.assertRaises(NegotiationError) as ctx:
            self.validate(
                180,
                initial_price=200,
                min_fare=200,
                requester_role="driver",
            )
        self.assertEqual(ctx.exception.code, "offer_below_min_fare")

    def test_client_must_lower_by_at_least_min_step(self):
        self.validate(
            780,
            last_amount=800,
            last_author_role="driver",
            requester_role="client",
        )
        self.validate(
            790,
            last_amount=800,
            last_author_role="driver",
            requester_role="client",
        )
        for amount in (800, 810):
            with self.subTest(amount=amount):
                with self.assertRaises(NegotiationError) as ctx:
                    self.validate(
                        amount,
                        last_amount=800,
                        last_author_role="driver",
                        requester_role="client",
                    )
                self.assertEqual(ctx.exception.code, "offer_not_monotonic")

    def test_driver_must_raise_by_at_least_min_step(self):
        self.validate(
            810,
            last_amount=800,
            last_author_role="client",
            requester_role="driver",
        )
        self.validate(
            820,
            last_amount=800,
            last_author_role="client",
            requester_role="driver",
        )
        for amount in (800, 790):
            with self.subTest(amount=amount):
                with self.assertRaises(NegotiationError) as ctx:
                    self.validate(
                        amount,
                        last_amount=800,
                        last_author_role="client",
                        requester_role="driver",
                    )
                self.assertEqual(ctx.exception.code, "offer_not_monotonic")

    def test_min_step_gap_is_enforced_independently_of_direction(self):
        with self.assertRaises(NegotiationError) as ctx:
            self.validate(
                800,
                min_step=5,
                last_amount=800,
                last_author_role="driver",
                requester_role="client",
            )
        self.assertEqual(ctx.exception.code, "offer_not_monotonic")


class TimeRuleTests(TestCase):
    def test_total_ttl_and_hold_expiry(self):
        now = timezone.now()
        future = now + timedelta(minutes=5)
        past = now - timedelta(minutes=1)

        self.assertFalse(
            is_expired(expires_at=future, hold_expires_at=future, now=now)
        )
        self.assertTrue(
            is_expired(expires_at=past, hold_expires_at=future, now=now)
        )
        self.assertTrue(
            is_expired(expires_at=future, hold_expires_at=past, now=now)
        )
        self.assertFalse(
            is_expired(expires_at=future, hold_expires_at=None, now=now)
        )

    def test_next_hold_deadline_is_capped_by_total_ttl(self):
        now = timezone.now()
        expires = now + timedelta(minutes=10)
        self.assertEqual(
            next_hold_deadline(now, offer_ttl_minutes=15, expires_at=expires),
            expires,
        )
        self.assertEqual(
            next_hold_deadline(
                now,
                offer_ttl_minutes=5,
                expires_at=now + timedelta(minutes=30),
            ),
            now + timedelta(minutes=5),
        )
