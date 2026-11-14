"""Expire stale negotiations and release expired mission holds.

Usage:
    python manage.py expire_pricing
    python manage.py expire_pricing --dry-run
    python manage.py expire_pricing --now 2026-11-14T12:00:00+01:00

Expiry is primarily lazy (it happens when a negotiation endpoint is called).
This command is the periodic safety net for abandoned negotiations. Quotes
remain governed by their own ``expires_at`` check at confirmation time.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from missions.negotiation_services import expire_stale_negotiations


class Command(BaseCommand):
    help = "Expire stale negotiations and clear expired mission holds."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would expire without writing anything.",
        )
        parser.add_argument(
            "--now",
            dest="now",
            default=None,
            help="Override the current time with an ISO-8601 datetime.",
        )

    def handle(self, *args, **options):
        now = timezone.now()
        if options["now"]:
            parsed = parse_datetime(options["now"])
            if parsed is None:
                raise CommandError(
                    "--now must be an ISO-8601 datetime, e.g. 2026-11-14T12:00:00+01:00."
                )
            if timezone.is_naive(parsed):
                parsed = timezone.make_aware(
                    parsed, timezone.get_current_timezone()
                )
            now = parsed

        result = expire_stale_negotiations(
            now=now, dry_run=bool(options["dry_run"])
        )
        prefix = "Would expire" if options["dry_run"] else "Expired"
        self.stdout.write(
            self.style.SUCCESS(
                f"{prefix} {result['negotiations_expired']} negotiation(s); "
                f"cleared {result['holds_cleared']} stale hold(s)."
            )
        )
