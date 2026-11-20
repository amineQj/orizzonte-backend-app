import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from .pricing import Configuration, PricingError


class TruckType(models.TextChoices):
    SMALL = "small", "Small"
    MEDIUM = "medium", "Medium"
    LARGE = "large", "Large"
    EXTRA_LARGE = "extra_large", "Extra Large"


class TruckSize(models.TextChoices):
    SMALL = "small", "Small"
    MEDIUM = "medium", "Medium"
    BIG = "big", "Big"


class TruckCategory(models.TextChoices):
    OPEN = "open", "Open"
    CLOSED = "closed", "Closed"
    REFRIGERATED = "refrigerated", "Refrigerated"
    FLATBED = "flatbed", "Flatbed"


class MissionStatus(models.TextChoices):
    PENDING     = "pending",     "Pending"
    ACCEPTED    = "accepted",    "Accepted"
    IN_PROGRESS = "in_progress", "In Progress"
    COMPLETED   = "completed",   "Completed"
    CANCELLED   = "cancelled",   "Cancelled"


class Mission(models.Model):
    """
    Represents a confirmed moving order (mission).
    Created only after the client reviews and confirms the price estimation.
    """

    # Shipment — the primary attribute the client selects first.
    # Free-form CharField so the list of types can grow without a migration.
    # Examples: "house_moving", "commercial_goods", "heavy_equipment"
    shipment_type = models.CharField(
        max_length=50,
        help_text="Kind of goods being transported (e.g. house_moving, commercial_goods).",
    )

    # Contact
    phone_number = models.CharField(
        max_length=10,
        help_text="Client contact number — must start with 05, 06, or 07 and be 10 digits.",
    )

    # Ownership
    client = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="missions_as_client",
        help_text="The client who requested this mission.",
    )
    assigned_employee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="missions_as_employee",
        help_text="The employee who accepted this mission. Null until accepted.",
    )

    # Truck details
    truck_type = models.CharField(
        max_length=20,
        choices=TruckType.choices,
    )
    category = models.CharField(
        max_length=20,
        choices=TruckCategory.choices,
    )
    capacity = models.PositiveIntegerField(
        help_text="Truck capacity in tons.",
    )
    size = models.CharField(
        max_length=10,
        choices=TruckSize.choices,
        help_text="Truck size — affects the final price.",
    )

    # Locations — stored as flat decimal fields for simplicity and query speed.
    # Use PostGIS PointField instead if spatial queries become a requirement.
    departure_latitude = models.DecimalField(max_digits=9, decimal_places=6)
    departure_longitude = models.DecimalField(max_digits=9, decimal_places=6)
    arrival_latitude = models.DecimalField(max_digits=9, decimal_places=6)
    arrival_longitude = models.DecimalField(max_digits=9, decimal_places=6)

    # Trip info
    distance = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        help_text="Distance in kilometres.",
    )
    date = models.DateField()
    time = models.TimeField()
    workers = models.PositiveSmallIntegerField(
        default=0,
        help_text="Number of additional workers (0–5).",
    )

    # Financials
    price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        help_text="Calculated price in DZD.",
    )

    # Pricing engine snapshot (null for legacy, pre-pricing-engine missions).
    negotiable = models.BooleanField(
        default=False,
        help_text="True when the client allowed the price to be negotiated.",
    )
    quote = models.ForeignKey(
        "Quote",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="missions",
        help_text="Server-side quote used to confirm this mission, when any.",
    )
    pricing_config_version = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Version of the pricing configuration used at confirmation.",
    )
    price_components = models.JSONField(
        null=True,
        blank=True,
        default=None,
        help_text="Rounded pricing components snapshot; no GPS coordinates.",
    )
    commission_rate = models.DecimalField(
        max_digits=5,
        decimal_places=4,
        null=True,
        blank=True,
        help_text="Commission rate snapshot (0–1).",
    )
    commission_amount = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Platform commission in DZD, rounded.",
    )
    driver_net = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Driver net after commission, in DZD.",
    )
    price_locked_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Set when the price becomes immutable.",
    )

    # Negotiation hold (no new Mission status: the mission stays pending).
    held_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="held_missions",
        help_text="Driver holding this mission during an open negotiation.",
    )
    hold_expires_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text="Lazy expiry deadline for the negotiation hold.",
    )

    # Lifecycle
    status = models.CharField(
        max_length=20,
        choices=MissionStatus.choices,
        default=MissionStatus.PENDING,
        db_index=True,
    )

    # Audit
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Mission"
        verbose_name_plural = "Missions"
        constraints = [
            models.UniqueConstraint(
                fields=["assigned_employee"],
                condition=Q(status__in=["accepted", "in_progress"]),
                name="uniq_active_driver_mission",
            ),
        ]

    def __str__(self) -> str:
        return f"Mission #{self.pk} — {self.client} [{self.status}]"


# ---------------------------------------------------------------------------
# Pricing engine configuration
# ---------------------------------------------------------------------------

