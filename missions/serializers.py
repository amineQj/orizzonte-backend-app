"""
missions/serializers.py
~~~~~~~~~~~~~~~~~~~~~~~
Serializers handle validation and representation only.
No business logic — that lives in services.py.
"""
from __future__ import annotations
 
import re
from decimal import Decimal
  
from rest_framework import serializers
  
from .models import Mission, MissionStatus, TruckType, TruckCategory, TruckSize
from .services import calculate_price, MAX_WORKERS, TRUCK_SIZE_SURCHARGE
from .pricing import (
    MAX_DURATION_MINUTES,
    MAX_HANDLING_MINUTES,
    NEW_PRICING_FIELDS,
    VEHICLE_CLASSES,
)

# Algerian mobile number pattern: exactly 10 digits starting with 05, 06, or 07.
_PHONE_RE = re.compile(r"^0[567]\d{8}$")


def _has_new_pricing_fields(initial_data) -> bool:
    """True when the client explicitly opted into the new pricing engine."""
    return any(
        initial_data.get(field) is not None for field in NEW_PRICING_FIELDS
    )


def _validate_coordinates(attrs) -> None:
    """Reject impossible latitude/longitude values with a clear 400."""
    for field in ("departure_latitude", "arrival_latitude"):
        value = attrs.get(field)
        if value is not None and not (
            Decimal("-90") <= Decimal(value) <= Decimal("90")
        ):
            raise serializers.ValidationError(
                {field: "Latitude must be between -90 and 90."}
            )
    for field in ("departure_longitude", "arrival_longitude"):
        value = attrs.get(field)
        if value is not None and not (
            Decimal("-180") <= Decimal(value) <= Decimal("180")
        ):
            raise serializers.ValidationError(
                {field: "Longitude must be between -180 and 180."}
            )

 
 
# ---------------------------------------------------------------------------
# Step 1 — Price estimation (no DB write, client-facing)
# Intentionally does NOT include phone_number or shipment_type:
# those fields are only relevant at confirmation time.
# ---------------------------------------------------------------------------
 
class PriceEstimateSerializer(serializers.Serializer):
    """
    Validates the client's trip parameters and returns a price estimate.
    No Mission is created at this stage.
  
    phone_number and shipment_type are deliberately absent — the estimate
    step is purely about calculating a price, not collecting client details.

    New pricing fields (vehicle_class, helpers, floors, handling_minutes,
    duration_min) are optional. Their presence — or an explicit vehicle
    class — switches the request to the new pricing engine. Requests without
    them keep the historical amount and response shape for compatibility.
    """
  
    truck_type = serializers.ChoiceField(choices=TruckType.choices)
    category = serializers.ChoiceField(choices=TruckCategory.choices)
    capacity = serializers.IntegerField(min_value=1)
    size = serializers.ChoiceField(
        choices=TruckSize.choices,
        required=False,
        help_text="Truck size — determines the vehicle class when not given explicitly.",
    )
  
    departure_latitude = serializers.DecimalField(max_digits=9, decimal_places=6)
    departure_longitude = serializers.DecimalField(max_digits=9, decimal_places=6)
    arrival_latitude = serializers.DecimalField(max_digits=9, decimal_places=6)
    arrival_longitude = serializers.DecimalField(max_digits=9, decimal_places=6)
  
    distance = serializers.DecimalField(
        max_digits=8,
        decimal_places=2,
        min_value=Decimal("0.01"),
        required=False,
        help_text="Distance in km. Optional on the new pricing path (haversine fallback).",
    )
    date = serializers.DateField()
    time = serializers.TimeField()
    workers = serializers.IntegerField(
        min_value=0, max_value=MAX_WORKERS, required=False, default=0
    )

    # ── New pricing-engine inputs (all optional) ─────────────────────────────
    vehicle_class = serializers.ChoiceField(
        choices=[(name, name.title()) for name in VEHICLE_CLASSES],
        required=False,
        help_text="Explicit pricing class: standard, van or truck.",
    )
    helpers = serializers.IntegerField(min_value=0, required=False)
    floors = serializers.IntegerField(min_value=0, required=False)
    handling_minutes = serializers.IntegerField(
        min_value=0, max_value=MAX_HANDLING_MINUTES, required=False
    )
    duration_min = serializers.IntegerField(
        min_value=1, max_value=MAX_DURATION_MINUTES, required=False
    )
  
    def validate(self, attrs):
        new_flow = _has_new_pricing_fields(self.initial_data)

        if not new_flow:
            # Legacy path: keep exactly the historical requirements and amount.
            if attrs.get("distance") is None:
                raise serializers.ValidationError(
                    {"distance": "This field is required."}
                )
            if not attrs.get("size"):
                raise serializers.ValidationError(
                    {"size": "This field is required."}
                )
            attrs["estimated_price"] = calculate_price(
                attrs["distance"], attrs.get("workers", 0), attrs["size"]
            )

        attrs["pricing_new_flow"] = new_flow
        _validate_coordinates(attrs)
        return attrs
 
 
