"""
manage.py create_boss

Creates THE boss account: role="boss", verified, is_staff, is_superuser.

The boss can NEVER be created through the public API — POST /api/signup/
only accepts "client" and "driver".

Password source, in this order:
    1. the BOSS_PASSWORD environment variable (non-interactive), or
    2. an interactive prompt (hidden, entered twice).

Security rules enforced here:
    * the password is validated by Django's password validators
      (too short / too common / all-numeric / too similar → refused),
    * the password is NEVER printed, logged, or passed as a CLI argument,
    * an existing email is a clear error — no duplicate, no silent overwrite.
"""
import os
import re
import sys
from getpass import getpass

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

User = get_user_model()


def _derive_username(email: str) -> str:
    """Build a unique username from the email local part."""
    base = re.sub(r'[^A-Za-z0-9_.-]', '', email.split('@', 1)[0]) or 'boss'
    base = base[:140]
    username = base
    suffix = 1
    while User.objects.filter(username__iexact=username).exists():
        suffix += 1
        tail = f"_{suffix}"
        username = base[:150 - len(tail)] + tail
    return username


class Command(BaseCommand):
    help = "Create the boss account (role=boss, verified, staff, superuser)."

    def add_arguments(self, parser):
        parser.add_argument(
            '--email',
            help="Boss email address. Prompted interactively when omitted.",
        )

    def handle(self, *args, **options):
        email = (options.get('email') or '').strip()
        if not email:
            if not sys.stdin.isatty():
                raise CommandError("--email is required in a non-interactive terminal.")
            email = input("Boss email: ").strip()
        if not email:
            raise CommandError("An email address is required.")

        if User.objects.filter(email__iexact=email).exists():
            raise CommandError(
                f"A user with the email '{email}' already exists. "
                "Nothing was created (no overwrite)."
            )

        # ── Password: environment variable first, otherwise hidden prompt ──
        password = os.environ.get('BOSS_PASSWORD') or ''
        if password:
            self.stdout.write("Using the password from BOSS_PASSWORD.")
        else:
            if not sys.stdin.isatty():
                raise CommandError(
                    "No BOSS_PASSWORD is set and there is no interactive "
                    "terminal. Set BOSS_PASSWORD or run this command in a terminal."
                )
            password = getpass("Boss password: ")
            confirm = getpass("Confirm password: ")
            if password != confirm:
                raise CommandError("The two passwords do not match.")

        # ── Validate the password with Django's own validators ──
        candidate = User(username=_derive_username(email), email=email)
        try:
            validate_password(password, candidate)
        except ValidationError as exc:
            raise CommandError(
                "Password refused by Django's password validators: "
                + " ".join(exc.messages)
            )

        username = candidate.username
        user = User.objects.create_superuser(
            username=username,
            email=email,
            password=password,
            role=User.Role.BOSS,
            email_verified=True,
        )
        self.stdout.write(self.style.SUCCESS(
            f"Boss created: username='{user.username}' email='{user.email}' "
            "(role=boss, email_verified=True, is_staff=True, is_superuser=True)."
        ))
