"""
drivers/tests/base.py
~~~~~~~~~~~~~~~~~~~~~

Shared helpers for the drivers app test-suite.

Every test class derives from :class:`DriverAPITestCase`, which:

* isolates ``PRIVATE_MEDIA_ROOT`` in a throw-away temp directory,
* sets a known ``AI_AGENT_API_KEY`` (and disables auto-approval),
* clears the cache so the scoped throttles never leak between tests.
"""
import io
import os
import shutil
import tempfile
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image
from rest_framework.test import APIClient

from drivers.constants import DOCUMENT_TYPES
from drivers.models import DriverProfile
from drivers.services import get_or_create_profile

User = get_user_model()

PASSWORD = "TestPassword123!"
AGENT_KEY = "unit-test-agent-key-0123456789abcdef"


# ─────────────────────────────────────────────────────────────
#  Users / auth
# ─────────────────────────────────────────────────────────────

def make_user(role, *, username=None, email=None, verified=True, password=PASSWORD):
    username = username or f"{role}_user"
    email = email or f"{username}@example.com"
    return User.objects.create_user(
        username=username,
        email=email,
        password=password,
        role=role,
        email_verified=verified,
    )


def auth_client(user, password=PASSWORD):
    client = APIClient()
    response = client.post(
        reverse("token_obtain_pair"),
        {"email": user.email, "password": password},
        format="json",
    )
    assert response.status_code == 200, response.content
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['access']}")
    return client


def agent_client(key=AGENT_KEY):
    client = APIClient()
    client.credentials(HTTP_X_AGENT_KEY=key)
    return client


# ─────────────────────────────────────────────────────────────
#  Profiles
# ─────────────────────────────────────────────────────────────

def make_profile(user, *, complete=False, status=None, **fields):
    """Create/update the driver profile with valid values by default."""
    profile = get_or_create_profile(user)
    defaults = {
        "full_name": "Karim Benali",
        "phone": "0550000001",
        "date_of_birth": date(1990, 5, 20),
        "national_id_number": f"NID-{user.pk}",
        "driving_license_number": f"DL-{user.pk}",
        "driving_license_expiry": date.today() + timedelta(days=365),
        "vehicle_plate": f"PLT-{user.pk}",
    }
    defaults.update(fields)
    for key, value in defaults.items():
        setattr(profile, key, value)

    if complete:
        for name in DOCUMENT_TYPES:
            setattr(profile, name, f"documents/{name}-{user.pk}.jpg")

    if status is not None:
        profile.status = status

    profile.save()
    return profile


# ─────────────────────────────────────────────────────────────
#  Images / uploads
# ─────────────────────────────────────────────────────────────

_FORMAT_EXT = {
    "PNG": ("png", "image/png"),
    "JPEG": ("jpg", "image/jpeg"),
    "WEBP": ("webp", "image/webp"),
}


def image_bytes(fmt="PNG", size=(16, 16), color=(200, 40, 40), exif=None):
    buffer = io.BytesIO()
    if exif is not None and hasattr(exif, "tobytes"):
        exif = exif.tobytes()
    image = Image.new("RGB", size, color)
    if exif is None:
        image.save(buffer, format=fmt)
    else:
        image.save(buffer, format=fmt, exif=exif)
    return buffer.getvalue()


def document_file(fmt="PNG", name=None, size=(16, 16), exif=None):
    ext, content_type = _FORMAT_EXT[fmt]
    data = image_bytes(fmt=fmt, size=size, exif=exif)
    return SimpleUploadedFile(name or f"document.{ext}", data, content_type=content_type)


def upload_document(client, doc_type, fmt="PNG", name=None, exif=None, size=(16, 16)):
    return client.post(
        reverse("driver-document-upload"),
        {"type": doc_type, "file": document_file(fmt=fmt, name=name, exif=exif, size=size)},
        format="multipart",
    )


def download_document(client, user_id, doc_type):
    return client.get(reverse("driver-document", args=[user_id, doc_type]))


def streaming_bytes(response):
    return b"".join(response.streaming_content)


# ─────────────────────────────────────────────────────────────
#  Missions
# ─────────────────────────────────────────────────────────────

def make_mission(client_user, **overrides):
    from missions.models import Mission, TruckCategory, TruckSize, TruckType

    defaults = {
        "shipment_type": "house_moving",
        "phone_number": "0550000000",
        "client": client_user,
        "truck_type": TruckType.SMALL,
        "category": TruckCategory.CLOSED,
        "capacity": 2,
        "size": TruckSize.SMALL,
        "departure_latitude": Decimal("36.753800"),
        "departure_longitude": Decimal("3.058800"),
        "arrival_latitude": Decimal("36.700000"),
        "arrival_longitude": Decimal("3.100000"),
        "distance": Decimal("12.5"),
        "date": date.today(),
        "time": "10:00:00",
        "workers": 0,
        "price": Decimal("5000.00"),
    }
    defaults.update(overrides)
    return Mission.objects.create(**defaults)


# ─────────────────────────────────────────────────────────────
#  Base test cases
# ─────────────────────────────────────────────────────────────

class PrivateStorageMixin:
    """Give every test class its own private storage directory."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._private_dir = tempfile.mkdtemp(prefix="orizzonte_drivers_")
        cls._settings_override = override_settings(
            PRIVATE_MEDIA_ROOT=cls._private_dir,
            AI_AGENT_API_KEY=AGENT_KEY,
            AI_AUTO_APPROVE=False,
            AI_AUTO_APPROVE_MIN_CONFIDENCE=0.90,
            MAX_UPLOAD_MB=5,
        )
        cls._settings_override.enable()

    @classmethod
    def tearDownClass(cls):
        cls._settings_override.disable()
        shutil.rmtree(cls._private_dir, ignore_errors=True)
        super().tearDownClass()


class DriverAPITestCase(PrivateStorageMixin, TestCase):
    """Base class for all driver/agent/mission API tests."""

    def setUp(self):
        super().setUp()
        cache.clear()  # reset the scoped throttles between tests


class PlainTestCase(PrivateStorageMixin, TestCase):
    """Base class without the API client (storage/management tests)."""

    def setUp(self):
        super().setUp()
        cache.clear()


__all__ = [
    "AGENT_KEY",
    "PASSWORD",
    "DriverAPITestCase",
    "PlainTestCase",
    "PrivateStorageMixin",
    "User",
    "agent_client",
    "auth_client",
    "document_file",
    "download_document",
    "image_bytes",
    "make_mission",
    "make_profile",
    "make_user",
    "streaming_bytes",
    "upload_document",
]