class PricingConfig(models.Model):
    """Versioned pricing parameters.

    Exactly one row may be active at a time (enforced by a partial unique
    constraint). ``parameters`` mirrors ``missions/data/pricing_v1.json``;
    ``clean()`` validates it before it can be saved from the admin.

    The seeded values are PLACEHOLDERS. ``calibrated`` stays False until the
    boss replaces them with market-derived parameters.
    """

    version = models.PositiveIntegerField(unique=True)
    calibrated = models.BooleanField(
        default=False,
        help_text="Keep False until the values are fitted to real market quotes.",
    )
    is_active = models.BooleanField(default=False, db_index=True)
    parameters = models.JSONField(default=dict)
    notes = models.TextField(blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="pricing_configs_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-version"]
        verbose_name = "Pricing configuration"
        verbose_name_plural = "Pricing configurations"
        constraints = [
            models.UniqueConstraint(
                fields=["is_active"],
                condition=Q(is_active=True),
                name="uniq_active_pricing_config",
            ),
        ]

    def __str__(self) -> str:
        state = "active" if self.is_active else "inactive"
        flag = "calibrated" if self.calibrated else "placeholder"
        return f"PricingConfig v{self.version} ({state}, {flag})"

    def clean(self):
        if not isinstance(self.parameters, dict):
            raise ValidationError({"parameters": "Parameters must be a JSON object."})
        try:
            Configuration.from_mapping(self.parameters)
        except PricingError as exc:
            raise ValidationError({"parameters": exc.message}) from exc

    def save(self, *args, **kwargs):
        # Activating one configuration deactivates the others. The partial
        # unique constraint is the final safety net.
        if self.is_active:
            (
                PricingConfig.objects.filter(is_active=True)
                .exclude(pk=self.pk)
                .update(is_active=False)
            )
        super().save(*args, **kwargs)


class Quote(models.Model):
    """A server-side price quote, confirmable until ``expires_at``.

    Deliberately stores no GPS coordinates: only rounded trip inputs and the
    computed components. The confirmed route is re-validated against the
    quote's distance at confirmation time.
    """

    class Confidence(models.TextChoices):
        NORMAL = "normal", "Normal"
        LOW = "low", "Low"

    public_id = models.UUIDField(
        default=uuid.uuid4, unique=True, editable=False, db_index=True
    )
    client = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="pricing_quotes",
    )
    config = models.ForeignKey(
        PricingConfig, on_delete=models.PROTECT, related_name="quotes"
    )
    config_version = models.PositiveIntegerField()
    inputs = models.JSONField(default=dict)
    components = models.JSONField(default=dict)
    amount = models.PositiveIntegerField()
    low = models.PositiveIntegerField()
    high = models.PositiveIntegerField()
    currency = models.CharField(max_length=3, default="DZD")
    confidence = models.CharField(
        max_length=10, choices=Confidence.choices, default=Confidence.NORMAL
    )
    calibrated = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)
    used_at = models.DateTimeField(null=True, blank=True)
    mission = models.ForeignKey(
        Mission,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="source_quotes",
    )

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Pricing quote"
        verbose_name_plural = "Pricing quotes"
        indexes = [
            models.Index(fields=["client", "used_at"], name="quote_client_used_idx"),
            models.Index(fields=["expires_at"], name="quote_expires_idx"),
        ]

    def __str__(self) -> str:
        state = "used" if self.used_at else "open"
        return f"Quote {self.public_id} ({state}, {self.amount} DZD)"


class PricingOverride(models.Model):
    """Boss-only manual multiplier, always time-limited.

    Applied to a specific mission, or to a client's estimates when the
    mission is not known yet. Every application is written to
    ``PricingAuditLog``.
    """

    mission = models.ForeignKey(
        Mission,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="pricing_overrides",
    )
    client = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="pricing_overrides",
    )
    multiplier = models.DecimalField(max_digits=5, decimal_places=4)
    expires_at = models.DateTimeField(db_index=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="pricing_overrides_created",
    )
    reason = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Pricing override"
        verbose_name_plural = "Pricing overrides"
        constraints = [
            models.CheckConstraint(
                check=Q(multiplier__gt=0),
                name="pricing_override_multiplier_positive",
            ),
            models.CheckConstraint(
                check=Q(mission__isnull=False) | Q(client__isnull=False),
                name="pricing_override_target_required",
            ),
        ]

    def __str__(self) -> str:
        target = f"mission #{self.mission_id}" if self.mission_id else f"client #{self.client_id}"
        return f"PricingOverride x{self.multiplier} on {target}"


class PricingAuditLog(models.Model):
    """Append-only journal of pricing overrides and price locks.

    Never store personal data or GPS coordinates in ``payload``.
    """

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="pricing_audit_logs",
    )
    action = models.CharField(max_length=50)
    mission = models.ForeignKey(
        Mission,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="pricing_audit_logs",
    )
    quote = models.ForeignKey(
        Quote,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="pricing_audit_logs",
    )
    override = models.ForeignKey(
        PricingOverride,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="audit_logs",
    )
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Pricing audit log"
        verbose_name_plural = "Pricing audit logs"

    def __str__(self) -> str:
        return f"{self.created_at:%Y-%m-%d %H:%M} {self.action}"