# ---------------------------------------------------------------------------
# Step 2 — Mission confirmation (creates Mission in DB)
# ---------------------------------------------------------------------------
 
class MissionCreateSerializer(serializers.Serializer):
    """
    Validates all fields required to confirm and persist a Mission.
 
    Extends the estimate fields with:
      - shipment_type: what kind of goods are being transported (listed first,
                       as it is the primary attribute the client selects).
      - phone_number:  client contact number, validated to Algerian mobile
                       format (05/06/07 + 8 digits = 10 digits total).
 
    Price is always recalculated server-side — never trusted from the client.
    """
 
    # ── New confirm-only fields ──────────────────────────────────────────────
    # shipment_type is intentionally first: it is the primary selection in UI.
    shipment_type = serializers.CharField(
        max_length=50,
        help_text=(
            "Type of goods being transported. "
            "Examples: house_moving, commercial_goods, heavy_equipment."
        ),
    )
    phone_number = serializers.CharField(
        max_length=10,
        help_text="Algerian mobile number: starts with 05, 06, or 07; exactly 10 digits.",
    )
 
    # ── Trip fields (same as estimate) ──────────────────────────────────────
    truck_type = serializers.ChoiceField(choices=TruckType.choices)
    category = serializers.ChoiceField(choices=TruckCategory.choices)
    capacity = serializers.IntegerField(min_value=1)
    size = serializers.ChoiceField(
        choices=TruckSize.choices,
        required=False,
        help_text="Truck size — determines the vehicle class when not given explicitly.",
    )
  
    departure_latitude = serializers.DecimalField(max_digits=9, decimal_places=6)
    departure_longitude = serializers.DecimalField(max_digits=9, decimal_places=6)
    arrival_latitude = serializers.DecimalField(max_digits=9, decimal_places=6)
    arrival_longitude = serializers.DecimalField(max_digits=9, decimal_places=6)
  
    distance = serializers.DecimalField(
        max_digits=8,
        decimal_places=2,
        min_value=Decimal("0.01"),
        required=False,
        help_text="Required for legacy confirmations; a quote may supply it.",
    )
    date = serializers.DateField(required=False)
    time = serializers.TimeField(required=False)
    workers = serializers.IntegerField(
        min_value=0, max_value=MAX_WORKERS, required=False, default=0
    )

    # ── New pricing-engine inputs (all optional) ─────────────────────────────
    vehicle_class = serializers.ChoiceField(
        choices=[(name, name.title()) for name in VEHICLE_CLASSES],
        required=False,
    )
    helpers = serializers.IntegerField(min_value=0, required=False)
    floors = serializers.IntegerField(min_value=0, required=False)
    handling_minutes = serializers.IntegerField(
        min_value=0, max_value=MAX_HANDLING_MINUTES, required=False
    )
    duration_min = serializers.IntegerField(
        min_value=1, max_value=MAX_DURATION_MINUTES, required=False
    )
    quote_id = serializers.UUIDField(
        required=False,
        help_text="Server-issued quote to confirm at the quoted price.",
    )
    negotiable = serializers.BooleanField(required=False, default=False)
  
    def validate_phone_number(self, value: str) -> str:
        """
        Enforce Algerian mobile number format:
          - Exactly 10 digits
          - Must start with 05, 06, or 07
 
        Examples of valid numbers:   0551234567, 0661234567, 0771234567
        Examples of invalid numbers: 0412345678 (wrong prefix),
                                     055123456  (9 digits),
                                     055123456a (non-digit character)
        """
        if not _PHONE_RE.match(value):
            raise serializers.ValidationError(
                "Phone number must be exactly 10 digits and start with 05, 06, or 07. "
                f"Got: '{value}'."
            )
        return value
 
    def validate_shipment_type(self, value: str) -> str:
        """Normalise to lowercase and strip surrounding whitespace."""
        return value.strip().lower()

    def validate(self, attrs):
        """
        Route the confirmation to the legacy or the new pricing path.

        * ``quote_id`` present — the quote supplies distance, schedule and
          pricing inputs; any conflicting value is rejected later.
        * new pricing fields present — the price is computed by the engine.
        * otherwise — the historical formula applies and the legacy month
          fields stay mandatory.
        """
        has_quote = attrs.get("quote_id") is not None
        new_flow = has_quote or _has_new_pricing_fields(self.initial_data)

        if not has_quote:
            missing = {}
            if attrs.get("distance") is None:
                missing["distance"] = "This field is required."
            if attrs.get("date") is None:
                missing["date"] = "This field is required."
            if attrs.get("time") is None:
                missing["time"] = "This field is required."
            if missing:
                raise serializers.ValidationError(missing)
            if not new_flow and not attrs.get("size"):
                raise serializers.ValidationError(
                    {"size": "This field is required."}
                )

        attrs["pricing_new_flow"] = new_flow
        _validate_coordinates(attrs)
        return attrs
  
  
