"""
drivers/views.py
~~~~~~~~~~~~~~~~

API endpoints for driver profiles, private documents and validation.

Nothing here builds a file path from user input: document types come from a
strict whitelist and the stored names are random UUIDs.
"""
import os

from django.http import FileResponse, Http404
from rest_framework import status
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.authentication import JWTAuthentication

from accounts.permissions import IsBoss, IsDriver
from .agent_auth import AgentKeyAuthentication
from .constants import DOCUMENT_TYPES
from .documents import DocumentValidationError
from .models import DriverProfile
from .permissions import CanAccessDriverDocuments, IsAgent
from .serializers import (
    AgentDriverDetailSerializer,
    AgentPendingDriverSerializer,
    AgentReviewSerializer,
    BossDriverDetailSerializer,
    BossDriverListSerializer,
    DocumentUploadSerializer,
    DriverProfileSerializer,
    DriverProfileUpdateSerializer,
    DriverRejectSerializer,
)
from .services import (
    MissingFieldsError,
    ProfileTransitionError,
    VersionConflictError,
    agent_review,
    approve_profile,
    get_or_create_profile,
    reject_profile,
    save_document,
    submit_profile,
    update_profile,
)
from .storage import private_media_storage

_EXTENSION_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


class DriverPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100


def _profile_or_404(user_id) -> DriverProfile:
    profile = DriverProfile.objects.filter(user_id=user_id).first()
    if profile is None:
        raise Http404
    return profile


# ─────────────────────────────────────────────────────────────
#  Driver endpoints
# ─────────────────────────────────────────────────────────────

class DriverProfileView(APIView):
    """
    GET   /api/drivers/me/profile/   → profile + status + missing fields
    PATCH /api/drivers/me/profile/   → update the driver-editable fields

    Permissions: driver only. ``status`` / ``reviewed_*`` /
    ``documents_version`` are read-only (ignored if sent).
    """

    permission_classes = [IsDriver]

    def get(self, request):
        profile = get_or_create_profile(request.user)
        return Response(DriverProfileSerializer(profile).data)

    def patch(self, request):
        profile = get_or_create_profile(request.user)
        serializer = DriverProfileUpdateSerializer(
            profile, data=request.data, partial=True
        )
        serializer.is_valid(raise_exception=True)
        profile, _reopened = update_profile(profile, serializer.validated_data)
        return Response(DriverProfileSerializer(profile).data)


