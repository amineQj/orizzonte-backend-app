"""Shared constants for the drivers app (single source of truth)."""

# The 5 documents a driver must upload. The model field names are exactly the
# same as the API ``type`` values so there is no mapping to keep in sync.
DOCUMENT_TYPES = (
    "selfie",
    "id_card_front",
    "id_card_back",
    "driving_license",
    "vehicle_registration",
)

DOCUMENT_TYPE_CHOICES = tuple((name, name) for name in DOCUMENT_TYPES)

# Information fields that must be filled before a profile can be submitted.
INFO_FIELDS = (
    "full_name",
    "phone",
    "date_of_birth",
    "national_id_number",
    "driving_license_number",
    "driving_license_expiry",
    "vehicle_plate",
)

# Changing one of these fields (once approved) forces a new review.
KEY_INFO_FIELDS = frozenset(
    {
        "national_id_number",
        "driving_license_number",
        "driving_license_expiry",
        "vehicle_plate",
    }
)
