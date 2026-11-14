"""
missions/permissions.py
~~~~~~~~~~~~~~~~~~~~~~~
Role-based DRF permission classes.

The User model has a `role` field with values:
    "client" | "driver" | "boss"
"""
from rest_framework.permissions import IsAuthenticated

from drivers.models import DriverProfile


class IsClient(IsAuthenticated):
    """Allow access only to authenticated users with role='client'."""

    def has_permission(self, request, view) -> bool:
        return (
            super().has_permission(request, view)
            and getattr(request.user, "role", None) == "client"
        )


class IsDriver(IsAuthenticated):
    """Allow access only to authenticated users with role='driver'."""

    def has_permission(self, request, view) -> bool:
        return (
            super().has_permission(request, view)
            and getattr(request.user, "role", None) == "driver"
        )


class IsBoss(IsAuthenticated):
    """Allow access only to authenticated users with role='boss'."""

    def has_permission(self, request, view) -> bool:
        return (
            super().has_permission(request, view)
            and getattr(request.user, "role", None) == "boss"
        )


class IsDriverOrBoss(IsAuthenticated):
    """Allow access to drivers and bosses."""

    def has_permission(self, request, view) -> bool:
        return (
            super().has_permission(request, view)
            and getattr(request.user, "role", None) in ("driver", "boss")
        )


class IsClientOrApprovedDriver(IsAuthenticated):
    """Allow a client, or a driver whose profile is approved.

    Used by the negotiation endpoints, which both sides call. A driver whose
    profile is not approved receives the stable ``driver_not_approved`` body
    (403), exactly like the existing accept/start endpoints.
    """

    message = "Access denied. Your driver profile is not approved."

    def has_permission(self, request, view) -> bool:
        if not super().has_permission(request, view):
            return False

        role = getattr(request.user, "role", None)
        if role == "client":
            return True
        if role != "driver":
            return False

        profile = getattr(request.user, "driver_profile", None)
        status = profile.status if profile is not None else DriverProfile.Status.INCOMPLETE
        if status != DriverProfile.Status.APPROVED:
            self.message = {
                "detail": "Your driver profile must be approved before you can "
                          "negotiate a mission.",
                "code": "driver_not_approved",
                "profile_status": status,
            }
            return False
        return True
