"""Negotiation API endpoints.

All four endpoints are mission-centric: there is at most one active
negotiation per mission, so the mission PK is enough to identify it.

    GET  /api/missions/<id>/negotiation/
    POST /api/missions/<id>/negotiation/offer/
    POST /api/missions/<id>/negotiation/accept/
    POST /api/missions/<id>/negotiation/decline/

Non-participants receive 404 (the same response as a missing negotiation) so
the API never discloses another user's negotiation. Role failures receive 403.
"""
from __future__ import annotations

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .negotiation import NegotiationError
from .negotiation_serializers import NegotiationSerializer
from .negotiation_services import (
    accept_negotiation,
    decline_negotiation,
    get_negotiation,
    make_offer,
)
from .permissions import IsClientOrApprovedDriver
from .serializers import MissionClientSerializer, MissionDriverListSerializer


def _negotiation_error_response(exc: NegotiationError) -> Response:
    body = {"detail": exc.message, "code": exc.code}
    if exc.field:
        body["field"] = exc.field
    if exc.extra:
        body.update(exc.extra)
    return Response(body, status=exc.http_status)


def _serializer(negotiation, request) -> NegotiationSerializer:
    return NegotiationSerializer(negotiation, context={"request": request})


class MissionNegotiationView(APIView):
    """Read the current (or latest) negotiation for a mission."""

    permission_classes = [IsClientOrApprovedDriver]

    def get(self, request, pk: int):
        try:
            negotiation = get_negotiation(pk, request.user)
        except NegotiationError as exc:
            return _negotiation_error_response(exc)
        return Response(
            _serializer(negotiation, request).data,
            status=status.HTTP_200_OK,
        )


class NegotiationOfferView(APIView):
    """Make the first offer (opens the negotiation) or the next counter-offer.

    Body::

        {"amount": 800, "idempotency_key": "<opaque unique key>"}
    """

    permission_classes = [IsClientOrApprovedDriver]

    def post(self, request, pk: int):
        try:
            negotiation, created = make_offer(pk, request.user, request.data)
        except NegotiationError as exc:
            return _negotiation_error_response(exc)
        return Response(
            _serializer(negotiation, request).data,
            status=(
                status.HTTP_201_CREATED if created else status.HTTP_200_OK
            ),
        )


class NegotiationAcceptView(APIView):
    """Accept the latest offer; assigns the driver and locks the price."""

    permission_classes = [IsClientOrApprovedDriver]

    def post(self, request, pk: int):
        try:
            negotiation, mission, _idempotent = accept_negotiation(
                pk, request.user
            )
        except NegotiationError as exc:
            return _negotiation_error_response(exc)

        if getattr(request.user, "role", None) == "driver":
            mission_data = MissionDriverListSerializer(mission).data
        else:
            mission_data = MissionClientSerializer(mission).data

        return Response(
            {
                "negotiation": _serializer(negotiation, request).data,
                "mission": mission_data,
            },
            status=status.HTTP_200_OK,
        )


class NegotiationDeclineView(APIView):
    """Decline the latest offer; the mission stays open for another driver."""

    permission_classes = [IsClientOrApprovedDriver]

    def post(self, request, pk: int):
        try:
            negotiation = decline_negotiation(pk, request.user)
        except NegotiationError as exc:
            return _negotiation_error_response(exc)
        return Response(
            _serializer(negotiation, request).data,
            status=status.HTTP_200_OK,
        )
