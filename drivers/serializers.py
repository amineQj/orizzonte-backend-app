"""
drivers/serializers.py
~~~~~~~~~~~~~~~~~~~~~~

Request/response shapes for the driver, boss and AI-agent endpoints.

Security notes:
* the driver-editable serializer never exposes ``status`` / ``reviewed_*`` /
  ``documents_version``;
* document paths are never serialized — only an ``uploaded`` flag and the
  authenticated download URL;
* in boss lists, sensitive numbers are masked (last 4 characters kept).
"""
import re
from datetime import date

from django.urls import reverse
from rest_framework import serializers

from .constants import DOCUMENT_TYPES, DOCUMENT_TYPE_CHOICES
from .models import DriverProfile, DriverReviewLog


def mask_sensitive(value) -> str:
    """Keep only the last 4 characters of a sensitive value."""
    if not value:
        return ""
    value = str(value)
    if len(value) <= 4:
        return "*" * len(value)
    return "*" * (len(value) - 4) + value[-4:]


def _document_payload(profile, url_name: str) -> dict:
    payload = {}
    for name in DOCUMENT_TYPES:
        uploaded = bool(getattr(profile, name))
        payload[name] = {
            "uploaded": uploaded,
            "url": reverse(
                url_name,
                kwargs={"pk": profile.user_id, "doc_type": name},
            ),
        }
    return payload


# ─────────────────────────────────────────────────────────────
#  Driver-facing
# ─────────────────────────────────────────────────────────────

