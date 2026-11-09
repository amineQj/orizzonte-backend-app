"""Signal receivers for the drivers app."""
from django.db.models.signals import post_delete
from django.dispatch import receiver

from .constants import DOCUMENT_TYPES
from .models import DriverProfile
from .storage import private_media_storage


@receiver(post_delete, sender=DriverProfile)
def _delete_private_files(sender, instance, **kwargs):
    """
    Remove every stored document when a profile is deleted.

    This fires for a CASCADE delete as well (boss deleting a driver,
    ``manage.py reset_users``), because the receiver disables Django's
    "fast delete" path for ``DriverProfile``.
    """
    for field in DOCUMENT_TYPES:
        name = getattr(instance, field).name
        if not name:
            continue
        try:
            private_media_storage.delete(name)
        except Exception:
            # Never let a cleanup failure block the deletion of the account.
            pass
