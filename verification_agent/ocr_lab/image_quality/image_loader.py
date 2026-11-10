"""In-memory image loading, format detection, and EXIF orientation."""

from __future__ import annotations

import io
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError

from .errors import ImageQualityError

SUPPORTED_FORMATS = frozenset({"JPEG", "PNG", "WEBP", "BMP", "TIFF"})
_GRAYSCALE_MODES = frozenset({"1", "L", "LA", "I", "I;16", "F"})


@dataclass(frozen=True)
class LoadedImage:
    array: np.ndarray
    original_width: int
    original_height: int
    oriented_width: int
    oriented_height: int
    format: str
    source_mode: str
    is_grayscale: bool
    exif_orientation: int | None
    exif_applied: bool

    @property
    def original_megapixels(self) -> float:
        return (self.original_width * self.original_height) / 1_000_000.0

    @property
    def oriented_megapixels(self) -> float:
        return (self.oriented_width * self.oriented_height) / 1_000_000.0


def _open_source(source: bytes | bytearray | str | Path) -> tuple[Image.Image, str]:
    if isinstance(source, (bytes, bytearray)):
        if len(source) == 0:
            raise ImageQualityError("empty_input")
        try:
            image = Image.open(io.BytesIO(bytes(source)))
        except UnidentifiedImageError as exc:
            raise ImageQualityError("corrupt_image") from exc
        except Image.DecompressionBombError as exc:
            raise ImageQualityError("megapixel_limit") from exc
        except OSError as exc:
            raise ImageQualityError("corrupt_image") from exc
        return image, "bytes"

    path = Path(source)
    if not path.exists():
        raise ImageQualityError("file_not_found")
    if not path.is_file():
        raise ImageQualityError("not_a_file")
    try:
        image = Image.open(path)
    except UnidentifiedImageError as exc:
        raise ImageQualityError("corrupt_image") from exc
    except Image.DecompressionBombError as exc:
        raise ImageQualityError("megapixel_limit") from exc
    except PermissionError as exc:
        raise ImageQualityError("file_access_denied") from exc
    except OSError as exc:
        raise ImageQualityError("file_read_error") from exc
    return image, "path"


def load_image(
    source: bytes | bytearray | str | Path,
    max_megapixels: float,
) -> LoadedImage:
    """Decode an image from bytes or a path without saving anything.

    The real format is taken from the decoded content, not from a file suffix.
    EXIF orientation is applied to the in-memory pixel array.
    """

    image, _source_mode = _open_source(source)
    try:
        with image:
            image_format = image.format or "UNKNOWN"
            if image_format not in SUPPORTED_FORMATS:
                raise ImageQualityError("unsupported_format")

            original_width, original_height = image.size
            if original_width <= 0 or original_height <= 0:
                raise ImageQualityError("invalid_dimensions")

            megapixels = (original_width * original_height) / 1_000_000.0
            if megapixels > float(max_megapixels):
                raise ImageQualityError("megapixel_limit")

            source_mode = image.mode
            is_grayscale = source_mode in _GRAYSCALE_MODES

            exif_orientation: int | None = None
            try:
                exif = image.getexif()
                raw_orientation = exif.get(274) if exif else None
                if raw_orientation is not None:
                    exif_orientation = int(raw_orientation)
            except (OSError, ValueError, TypeError):
                exif_orientation = None

            with warnings.catch_warnings():
                warnings.simplefilter("ignore", Image.DecompressionBombWarning)
                oriented = ImageOps.exif_transpose(image)

            oriented_width, oriented_height = oriented.size
            if oriented is not image:
                # exif_transpose returns a new image; close the original later.
                pass

            rgb = oriented.convert("RGB")
            array = np.array(rgb, dtype=np.uint8, copy=True)
            if oriented is not image:
                oriented.close()
            rgb.close()
    except ImageQualityError:
        raise
    except Image.DecompressionBombError as exc:
        raise ImageQualityError("megapixel_limit") from exc
    except MemoryError as exc:
        raise ImageQualityError("memory_limit") from exc
    except (OSError, ValueError) as exc:
        raise ImageQualityError("decode_error") from exc

    exif_applied = exif_orientation is not None and exif_orientation not in (0, 1)
    return LoadedImage(
        array=array,
        original_width=original_width,
        original_height=original_height,
        oriented_width=oriented_width,
        oriented_height=oriented_height,
        format=image_format,
        source_mode=source_mode,
        is_grayscale=is_grayscale,
        exif_orientation=exif_orientation,
        exif_applied=exif_applied,
    )
