"""
drivers/models.py
~~~~~~~~~~~~~~~~~

The driver profile and its review journal.

A ``DriverProfile`` is created automatically (in the same transaction) when a
driver signs up. It references the custom user model through
``settings.AUTH_USER_MODEL`` so this app never imports ``missions``.
"""
import os
import uuid

from django.conf import settings
from django.db import models

from .constants import DOCUMENT_TYPES, INFO_FIELDS
from .storage import private_media_storage


def _document_upload_to(instance, filename):
    """
    Random file name, extension taken from the original name as a last
    resort. The API always assigns UUID names explicitly through the
    storage layer; this is only a safety net (e.g. Django admin).
    """
    ext = os.path.splitext(filename or "")[1].lower()
    if ext not in {".jpg", ".jpeg", ".png", ".webp"}:
        ext = ".bin"
    return f"documents/{uuid.uuid4().hex}{ext}"


class DriverProfile(models.Model):
    class Status(models.TextChoices):
        INCOMPLETE = "incomplete", "Incomplete"
        PENDING_REVIEW = "pending_review", "Pending review"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    class ReviewedByType(models.TextChoices):
        NONE = "", "—"
        AI = "ai", "AI agent"
        BOSS = "boss", "Boss"

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="driver_profile",
    )

    # ── Identity ────────────────────────────────────────────────────────
    full_name = models.CharField(max_length=200, blank=True, default="")
    phone = models.CharField(max_length=20, blank=True, default="")
    date_of_birth = models.DateField(null=True, blank=True)
    national_id_number = models.CharField(
        max_length=64, null=True, blank=True, unique=True
    )
    driving_license_number = models.CharField(
        max_length=64, null=True, blank=True, unique=True
    )
    driving_license_expiry = models.DateField(null=True, blank=True)
    vehicle_plate = models.CharField(max_length=32, blank=True, default="")

    # ── Private documents ───────────────────────────────────────────────
    selfie = models.FileField(
        upload_to=_document_upload_to, storage=private_media_storage, blank=True, null=True
    )
    id_card_front = models.FileField(
        upload_to=_document_upload_to, storage=private_media_storage, blank=True, null=True
    )
    id_card_back = models.FileField(
        upload_to=_document_upload_to, storage=private_media_storage, blank=True, null=True
    )
    driving_license = models.FileField(
        upload_to=_document_upload_to, storage=private_media_storage, blank=True, null=True
    )
    vehicle_registration = models.FileField(
        upload_to=_document_upload_to, storage=private_media_storage, blank=True, null=True
    )

    # ── Review state (read-only for the driver) ─────────────────────────
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.INCOMPLETE,
        db_index=True,
    )
    rejection_reason = models.TextField(blank=True, default="")
    reviewed_by_type = models.CharField(
        max_length=10, choices=ReviewedByType.choices, blank=True, default=""
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="driver_reviews_performed",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    documents_version = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Driver profile"
        verbose_name_plural = "Driver profiles"

    def __str__(self):
        return f"DriverProfile<{self.user_id}> [{self.status}]"

    # ── Completeness (single source of truth) ──────────────────────────
    @property
    def missing_info(self):
        missing = []
        for field in INFO_FIELDS:
            value = getattr(self, field)
            if value is None or (isinstance(value, str) and not value.strip()):
                missing.append(field)
        return missing

    @property
    def missing_documents(self):
        return [name for name in DOCUMENT_TYPES if not getattr(self, name)]

    def missing_fields(self):
        """``{"info": [...], "documents": [...]}`` — used everywhere."""
        return {"info": self.missing_info, "documents": self.missing_documents}

    @property
    def is_complete(self):
        return not self.missing_info and not self.missing_documents


class DriverReviewLog(models.Model):
    """Immutable journal of every review decision (boss and AI agent)."""

    class Source(models.TextChoices):
        AI = "ai", "AI agent"
        BOSS = "boss", "Boss"

    class Decision(models.TextChoices):
        APPROVE = "approve", "Approve"
        REJECT = "reject", "Reject"
        MANUAL_REVIEW = "manual_review", "Manual review"

    profile = models.ForeignKey(
        DriverProfile, on_delete=models.CASCADE, related_name="review_logs"
    )
    source = models.CharField(max_length=10, choices=Source.choices)
    decision = models.CharField(max_length=20, choices=Decision.choices)
    reasons = models.JSONField(default=list, blank=True)
    confidence = models.FloatField(null=True, blank=True)
    documents_version = models.PositiveIntegerField(default=0)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="driver_review_logs",
    )
    applied = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Driver review log"
        verbose_name_plural = "Driver review logs"

    def __str__(self):
        return f"{self.source}:{self.decision} (profile={self.profile_id})"
