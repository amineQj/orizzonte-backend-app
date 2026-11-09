"""
drivers/services.py
~~~~~~~~~~~~~~~~~~~

Business logic for driver profiles and the review workflow.

Every state change is wrapped in ``transaction.atomic()`` with
``select_for_update()`` so the driver, the boss and the AI agent can never
race each other.

This module never imports ``missions`` (dependency direction: missions →
drivers).
"""
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .constants import KEY_INFO_FIELDS
from .documents import (
    DocumentValidationError,  # re-exported for the views
    delete_document,
    process_image,
    store_document,
)
from .models import DriverProfile, DriverReviewLog


class ProfileTransitionError(Exception):
    """An invalid profile status transition (→ HTTP 409)."""


class MissingFieldsError(Exception):
    """Raised by ``submit_profile`` when the profile is incomplete (→ 400)."""

    def __init__(self, missing):
        self.missing = missing
        super().__init__("Profile is incomplete.")


class VersionConflictError(Exception):
    """The agent analysed an outdated ``documents_version`` (→ HTTP 409)."""

    def __init__(self, current: int):
        self.current = current
        super().__init__("The driver's documents changed since the analysis.")


# ─────────────────────────────────────────────────────────────
#  Profile lifecycle
# ─────────────────────────────────────────────────────────────

def get_or_create_profile(user) -> DriverProfile:
    """Return the driver's profile, creating an empty one if needed."""
    profile, _ = DriverProfile.objects.get_or_create(user=user)
    return profile


def create_driver_profile(user) -> DriverProfile:
    """Called from signup, inside the signup transaction."""
    profile, _ = DriverProfile.objects.get_or_create(user=user)
    return profile


def update_profile(profile, validated_data):
    """
    Apply driver-editable fields.

    Returns ``(profile, reopened)`` where *reopened* is True when an approved
    profile went back to ``pending_review`` because a key field changed.
    """
    with transaction.atomic():
        locked = DriverProfile.objects.select_for_update().get(pk=profile.pk)

        changed = []
        for field, value in validated_data.items():
            if getattr(locked, field) != value:
                setattr(locked, field, value)
                changed.append(field)

        if not changed:
            return locked, False

        locked.documents_version += 1
        reopened = False
        if (
            locked.status == DriverProfile.Status.APPROVED
            and any(field in KEY_INFO_FIELDS for field in changed)
        ):
            locked.status = DriverProfile.Status.PENDING_REVIEW
            reopened = True

        locked.save()

    return DriverProfile.objects.get(pk=profile.pk), reopened


def save_document(profile, doc_type, uploaded_file):
    """
    Validate, store and attach a document, replacing any previous file.

    An approved profile goes back to ``pending_review`` (any document change
    invalidates a previous approval). Raises ``DocumentValidationError``.
    """
    file_bytes, extension = process_image(uploaded_file)
    new_name = store_document(file_bytes, extension)

    try:
        with transaction.atomic():
            locked = DriverProfile.objects.select_for_update().get(pk=profile.pk)
            old_name = getattr(locked, doc_type).name

            setattr(locked, doc_type, new_name)
            locked.documents_version += 1
            if locked.status == DriverProfile.Status.APPROVED:
                locked.status = DriverProfile.Status.PENDING_REVIEW
            locked.save(
                update_fields=[doc_type, "documents_version", "status", "updated_at"]
            )
    except Exception:
        # The DB write failed → do not leave an orphan file behind.
        delete_document(new_name)
        raise

    if old_name and old_name != new_name:
        # Only remove the previous file once the DB reflects the new one.
        delete_document(old_name)

    return DriverProfile.objects.get(pk=profile.pk)


def submit_profile(profile) -> DriverProfile:
    """``incomplete``/``rejected`` → ``pending_review`` (when complete)."""
    with transaction.atomic():
        locked = DriverProfile.objects.select_for_update().get(pk=profile.pk)

        if locked.status == DriverProfile.Status.PENDING_REVIEW:
            raise ProfileTransitionError("This profile is already pending review.")
        if locked.status == DriverProfile.Status.APPROVED:
            raise ProfileTransitionError("This profile is already approved.")

        missing = locked.missing_fields()
        if missing["info"] or missing["documents"]:
            raise MissingFieldsError(missing)

        locked.status = DriverProfile.Status.PENDING_REVIEW
        locked.rejection_reason = ""
        locked.save(update_fields=["status", "rejection_reason", "updated_at"])

    return DriverProfile.objects.get(pk=profile.pk)


# ─────────────────────────────────────────────────────────────
#  Boss decisions
# ─────────────────────────────────────────────────────────────

