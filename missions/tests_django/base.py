"""Shared helpers for the Django-discovered pricing tests.

These tests are deliberately self-contained: they do not import the legacy
pytest-style helpers in ``missions/tests`` (that package has no
``__init__.py`` and is not discovered by ``manage.py test``).
"""
from __future__ import annotations

from rest_framework.test import APIClient

from accounts.models import User
from drivers.models import DriverProfile

PASSWORD = "TestPassword123!"

# Algiers centre → a point ~10 km east (same latitude). The straight-line
# distance is close to 10.36 km; a 12 km client distance stays inside the
# configured [1.0, 3.0] ratio band.
DEPARTURE_LAT = "36.753800"
DEPARTURE_LON = "3.058800"
ARRIVAL_LAT = "36.753800"
ARRIVAL_LON = "3.175000"


def make_user(role: str, username: str) -> User:
    return User.objects.create_user(
        username=username,
        email=f"{username}@example.dz",
        password=PASSWORD,
        role=role,
    )


def make_client(username: str = "pricing-client") -> User:
    return make_user("client", username)


def make_boss(username: str = "pricing-boss") -> User:
    return make_user("boss", username)


def make_driver(username: str = "pricing-driver", approved: bool = True) -> User:
    user = make_user("driver", username)
    DriverProfile.objects.create(
        user=user,
        status=(
            DriverProfile.Status.APPROVED
            if approved
            else DriverProfile.Status.INCOMPLETE
        ),
    )
    return user


def api_client(user: User | None = None) -> APIClient:
    client = APIClient()
    if user is not None:
        client.force_authenticate(user=user)
    return client


def legacy_estimate_payload(**overrides) -> dict:
    """The exact historical estimate payload (327 km Algiers → Constantine)."""
    payload = {
        "truck_type": "large",
        "category": "closed",
        "capacity": 5,
        "size": "medium",
        "departure_latitude": "36.737232",
        "departure_longitude": "3.086472",
        "arrival_latitude": "36.365000",
        "arrival_longitude": "6.614000",
        "distance": "300.00",
        "date": "2024-08-01",
        "time": "09:00",
        "workers": 2,
    }
    payload.update(overrides)
    return payload


def new_estimate_payload(**overrides) -> dict:
    """New-engine estimate: standard vehicle, 12 km, 20 min, Monday 10:00."""
    payload = {
        "truck_type": "large",
        "category": "closed",
        "capacity": 5,
        "departure_latitude": DEPARTURE_LAT,
        "departure_longitude": DEPARTURE_LON,
        "arrival_latitude": ARRIVAL_LAT,
        "arrival_longitude": ARRIVAL_LON,
        "distance": "12.00",
        "date": "2026-11-16",
        "time": "10:00",
        "workers": 0,
        "vehicle_class": "standard",
        "helpers": 0,
        "floors": 0,
        "duration_min": 20,
    }
    payload.update(overrides)
    return payload


def confirm_payload(**overrides) -> dict:
    """Legacy-shaped confirmation payload (adds the contact fields)."""
    payload = legacy_estimate_payload()
    payload.update(
        {
            "shipment_type": "house_moving",
            "phone_number": "0551234567",
        }
    )
    payload.update(overrides)
    return payload


def new_confirm_payload(**overrides) -> dict:
    """New-engine confirmation payload without a quote."""
    payload = new_estimate_payload()
    payload.update(
        {
            "shipment_type": "house_moving",
            "phone_number": "0551234567",
        }
    )
    payload.update(overrides)
    return payload


def quote_confirm_payload(quote_id, **overrides) -> dict:
    """Confirmation that relies entirely on the quote snapshot."""
    payload = {
        "shipment_type": "house_moving",
        "phone_number": "0551234567",
        "truck_type": "large",
        "category": "closed",
        "capacity": 5,
        "departure_latitude": DEPARTURE_LAT,
        "departure_longitude": DEPARTURE_LON,
        "arrival_latitude": ARRIVAL_LAT,
        "arrival_longitude": ARRIVAL_LON,
        "quote_id": str(quote_id),
    }
    payload.update(overrides)
    return payload


# Small helpers used by assertions.
STANDARD_12KM_20MIN = 850  # 150 base + 12 km × 50 + 20 min × 5
COMMISSION_10_PCT_850 = 85
DRIVER_NET_850 = 765
