from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from .models import User


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    """Admin for the custom user model — role choices come from the model."""

    list_display = ('username', 'email', 'role', 'email_verified', 'is_staff', 'is_active')
    list_filter = ('role', 'email_verified', 'is_staff', 'is_superuser', 'is_active')
    search_fields = ('username', 'email')
    ordering = ('-date_joined',)

    # Add the ORIZZONTE-specific fields to both the edit and add forms so the
    # admin exposes the same role values as the API (client / driver / boss).
    fieldsets = DjangoUserAdmin.fieldsets + (
        ('ORIZZONTE', {'fields': ('role', 'email_verified')}),
    )
    add_fieldsets = DjangoUserAdmin.add_fieldsets + (
        ('ORIZZONTE', {'fields': ('role', 'email_verified')}),
    )
