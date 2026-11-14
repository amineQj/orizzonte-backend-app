"""Seed the placeholder pricing configuration.

Usage:
    python manage.py seed_pricing
    python manage.py seed_pricing --activate
    python manage.py seed_pricing --config-version 2 --json path/to/config.json --force

The bundled values are PLACEHOLDERS and are seeded with ``calibrated=false``.
Re-running without ``--force`` never overwrites an existing version, so boss
edits made in the admin are safe.
"""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from missions.models import PricingConfig
from missions.pricing import Configuration, PricingError
from missions.pricing_services import (
    DEFAULT_CONFIG_PATH,
    load_default_parameters,
    seed_default_config,
)


class Command(BaseCommand):
    help = "Seed or refresh the pricing configuration (placeholder values, calibrated=false)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--config-version",
            dest="config_version",
            type=int,
            default=None,
            help="Configuration version to create (defaults to the JSON version).",
        )
        parser.add_argument(
            "--json",
            dest="json_path",
            default=None,
            help="Path to a JSON configuration (defaults to missions/data/pricing_v1.json).",
        )
        parser.add_argument(
            "--activate",
            action="store_true",
            help="Make the seeded version the active configuration.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Overwrite the parameters of an existing version.",
        )

    def handle(self, *args, **options):
        json_path = options["json_path"]
        if json_path:
            try:
                with open(json_path, encoding="utf-8") as handle:
                    data = json.load(handle)
            except OSError as exc:
                raise CommandError(f"Cannot read {json_path}: {exc}") from exc
            except json.JSONDecodeError as exc:
                raise CommandError(f"Invalid JSON in {json_path}: {exc}") from exc
        else:
            data = load_default_parameters()

        version = options["config_version"] or int(data.get("version", 1))
        data = {**data, "version": version}

        try:
            Configuration.from_mapping(data)
        except PricingError as exc:
            raise CommandError(f"Invalid pricing configuration: {exc.message}") from exc

        activate = options["activate"] or not PricingConfig.objects.filter(
            is_active=True
        ).exists()
        config = seed_default_config(
            version=version,
            parameters=data,
            activate=activate,
            force=options["force"],
        )

        active = PricingConfig.objects.filter(is_active=True).order_by("-version").first()
        self.stdout.write(
            self.style.SUCCESS(
                f"Pricing config v{config.version} ready "
                f"(calibrated={str(config.calibrated).lower()}, "
                f"active={str(config.is_active).lower()})."
            )
        )
        if not config.calibrated:
            self.stdout.write(
                self.style.WARNING(
                    "Values are placeholders, not market-calibrated. "
                    "Set calibrated=true only after real price calibration."
                )
            )
        if active is not None:
            self.stdout.write(f"Active configuration: v{active.version}.")
        if not options["json_path"]:
            self.stdout.write(f"Source: {DEFAULT_CONFIG_PATH}")
