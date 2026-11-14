"""Shared helpers for the Stage 2 negotiation tests.

Kept in a separate module so the Stage 1 test helpers remain untouched.
"""
from __future__ import annotations

from datetime import date, time
from decimal import Decimal

from django.urls import reverse
from django.utils import timezone

from missions.models import Mission, MissionStatus
from missions.pricing_services import seed_default_config

from .base import api_client, make_boss, make_client, make_driver

# Mission priced at the Stage 1 standard sample: 850 DZD.
MISSION_PRICE = 850
OFFER_LOW = 730  # ceil(0.85 × 850) to 10
OFFER_HIGH = 970  # floor(1.15 × 850) to 10
EFFECTIVE_MIN_FARE = 300  # min(config standard min_fare=300, 850)
MIN_STEP = 10
MAX_OFFERS = 3
OFFER_TTL_MINUTES = 15
TOTAL_TTL_MINUTES = 30


def make_mission(client, **overrides) -> Mission:
    """Create a negotiable, pending mission directly (no pricing flow)."""
    now = timezone.now()
    defaults = {
        "client": client,
        "shipment_type": "house_moving",
        "phone_number": "0551234567",
        "truck_type": "large",
        "category": "closed",
        "capacity": 5,
        "size": "small",
        "departure_latitude": Decimal("36.753800"),
        "departure_longitude": Decimal("3.058800"),
        "arrival_latitude": Decimal("36.753800"),
        "arrival_longitude": Decimal("3.175000"),
        "distance": Decimal("12.00"),
        "date": date(2026, 11, 16),
        "time": time(10, 0),
        "workers": 0,
        "price": Decimal(MISSION_PRICE),
        "negotiable": True,
        "pricing_config_version": 1,
        "price_locked_at": now,
        "status": MissionStatus.PENDING,
    }
    defaults.update(overrides)
    return Mission.objects.create(**defaults)


def negotiation_url(mission: Mission) -> str:
    return reverse("missions:mission-negotiation", args=[mission.pk])


def offer_url(mission: Mission) -> str:
    return reverse("missions:mission-negotiation-offer", args=[mission.pk])


def accept_url(mission: Mission) -> str:
    return reverse("missions:mission-negotiation-accept", args=[mission.pk])


def decline_url(mission: Mission) -> str:
    return reverse("missions:mission-negotiation-decline", args=[mission.pk])


def make_offer_payload(amount: int = 800, key: str = "offer-key-1") -> dict:
    return {"amount": amount, "idempotency_key": key}


class NegotiationAPITestMixin:
    """Common setup: client, two approved drivers, boss and active config."""

    def setUp(self):
        super().setUp()
        seed_default_config()
        self.client_user = make_client("neg-client")
        self.other_client = make_client("neg-other-client")
        self.driver = make_driver("neg-driver")
        self.other_driver = make_driver("neg-other-driver")
        self.unapproved_driver = make_driver("neg-unapproved", approved=False)
        self.boss = make_boss("neg-boss")
        self.mission = make_mission(self.client_user)
        self.driver_api = api_client(self.driver)
        self.client_api = api_client(self.client_user)
        self.other_driver_api = api_client(self.other_driver)
        self.other_client_api = api_client(self.other_client)
        self.boss_api = api_client(self.boss)
        self.unapproved_api = api_client(self.unapproved_driver)

    def open_negotiation(self, amount: int = 800, key: str = "offer-key-1"):
        response = self.driver_api.post(
            offer_url(self.mission),
            make_offer_payload(amount, key),
            format="json",
        )
        return response

    def counter(self, api_client_obj, amount: int, key: str):
        return api_client_obj.post(
            offer_url(self.mission),
            make_offer_payload(amount, key),
            format="json",
        )

