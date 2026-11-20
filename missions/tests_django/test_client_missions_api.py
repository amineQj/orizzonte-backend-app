"""API tests for ``GET /api/missions/client/my/``.

A client can list only the missions they created; drivers, bosses and
anonymous callers cannot use the endpoint.
"""
from __future__ import annotations

from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from missions.models import MissionStatus

from .base import api_client, make_boss, make_client, make_driver
from .negotiation_base import make_mission

CLIENT_MY_URL = reverse("missions:mission-client-my")


class ClientMissionHistoryAPITests(TestCase):
    def setUp(self):
        self.client_user = make_client("my-missions-client")
        self.other_client = make_client("my-missions-client-2")
        self.driver = make_driver("my-missions-driver")
        self.boss = make_boss("my-missions-boss")

        self.mine_pending = make_mission(
            self.client_user, status=MissionStatus.PENDING
        )
        self.mine_completed = make_mission(
            self.client_user, status=MissionStatus.COMPLETED
        )
        self.theirs = make_mission(self.other_client)

    def test_client_lists_only_own_missions(self):
        response = api_client(self.client_user).get(CLIENT_MY_URL)
        self.assertEqual(response.status_code, 200)
        body = response.data
        self.assertIn("results", body)
        self.assertEqual(body["count"], 2)
        ids = {row["id"] for row in body["results"]}
        self.assertEqual(ids, {self.mine_pending.pk, self.mine_completed.pk})
        self.assertNotIn(self.theirs.pk, ids)

    def test_client_response_uses_existing_client_serializer_shape(self):
        response = api_client(self.client_user).get(CLIENT_MY_URL)
        self.assertEqual(response.status_code, 200)
        row = response.data["results"][0]
        for field in (
            "id",
            "shipment_type",
            "phone_number",
            "truck_type",
            "category",
            "capacity",
            "size",
            "departure_latitude",
            "departure_longitude",
            "arrival_latitude",
            "arrival_longitude",
            "distance",
            "date",
            "time",
            "workers",
            "price",
            "negotiable",
            "status",
            "created_at",
        ):
            self.assertIn(field, row)
        self.assertEqual(Decimal(row["price"]), self.mine_completed.price)
        self.assertIn(row["status"], {MissionStatus.PENDING, MissionStatus.COMPLETED})

    def test_client_with_no_missions_gets_empty_page(self):
        empty_client = make_client("my-missions-empty-client")
        response = api_client(empty_client).get(CLIENT_MY_URL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 0)
        self.assertEqual(response.data["results"], [])

    def test_pagination_page_size_query_param(self):
        response = api_client(self.client_user).get(
            CLIENT_MY_URL, {"page_size": 1}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 2)
        self.assertEqual(len(response.data["results"]), 1)
        self.assertIsNotNone(response.data["next"])

    def test_driver_is_forbidden(self):
        response = api_client(self.driver).get(CLIENT_MY_URL)
        self.assertEqual(response.status_code, 403)

    def test_boss_is_forbidden(self):
        response = api_client(self.boss).get(CLIENT_MY_URL)
        self.assertEqual(response.status_code, 403)

    def test_anonymous_is_unauthorized(self):
        response = api_client().get(CLIENT_MY_URL)
        self.assertEqual(response.status_code, 401)
