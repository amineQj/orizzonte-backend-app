# -*- coding: utf-8 -*-
"""Boss-only pricing configuration management endpoints."""

from __future__ import annotations

from rest_framework import status
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle
from rest_framework.views import APIView

from .permissions import IsBoss
from .pricing_services import get_active_config, update_pricing_config
from .serializers import PricingConfigSerializer, PricingConfigUpdateSerializer


class PricingConfigUpdateThrottle(UserRateThrottle):
    rate = "5/min"


class PricingConfigView(APIView):
    """GET the current active pricing configuration (boss only)."""

    permission_classes = [IsBoss]

    def get(self, request):
        config = get_active_config()
        return Response(
            PricingConfigSerializer(config).data,
            status=status.HTTP_200_OK,
        )


class PricingConfigUpdateView(APIView):
    """POST a partial update to the pricing configuration (boss only, password-gated)."""

    permission_classes = [IsBoss]
    throttle_classes = [PricingConfigUpdateThrottle]

    def post(self, request):
        serializer = PricingConfigUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        password = serializer.validated_data["password"]
        parameters = serializer.validated_data["parameters"]

        if not request.user.check_password(password):
            return Response(
                {"detail": "Incorrect password.", "code": "invalid_password"},
                status=status.HTTP_403_FORBIDDEN,
            )

        try:
            new_config = update_pricing_config(
                boss=request.user,
                parameters=parameters,
            )
        except Exception as exc:  # noqa: BLE001
            # DRF's ValidationError carries .detail; re-raise so the handler
            # produces a proper 400. Other exceptions become 500.
            from rest_framework.exceptions import ValidationError as DRFValidationError

            if isinstance(exc, DRFValidationError):
                return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
            raise

        return Response(
            PricingConfigSerializer(new_config).data,
            status=status.HTTP_200_OK,
        )