def approve_profile(profile, boss) -> DriverProfile:
    """Boss approves (or corrects to approved). Journalised."""
    with transaction.atomic():
        locked = DriverProfile.objects.select_for_update().get(pk=profile.pk)
        if locked.status == DriverProfile.Status.INCOMPLETE:
            raise ProfileTransitionError(
                "Cannot approve an incomplete profile."
            )

        locked.status = DriverProfile.Status.APPROVED
        locked.rejection_reason = ""
        locked.reviewed_by_type = DriverProfile.ReviewedByType.BOSS
        locked.reviewed_by = boss
        locked.reviewed_at = timezone.now()
        locked.save(
            update_fields=[
                "status",
                "rejection_reason",
                "reviewed_by_type",
                "reviewed_by",
                "reviewed_at",
                "updated_at",
            ]
        )

        DriverReviewLog.objects.create(
            profile=locked,
            source=DriverReviewLog.Source.BOSS,
            decision=DriverReviewLog.Decision.APPROVE,
            reasons=[],
            confidence=None,
            documents_version=locked.documents_version,
            actor=boss,
            applied=True,
        )

    return DriverProfile.objects.get(pk=profile.pk)


def reject_profile(profile, boss, reason: str) -> DriverProfile:
    """Boss rejects (or corrects a previous decision). ``reason`` is required."""
    reason = (reason or "").strip()
    if not reason:
        raise ProfileTransitionError("A rejection reason is required.")

    with transaction.atomic():
        locked = DriverProfile.objects.select_for_update().get(pk=profile.pk)

        if locked.status == DriverProfile.Status.INCOMPLETE:
            raise ProfileTransitionError("This profile has not been submitted yet.")

        locked.status = DriverProfile.Status.REJECTED
        locked.rejection_reason = reason
        locked.reviewed_by_type = DriverProfile.ReviewedByType.BOSS
        locked.reviewed_by = boss
        locked.reviewed_at = timezone.now()
        locked.save(
            update_fields=[
                "status",
                "rejection_reason",
                "reviewed_by_type",
                "reviewed_by",
                "reviewed_at",
                "updated_at",
            ]
        )

        DriverReviewLog.objects.create(
            profile=locked,
            source=DriverReviewLog.Source.BOSS,
            decision=DriverReviewLog.Decision.REJECT,
            reasons=[reason],
            confidence=None,
            documents_version=locked.documents_version,
            actor=boss,
            applied=True,
        )

    return DriverProfile.objects.get(pk=profile.pk)


# ─────────────────────────────────────────────────────────────
#  AI agent review
# ─────────────────────────────────────────────────────────────

def _reasons_text(reasons) -> str:
    return " ".join(str(item).strip() for item in reasons if str(item).strip())[:500]


def agent_review(profile, *, decision, reasons, confidence, documents_version):
    """
    Apply an AI agent decision.

    Guards:
    * the profile must be ``pending_review``;
    * ``documents_version`` must match (otherwise the driver changed the
      documents between analysis and submission) → ``VersionConflictError``.

    Returns ``(profile, applied)``. ``applied`` is False when the decision was
    recorded as a recommendation only (``AI_AUTO_APPROVE`` off, or a low
    confidence).
    """
    reasons = list(reasons or [])

    with transaction.atomic():
        locked = DriverProfile.objects.select_for_update().get(pk=profile.pk)

        if locked.status != DriverProfile.Status.PENDING_REVIEW:
            raise ProfileTransitionError(
                "The profile is not pending review anymore."
            )
        if int(documents_version) != locked.documents_version:
            raise VersionConflictError(locked.documents_version)

        auto_approve = bool(getattr(settings, "AI_AUTO_APPROVE", False))
        min_confidence = float(
            getattr(settings, "AI_AUTO_APPROVE_MIN_CONFIDENCE", 0.90)
        )
        applied = False

        if decision == DriverReviewLog.Decision.REJECT:
            locked.status = DriverProfile.Status.REJECTED
            locked.rejection_reason = _reasons_text(reasons)
            locked.reviewed_by_type = DriverProfile.ReviewedByType.AI
            locked.reviewed_by = None
            locked.reviewed_at = timezone.now()
            locked.save(
                update_fields=[
                    "status",
                    "rejection_reason",
                    "reviewed_by_type",
                    "reviewed_by",
                    "reviewed_at",
                    "updated_at",
                ]
            )
            applied = True

        elif decision == DriverReviewLog.Decision.APPROVE:
            if auto_approve and confidence is not None and confidence >= min_confidence:
                locked.status = DriverProfile.Status.APPROVED
                locked.rejection_reason = ""
                locked.reviewed_by_type = DriverProfile.ReviewedByType.AI
                locked.reviewed_by = None
                locked.reviewed_at = timezone.now()
                locked.save(
                    update_fields=[
                        "status",
                        "rejection_reason",
                        "reviewed_by_type",
                        "reviewed_by",
                        "reviewed_at",
                        "updated_at",
                    ]
                )
                applied = True
            # Otherwise: keep ``pending_review`` and record the recommendation.

        DriverReviewLog.objects.create(
            profile=locked,
            source=DriverReviewLog.Source.AI,
            decision=decision,
            reasons=reasons,
            confidence=confidence,
            documents_version=locked.documents_version,
            actor=None,
            applied=applied,
        )

    return DriverProfile.objects.get(pk=profile.pk), applied