# ---------------------------------------------------------------------------
# Mission read serializers — different verbosity per audience.
# Both phone_number and shipment_type are visible in all responses so that
# drivers and the boss have complete context for every mission.
# ---------------------------------------------------------------------------
 
class MissionDriverListSerializer(serializers.ModelSerializer):
    """
    Serializer for the driver dashboard.
 
    Computed fields for frontend colour-coding:
      display_status  → one of: "available" | "taken" | "in_progress" | "completed"
      is_taken        → True when the mission is no longer open to accept
      taken_by        → name of the assigned driver (or None)
 
    Frontend mapping:
      available   → green   (pending, can be accepted)
      taken       → red     (accepted by someone else)
      in_progress → orange  (currently being executed)
      completed   → grey    (finished)
    """
 
    display_status = serializers.SerializerMethodField()
    is_taken = serializers.SerializerMethodField()
    taken_by = serializers.SerializerMethodField()
    phone_number = serializers.SerializerMethodField()

    class Meta:
        model = Mission
        fields = [
            "id",
            # Confirm-only fields first, matching the importance in the workflow
            "shipment_type",
            "phone_number",
            # Truck
            "truck_type",
            "category",
            "capacity",
            "size",
            # Route
            "departure_latitude",
            "departure_longitude",
            "arrival_latitude",
            "arrival_longitude",
            "distance",
            # Schedule
            "date",
            "time",
            "workers",
            # Financials & status
            "price",
            "driver_net",
            "commission_rate",
            "status",
            "display_status",
            "is_taken",
            "taken_by",
            "created_at",
        ]
 
    def get_display_status(self, obj: Mission) -> str:
        """
        Map internal status to a frontend display hint.
 
        pending     → "available"   (green  — open to accept)
        accepted    → "taken"       (red    — locked to one driver)
        in_progress → "in_progress" (orange — being executed)
        completed   → "completed"   (grey   — finished)
        """
        return {
            MissionStatus.PENDING:     "available",
            MissionStatus.ACCEPTED:    "taken",
            MissionStatus.IN_PROGRESS: "in_progress",
            MissionStatus.COMPLETED:   "completed",
        }.get(obj.status, obj.status)  # fallback to raw value for forward-compat
 
    def get_is_taken(self, obj: Mission) -> bool:
        """True for any status that is no longer open to accept."""
        return obj.status != MissionStatus.PENDING
 
    def get_taken_by(self, obj: Mission) -> str | None:
        if obj.assigned_employee:
            emp = obj.assigned_employee
            full_name = f"{emp.first_name} {emp.last_name}".strip()
            return full_name or emp.username
        return None

    def get_phone_number(self, obj: Mission):
        request = self.context.get("request") if isinstance(self.context, dict) else None
        user = getattr(request, "user", None)
        if user is not None and getattr(user, "is_authenticated", False):
            if getattr(user, "is_staff", False) or getattr(user, "is_superuser", False):
                return obj.phone_number
            if obj.assigned_employee_id is not None and obj.assigned_employee_id == getattr(user, "pk", None):
                return obj.phone_number
        return None
 
 
