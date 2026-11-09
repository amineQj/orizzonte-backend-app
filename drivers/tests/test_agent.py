"""
drivers/tests/test_agent.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~

The locked-down AI-agent API: key authentication, queries and review.
"""
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from drivers.models import DriverProfile, DriverReviewLog
from drivers.tests.base import (
    AGENT_KEY,
    DriverAPITestCase,
    agent_client,
    auth_client,
    make_profile,
    make_user,
)

User = get_user_model()
Status = DriverProfile.Status


class AgentAuthenticationTests(DriverAPITestCase):
    def test_missing_key_is_unauthorized(self):
        response = APIClient().get(reverse("agent-driver-pending"))
        self.assertEqual(response.status_code, 401)

    def test_wrong_key_is_unauthorized(self):
        response = agent_client("this-is-not-the-key").get(
            reverse("agent-driver-pending")
        )
        self.assertEqual(response.status_code, 401)

    def test_endpoints_are_disabled_when_key_is_empty(self):
        with override_settings(AI_AGENT_API_KEY=""):
            response = agent_client("anything").get(
                reverse("agent-driver-pending")
            )
        self.assertEqual(response.status_code, 401)

    def test_jwt_alone_cannot_use_agent_endpoints(self):
        boss = make_user(User.Role.BOSS, username="agent_boss")
        response = auth_client(boss).get(reverse("agent-driver-pending"))
        self.assertEqual(response.status_code, 401)

    def test_agent_key_cannot_use_boss_endpoints(self):
        response = agent_client().get(reverse("driver-pending"))
        self.assertEqual(response.status_code, 401)

    def test_agent_key_does_not_authenticate_on_boss_detail(self):
        driver = make_user(User.Role.DRIVER, username="agent_probe")
        make_profile(driver, status=Status.PENDING_REVIEW)
        response = agent_client().get(reverse("driver-detail", args=[driver.pk]))
        self.assertEqual(response.status_code, 401)


class AgentQueryTests(DriverAPITestCase):
    def setUp(self):
        super().setUp()
        self.agent = agent_client()
        self.driver = make_user(User.Role.DRIVER, username="agent_query")
        self.profile = make_profile(
            self.driver, complete=True, status=Status.PENDING_REVIEW
        )

    def test_pending_list_returns_minimal_payload(self):
        response = self.agent.get(reverse("agent-driver-pending"))
        self.assertEqual(response.status_code, 200)
        row = next(
            r for r in response.json() if r["user_id"] == self.driver.pk
        )
        self.assertEqual(row["documents_version"], 0)
        self.assertEqual(row["status"], Status.PENDING_REVIEW)
        # No personal data is exposed on the list endpoint.
        self.assertNotIn("full_name", row)
        self.assertNotIn("national_id_number", row)

    def test_detail_returns_documents(self):
        response = self.agent.get(
            reverse("agent-driver-detail", args=[self.driver.pk])
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["user_id"], self.driver.pk)
        self.assertTrue(body["documents"]["selfie"]["uploaded"])
        self.assertEqual(
            body["documents"]["selfie"]["url"],
            reverse("agent-driver-document", args=[self.driver.pk, "selfie"]),
        )

    def test_pending_list_excludes_other_statuses(self):
        other = make_user(User.Role.DRIVER, username="agent_approved")
        make_profile(other, complete=True, status=Status.APPROVED)
        response = self.agent.get(reverse("agent-driver-pending"))
        ids = [r["user_id"] for r in response.json()]
        self.assertIn(self.driver.pk, ids)
        self.assertNotIn(other.pk, ids)


class AgentReviewTests(DriverAPITestCase):
    def setUp(self):
        super().setUp()
        self.driver = make_user(User.Role.DRIVER, username="agent_review")
        self.profile = make_profile(
            self.driver, complete=True, status=Status.PENDING_REVIEW
        )
        self.agent = agent_client()
        self.url = reverse("agent-driver-review", args=[self.driver.pk])

    def _review(self, **payload):
        payload.setdefault("documents_version", self.profile.documents_version)
        return self.agent.post(self.url, payload, format="json")

    def test_approve_is_only_a_recommendation_by_default(self):
        response = self._review(
            decision="approve", reasons=["Documents look valid"], confidence=0.99
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(response.json()["applied"])
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.status, Status.PENDING_REVIEW)
        log = DriverReviewLog.objects.get(profile=self.profile)
        self.assertEqual(log.source, "ai")
        self.assertEqual(log.decision, "approve")
        self.assertFalse(log.applied)

    @override_settings(AI_AUTO_APPROVE=True, AI_AUTO_APPROVE_MIN_CONFIDENCE=0.90)
    def test_approve_applied_when_auto_and_confident(self):
        response = self._review(
            decision="approve", reasons=["clear"], confidence=0.95
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["applied"])
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.status, Status.APPROVED)
        self.assertEqual(self.profile.reviewed_by_type, "ai")

    @override_settings(AI_AUTO_APPROVE=True, AI_AUTO_APPROVE_MIN_CONFIDENCE=0.90)
    def test_approve_not_applied_when_confidence_too_low(self):
        response = self._review(decision="approve", reasons=["maybe"], confidence=0.40)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(response.json()["applied"])
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.status, Status.PENDING_REVIEW)

    def test_reject_is_always_applied(self):
        response = self._review(
            decision="reject", reasons=["Blurred documents"], confidence=0.80
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["applied"])
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.status, Status.REJECTED)
        self.assertIn("Blurred", self.profile.rejection_reason)

    def test_manual_review_keeps_status(self):
        response = self._review(
            decision="manual_review", reasons=["unsure"], confidence=0.50
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(response.json()["applied"])
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.status, Status.PENDING_REVIEW)

    def test_version_conflict_is_409(self):
        response = self.agent.post(
            self.url,
            {
                "decision": "approve",
                "reasons": ["ok"],
                "confidence": 0.99,
                "documents_version": 99,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "documents_version_conflict")
        self.assertEqual(response.json()["current_documents_version"], 0)

    def test_acting_on_a_non_pending_profile_is_409(self):
        self.profile.status = Status.APPROVED
        self.profile.save(update_fields=["status"])
        response = self._review(
            decision="approve", reasons=["ok"], confidence=0.99
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "invalid_profile_transition")

    def test_reasons_with_numbers_are_rejected(self):
        response = self._review(
            decision="reject", reasons=["ID 1234567890 invalid"], confidence=0.5
        )
        self.assertEqual(response.status_code, 400)

    def test_unknown_driver_is_404(self):
        response = self.agent.post(
            reverse("agent-driver-review", args=[999999]),
            {
                "decision": "approve",
                "reasons": ["ok"],
                "confidence": 0.99,
                "documents_version": 1,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 404)
