"""
drivers/tests/test_storage_and_commands.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Private storage hardening, the ``drivers.E001`` system check, file cleanup on
delete and the ``reset_users`` management command.
"""
import os
import unittest
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import override_settings
from django.urls import reverse

from drivers.checks import check_private_media_root
from drivers.models import DriverProfile
from drivers.storage import private_media_storage
from drivers.tests.base import (
    DriverAPITestCase,
    auth_client,
    make_profile,
    make_user,
    upload_document,
)

User = get_user_model()


class SystemCheckTests(DriverAPITestCase):
    def test_missing_root_raises_drivers_e001(self):
        with override_settings(PRIVATE_MEDIA_ROOT=""):
            errors = check_private_media_root(None)
        self.assertTrue(any(e.id == "drivers.E001" for e in errors))

    def test_valid_root_produces_no_error(self):
        self.assertEqual(check_private_media_root(None), [])


class StorageHardeningTests(DriverAPITestCase):
    def test_url_raises(self):
        with self.assertRaises(Exception):
            private_media_storage.url("documents/x.jpg")

    def test_base_url_is_none(self):
        self.assertIsNone(private_media_storage.base_url)

    @unittest.skipIf(os.name == "nt", "POSIX permissions are not meaningful on Windows")
    def test_files_are_written_with_restrictive_permissions(self):
        driver = make_user(User.Role.DRIVER, username="perm_driver")
        make_profile(driver)
        upload_document(auth_client(driver), "selfie")
        profile = DriverProfile.objects.get(user=driver)
        path = private_media_storage.path(profile.selfie.name)
        mode = os.stat(path).st_mode & 0o777
        self.assertEqual(mode, 0o600)


class DeleteCleanupTests(DriverAPITestCase):
    def test_deleting_user_removes_private_files(self):
        driver = make_user(User.Role.DRIVER, username="del_driver")
        make_profile(driver)
        upload_document(auth_client(driver), "selfie")
        profile = DriverProfile.objects.get(user=driver)
        name = profile.selfie.name
        self.assertTrue(private_media_storage.exists(name))

        driver.delete()
        self.assertFalse(private_media_storage.exists(name))
        self.assertFalse(DriverProfile.objects.filter(pk=profile.pk).exists())

    def test_boss_delete_driver_removes_private_files(self):
        boss = make_user(User.Role.BOSS, username="del_boss")
        driver = make_user(User.Role.DRIVER, username="del_driver2")
        make_profile(driver)
        upload_document(auth_client(driver), "selfie")
        profile = DriverProfile.objects.get(user=driver)
        name = profile.selfie.name

        response = auth_client(boss).delete(
            reverse("employee-detail", args=[driver.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(private_media_storage.exists(name))


class ResetUsersCommandTests(DriverAPITestCase):
    def test_reset_users_removes_private_documents(self):
        driver = make_user(User.Role.DRIVER, username="reset_driver")
        make_profile(driver)
        upload_document(auth_client(driver), "selfie")
        profile = DriverProfile.objects.get(user=driver)
        name = profile.selfie.name
        self.assertTrue(private_media_storage.exists(name))

        call_command(
            "reset_users",
            yes=True,
            no_backup=True,
            allow_production=True,
            stdout=StringIO(),
        )

        self.assertFalse(DriverProfile.objects.filter(user=driver).exists())
        self.assertFalse(private_media_storage.exists(name))
        self.assertEqual(User.objects.count(), 0)
