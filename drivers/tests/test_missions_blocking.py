"""
drivers/tests/test_missions_blocking.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Mission access rules driven by the driver profile status:

* accept / start require an *approved* profile (403 ``driver_not_approved``);
* complete keeps the historical ``IsDriver`` rule (documented decision), but
  still enforces "only the assigned driver" and "only while in progress";
* the boss cannot assign a mission to an unapproved driver (409);
* listing available / my missions stays open to any driver.
"""
from django.contrib.auth import get_user_model
from django.urls import reverse

from drivers.models import DriverProfile
from drivers.tests.base import (
    DriverAPITestCase,
    auth_client,
    make_mission,
    make_profile,
    make_user,
)

User = get_user_model()
Status = DriverProfile.Status


class AcceptStartBlockingTests(DriverAPITestCase):
    def setUp(self):
        super().setUp()
        self.client_user = make_user(User.Role.CLIENT, username="m_client")
        self.driver = make_user(User.Role.DRIVER, username="m_driver")
        self.driver_client = auth_client(self.driver)

    def test_incomplete_driver_cannot_accept(self):
        mission = make_mission(self.client_user)
        response = self.driver_client.post(
            reverse("missions:mission-accept", args=[mission.pk])
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "driver_not_approved")
        self.assertEqual(response.json()["profile_status"], Status.INCOMPLETE)

    def test_pending_driver_cannot_start(self):
        make_profile(self.driver, complete=True, status=Status.PENDING_REVIEW)
        mission = make_mission(
            self.client_user, status="accepted", assigned_employee=self.driver
        )
        response = self.driver_client.post(
            reverse("missions:mission-start", args=[mission.pk])
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "driver_not_approved")
        self.assertEqual(response.json()["profile_status"], Status.PENDING_REVIEW)

    def test_rejected_driver_cannot_accept(self):
        make_profile(self.driver, complete=True, status=Status.REJECTED)
        mission = make_mission(self.client_user)
        response = self.driver_client.post(
            reverse("missions:mission-accept", args=[mission.pk])
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["profile_status"], Status.REJECTED)

    def test_approved_driver_can_accept_then_start(self):
        make_profile(self.driver, status=Status.APPROVED)
        mission = make_mission(self.client_user)

        accept = self.driver_client.post(
            reverse("missions:mission-accept", args=[mission.pk])
        )
        self.assertEqual(accept.status_code, 200, accept.content)
        mission.refresh_from_db()
        self.assertEqual(mission.assigned_employee, self.driver)

        start = self.driver_client.post(
            reverse("missions:mission-start", args=[mission.pk])
        )
        self.assertEqual(start.status_code, 200, start.content)
        mission.refresh_from_db()
        self.assertEqual(mission.status, "in_progress")

    def test_unapproved_driver_can_still_list_missions(self):
        self.assertEqual(
            self.driver_client.get(reverse("missions:mission-available")).status_code,
            200,
        )
        self.assertEqual(
            self.driver_client.get(reverse("missions:mission-my")).status_code, 200
        )


class BossAssignmentBlockingTests(DriverAPITestCase):
    def setUp(self):
        super().setUp()
        self.client_user = make_user(User.Role.CLIENT, username="a_client")
        self.boss = make_user(User.Role.BOSS, username="a_boss")
        self.driver = make_user(User.Role.DRIVER, username="a_driver")
        self.boss_client = auth_client(self.boss)

    def _assign(self, mission, driver):
        return self.boss_client.post(
            reverse("missions:mission-assign", args=[mission.pk]),
            {"employee_id": driver.pk},
            format="json",
        )

    def test_assign_to_unapproved_driver_is_409(self):
        mission = make_mission(self.client_user)
        response = self._assign(mission, self.driver)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "driver_not_approved")
        self.assertEqual(response.json()["profile_status"], Status.INCOMPLETE)
        mission.refresh_from_db()
        self.assertEqual(mission.status, "pending")
        self.assertIsNone(mission.assigned_employee)

    def test_assign_to_approved_driver_succeeds(self):
        make_profile(self.driver, status=Status.APPROVED)
        mission = make_mission(self.client_user)
        response = self._assign(mission, self.driver)
        self.assertEqual(response.status_code, 200, response.content)
        mission.refresh_from_db()
        self.assertEqual(mission.status, "accepted")
        self.assertEqual(mission.assigned_employee, self.driver)


class CompleteMissionTests(DriverAPITestCase):
    def setUp(self):
        super().setUp()
        self.client_user = make_user(User.Role.CLIENT, username="c_client")
        self.driver = make_user(User.Role.DRIVER, username="c_driver")
        self.other_driver = make_user(User.Role.DRIVER, username="c_other")
        self.driver_client = auth_client(self.driver)
        self.other_client = auth_client(self.other_driver)

    def test_only_assigned_driver_can_complete(self):
        mission = make_mission(
            self.client_user, status="in_progress", assigned_employee=self.driver
        )
        response = self.other_client.post(
            reverse("missions:mission-complete", args=[mission.pk])
        )
        self.assertEqual(response.status_code, 403)
        mission.refresh_from_db()
        self.assertEqual(mission.status, "in_progress")

    def test_cannot_complete_mission_not_in_progress(self):
        mission = make_mission(
            self.client_user, status="accepted", assigned_employee=self.driver
        )
        response = self.driver_client.post(
            reverse("missions:mission-complete", args=[mission.pk])
        )
        self.assertEqual(response.status_code, 409)
        self.assertIn("expected status", response.json()["detail"])

    def test_cannot_complete_cancelled_mission(self):
        mission = make_mission(
            self.client_user, status="cancelled", assigned_employee=self.driver
        )
        response = self.driver_client.post(
            reverse("missions:mission-complete", args=[mission.pk])
        )
        self.assertEqual(response.status_code, 409)

    def test_cannot_complete_already_completed_mission(self):
        mission = make_mission(
            self.client_user, status="completed", assigned_employee=self.driver
        )
        response = self.driver_client.post(
            reverse("missions:mission-complete", args=[mission.pk])
        )
        self.assertEqual(response.status_code, 409)
        mission.refresh_from_db()
        self.assertEqual(mission.status, "completed")

    def test_complete_unknown_mission_is_404(self):
        response = self.driver_client.post(
            reverse("missions:mission-complete", args=[999999])
        )
        self.assertEqual(response.status_code, 404)

    def test_complete_keeps_is_driver_rule_without_approval(self):
        # Documented decision: ``complete`` only requires the driver role.
        # The profile is deliberately left ``incomplete`` here.
        mission = make_mission(
            self.client_user, status="in_progress", assigned_employee=self.driver
        )
        response = self.driver_client.post(
            reverse("missions:mission-complete", args=[mission.pk])
        )
        self.assertEqual(response.status_code, 200, response.content)
        mission.refresh_from_db()
        self.assertEqual(mission.status, "completed")
