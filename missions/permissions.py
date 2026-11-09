"""
missions/permissions.py
~~~~~~~~~~~~~~~~~~~~~~~
Role-based DRF permission classes.

The User model has a `role` field with values:
    "client" | "driver" | "boss"
"""
from rest_framework.permissions import IsAuthenticated


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
