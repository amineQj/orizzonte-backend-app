"""Permission classes for the drivers app and for missions."""
from rest_framework.permissions import BasePermission

from accounts.permissions import IsDriver

from .models import DriverProfile


class IsApprovedDriver(IsDriver):
    """
    Allow a driver only when their profile status is ``approved``.

    Used by the mission endpoints ``accept``/``start``. The response body
    carries a stable machine-readable code::

        {
            "detail": "...",
            "code": "driver_not_approved",
            "profile_status": "incomplete" | "pending_review" | "rejected"
        }

    A driver without a profile is treated as ``incomplete`` (never a 500).
    """

    message = "Access denied. Your driver profile is not approved."

    def has_permission(self, request, view):
        if not super().has_permission(request, view):
            return False

        profile = getattr(request.user, "driver_profile", None)
        status = profile.status if profile is not None else DriverProfile.Status.INCOMPLETE
        if status != DriverProfile.Status.APPROVED:
            self.message = {
                "detail": "Your driver profile must be approved before you can "
                          "accept or start a mission.",
                "code": "driver_not_approved",
                "profile_status": status,
            }
            return False
        return True


class IsAgent(BasePermission):
    """Allow access only to a request authenticated with a valid agent key."""

    message = "Access denied. A valid X-Agent-Key header is required."

    def has_permission(self, request, view):
        return bool(getattr(request.user, "is_agent", False))


class CanAccessDriverDocuments(BasePermission):
    """
    Download permission for ``GET /api/drivers/<id>/documents/<type>/``.

    Allowed for: the driver who owns the documents, the boss, and the AI
    agent (``X-Agent-Key``). Anything else is refused (neutral message).
    """

    message = "Access denied."

    def has_permission(self, request, view):
        user = request.user

        if getattr(user, "is_agent", False):
            return True

        if not (user and user.is_authenticated):
            return False

        if getattr(user, "is_boss", False):
            return True

        return str(user.pk) == str(view.kwargs.get("pk"))