class DriverDocumentUploadView(APIView):
    """POST /api/drivers/me/documents/ — multipart ``type`` + ``file``."""

    permission_classes = [IsDriver]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "driver-upload"

    def post(self, request):
        serializer = DocumentUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        profile = get_or_create_profile(request.user)
        try:
            profile = save_document(
                profile,
                serializer.validated_data["type"],
                serializer.validated_data["file"],
            )
        except DocumentValidationError as exc:
            return Response(
                {"detail": str(exc), "code": "invalid_document"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(DriverProfileSerializer(profile).data)


class DriverSubmitView(APIView):
    """POST /api/drivers/me/submit/ — incomplete/rejected → pending_review."""

    permission_classes = [IsDriver]

    def post(self, request):
        profile = get_or_create_profile(request.user)
        try:
            profile = submit_profile(profile)
        except MissingFieldsError as exc:
            return Response(
                {
                    "detail": "Your profile is incomplete.",
                    "code": "profile_incomplete",
                    "missing": exc.missing,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        except ProfileTransitionError as exc:
            return Response(
                {"detail": str(exc), "code": "invalid_profile_transition"},
                status=status.HTTP_409_CONFLICT,
            )
        return Response(DriverProfileSerializer(profile).data)


# ─────────────────────────────────────────────────────────────
#  Document download (owner, boss or agent)
# ─────────────────────────────────────────────────────────────

class DriverDocumentView(APIView):
    """
    GET /api/drivers/<id>/documents/<type>/

    Streams a private document. Never exposes a public URL. The response is
    non-cacheable and marked ``nosniff`` with a neutral file name.
    """

    authentication_classes = [JWTAuthentication, AgentKeyAuthentication]
    permission_classes = [CanAccessDriverDocuments]

    def get(self, request, pk, doc_type):
        if doc_type not in DOCUMENT_TYPES:
            raise Http404

        profile = DriverProfile.objects.filter(user_id=pk).first()
        if profile is None:
            raise Http404

        name = getattr(profile, doc_type).name
        if not name:
            raise Http404

        try:
            file_handle = private_media_storage.open(name, "rb")
        except (FileNotFoundError, OSError):
            raise Http404

        extension = os.path.splitext(name)[1].lower()
        response = FileResponse(
            file_handle,
            content_type=_EXTENSION_MIME.get(extension, "application/octet-stream"),
        )
        response["Content-Disposition"] = 'inline; filename="document"'
        response["Cache-Control"] = "private, no-store"
        response["X-Content-Type-Options"] = "nosniff"
        response["Pragma"] = "no-cache"
        return response


# ─────────────────────────────────────────────────────────────
#  Boss endpoints
# ─────────────────────────────────────────────────────────────

class PendingDriversView(APIView):
    """GET /api/drivers/pending/ — profiles awaiting review (paginated)."""

    permission_classes = [IsBoss]

    def get(self, request):
        queryset = DriverProfile.objects.filter(
            status=DriverProfile.Status.PENDING_REVIEW
        ).order_by("updated_at")
        paginator = DriverPagination()
        page = paginator.paginate_queryset(queryset, request, view=self)
        serializer = BossDriverListSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)


class BossDriverDetailView(APIView):
    """GET /api/drivers/<id>/ — full profile detail for the boss."""

    permission_classes = [IsBoss]

    def get(self, request, pk):
        profile = _profile_or_404(pk)
        return Response(BossDriverDetailSerializer(profile).data)


class ApproveDriverView(APIView):
    """POST /api/drivers/<id>/approve/"""

    permission_classes = [IsBoss]

    def post(self, request, pk):
        profile = _profile_or_404(pk)
        try:
            profile = approve_profile(profile, request.user)
        except ProfileTransitionError as exc:
            return Response(
                {"detail": str(exc), "code": "invalid_profile_transition"},
                status=status.HTTP_409_CONFLICT,
            )
        return Response(BossDriverDetailSerializer(profile).data)


class RejectDriverView(APIView):
    """POST /api/drivers/<id>/reject/ — body ``{"reason": "..."}``."""

    permission_classes = [IsBoss]

    def post(self, request, pk):
        profile = _profile_or_404(pk)

        serializer = DriverRejectSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            profile = reject_profile(profile, request.user, serializer.validated_data["reason"])
        except ProfileTransitionError as exc:
            return Response(
                {"detail": str(exc), "code": "invalid_profile_transition"},
                status=status.HTTP_409_CONFLICT,
            )
        return Response(BossDriverDetailSerializer(profile).data)


# ─────────────────────────────────────────────────────────────
#  AI agent endpoints (X-Agent-Key only, never JWT)
# ─────────────────────────────────────────────────────────────

class AgentBaseView(APIView):
    authentication_classes = [AgentKeyAuthentication]
    permission_classes = [IsAgent]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "agent"


class AgentPendingDriversView(AgentBaseView):
    """GET /api/agent/drivers/pending/"""

    def get(self, request):
        queryset = DriverProfile.objects.filter(
            status=DriverProfile.Status.PENDING_REVIEW
        ).order_by("updated_at")
        return Response(AgentPendingDriverSerializer(queryset, many=True).data)


class AgentDriverDetailView(AgentBaseView):
    """GET /api/agent/drivers/<id>/"""

    def get(self, request, pk):
        profile = _profile_or_404(pk)
        return Response(AgentDriverDetailSerializer(profile).data)


class AgentDriverDocumentView(AgentBaseView):
    """GET /api/agent/drivers/<id>/documents/<type>/"""

    def get(self, request, pk, doc_type):
        if doc_type not in DOCUMENT_TYPES:
            raise Http404

        profile = DriverProfile.objects.filter(user_id=pk).first()
        if profile is None:
            raise Http404

        name = getattr(profile, doc_type).name
        if not name:
            raise Http404

        try:
            file_handle = private_media_storage.open(name, "rb")
        except (FileNotFoundError, OSError):
            raise Http404

        extension = os.path.splitext(name)[1].lower()
        response = FileResponse(
            file_handle,
            content_type=_EXTENSION_MIME.get(extension, "application/octet-stream"),
        )
        response["Content-Disposition"] = 'inline; filename="document"'
        response["Cache-Control"] = "private, no-store"
        response["X-Content-Type-Options"] = "nosniff"
        response["Pragma"] = "no-cache"
        return response


class AgentDriverReviewView(AgentBaseView):
    """POST /api/agent/drivers/<id>/review/"""

    def post(self, request, pk):
        profile = _profile_or_404(pk)

        serializer = AgentReviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            profile, applied = agent_review(profile, **serializer.validated_data)
        except ProfileTransitionError as exc:
            return Response(
                {"detail": str(exc), "code": "invalid_profile_transition"},
                status=status.HTTP_409_CONFLICT,
            )
        except VersionConflictError as exc:
            return Response(
                {
                    "detail": str(exc),
                    "code": "documents_version_conflict",
                    "current_documents_version": exc.current,
                },
                status=status.HTTP_409_CONFLICT,
            )

        return Response(
            {
                "user_id": profile.user_id,
                "status": profile.status,
                "applied": applied,
                "documents_version": profile.documents_version,
            }
        )
