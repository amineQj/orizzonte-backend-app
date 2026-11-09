"""Project checks for the drivers app (run by ``manage.py check``)."""
from django.core import checks
from django.core.exceptions import ImproperlyConfigured

from .storage import private_media_root


@checks.register()
def check_private_media_root(app_configs, **kwargs):
    """Fail fast, at startup, when the private storage is misconfigured."""
    try:
        private_media_root()
    except ImproperlyConfigured as exc:
        return [checks.Error(str(exc), id="drivers.E001")]
    return []
