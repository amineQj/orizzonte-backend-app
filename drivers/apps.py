from django.apps import AppConfig


class DriversConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "drivers"
    verbose_name = "Drivers"

    def ready(self):
        # Register the post_delete receiver that removes the private files
        # of a driver profile (covers the boss deleting a driver and
        # ``manage.py reset_users``).
        from . import signals  # noqa: F401

        # Register the ``drivers.E001`` system check (Django does not
        # auto-import ``<app>.checks``).
        from . import checks  # noqa: F401
