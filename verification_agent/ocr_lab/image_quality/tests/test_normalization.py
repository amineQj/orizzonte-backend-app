from __future__ import annotations

from image_quality.image_loader import load_image
from image_quality.normalization import normalize_image

from . import synthetic as s


def test_large_image_is_downscaled_to_fixed_long_side() -> None:
    loaded = load_image(s.good_document_bytes(2400, 1800), 40.0)
    working = normalize_image(loaded, 1600)
    assert working.resized is True
    assert max(working.working_width, working.working_height) == 1600
    assert working.working_width == 1600
    assert working.working_height == 1200
    assert working.scale_x == 1600 / 2400
    assert working.scale_y == 1200 / 1800


def test_small_image_is_not_upscaled() -> None:
    loaded = load_image(s.good_document_bytes(800, 600), 40.0)
    working = normalize_image(loaded, 1600)
    assert working.resized is False
    assert working.working_width == 800
    assert working.working_height == 600
    assert working.scale_x == 1.0
    assert working.scale_y == 1.0


def test_orientation_dimensions_feed_scale() -> None:
    loaded = load_image(s.exif_rotated_jpeg_bytes(), 40.0)
    working = normalize_image(loaded, 1600)
    assert working.oriented_width == 1000
    assert working.oriented_height == 1600
    assert working.working_width == 1000
    assert working.working_height == 1600
    assert working.resized is False
    assert working.scale_x == 1.0
    assert working.scale_y == 1.0