class MissionBossSerializer(serializers.ModelSerializer):
    """
    Full-detail serializer for the boss dashboard.
    Includes client info, assigned driver info, price, and status.
    """
 
    client = serializers.SerializerMethodField()
    assigned_employee = serializers.SerializerMethodField()
 
    class Meta:
        model = Mission
        fields = [
            "id",
            "client",
            # Confirm-only fields
            "shipment_type",
            "phone_number",
            # Truck
            "truck_type",
            "category",
            "capacity",
            "size",
            # Route
            "departure_latitude",
            "departure_longitude",
            "arrival_latitude",
            "arrival_longitude",
            "distance",
            # Schedule
            "date",
            "time",
            "workers",
            # Financials & lifecycle
            "price",
            "negotiable",
            "pricing_config_version",
            "commission_rate",
            "commission_amount",
            "driver_net",
            "price_locked_at",
            "status",
            "assigned_employee",
            "created_at",
            "updated_at",
        ]
 
    def get_client(self, obj: Mission) -> dict:
        u = obj.client
        return {
            "id": u.pk,
            "username": u.username,
            "full_name": f"{u.first_name} {u.last_name}".strip() or u.username,
            "email": u.email,
        }
 
    def get_assigned_employee(self, obj: Mission) -> dict | None:
        if not obj.assigned_employee:
            return None
        u = obj.assigned_employee
        return {
            "id": u.pk,
            "username": u.username,
            "full_name": f"{u.first_name} {u.last_name}".strip() or u.username,
        }
 
 
class MissionClientSerializer(serializers.ModelSerializer):
    """Read-only serializer returned to the client after mission confirmation."""
 
    class Meta:
        model = Mission
        fields = [
            "id",
            "shipment_type",
            "phone_number",
            "truck_type",
            "category",
            "capacity",
            "size",
            "departure_latitude",
            "departure_longitude",
            "arrival_latitude",
            "arrival_longitude",
            "distance",
            "date",
            "time",
            "workers",
            "price",
            "negotiable",
            "status",
            "created_at",
        ]
        read_only_fields = fields
 
 
# ---------------------------------------------------------------------------
# Boss dashboard analytics serializer
# ---------------------------------------------------------------------------
 
class MonthlyOverviewEntrySerializer(serializers.Serializer):
    """One entry in the monthly_overview list."""
    month     = serializers.CharField()
    year      = serializers.IntegerField()
    completed = serializers.IntegerField()
 
 
class BossDashboardSerializer(serializers.Serializer):
    """
    Validates and documents the shape of the boss dashboard response.
 
    Using an explicit serializer (rather than returning the dict directly)
    means the response contract is self-documenting and regressions in the
    service layer will surface as serializer validation errors.
    """
    total_missions_month  = serializers.IntegerField(min_value=0)
    completed_missions    = serializers.IntegerField(min_value=0)
    completion_percentage = serializers.FloatField(min_value=0, max_value=100)
    today_missions        = serializers.IntegerField(min_value=0)
    monthly_revenue       = serializers.DecimalField(max_digits=14, decimal_places=2)
    monthly_overview      = MonthlyOverviewEntrySerializer(many=True)
 


# ---------------------------------------------------------------------------
# Pricing configuration management (boss-only)
# ---------------------------------------------------------------------------


class PricingConfigSerializer(serializers.Serializer):
    """Read serializer for a PricingConfig row (boss view)."""

    version = serializers.IntegerField(read_only=True)
    calibrated = serializers.BooleanField(read_only=True)
    created_at = serializers.DateTimeField(read_only=True)
    updated_at = serializers.DateTimeField(read_only=True)
    parameters = serializers.JSONField(read_only=True)


class PricingConfigUpdateSerializer(serializers.Serializer):
    """Validate the update request body."""

    password = serializers.CharField(write_only=True, required=True)
    parameters = serializers.JSONField(required=True)

    def validate_parameters(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError("Parameters must be a JSON object.")
        value.pop("version", None)
        value.pop("calibrated", None)
        return value
