"""
drivers/tests/test_documents.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Private document upload, validation, EXIF stripping and download access.
"""
import io
import os

from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from PIL import Image
from rest_framework.test import APIClient

from drivers.models import DriverProfile
from drivers.storage import private_media_root, private_media_storage
from drivers.tests.base import (
    DriverAPITestCase,
    agent_client,
    auth_client,
    document_file,
    download_document,
    image_bytes,
    make_profile,
    make_user,
    streaming_bytes,
    upload_document,
)

User = get_user_model()
Status = DriverProfile.Status


class DocumentUploadTests(DriverAPITestCase):
    def setUp(self):
        super().setUp()
        self.driver = make_user(User.Role.DRIVER, username="doc_upload")
        self.driver_client = auth_client(self.driver)

    def test_upload_valid_png(self):
        response = upload_document(self.driver_client, "selfie", fmt="PNG")
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertTrue(body["documents"]["selfie"]["uploaded"])
        self.assertEqual(body["documents_version"], 1)

        profile = DriverProfile.objects.get(user=self.driver)
        self.assertTrue(private_media_storage.exists(profile.selfie.name))
        # The stored (random) name is never echoed back.
        self.assertNotIn(profile.selfie.name, response.content.decode())
        # The download link points at the authenticated endpoint.
        self.assertEqual(
            body["documents"]["selfie"]["url"],
            reverse("driver-document", args=[self.driver.pk, "selfie"]),
        )

    def test_upload_replaces_and_deletes_old_file(self):
        upload_document(self.driver_client, "selfie", fmt="PNG")
        profile = DriverProfile.objects.get(user=self.driver)
        old_name = profile.selfie.name
        self.assertTrue(private_media_storage.exists(old_name))

        upload_document(self.driver_client, "selfie", fmt="JPEG")
        profile.refresh_from_db()
        new_name = profile.selfie.name
        self.assertNotEqual(old_name, new_name)
        self.assertFalse(private_media_storage.exists(old_name))
        self.assertTrue(private_media_storage.exists(new_name))

    def test_pdf_renamed_as_image_is_rejected(self):
        fake = SimpleUploadedFile(
            "document.png",
            b"%PDF-1.7\nnot really an image",
            content_type="image/png",
        )
        response = self.driver_client.post(
            reverse("driver-document-upload"),
            {"type": "selfie", "file": fake},
            format="multipart",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "invalid_document")

    def test_oversize_is_rejected(self):
        with override_settings(MAX_UPLOAD_MB=1):
            big = SimpleUploadedFile(
                "big.png", os.urandom(1024 * 1024 + 64), content_type="image/png"
            )
            response = self.driver_client.post(
                reverse("driver-document-upload"),
                {"type": "selfie", "file": big},
                format="multipart",
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "invalid_document")

    def test_unknown_document_type_is_rejected(self):
        response = self.driver_client.post(
            reverse("driver-document-upload"),
            {"type": "passport", "file": document_file()},
            format="multipart",
        )
        self.assertEqual(response.status_code, 400)

    def test_exif_metadata_is_stripped(self):
        exif = Image.Exif()
        exif[0x0112] = 6  # Orientation
        upload = SimpleUploadedFile(
            "selfie.jpg",
            image_bytes(fmt="JPEG", exif=exif),
            content_type="image/jpeg",
        )
        response = self.driver_client.post(
            reverse("driver-document-upload"),
            {"type": "selfie", "file": upload},
            format="multipart",
        )
        self.assertEqual(response.status_code, 200, response.content)

        download = download_document(self.driver_client, self.driver.pk, "selfie")
        with Image.open(io.BytesIO(streaming_bytes(download))) as img:
            self.assertEqual(dict(img.getexif()), {})

    def test_upload_reopens_approved_profile(self):
        make_profile(self.driver, complete=True, status=Status.APPROVED)
        response = upload_document(self.driver_client, "selfie", fmt="PNG")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["status"], Status.PENDING_REVIEW)


class DocumentDownloadTests(DriverAPITestCase):
    def setUp(self):
        super().setUp()
        self.owner = make_user(User.Role.DRIVER, username="doc_owner")
        self.other = make_user(User.Role.DRIVER, username="doc_other")
        self.boss = make_user(User.Role.BOSS, username="doc_boss")
        self.owner_client = auth_client(self.owner)
        make_profile(self.owner, complete=False)
        upload_document(self.owner_client, "selfie", fmt="PNG")
        self.owner.refresh_from_db()

    def test_owner_can_download_with_hardening_headers(self):
        response = download_document(self.owner_client, self.owner.pk, "selfie")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertIn("filename=", response["Content-Disposition"])
        self.assertNotIn(
            self.owner.driver_profile.selfie.name, response["Content-Disposition"]
        )
        self.assertEqual(
            len(streaming_bytes(response)), self.owner.driver_profile.selfie.size
        )

    def test_other_driver_is_forbidden(self):
        response = download_document(
            auth_client(self.other), self.owner.pk, "selfie"
        )
        self.assertEqual(response.status_code, 403)

    def test_boss_is_allowed(self):
        response = download_document(
            auth_client(self.boss), self.owner.pk, "selfie"
        )
        self.assertEqual(response.status_code, 200)

    def test_agent_key_is_allowed(self):
        response = download_document(agent_client(), self.owner.pk, "selfie")
        self.assertEqual(response.status_code, 200)

    def test_anonymous_is_unauthorized(self):
        response = download_document(APIClient(), self.owner.pk, "selfie")
        self.assertEqual(response.status_code, 401)

    def test_unknown_document_type_is_404(self):
        response = self.owner_client.get(
            reverse("driver-document", args=[self.owner.pk, "passport"])
        )
        self.assertEqual(response.status_code, 404)

    def test_unknown_driver_is_404(self):
        response = auth_client(self.boss).get(
            reverse("driver-document", args=[999999, "selfie"])
        )
        self.assertEqual(response.status_code, 404)

    def test_missing_file_is_404(self):
        private_media_storage.delete(self.owner.driver_profile.selfie.name)
        response = download_document(self.owner_client, self.owner.pk, "selfie")
        self.assertEqual(response.status_code, 404)


class PrivateMediaIsNotPublicTests(DriverAPITestCase):
    def test_storage_url_raises(self):
        with self.assertRaises(Exception):
            private_media_storage.url("documents/whatever.jpg")

    def test_media_url_is_not_routed(self):
        response = self.client.get("/media/documents/whatever.jpg")
        self.assertEqual(response.status_code, 404)

    def test_root_must_be_outside_static(self):
        with override_settings(
            STATIC_ROOT=self._private_dir,
            PRIVATE_MEDIA_ROOT=os.path.join(self._private_dir, "nested"),
        ):
            with self.assertRaises(ImproperlyConfigured):
                private_media_root()

    def test_root_must_be_absolute(self):
        with override_settings(PRIVATE_MEDIA_ROOT="relative/path"):
            with self.assertRaises(ImproperlyConfigured):
                private_media_root()

    def test_root_must_be_set(self):
        with override_settings(PRIVATE_MEDIA_ROOT=""):
            with self.assertRaises(ImproperlyConfigured):
                private_media_root()
