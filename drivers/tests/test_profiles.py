"""
drivers/tests/test_profiles.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Driver profile lifecycle, submission and boss decisions.
"""
from django.contrib.auth import get_user_model
from django.urls import reverse

from drivers.constants import DOCUMENT_TYPES
from drivers.models import DriverProfile, DriverReviewLog
from drivers.tests.base import (
    DriverAPITestCase,
    PASSWORD,
    auth_client,
    make_profile,
    make_user,
)

User = get_user_model()
Status = DriverProfile.Status


class SignupCreatesProfileTests(DriverAPITestCase):
    def test_driver_signup_creates_incomplete_profile(self):
        response = self.client.post(
            reverse("signup"),
            {
                "username": "newdriver",
                "email": "newdriver@example.com",
                "password": PASSWORD,
                "role": "driver",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        user = User.objects.get(email="newdriver@example.com")
        profile = DriverProfile.objects.get(user=user)
        self.assertEqual(profile.status, Status.INCOMPLETE)

    def test_client_signup_does_not_create_profile(self):
        response = self.client.post(
            reverse("signup"),
            {
                "username": "newclient",
                "email": "newclient@example.com",
                "password": PASSWORD,
                "role": "client",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        user = User.objects.get(email="newclient@example.com")
        self.assertFalse(DriverProfile.objects.filter(user=user).exists())


class DriverProfileApiTests(DriverAPITestCase):
    def setUp(self):
        super().setUp()
        self.driver = make_user(User.Role.DRIVER, username="driver_api")
        self.client_user = make_user(User.Role.CLIENT, username="client_api")
        self.boss = make_user(User.Role.BOSS, username="boss_api")
        self.driver_client = auth_client(self.driver)
        self.client_auth = auth_client(self.client_user)
        self.boss_client = auth_client(self.boss)
        self.url = reverse("driver-profile")

    def test_get_creates_and_returns_incomplete_profile(self):
        response = self.driver_client.get(self.url)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], Status.INCOMPLETE)
        self.assertEqual(body["documents_version"], 0)
        self.assertIn("national_id_number", body["missing"]["info"])
        self.assertEqual(
            sorted(body["missing"]["documents"]),
            sorted(["selfie", "id_card_front", "id_card_back",
                    "driving_license", "vehicle_registration"]),
        )
        # No file path is ever exposed: only an uploaded flag + a URL.
        self.assertFalse(body["documents"]["selfie"]["uploaded"])
        self.assertTrue(body["documents"]["selfie"]["url"].startswith("/api/drivers/"))

    def test_patch_persists_editable_fields(self):
        response = self.driver_client.patch(
            self.url,
            {
                "full_name": "Amina Cherif",
                "phone": "0660000002",
                "date_of_birth": "1995-03-10",
                "national_id_number": "11112222",
                "driving_license_number": "DL-999",
                "driving_license_expiry": "2030-01-01",
                "vehicle_plate": "ab 123 cd",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body["full_name"], "Amina Cherif")
        self.assertEqual(body["vehicle_plate"], "AB 123 CD")  # uppercased

    def test_patch_rejects_underage_driver(self):
        response = self.driver_client.patch(
            self.url, {"date_of_birth": "2015-01-01"}, format="json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("date_of_birth", response.json())

    def test_patch_rejects_expired_licence(self):
        response = self.driver_client.patch(
            self.url, {"driving_license_expiry": "2000-01-01"}, format="json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("driving_license_expiry", response.json())

    def test_patch_ignores_readonly_fields(self):
        self.driver_client.patch(self.url, {"full_name": "X"}, format="json")
        response = self.driver_client.patch(
            self.url,
            {
                "status": Status.APPROVED,
                "documents_version": 999,
                "reviewed_by_type": "boss",
                "rejection_reason": "injected",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        profile = DriverProfile.objects.get(user=self.driver)
        self.assertEqual(profile.status, Status.INCOMPLETE)
        self.assertEqual(profile.reviewed_by_type, "")
        self.assertEqual(profile.rejection_reason, "")

    def test_patch_rejects_duplicate_national_id(self):
        other = make_user(User.Role.DRIVER, username="driver_other")
        make_profile(other, national_id_number="DUP-0001")
        response = self.driver_client.patch(
            self.url, {"national_id_number": "DUP-0001"}, format="json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("national_id_number", response.json())

    def test_client_cannot_access_driver_profile(self):
        self.assertEqual(self.client_auth.get(self.url).status_code, 403)

    def test_driver_cannot_access_boss_endpoints(self):
        response = self.driver_client.get(reverse("driver-pending"))
        self.assertEqual(response.status_code, 403)

    def test_boss_list_and_detail_expose_profile_status(self):
        make_profile(self.driver, status=Status.PENDING_REVIEW)
        listing = self.boss_client.get(reverse("employee-list"))
        self.assertEqual(listing.status_code, 200)
        rows = listing.json()
        if isinstance(rows, dict):
            rows = rows.get("results", [])
        row = next(r for r in rows if r["id"] == self.driver.pk)
        self.assertEqual(row["profile_status"], Status.PENDING_REVIEW)

        detail = self.boss_client.get(
            reverse("employee-detail", args=[self.driver.pk])
        )
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["role"], "driver")
        self.assertEqual(detail.json()["profile_status"], Status.PENDING_REVIEW)

    def test_boss_pending_list_masks_sensitive_numbers(self):
        make_profile(
            self.driver,
            status=Status.PENDING_REVIEW,
            national_id_number="1234567890",
            driving_license_number="DL-ABCDEF",
        )
        response = self.boss_client.get(reverse("driver-pending"))
        self.assertEqual(response.status_code, 200)
        body = response.json()
        rows = body.get("results", body)
        row = next(r for r in rows if r["user_id"] == self.driver.pk)
        self.assertEqual(row["national_id_number"], "******7890")
        self.assertTrue(row["driving_license_number"].endswith("CDEF"))
        self.assertNotIn("1234567890", response.content.decode())

    def test_boss_detail_shows_full_numbers(self):
        make_profile(
            self.driver,
            status=Status.PENDING_REVIEW,
            national_id_number="1234567890",
        )
        response = self.boss_client.get(
            reverse("driver-detail", args=[self.driver.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["national_id_number"], "1234567890")


class ProfileSubmitTests(DriverAPITestCase):
    def setUp(self):
        super().setUp()
        self.driver = make_user(User.Role.DRIVER, username="driver_submit")
        self.driver_client = auth_client(self.driver)
        self.url = reverse("driver-submit")

    def test_submit_incomplete_returns_missing(self):
        response = self.driver_client.post(self.url)
        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertEqual(body["code"], "profile_incomplete")
        self.assertTrue(body["missing"]["info"])
        self.assertEqual(len(body["missing"]["documents"]), 5)

    def test_submit_complete_transitions_to_pending(self):
        profile = make_profile(self.driver, complete=True)
        profile.verification_results = {
            name: {
                "final_decision": "ACCEPT",
                "quality_status": "ACCEPTABLE_QUALITY",
                "reason_codes": [],
                "documents_version": profile.documents_version,
                "created_at": "2026-01-01T00:00:00+00:00",
            }
            for name in DOCUMENT_TYPES
        }
        profile.verification_reject_counts = {name: 0 for name in DOCUMENT_TYPES}
        profile.save()
        response = self.driver_client.post(self.url)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["status"], Status.PENDING_REVIEW)

    def test_submit_twice_conflicts(self):
        profile = make_profile(self.driver, complete=True)
        profile.verification_results = {
            name: {
                "final_decision": "ACCEPT",
                "quality_status": "ACCEPTABLE_QUALITY",
                "reason_codes": [],
                "documents_version": profile.documents_version,
                "created_at": "2026-01-01T00:00:00+00:00",
            }
            for name in DOCUMENT_TYPES
        }
        profile.verification_reject_counts = {name: 0 for name in DOCUMENT_TYPES}
        profile.save()
        self.driver_client.post(self.url)
        response = self.driver_client.post(self.url)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "invalid_profile_transition")


class BossDecisionTests(DriverAPITestCase):
    def setUp(self):
        super().setUp()
        self.boss = make_user(User.Role.BOSS, username="boss_decide")
        self.driver = make_user(User.Role.DRIVER, username="driver_decide")
        self.boss_client = auth_client(self.boss)
        self.driver_client = auth_client(self.driver)

    def _approve(self, user_id):
        return self.boss_client.post(reverse("driver-approve", args=[user_id]))

    def _reject(self, user_id, reason):
        return self.boss_client.post(
            reverse("driver-reject", args=[user_id]),
            {"reason": reason},
            format="json",
        )

    def test_approve_marks_profile_approved_and_logs(self):
        make_profile(self.driver, complete=True, status=Status.PENDING_REVIEW)
        response = self._approve(self.driver.pk)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["status"], Status.APPROVED)
        profile = DriverProfile.objects.get(user=self.driver)
        self.assertEqual(profile.reviewed_by_type, "boss")
        self.assertEqual(profile.reviewed_by, self.boss)
        self.assertTrue(
            DriverReviewLog.objects.filter(
                profile=profile, source="boss", decision="approve", applied=True
            ).exists()
        )

    def test_approve_incomplete_profile_conflicts(self):
        make_profile(self.driver, status=Status.INCOMPLETE)
        response = self._approve(self.driver.pk)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "invalid_profile_transition")

    def test_reject_requires_non_blank_reason(self):
        make_profile(self.driver, complete=True, status=Status.PENDING_REVIEW)
        response = self._reject(self.driver.pk, "   ")
        self.assertEqual(response.status_code, 400)

    def test_reject_requires_reason_field(self):
        make_profile(self.driver, complete=True, status=Status.PENDING_REVIEW)
        response = self.boss_client.post(
            reverse("driver-reject", args=[self.driver.pk]), {}, format="json"
        )
        self.assertEqual(response.status_code, 400)

    def test_reject_records_reason_and_visible_to_driver(self):
        make_profile(self.driver, complete=True, status=Status.PENDING_REVIEW)
        response = self._reject(self.driver.pk, "The selfie is blurred.")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["status"], Status.REJECTED)

        profile = DriverProfile.objects.get(user=self.driver)
        self.assertEqual(profile.rejection_reason, "The selfie is blurred.")
        self.assertEqual(
            DriverReviewLog.objects.get(profile=profile, decision="reject").reasons,
            ["The selfie is blurred."],
        )
        # The driver sees the rejection reason on their own profile.
        driver_view = self.driver_client.get(reverse("driver-profile")).json()
        self.assertEqual(driver_view["status"], Status.REJECTED)
        self.assertIn("blurred", driver_view["rejection_reason"])

    def test_rejected_profile_can_be_resubmitted(self):
        profile = make_profile(self.driver, complete=True, status=Status.PENDING_REVIEW)
        profile.verification_results = {
            name: {
                "final_decision": "ACCEPT",
                "quality_status": "ACCEPTABLE_QUALITY",
                "reason_codes": [],
                "documents_version": profile.documents_version,
                "created_at": "2026-01-01T00:00:00+00:00",
            }
            for name in DOCUMENT_TYPES
        }
        profile.verification_reject_counts = {name: 0 for name in DOCUMENT_TYPES}
        profile.save()
        self._reject(self.driver.pk, "Please retake the selfie.")
        response = self.driver_client.post(reverse("driver-submit"))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["status"], Status.PENDING_REVIEW)

    def test_approved_key_field_change_reopens_review(self):
        make_profile(self.driver, complete=True, status=Status.APPROVED)
        response = self.driver_client.patch(
            reverse("driver-profile"),
            {"driving_license_number": "DL-NEW-0001"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["status"], Status.PENDING_REVIEW)

    def test_approved_non_key_change_keeps_approval(self):
        make_profile(self.driver, complete=True, status=Status.APPROVED)
        response = self.driver_client.patch(
            reverse("driver-profile"), {"phone": "0770000009"}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["status"], Status.APPROVED)
