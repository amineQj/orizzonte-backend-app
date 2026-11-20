"""Custom DRF exception handler preserving the project's error contract."""

import logging

from rest_framework import status
from rest_framework.views import exception_handler

logger = logging.getLogger(__name__)


def custom_exception_handler(exc, context):
    response = exception_handler(exc, context)
    if response is None:
        logger.exception("Unhandled exception in %s", context.get("view"))
        return None

    data = response.data
    if isinstance(data, dict):
        if "detail" not in data:
            if "message" in data:
                data["detail"] = data["message"]
            elif "errors" in data:
                data["detail"] = "Request validation failed."
            else:
                data["detail"] = "Request failed."
        if "code" not in data:
            data["code"] = getattr(exc, "default_code", "error")
    return response