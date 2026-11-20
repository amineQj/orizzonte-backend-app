"""HTTP endpoint for the standalone quality and classification module."""

from __future__ import annotations

import atexit
import os
import tempfile
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from django.conf import settings
from django.utils import timezone as django_timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView
from demenagement.throttling import DynamicScopedRateThrottle as ScopedRateThrottle

from accounts.permissions import IsDriver

from .documents import DocumentValidationError, process_image
from .models import DriverProfile
from .serializers import VerificationCheckSerializer
from .verification import (
    VerificationUnavailable,
    build_response,
    run_verification,
)

_TASKS: dict[str, dict[str, Any]] = {}
_TASK_LOCK = threading.Lock()
_TASK_EXECUTOR = ThreadPoolExecutor(max_workers=4)
atexit.register(_TASK_EXECUTOR.shutdown, wait=False)

_MAX_PENDING_PER_USER = 5
_MAX_PENDING_GLOBAL = 100
_TASK_TTL_SECONDS = 3600


def _cleanup_finished_tasks() -> None:
    now = django_timezone.now()
    with _TASK_LOCK:
        expired = []
        for task_id, task in list(_TASKS.items()):
            if task.get("status") in {"completed", "failed"}:
                finished_at = task.get("finished_at")
                if finished_at is not None and (now - finished_at).total_seconds() > _TASK_TTL_SECONDS:
                    expired.append(task_id)
        for task_id in expired:
            _TASKS.pop(task_id, None)


def _submit_verification_task(
    *,
    image_bytes: bytes,
    slot: str,
    debug_enabled: bool,
    user_id: int | None,
) -> str:
    _cleanup_finished_tasks()

    with _TASK_LOCK:
        user_pending = sum(
            1
            for task in _TASKS.values()
            if task.get("user_id") == user_id
            and task.get("status") in {"pending", "processing"}
        )
        global_pending = sum(
            1
            for task in _TASKS.values()
            if task.get("status") in {"pending", "processing"}
        )
    if user_pending >= _MAX_PENDING_PER_USER:
        raise RuntimeError("too_many_pending_tasks")
    if global_pending >= _MAX_PENDING_GLOBAL:
        raise RuntimeError("verification_service_busy")

    fd, image_path = tempfile.mkstemp(prefix="orizzonte_verification_")
    with os.fdopen(fd, "wb") as handle:
        handle.write(image_bytes)

    task_id = uuid.uuid4().hex
    with _TASK_LOCK:
        _TASKS[task_id] = {
            "task_id": task_id,
            "user_id": user_id,
            "slot": slot,
            "debug_enabled": debug_enabled,
            "image_path": image_path,
            "status": "pending",
            "created_at": django_timezone.now(),
            "service_unavailable": False,
        }

    _TASK_EXECUTOR.submit(_process_verification_task, task_id)
    return task_id


def _process_verification_task(task_id: str) -> None:
    with _TASK_LOCK:
        task = _TASKS.get(task_id)
        if task is None:
            return
        task["status"] = "processing"
        slot = task["slot"]
        debug_enabled = task["debug_enabled"]
        image_path = task["image_path"]

    try:
        with open(image_path, "rb") as handle:
            image_bytes = handle.read()
        result = run_verification(
            image_bytes,
            slot,
            debug_ocr=debug_enabled,
        )
        response = build_response(slot, result, include_debug=debug_enabled)
        user_id = None
        with _TASK_LOCK:
            task = _TASKS.get(task_id)
            if task is not None:
                task["status"] = "completed"
                task["result"] = response
                task["finished_at"] = django_timezone.now()
                user_id = task.get("user_id")
        if user_id is not None:
            _persist_verification_result(task_id, slot, user_id, response)
    except VerificationUnavailable as exc:
        with _TASK_LOCK:
            task = _TASKS.get(task_id)
            if task is not None:
                task["status"] = "failed"
                task["detail"] = "Verification service is unavailable."
                task["code"] = exc.category
                task["service_unavailable"] = True
                task["finished_at"] = django_timezone.now()
    except Exception:  # pragma: no cover - defensive background handling
        with _TASK_LOCK:
            task = _TASKS.get(task_id)
            if task is not None:
                task["status"] = "failed"
                task["detail"] = "Verification processing failed."
                task["code"] = "verification_processing_failed"
                task["service_unavailable"] = False
                task["finished_at"] = django_timezone.now()
    finally:
        try:
            os.remove(image_path)
        except OSError:
            pass


class VerificationCheckView(APIView):
    """
    POST /api/verification/check/

    Multipart fields: ``slot`` and ``file``.  The uploaded image is validated,
    re-encoded without metadata, and queued for asynchronous verification.  The
    endpoint returns immediately with a ``task_id``; poll
    ``/api/verification/status/{task_id}/`` for the final result.
    """

    permission_classes = [IsDriver]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "verification-check"

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
            task_id = _submit_verification_task(
                image_bytes=image_bytes,
                slot=slot,
                debug_enabled=False,
                user_id=getattr(request.user, "id", None),
            )
        except RuntimeError as exc:
            if str(exc) == "too_many_pending_tasks":
                return Response(
                    {"detail": "Too many pending verification tasks.", "code": "too_many_pending_tasks"},
                    status=status.HTTP_429_TOO_MANY_REQUESTS,
                )
            return Response(
                {"detail": "Verification service is busy.", "code": "verification_service_busy"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        return Response(
            {"task_id": task_id, "status": "pending"},
            status=status.HTTP_202_ACCEPTED,
        )


class VerificationStatusView(APIView):
    """GET /api/verification/status/{task_id}/"""

    permission_classes = [IsDriver]

    def get(self, request, task_id: str):
        with _TASK_LOCK:
            task = _TASKS.get(task_id)

        if task is None or task.get("user_id") is None or task.get("user_id") != getattr(request.user, "id", None):
            return Response(
                {"detail": "Unknown verification task.", "code": "unknown_task"},
                status=status.HTTP_404_NOT_FOUND,
            )

        task_status = task["status"]
        if task_status in {"pending", "processing"}:
            return Response(
                {"task_id": task_id, "status": task_status},
                status=status.HTTP_202_ACCEPTED,
            )

        if task_status == "failed":
            body = {
                "task_id": task_id,
                "status": "failed",
                "detail": task.get("detail", "Verification processing failed."),
                "code": task.get("code", "verification_processing_failed"),
            }
            if task.get("service_unavailable"):
                return Response(body, status=status.HTTP_503_SERVICE_UNAVAILABLE)
            return Response(body, status=status.HTTP_200_OK)

        result = dict(task.get("result") or {})
        result["task_id"] = task_id
        result["status"] = "completed"
        return Response(result, status=status.HTTP_200_OK)


def _persist_verification_result(task_id, slot, user_id, response):
    if user_id is None:
        return
    try:
        profile = DriverProfile.objects.get(user_id=user_id)
    except DriverProfile.DoesNotExist:
        return
    result = {
        "final_decision": response.get("final_decision"),
        "quality_status": response.get("quality_status"),
        "reason_codes": list(response.get("reason_codes") or []),
        "documents_version": profile.documents_version,
        "created_at": django_timezone.now().isoformat(),
    }
    results = dict(profile.verification_results or {})
    results[slot] = result
    counts = dict(profile.verification_reject_counts or {})
    if result["final_decision"] == "REJECT":
        counts[slot] = int(counts.get(slot, 0)) + 1
    else:
        counts[slot] = 0
    profile.verification_results = results
    profile.verification_reject_counts = counts
    profile.save(update_fields=["verification_results", "verification_reject_counts", "updated_at"])
