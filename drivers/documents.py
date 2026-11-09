"""
drivers/documents.py
~~~~~~~~~~~~~~~~~~~~

Image validation and storage for driver documents.

Everything here is deliberately strict because the files are identity
documents:

* only JPEG / PNG / WebP (PDF and everything else refused),
* maximum size ``MAX_UPLOAD_MB`` (default 5 MB),
* real content is checked with Pillow (``verify()`` then reopen),
* decompression-bomb and dimension protection,
* the file is re-encoded so EXIF metadata (GPS, device) is dropped,
* stored under a random UUID name, extension derived from the real format.
"""
import io
import uuid

from django.conf import settings
from PIL import Image, ImageOps

from .storage import private_media_storage

# JPEG / PNG / WebP only — keyed by Pillow's detected format.
ALLOWED_FORMATS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}
MAX_DIMENSION = 6000
# Any image decoding more than this many pixels is refused (before Pillow's
# own hard limit of 2x this value).
Image.MAX_IMAGE_PIXELS = 40_000_000


class DocumentValidationError(Exception):
    """Raised when an uploaded document is not acceptable (→ HTTP 400)."""


def _max_bytes() -> int:
    return int(getattr(settings, "MAX_UPLOAD_MB", 5)) * 1024 * 1024


def _too_large_message() -> str:
    return f"File is too large (maximum {getattr(settings, 'MAX_UPLOAD_MB', 5)} MB)."


def process_image(uploaded_file) -> tuple[bytes, str]:
    """
    Validate, strip metadata and re-encode *uploaded_file*.

    Returns ``(bytes, extension)``. Raises ``DocumentValidationError`` for
    anything that is not a clean, reasonably sized JPEG/PNG/WebP image.
    """
    if uploaded_file is None:
        raise DocumentValidationError("A file is required.")

    size = getattr(uploaded_file, "size", None)
    if size is not None and size > _max_bytes():
        raise DocumentValidationError(_too_large_message())

    data = uploaded_file.read()
    if not data:
        raise DocumentValidationError("The uploaded file is empty.")
    if len(data) > _max_bytes():
        raise DocumentValidationError(_too_large_message())

    # 1) Verify the real content (catches "renamed .jpg" and PDFs).
    try:
        with Image.open(io.BytesIO(data)) as probe:
            probe.verify()
    except Image.DecompressionBombError:
        raise DocumentValidationError("Image is too large to be processed.")
    except Exception:
        raise DocumentValidationError("Unsupported or corrupted image file.")

    # 2) Reopen, enforce size limits and re-encode (drops EXIF).
    try:
        with Image.open(io.BytesIO(data)) as img:
            fmt = img.format
            if fmt not in ALLOWED_FORMATS:
                raise DocumentValidationError(
                    "Unsupported image format. Allowed: JPEG, PNG, WebP."
                )

            width, height = img.size
            if width <= 0 or height <= 0:
                raise DocumentValidationError("Invalid image dimensions.")
            if width > MAX_DIMENSION or height > MAX_DIMENSION:
                raise DocumentValidationError("Image dimensions are too large.")
            if width * height > Image.MAX_IMAGE_PIXELS:
                raise DocumentValidationError("Image is too large to be processed.")

            img = ImageOps.exif_transpose(img)  # honour orientation...
            rgb = img.convert("RGB")            # ...then drop all metadata
            out = io.BytesIO()
            save_kwargs = {"quality": 88, "optimize": True} if fmt == "JPEG" else {}
            rgb.save(out, format=fmt, **save_kwargs)
    except DocumentValidationError:
        raise
    except Image.DecompressionBombError:
        raise DocumentValidationError("Image is too large to be processed.")
    except Exception:
        raise DocumentValidationError("Unsupported or corrupted image file.")

    return out.getvalue(), ALLOWED_FORMATS[fmt]


def store_document(file_bytes: bytes, extension: str) -> str:
    """Persist *file_bytes* under a random name; return the relative path."""
    name = f"documents/{uuid.uuid4().hex}{extension}"
    return private_media_storage.save(name, io.BytesIO(file_bytes))


def delete_document(name: str) -> None:
    """Delete a stored document, ignoring any cleanup error."""
    if not name:
        return
    try:
        private_media_storage.delete(name)
    except Exception:
        pass