# ---------------------------------------------------------------------------
# Price negotiation
# ---------------------------------------------------------------------------


class Negotiation(models.Model):
    """One price negotiation for a mission.

    A negotiation is opened by the first driver offer and is bound to exactly
    one driver. Only one negotiation per mission may be ``open`` at a time
    (partial unique constraint); declined/expired/closed rows remain as
    history. No personal data is stored: only amounts, roles, statuses and
    configuration snapshots.
    """

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        ACCEPTED = "accepted", "Accepted"
        DECLINED = "declined", "Declined"
        EXPIRED = "expired", "Expired"
        CLOSED = "closed", "Closed"

    mission = models.ForeignKey(
        Mission,
        on_delete=models.CASCADE,
        related_name="negotiations",
    )
    client = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="negotiations_as_client",
    )
    driver = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="negotiations_as_driver",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.OPEN,
        db_index=True,
    )

    # Configuration snapshot, frozen when the negotiation opens so a later
    # config edit cannot change the bounds of an ongoing negotiation.
    config_version = models.PositiveIntegerField()
    calibrated = models.BooleanField(default=False)
    initial_price = models.PositiveIntegerField(
        help_text="Mission price when the negotiation opened, in DZD.",
    )
    min_amount = models.PositiveIntegerField(
        help_text="Lowest allowed offer, in DZD (rounded to min_step).",
    )
    max_amount = models.PositiveIntegerField(
        help_text="Highest allowed offer, in DZD (rounded to min_step).",
    )
    min_fare = models.PositiveIntegerField(
        help_text="Effective minimum fare for this mission, in DZD.",
    )
    tolerance = models.DecimalField(max_digits=5, decimal_places=4)
    max_offers = models.PositiveSmallIntegerField()
    min_step = models.PositiveIntegerField()
    offer_ttl_minutes = models.PositiveSmallIntegerField()
    total_ttl_minutes = models.PositiveSmallIntegerField()
    commission_rate = models.DecimalField(max_digits=5, decimal_places=4)

    # Filled when the negotiation is accepted.
    accepted_offer = models.ForeignKey(
        "NegotiationOffer",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    accepted_amount = models.PositiveIntegerField(null=True, blank=True)
    commission_amount = models.PositiveIntegerField(null=True, blank=True)
    driver_net = models.PositiveIntegerField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    expires_at = models.DateTimeField(
        db_index=True,
        help_text="Absolute negotiation deadline (total TTL).",
    )
    accepted_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Negotiation"
        verbose_name_plural = "Negotiations"
        constraints = [
            models.UniqueConstraint(
                fields=["mission"],
                condition=Q(status="open"),
                name="uniq_open_negotiation_per_mission",
            ),
        ]
        indexes = [
            models.Index(fields=["mission", "status"], name="negotiation_mission_status_idx"),
            models.Index(fields=["driver", "status"], name="negotiation_driver_status_idx"),
            models.Index(fields=["expires_at"], name="negotiation_expires_idx"),
        ]

    def __str__(self) -> str:
        return f"Negotiation #{self.pk} mission #{self.mission_id} [{self.status}]"

    def requester_role(self, user) -> str | None:
        """Return ``"client"``, ``"driver"`` or ``None`` for *user*."""
        if user is None or not getattr(user, "is_authenticated", False):
            return None
        if user.pk == self.client_id:
            return "client"
        if user.pk == self.driver_id:
            return "driver"
        return None


class NegotiationOffer(models.Model):
    """One immutable offer in a negotiation.

    Offers alternate driver → client → driver, with at most
    ``Negotiation.max_offers`` rows. The last offer is marked final.
    """

    class AuthorRole(models.TextChoices):
        CLIENT = "client", "Client"
        DRIVER = "driver", "Driver"

    negotiation = models.ForeignKey(
        Negotiation,
        on_delete=models.CASCADE,
        related_name="offers",
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="negotiation_offers",
    )
    author_role = models.CharField(max_length=10, choices=AuthorRole.choices)
    sequence = models.PositiveSmallIntegerField(
        help_text="1-based offer number within the negotiation.",
    )
    amount = models.PositiveIntegerField(help_text="Offer amount in DZD.")
    is_final = models.BooleanField(
        default=False,
        help_text="True for the last allowed offer.",
    )
    idempotency_key = models.CharField(max_length=128)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["sequence"]
        verbose_name = "Negotiation offer"
        verbose_name_plural = "Negotiation offers"
        constraints = [
            models.UniqueConstraint(
                fields=["negotiation", "sequence"],
                name="uniq_negotiation_offer_sequence",
            ),
            models.UniqueConstraint(
                fields=["negotiation", "idempotency_key"],
                name="uniq_negotiation_offer_idempotency",
            ),
            models.CheckConstraint(
                check=Q(sequence__gt=0),
                name="negotiation_offer_sequence_positive",
            ),
            models.CheckConstraint(
                check=Q(amount__gt=0),
                name="negotiation_offer_amount_positive",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"Offer #{self.sequence} mission #{self.negotiation.mission_id} "
            f"[{self.author_role}] {self.amount} DZD"
        )


