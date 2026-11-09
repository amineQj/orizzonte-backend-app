"""
manage.py reset_users

Deletes EVERY user and their related data (missions + JWT tokens) from the
database. It does NOT create the boss — run `create_boss` afterwards.

Safety:
    * refuses to run when DEBUG=False unless --allow-production is given,
    * requires an explicit confirmation (--yes, or typing DELETE),
    * writes a timestamped backup of the SQLite file first (unless --no-backup).

Foreign keys pointing to User (what happens on delete):
    * missions.Mission.client               → CASCADE   (mission disappears)
    * missions.Mission.assigned_employee    → SET_NULL  (driver removed)
    * token_blacklist.OutstandingToken.user → SET_NULL  (orphan unless cleared)
    * admin.LogEntry.user                   → CASCADE   (admin log entry removed)
    * accounts User.groups / user_permissions (M2M) → rows removed

Because every mission has a client and all users are deleted, ALL missions
are deleted. Tokens are deleted explicitly so no orphan OutstandingToken
rows remain.

Driver identity documents (drivers.DriverProfile) are CASCADE-deleted with
the user; their private files are removed from PRIVATE_MEDIA_ROOT as well.
"""
import os
import shutil
from datetime import datetime

from django.conf import settings
from django.contrib.admin.models import LogEntry
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from rest_framework_simplejwt.token_blacklist.models import (
    BlacklistedToken,
    OutstandingToken,
)

from accounts.models import User
from missions.models import Mission


class Command(BaseCommand):
    help = "Delete all users (and their missions/tokens). Does not create the boss."
    def add_arguments(self, parser):
        parser.add_argument(
            '--yes', action='store_true',
            help="Skip the confirmation prompt.",
        )
        parser.add_argument(
            '--no-backup', action='store_true',
            help="Do not write a timestamped backup of the SQLite file.",
        )
        parser.add_argument(
            '--allow-production', action='store_true',
            help="Allow running when DEBUG=False (production-like database).",
        )

    def handle(self, *args, **options):
        # ── 1) Production guard ────────────────────────────────────────────
        if not settings.DEBUG and not options['allow_production']:
            raise CommandError(
                "DEBUG=False (production environment). reset_users is a "
                "destructive development command. Re-run with "
                "--allow-production if you really want to erase all users."
            )
        if not settings.DEBUG:
            self.stdout.write(self.style.WARNING(
                "--allow-production given: running on a production-like "
                "database (DEBUG=False)."
            ))

        # ── 2) Report what will be deleted ─────────────────────────────────
        counts = {
            'users': User.objects.count(),
            'missions': Mission.objects.count(),
            'outstanding_tokens': OutstandingToken.objects.count(),
            'blacklisted_tokens': BlacklistedToken.objects.count(),
            'admin_log_entries': LogEntry.objects.count(),
        }
        self.stdout.write("Objects that will be deleted:")
        for label, value in counts.items():
            self.stdout.write(f"  {label}: {value}")

        # ── 3) Explicit confirmation ───────────────────────────────────────
        if not options['yes']:
            self.stdout.write(
                "This will DELETE every user, mission and JWT token. "
                "Type SUPPRIMER (uppercase) to confirm."
            )
            try:
                answer = input("Confirmation: ")
            except (EOFError, KeyboardInterrupt):
                raise CommandError("No confirmation received — nothing was deleted.")
            if answer.strip() != "SUPPRIMER":
                self.stdout.write(self.style.WARNING(
                    "Confirmation failed — nothing was deleted."
                ))
                return

        # ── 4) Backup ──────────────────────────────────────────────────────
        if not options['no_backup']:
            self._backup()

        # ── 5) Remove private driver documents from the filesystem ─────────
        self._delete_private_documents()

        # ── 6) Delete in FK-safe order, atomically ─────────────────────────
        with transaction.atomic():
            blacklisted = BlacklistedToken.objects.all().delete()[0]
            outstanding = OutstandingToken.objects.all().delete()[0]
            missions = Mission.objects.all().delete()[0]
            log_entries = LogEntry.objects.all().delete()[0]
            users = User.objects.all().delete()[0]

        self.stdout.write(self.style.SUCCESS(
            f"Done. Deleted {users} user(s), {missions} mission(s), "
            f"{outstanding} outstanding token(s), {blacklisted} blacklisted "
            f"token(s), {log_entries} admin log entr(y/ies)."
        ))
        remaining = User.objects.count()
        self.stdout.write(f"Remaining users: {remaining}.")
        self.stdout.write(
            "Run 'python manage.py create_boss' to create the boss account."
        )

    def _backup(self):
        engine = settings.DATABASES['default']['ENGINE']
        name = str(settings.DATABASES['default']['NAME'])

        if 'sqlite3' not in engine:
            self.stdout.write(self.style.WARNING(
                f"Backup skipped: automatic backup only supports SQLite "
                f"(engine={engine})."
            ))
            return
        if name.startswith(':memory:') or 'mode=memory' in name:
            self.stdout.write("Backup skipped: in-memory database.")
            return
        if not os.path.exists(name):
            self.stdout.write(self.style.WARNING(
                f"Backup skipped: database file not found ({name})."
            ))
            return

        timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        destination = f"{name}.{timestamp}.bak"
        shutil.copy2(name, destination)
        self.stdout.write(self.style.SUCCESS(f"Backup written: {destination}"))

    def _delete_private_documents(self):
        """Remove every stored driver document from PRIVATE_MEDIA_ROOT.

        Done explicitly (before the users are deleted) so the private folder
        never keeps orphan identity files, even if the post_delete receiver
        were disabled. Files not referenced by any profile are left alone.
        """
        try:
            from drivers.models import DriverProfile
            from drivers.storage import private_media_storage, private_media_root
            from drivers.constants import DOCUMENT_TYPES

            private_media_root()  # validate configuration
        except Exception as exc:  # misconfigured or app unavailable
            self.stdout.write(self.style.WARNING(
                f"Private document cleanup skipped: {exc}"
            ))
            return

        deleted = 0
        for profile in DriverProfile.objects.all().iterator():
            for field in DOCUMENT_TYPES:
                name = getattr(profile, field).name
                if not name:
                    continue
                try:
                    private_media_storage.delete(name)
                    deleted += 1
                except Exception:
                    pass
        self.stdout.write(self.style.SUCCESS(
            f"Private driver documents deleted: {deleted}."
        ))
