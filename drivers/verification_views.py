"""HTTP endpoint for the standalone quality and classification module."""

from __future__ import annotations

import os
import tempfile
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from django.conf import settings
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

_TASKS: dict[str, dict[str, Any]] = {}
_TASK_LOCK = threading.Lock()
_TASK_EXECUTOR = ThreadPoolExecutor(max_workers=4)


def _submit_verification_task(
    *,
    image_bytes: bytes,
    slot: str,
    debug_enabled: bool,
    user_id: int | None,
) -> str:
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
        with _TASK_LOCK:
            task = _TASKS.get(task_id)
            if task is not None:
                task["status"] = "completed"
                task["result"] = response
    except VerificationUnavailable as exc:
        with _TASK_LOCK:
            task = _TASKS.get(task_id)
            if task is not None:
                task["status"] = "failed"
                task["detail"] = "Verification service is unavailable."
                task["code"] = exc.category
    except Exception:  # pragma: no cover - defensive background handling
        with _TASK_LOCK:
            task = _TASKS.get(task_id)
            if task is not None:
                task["status"] = "failed"
                task["detail"] = "Verification processing failed."
                task["code"] = "verification_processing_failed"
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

        debug_enabled = bool(
            getattr(settings, "DEBUG", False)
            and getattr(settings, "VERIFICATION_AGENT_DEBUG_OCR", False)
            and request.query_params.get("debug") == "1"
        )

        task_id = _submit_verification_task(
            image_bytes=image_bytes,
            slot=slot,
            debug_enabled=debug_enabled,
            user_id=getattr(request.user, "id", None),
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

        if task is None or task.get("user_id") not in (None, getattr(request.user, "id", None)):
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
            return Response(
                {
                    "task_id": task_id,
                    "status": "failed",
                    "detail": task.get("detail", "Verification processing failed."),
                    "code": task.get("code", "verification_processing_failed"),
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        result = dict(task.get("result") or {})
        result["task_id"] = task_id
        result["status"] = "completed"
        return Response(result, status=status.HTTP_200_OK)