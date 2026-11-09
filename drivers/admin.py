from django.contrib import admin

from .models import DriverProfile, DriverReviewLog


@admin.register(DriverProfile)
class DriverProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "status", "documents_version", "updated_at")
    list_filter = ("status", "reviewed_by_type")
    search_fields = ("user__username", "user__email", "full_name", "vehicle_plate")
    readonly_fields = ("documents_version", "created_at", "updated_at", "reviewed_at")


@admin.register(DriverReviewLog)
class DriverReviewLogAdmin(admin.ModelAdmin):
    list_display = ("profile", "source", "decision", "applied", "confidence", "created_at")
    list_filter = ("source", "decision", "applied")
    search_fields = ("profile__user__username",)
    readonly_fields = ("created_at",)
