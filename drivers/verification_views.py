"""HTTP endpoint for the standalone quality and classification module."""

from __future__ import annotations

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsDriver

from .documents import DocumentValidationError, process_image
from .serializers import VerificationCheckSerializer
from .verification import (
    VerificationUnavailable,
    build_response,
    run_verification,
)


class VerificationCheckView(APIView):
    """
    POST /api/verification/check/

    Multipart fields: ``slot`` and ``file``.  The uploaded image is validated,
    re-encoded without metadata, and passed to the isolated verification
    environment.  The endpoint never stores the image or accepts OCR text from
    the uploader.
    """

    permission_classes = [IsDriver]

    def post(self, request):
        serializer = VerificationCheckSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        slot = serializer.validated_data["slot"]
        try:
            image_bytes, _extension = process_image(
                serializer.validated_data["file"]
            )
        except DocumentValidationError as exc:
            return Response(
                {"detail": str(exc), "code": "invalid_document"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            result = run_verification(image_bytes, slot)
        except VerificationUnavailable as exc:
            return Response(
                {
                    "detail": "Verification service is unavailable.",
                    "code": exc.category,
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        return Response(build_response(slot, result))
