"""
accounts/models.py

Custom user model that extends Django's AbstractUser.
Adds a `role` field for RBAC: boss / driver / client.

The public API only ever exposes the roles "client", "driver" and "boss".
"boss" can NOT be obtained through signup — it is created server-side with
`python manage.py create_boss`.
"""
from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    class Role(models.TextChoices):
        BOSS   = 'boss',   'Boss'
        DRIVER = 'driver', 'Driver'
        CLIENT = 'client', 'Client'

    role = models.CharField(
        max_length=10,
        choices=Role.choices,
        default=Role.CLIENT,   # safest default — least privilege
    )

    # Make email required and unique (signup already validates this,
    # but the DB constraint is the real safety net)
    email = models.EmailField(unique=True)

    # Email verification — new accounts stay False until the user
    # clicks the link sent by the verification email.
    # NOT writable from any serializer: it is only set by VerifyEmailView.
    email_verified = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.username} ({self.role})"

    # ── Convenience properties used in permission classes ──────
    @property
    def is_boss(self):
        return self.role == self.Role.BOSS

    @property
    def is_driver(self):
        return self.role == self.Role.DRIVER

    @property
    def is_client(self):
        return self.role == self.Role.CLIENT


# Create your models here.
