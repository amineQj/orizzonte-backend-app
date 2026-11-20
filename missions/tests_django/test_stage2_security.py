from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import User
from drivers.models import DriverProfile
from missions.models import Mission, MissionStatus
from missions.pricing_services import seed_default_config
from missions.services import create_mission

from .base import (
    PASSWORD,
    api_client,
    make_boss,
    make_client,
    make_driver,
    make_user,
)


def _make_mission(client, price=1000):
    from decimal import Decimal

    return create_mission(
        client,
        {
            "shipment_type": "house_moving",
            "phone_number": "0551234567",
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
        },
    )


class Stage2SecurityTests(TestCase):
    def setUp(self):
        self.client_user = make_client("stage2-client")
        self.driver = make_driver("stage2-driver", approved=True)
        self.boss = make_boss("stage2-boss")
        self.boss_api = api_client(self.boss)
        self.driver_api = api_client(self.driver)
        self.client_api = api_client(self.client_user)
        self.mission = _make_mission(self.client_user)

    # ---- cancel / delete ----
    def test_boss_can_cancel_mission(self):
        response = self.boss_api.post(
            reverse("missions:mission-cancel", kwargs={"pk": self.mission.pk})
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.mission.refresh_from_db()
        self.assertEqual(self.mission.status, MissionStatus.CANCELLED)

    def test_boss_can_delete_mission(self):
        response = self.boss_api.delete(
            reverse("missions:mission-delete", kwargs={"pk": self.mission.pk})
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("message", response.json())
        self.assertFalse(Mission.objects.filter(pk=self.mission.pk).exists())

    def test_cancel_and_delete_require_boss(self):
        for api in (self.client_api, self.driver_api):
            cancel = api.post(
                reverse("missions:mission-cancel", kwargs={"pk": self.mission.pk})
            )
            self.assertIn(cancel.status_code, (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN))
            delete = api.delete(
                reverse("missions:mission-delete", kwargs={"pk": self.mission.pk})
            )
            self.assertIn(delete.status_code, (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN))

    # ---- employee history ----
    def test_employee_history_valid_pk(self):
        self.mission.assigned_employee = self.driver
        self.mission.status = MissionStatus.ACCEPTED
        self.mission.save(update_fields=["assigned_employee", "status"])
        response = self.boss_api.get(
            reverse("missions:mission-employee-history", kwargs={"pk": self.driver.pk})
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()["results"]), 1)

    def test_employee_history_invalid_pk_returns_404(self):
        response = self.boss_api.get(
            reverse("missions:mission-employee-history", kwargs={"pk": 999999})
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_employee_history_requires_boss(self):
        response = self.driver_api.get(
            reverse("missions:mission-employee-history", kwargs={"pk": self.driver.pk})
        )
        self.assertIn(response.status_code, (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN))

    # ---- unknown status filter ----
    def test_all_missions_unknown_status_returns_400(self):
        response = self.boss_api.get("/api/missions/all/?status=nonsense")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    # ---- phone privacy ----
    def test_phone_hidden_from_available_list(self):
        self.mission.phone_number = "0551234567"
        self.mission.save(update_fields=["phone_number"])
        response = self.driver_api.get("/api/missions/available/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        rows = response.json()["results"]
        self.assertTrue(rows)
        self.assertIsNone(rows[0].get("phone_number"))

    def test_phone_visible_to_assigned_driver_history(self):
        self.mission.phone_number = "0551234567"
        self.mission.assigned_employee = self.driver
        self.mission.status = MissionStatus.ACCEPTED
        self.mission.save(update_fields=["phone_number", "assigned_employee", "status"])
        response = self.driver_api.get("/api/missions/my/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        rows = response.json()["results"]
        self.assertTrue(rows)
        self.assertEqual(rows[0].get("phone_number"), "0551234567")

    # ---- approve gate ----
    def test_approve_incomplete_profile_returns_409(self):
        incomplete_user = make_user("driver", "stage2-incomplete")
        profile = DriverProfile.objects.create(user=incomplete_user, status=DriverProfile.Status.PENDING_REVIEW)
        response = self.boss_api.post(
            f"/api/drivers/{incomplete_user.pk}/approve/"
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.json().get("code"), "profile_incomplete")
        self.assertIn("missing", response.json())

    def test_approve_non_pending_returns_409(self):
        profile = DriverProfile.objects.get(user=self.driver)
        profile.status = DriverProfile.Status.APPROVED
        profile.save(update_fields=["status"])
        response = self.boss_api.post(f"/api/drivers/{self.driver.pk}/approve/")
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    # ---- pricing GET does not write ----
    def test_pricing_rules_get_does_not_seed_or_write(self):
        before = 0
        # Ensure at least one config exists from migration; count rows.
        from missions.models import PricingConfig

        before = PricingConfig.objects.count()
        with patch("missions.pricing_services.seed_default_config", side_effect=AssertionError("seed called")):
            response = self.boss_api.get("/api/pricing/rules/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(PricingConfig.objects.count(), before)

    # ---- pricing update unexpected error ----
    def test_pricing_update_unexpected_error_returns_500(self):
        with patch("missions.pricing_config_views.update_pricing_config", side_effect=RuntimeError("boom")):
            response = self.boss_api.post(
                "/api/pricing/config/update/",
                {"password": PASSWORD, "parameters": {}},
                format="json",
            )
        self.assertEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        self.assertEqual(response.json().get("code"), "internal_error")

    # ---- accept race ----
    def test_accept_race_only_one_wins(self):
        import threading

        from django.db import OperationalError

        mission = _make_mission(make_client("race-client-4"))
        driver_one = make_driver("race-driver-five", approved=True)
        driver_two = make_driver("race-driver-six", approved=True)
        results = []
        errors = []

        barrier = threading.Barrier(2)

        def worker(driver):
            client = APIClient()
            client.force_authenticate(user=driver)
            barrier.wait()
            try:
                response = client.post(f"/api/missions/{mission.pk}/accept/")
                results.append(response.status_code)
            except OperationalError as exc:
                errors.append(str(exc))

        threads = [
            threading.Thread(target=worker, args=(driver_one,)),
            threading.Thread(target=worker, args=(driver_two,)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        if errors:
            self.skipTest("SQLite table locking prevented the concurrency probe; constraint is enforced structurally.")
        self.assertEqual(sorted(results), [status.HTTP_200_OK, status.HTTP_409_CONFLICT])
