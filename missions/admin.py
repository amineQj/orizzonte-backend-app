from django.contrib import admin
from .models import (
    Mission,
    Negotiation,
    NegotiationOffer,
    PricingAuditLog,
    PricingConfig,
    PricingOverride,
    Quote,
)


@admin.register(Mission)
class MissionAdmin(admin.ModelAdmin):
    list_display = [
        "id",
        "client",
        "shipment_type",
        "phone_number",
        "truck_type",
        "category",
        "distance",
        "price",
        "status",
        "assigned_employee",
        "held_by",
        "hold_expires_at",
        "date",
        "created_at",
    ]
    list_filter = ["status", "truck_type", "category", "shipment_type", "date"]
    search_fields = [
        "client__username",
        "assigned_employee__username",
        "phone_number",
        "shipment_type",
    ]
    readonly_fields = [
        "price",
        "quote",
        "pricing_config_version",
        "price_components",
        "commission_rate",
        "commission_amount",
        "driver_net",
        "price_locked_at",
        "held_by",
        "hold_expires_at",
        "created_at",
        "updated_at",
    ]
    raw_id_fields = ["client", "assigned_employee", "held_by"]

    fieldsets = (
        ("Ownership", {"fields": ("client", "assigned_employee", "status")}),
        (
            "Shipment & Contact",
            {"fields": ("shipment_type", "phone_number")},
        ),
        (
            "Truck",
            {"fields": ("truck_type", "category", "capacity", "size")},
        ),
        (
            "Trip",
            {
                "fields": (
                    "departure_latitude",
                    "departure_longitude",
                    "arrival_latitude",
                    "arrival_longitude",
                    "distance",
                    "date",
                    "time",
                    "workers",
                )
            },
        ),
        ("Financials", {"fields": ("price", "negotiable")}),
        (
            "Pricing snapshot (read-only)",
            {
                "fields": (
                    "quote",
                    "pricing_config_version",
                    "price_components",
                    "commission_rate",
                    "commission_amount",
                    "driver_net",
                    "price_locked_at",
                )
            },
        ),
        (
            "Negotiation hold (read-only)",
            {"fields": ("held_by", "hold_expires_at")},
        ),
        ("Audit", {"fields": ("created_at", "updated_at")}),
    )


@admin.register(PricingConfig)
class PricingConfigAdmin(admin.ModelAdmin):
    list_display = ["version", "calibrated", "is_active", "updated_at"]
    list_filter = ["calibrated", "is_active"]
    readonly_fields = ["created_at", "updated_at"]
    search_fields = ["notes"]

    def save_model(self, request, obj, form, change):
        if obj.created_by_id is None and request.user.is_authenticated:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(Quote)
class QuoteAdmin(admin.ModelAdmin):
    list_display = [
        "public_id",
        "client",
        "amount",
        "confidence",
        "calibrated",
        "created_at",
        "expires_at",
        "used_at",
    ]
    list_filter = ["confidence", "calibrated"]
    search_fields = ["public_id", "client__username"]
    readonly_fields = [
        "public_id",
        "client",
        "config",
        "config_version",
        "inputs",
        "components",
        "amount",
        "low",
        "high",
        "currency",
        "confidence",
        "calibrated",
        "created_at",
        "expires_at",
        "used_at",
        "mission",
    ]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Negotiation)
class NegotiationAdmin(admin.ModelAdmin):
    list_display = [
        "id",
        "mission",
        "driver",
        "status",
        "initial_price",
        "accepted_amount",
        "created_at",
        "expires_at",
    ]
    list_filter = ["status", "calibrated"]
    search_fields = ["mission__id", "driver__username"]
    raw_id_fields = ["mission", "client", "driver", "accepted_offer"]
    readonly_fields = [
        "mission",
        "client",
        "driver",
        "status",
        "config_version",
        "calibrated",
        "initial_price",
        "min_amount",
        "max_amount",
        "min_fare",
        "tolerance",
        "max_offers",
        "min_step",
        "offer_ttl_minutes",
        "total_ttl_minutes",
        "commission_rate",
        "accepted_offer",
        "accepted_amount",
        "commission_amount",
        "driver_net",
        "created_at",
        "updated_at",
        "expires_at",
        "accepted_at",
        "closed_at",
    ]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(NegotiationOffer)
class NegotiationOfferAdmin(admin.ModelAdmin):
    list_display = [
        "id",
        "negotiation",
        "sequence",
        "author_role",
        "amount",
        "is_final",
        "created_at",
    ]
    list_filter = ["author_role", "is_final"]
    search_fields = ["negotiation__id"]
    raw_id_fields = ["negotiation", "author"]
    readonly_fields = [
        "negotiation",
        "author",
        "author_role",
        "sequence",
        "amount",
        "is_final",
        "idempotency_key",
        "created_at",
    ]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(PricingOverride)
class PricingOverrideAdmin(admin.ModelAdmin):
    list_display = [
        "id",
        "mission",
        "client",
        "multiplier",
        "expires_at",
        "created_by",
        "created_at",
    ]
    list_filter = ["expires_at"]
    raw_id_fields = ["mission", "client", "created_by"]
    readonly_fields = ["created_at"]


@admin.register(PricingAuditLog)
class PricingAuditLogAdmin(admin.ModelAdmin):
    list_display = ["id", "created_at", "action", "actor", "mission", "quote", "override"]
    list_filter = ["action"]
    readonly_fields = [
        "actor",
        "action",
        "mission",
        "quote",
        "override",
        "payload",
        "created_at",
    ]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

