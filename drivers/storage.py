"""
drivers/storage.py
~~~~~~~~~~~~~~~~~~

Private storage backend for driver identity documents.

Security goals
--------------
* No file is ever reachable through a public URL: ``url()`` raises and
  ``base_url`` is ``None``.
* The root directory is configured with ``PRIVATE_MEDIA_ROOT`` and is
  validated: it must be an absolute, non-empty path located outside of
  ``STATIC_ROOT`` / ``STATICFILES_DIRS``.
* Restrictive permissions: ``0o600`` files, ``0o700`` directories.

The class reads settings lazily so it is safe to import at project startup
(models are imported before a misconfiguration could raise). Misconfiguration
surfaces through the ``drivers.E001`` system check and on the first real
storage access — never through a silent default.
"""
import os

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.files.storage import FileSystemStorage
from django.utils.deconstruct import deconstructible


def _static_directories():
    """Return the absolute static directories (STATIC_ROOT + STATICFILES_DIRS)."""
    dirs = []
    static_root = getattr(settings, "STATIC_ROOT", None)
    if static_root:
        dirs.append(os.path.abspath(str(static_root)))
    for entry in getattr(settings, "STATICFILES_DIRS", []) or []:
        # An entry may be a path or a (prefix, path) tuple.
        path = entry[1] if isinstance(entry, (list, tuple)) and len(entry) == 2 else entry
        try:
            dirs.append(os.path.abspath(str(path)))
        except (TypeError, ValueError):
            continue
    return dirs


def private_media_root() -> str:
    """Return the validated, absolute private storage root."""
    raw = getattr(settings, "PRIVATE_MEDIA_ROOT", "") or ""
    raw = str(raw).strip()

    if not raw:
        raise ImproperlyConfigured(
            "PRIVATE_MEDIA_ROOT is not set. Add an absolute path to .env "
            "(for example PRIVATE_MEDIA_ROOT=/var/lib/orizzonte/private_media). "
            "There is deliberately no default: identity documents must never "
            "be written to an implicit location."
        )
    if not os.path.isabs(raw):
        raise ImproperlyConfigured("PRIVATE_MEDIA_ROOT must be an absolute path.")

    root = os.path.abspath(raw)
    for static_dir in _static_directories():
        if root == static_dir or root.startswith(static_dir + os.sep):
            raise ImproperlyConfigured(
                "PRIVATE_MEDIA_ROOT must be outside STATIC_ROOT and "
                "STATICFILES_DIRS (private documents must never be served "
                "as static files)."
            )
    return root


@deconstructible(path="drivers.storage.PrivateMediaStorage")
class PrivateMediaStorage(FileSystemStorage):
    """Filesystem storage for documents that must stay private."""

    def __init__(self):
        # Never read settings here: this may run at import time.
        super().__init__()

    # -- settings are resolved lazily so tests can override them --------
    @property
    def base_location(self):
        return private_media_root()

    @property
    def location(self):
        return private_media_root()

    @property
    def base_url(self):
        return None

    @property
    def file_permissions_mode(self):
        return 0o600

    @property
    def directory_permissions_mode(self):
        return 0o700

    def url(self, name):
        raise ValueError(
            "Private documents are not accessible through a public URL."
        )


private_media_storage = PrivateMediaStorage()