class DriverProfileSerializer(serializers.ModelSerializer):
    """Read representation returned to the owner of the profile."""

    missing = serializers.SerializerMethodField()
    documents = serializers.SerializerMethodField()

    class Meta:
        model = DriverProfile
        fields = [
            "user_id",
            "full_name",
            "phone",
            "date_of_birth",
            "national_id_number",
            "driving_license_number",
            "driving_license_expiry",
            "vehicle_plate",
            "status",
            "rejection_reason",
            "documents_version",
            "missing",
            "documents",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_missing(self, obj) -> dict:
        return obj.missing_fields()

    def get_documents(self, obj) -> dict:
        return _document_payload(obj, "driver-document")


class DriverProfileUpdateSerializer(serializers.ModelSerializer):
    """The only fields a driver may change (``status`` etc. are ignored)."""

    class Meta:
        model = DriverProfile
        fields = [
            "full_name",
            "phone",
            "date_of_birth",
            "national_id_number",
            "driving_license_number",
            "driving_license_expiry",
            "vehicle_plate",
        ]

    # -- validation ------------------------------------------------
    def validate_full_name(self, value):
        return (value or "").strip()

    def validate_phone(self, value):
        return (value or "").strip()

    def validate_vehicle_plate(self, value):
        return (value or "").strip().upper()

    def validate_date_of_birth(self, value):
        if value is None:
            return value
        today = date.today()
        age = (
            today.year
            - value.year
            - ((today.month, today.day) < (value.month, value.day))
        )
        if value > today:
            raise serializers.ValidationError("The date of birth cannot be in the future.")
        if age < 18:
            raise serializers.ValidationError(
                "The driver must be at least 18 years old."
            )
        return value

    def validate_driving_license_expiry(self, value):
        if value is not None and value <= date.today():
            raise serializers.ValidationError("The driving licence has expired.")
        return value

    def _unique(self, value, field):
        if not value:
            return None
        qs = DriverProfile.objects.filter(**{field: value})
        if self.instance is not None:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            # Neutral: never reveal whose number it is.
            raise serializers.ValidationError("This information is already in use.")
        return value

    def validate_national_id_number(self, value):
        return self._unique((value or "").strip() or None, "national_id_number")

    def validate_driving_license_number(self, value):
        return self._unique((value or "").strip() or None, "driving_license_number")


class DocumentUploadSerializer(serializers.Serializer):
    """Multipart body of ``POST /api/drivers/me/documents/``."""

    type = serializers.ChoiceField(
        choices=DOCUMENT_TYPE_CHOICES,
        error_messages={"invalid_choice": "Unknown document type."},
    )
    file = serializers.FileField()


class VerificationCheckSerializer(serializers.Serializer):
    """Multipart body of ``POST /api/verification/check/``."""

    slot = serializers.ChoiceField(
        choices=DOCUMENT_TYPE_CHOICES,
        error_messages={"invalid_choice": "Unknown document slot."},
    )
    file = serializers.FileField()


# ─────────────────────────────────────────────────────────────
#  Boss-facing
# ─────────────────────────────────────────────────────────────

class BossDriverListSerializer(serializers.ModelSerializer):
    """Masked representation used by the boss list/pending endpoints."""

    user_id = serializers.IntegerField(read_only=True)
    profile_status = serializers.CharField(source="status", read_only=True)
    national_id_number = serializers.SerializerMethodField()
    driving_license_number = serializers.SerializerMethodField()

    class Meta:
        model = DriverProfile
        fields = [
            "user_id",
            "full_name",
            "phone",
            "national_id_number",
            "driving_license_number",
            "vehicle_plate",
            "profile_status",
            "documents_version",
            "created_at",
            "updated_at",
        ]

    def get_national_id_number(self, obj) -> str:
        return mask_sensitive(obj.national_id_number)

    def get_driving_license_number(self, obj) -> str:
        return mask_sensitive(obj.driving_license_number)


class BossDriverDetailSerializer(serializers.ModelSerializer):
    """Full detail for the boss, with authenticated document links."""

    user_id = serializers.IntegerField(read_only=True)
    documents = serializers.SerializerMethodField()

    class Meta:
        model = DriverProfile
        fields = [
            "user_id",
            "full_name",
            "phone",
            "date_of_birth",
            "national_id_number",
            "driving_license_number",
            "driving_license_expiry",
            "vehicle_plate",
            "status",
            "rejection_reason",
            "reviewed_by_type",
            "reviewed_at",
            "documents_version",
            "documents",
            "created_at",
            "updated_at",
        ]

    def get_documents(self, obj) -> dict:
        return _document_payload(obj, "driver-document")


class DriverRejectSerializer(serializers.Serializer):
    reason = serializers.CharField(
        max_length=500,
        allow_blank=False,
        error_messages={"blank": "A rejection reason is required."},
    )

    def validate_reason(self, value):
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError("A rejection reason is required.")
        return value


# ─────────────────────────────────────────────────────────────
#  AI-agent-facing
# ─────────────────────────────────────────────────────────────

class AgentPendingDriverSerializer(serializers.ModelSerializer):
    """Minimal payload: no personal data, just what the agent must analyse."""

    user_id = serializers.IntegerField(read_only=True)

    class Meta:
        model = DriverProfile
        fields = ["user_id", "documents_version", "status", "updated_at"]


class AgentDriverDetailSerializer(serializers.ModelSerializer):
    user_id = serializers.IntegerField(read_only=True)
    documents = serializers.SerializerMethodField()

    class Meta:
        model = DriverProfile
        fields = [
            "user_id",
            "full_name",
            "phone",
            "date_of_birth",
            "national_id_number",
            "driving_license_number",
            "driving_license_expiry",
            "vehicle_plate",
            "status",
            "documents_version",
            "documents",
        ]

    def get_documents(self, obj) -> dict:
        return _document_payload(obj, "agent-driver-document")


_DIGIT_RUN = re.compile(r"\d{6,}")


class AgentReviewSerializer(serializers.Serializer):
    decision = serializers.ChoiceField(
        choices=[DriverReviewLog.Decision.APPROVE,
                 DriverReviewLog.Decision.REJECT,
                 DriverReviewLog.Decision.MANUAL_REVIEW]
    )
    reasons = serializers.ListField(
        child=serializers.CharField(max_length=200),
        required=False,
        default=list,
        max_length=20,
    )
    confidence = serializers.FloatField(
        min_value=0.0, max_value=1.0, required=False, allow_null=True
    )
    documents_version = serializers.IntegerField(min_value=0)

    def validate_reasons(self, value):
        for reason in value:
            if _DIGIT_RUN.search(reason):
                raise serializers.ValidationError(
                    "Reasons must not contain sensitive numbers."
                )
        return value
