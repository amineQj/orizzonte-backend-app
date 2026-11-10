from __future__ import annotations

import io

import pytest
from PIL import Image

from image_quality.errors import ImageQualityError
from image_quality.image_loader import load_image

from . import synthetic as s


def test_real_format_is_detected_from_content() -> None:
    loaded = load_image(s.webp_named_jpg_bytes(), 40.0)
    assert loaded.format == "WEBP"
    assert loaded.array.shape == (1000, 1600, 3)
    assert loaded.array.dtype.name == "uint8"


def test_exif_orientation_is_applied_in_memory() -> None:
    loaded = load_image(s.exif_rotated_jpeg_bytes(), 40.0)
    assert loaded.format == "JPEG"
    assert loaded.original_width == 1600
    assert loaded.original_height == 1000
    assert loaded.oriented_width == 1000
    assert loaded.oriented_height == 1600
    assert loaded.exif_orientation == 6
    assert loaded.exif_applied is True
    assert loaded.array.shape == (1600, 1000, 3)


def test_grayscale_is_preserved_as_metadata_and_expanded_to_rgb() -> None:
    loaded = load_image(s.grayscale_document_bytes(), 40.0)
    assert loaded.is_grayscale is True
    assert loaded.source_mode == "L"
    assert loaded.array.shape == (1000, 1600, 3)


def test_path_source_and_missing_path(tmp_path) -> None:
    path = tmp_path / "specimen.png"
    path.write_bytes(s.good_document_bytes())
    loaded = load_image(path, 40.0)
    assert loaded.format == "PNG"

    missing = tmp_path / "missing.png"
    with pytest.raises(ImageQualityError) as exc_info:
        load_image(missing, 40.0)
    assert exc_info.value.category == "file_not_found"
    assert exc_info.value.reason_code == "UNREADABLE_IMAGE"

    with pytest.raises(ImageQualityError) as exc_info:
        load_image(tmp_path, 40.0)
    assert exc_info.value.category == "not_a_file"


def test_empty_and_corrupt_inputs() -> None:
    with pytest.raises(ImageQualityError) as exc_info:
        load_image(b"", 40.0)
    assert exc_info.value.category == "empty_input"

    with pytest.raises(ImageQualityError) as exc_info:
        load_image(s.corrupt_bytes(), 40.0)
    assert exc_info.value.category == "corrupt_image"

    with pytest.raises(ImageQualityError) as exc_info:
        load_image(b"\x89PNG\r\n\x1a\nbroken", 40.0)
    assert exc_info.value.category == "corrupt_image"


def test_unsupported_format() -> None:
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 200, 200)).save(buffer, format="GIF")
    with pytest.raises(ImageQualityError) as exc_info:
        load_image(buffer.getvalue(), 40.0)
    assert exc_info.value.category == "unsupported_format"


def test_megapixel_limit() -> None:
    with pytest.raises(ImageQualityError) as exc_info:
        load_image(s.good_document_bytes(), 1.0)
    assert exc_info.value.category == "megapixel_limit"


def test_original_dimensions_are_not_the_oriented_dimensions_for_exif() -> None:
    loaded = load_image(s.exif_rotated_jpeg_bytes(), 40.0)
    assert loaded.original_megapixels == pytest.approx(1.6)
    assert loaded.oriented_megapixels == pytest.approx(1.6)
    assert (loaded.original_width, loaded.original_height) != (
        loaded.oriented_width,
        loaded.oriented_height,
    )
