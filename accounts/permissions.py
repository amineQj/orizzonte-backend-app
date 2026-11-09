"""
accounts/permissions.py

Custom DRF permission classes for role-based access control.
Roles: "boss" | "driver" | "client".

Usage in any view:
    from accounts.permissions import IsBoss, IsDriver, IsClient

    class MyView(APIView):
        permission_classes = [IsAuthenticated, IsBoss]
"""
from rest_framework.permissions import BasePermission


class IsBoss(BasePermission):
    """Allow access only to users whose role is 'boss'."""
    message = "Access denied. Only the Boss can perform this action."

    def has_permission(self, request, view):
        return (
            request.user
            and request.user.is_authenticated
            and request.user.is_boss
        )


class IsDriver(BasePermission):
    """Allow access only to users whose role is 'driver'."""
    message = "Access denied. This endpoint is for drivers only."

    def has_permission(self, request, view):
        return (
            request.user
            and request.user.is_authenticated
            and request.user.is_driver
        )


class IsClient(BasePermission):
    """Allow access only to users whose role is 'client'."""
    message = "Access denied. This endpoint is for clients only."

    def has_permission(self, request, view):
        return (
            request.user
            and request.user.is_authenticated
            and request.user.is_client
        )


class IsBossOrDriver(BasePermission):
    """Allow access to boss or drivers (internal staff)."""
    message = "Access denied. Staff only."

    def has_permission(self, request, view):
        return (
            request.user
            and request.user.is_authenticated
            and request.user.role in ('boss', 'driver')
        )
