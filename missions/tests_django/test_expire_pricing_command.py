"""The ``expire_pricing`` management command and lazy-expiry safety net."""
from __future__ import annotations

from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from missions.models import Mission, Negotiation, PricingAuditLog

from .negotiation_base import NegotiationAPITestMixin


class ExpirePricingCommandTests(NegotiationAPITestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.open_negotiation(800, "expire-open").status_code, 201)

    def _run(self, *args, now=None):
        out = StringIO()
        call_command("expire_pricing", *args, now=now, stdout=out)
        return out.getvalue()

    def test_expires_a_stale_negotiation_and_clears_the_hold(self):
        stale_now = timezone.now() - timedelta(minutes=1)
        Mission.objects.filter(pk=self.mission.pk).update(
            hold_expires_at=stale_now
        )

        output = self._run(now=timezone.now().isoformat())

        self.assertIn("Expired 1 negotiation", output)
        negotiation = Negotiation.objects.get()
        self.assertEqual(negotiation.status, Negotiation.Status.EXPIRED)
        self.assertIsNotNone(negotiation.closed_at)
        self.mission.refresh_from_db()
        self.assertIsNone(self.mission.held_by_id)
        self.assertIsNone(self.mission.hold_expires_at)
        self.assertTrue(
            PricingAuditLog.objects.filter(action="negotiation_expired").exists()
        )

    def test_dry_run_writes_nothing(self):
        stale_now = timezone.now() - timedelta(minutes=1)
        Mission.objects.filter(pk=self.mission.pk).update(
            hold_expires_at=stale_now
        )

        output = self._run("--dry-run", now=timezone.now().isoformat())

        self.assertIn("Would expire 1 negotiation", output)
        self.assertEqual(
            Negotiation.objects.get().status, Negotiation.Status.OPEN
        )
        self.mission.refresh_from_db()
        self.assertIsNotNone(self.mission.held_by_id)

    def test_active_negotiation_is_untouched(self):
        output = self._run(now=timezone.now().isoformat())
        self.assertIn("Expired 0 negotiation", output)
        self.assertEqual(
            Negotiation.objects.get().status, Negotiation.Status.OPEN
        )
        self.mission.refresh_from_db()
        self.assertIsNotNone(self.mission.held_by_id)

    def test_expires_an_absolute_ttl_even_when_the_hold_is_later(self):
        stale = timezone.now() - timedelta(minutes=2)
        Negotiation.objects.filter(mission=self.mission).update(expires_at=stale)
        Mission.objects.filter(pk=self.mission.pk).update(
            hold_expires_at=timezone.now() + timedelta(minutes=10)
        )

        output = self._run(now=timezone.now().isoformat())

        self.assertIn("Expired 1 negotiation", output)
        self.assertEqual(
            Negotiation.objects.get().status, Negotiation.Status.EXPIRED
        )
        self.mission.refresh_from_db()
        self.assertIsNone(self.mission.held_by_id)

    def test_orphan_hold_is_cleared(self):
        Mission.objects.filter(pk=self.mission.pk).update(
            held_by=self.driver,
            hold_expires_at=timezone.now() - timedelta(minutes=1),
        )
        Negotiation.objects.filter(mission=self.mission).update(
            status=Negotiation.Status.DECLINED
        )

        output = self._run(now=timezone.now().isoformat())

        self.assertIn("cleared 1 stale hold", output)
        self.mission.refresh_from_db()
        self.assertIsNone(self.mission.held_by_id)
        self.assertIsNone(self.mission.hold_expires_at)

    def test_invalid_now_is_a_command_error(self):
        with self.assertRaises(CommandError):
            call_command("expire_pricing", now="not-a-date")
