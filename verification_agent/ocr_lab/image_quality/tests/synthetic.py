"""Synthetic SPECIMEN / TEST images used only by the test suite.

These are deliberately generic, fictional layouts.  They are not replicas of
real Algerian documents and contain no personal data.
"""

from __future__ import annotations

import io
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont


def _font(size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.truetype("arial.ttf", max(8, int(size)))
    except OSError:
        return ImageFont.load_default()


def _draw_document_card(
    image: Image.Image,
    rect: tuple[int, int, int, int],
    *,
    paper: tuple[int, int, int] = (226, 226, 226),
    ink: tuple[int, int, int] = (30, 30, 30),
    outline: tuple[int, int, int] = (70, 70, 70),
    header_fill: tuple[int, int, int] = (185, 185, 185),
    visible_marks: bool = False,
) -> None:
    draw = ImageDraw.Draw(image)
    x0, y0, x1, y1 = rect
    width = max(1, x1 - x0)
    height = max(1, y1 - y0)
    draw.rectangle(rect, fill=paper, outline=outline, width=max(2, width // 350))
    header_height = max(18, int(height * 0.17))
    draw.rectangle(
        [x0 + 2, y0 + 2, x1 - 2, y0 + header_height],
        fill=header_fill,
    )
    big = _font(max(10, int(height * 0.065)))
    small = _font(max(8, int(height * 0.042)))
    draw.text(
        (x0 + int(width * 0.06), y0 + int(header_height * 0.22)),
        "SPECIMEN / TEST",
        fill=ink,
        font=big,
    )
    draw.text(
        (x0 + int(width * 0.06), y0 + header_height + int(height * 0.035)),
        "FICTIONAL DOCUMENT - NOT A REAL ID",
        fill=ink,
        font=small,
    )

    line_top = y0 + header_height + int(height * 0.20)
    line_height = max(2, int(height * 0.025))
    for index in range(5):
        y = line_top + index * int(height * 0.105)
        if y + line_height >= y1:
            break
        draw.rectangle(
            [x0 + int(width * 0.06), y, x0 + int(width * 0.48), y + line_height],
            fill=ink,
        )
        draw.rectangle(
            [x0 + int(width * 0.06), y + line_height + 2, x0 + int(width * 0.36), y + line_height + 2],
            fill=outline,
        )

    photo_x0 = x0 + int(width * 0.62)
    photo_y0 = y0 + int(height * 0.30)
    photo_x1 = x0 + int(width * 0.92)
    photo_y1 = y0 + int(height * 0.82)
    draw.rectangle([photo_x0, photo_y0, photo_x1, photo_y1], outline=outline, width=max(2, width // 500))
    draw.line([photo_x0, photo_y0, photo_x1, photo_y1], fill=outline, width=2)
    draw.line([photo_x1, photo_y0, photo_x0, photo_y1], fill=outline, width=2)

    if visible_marks:
        draw.text(
            (int(width * 0.08), int(height * 0.08)),
            "SPECIMEN / TEST",
            fill=ink,
            font=_font(max(12, int(height * 0.07))),
        )
        for index in range(8):
            y = int(height * 0.22) + index * int(height * 0.065)
            draw.rectangle(
                [int(width * 0.10), y, int(width * 0.45), y + max(3, int(height * 0.018))],
                fill=ink,
            )


def _to_bytes(image: Image.Image, image_format: str = "PNG", **save_kwargs: Any) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format=image_format, **save_kwargs)
    return buffer.getvalue()


def document_image(
    width: int = 1600,
    height: int = 1000,
    *,
    margin_fraction: float = 0.10,
    paper: tuple[int, int, int] = (226, 226, 226),
    ink: tuple[int, int, int] = (30, 30, 30),
    outline: tuple[int, int, int] = (70, 70, 70),
    header_fill: tuple[int, int, int] = (185, 185, 185),
    background: tuple[int, int, int] = (38, 42, 48),
) -> Image.Image:
    image = Image.new("RGB", (width, height), background)
    margin_x = int(width * margin_fraction)
    margin_y = int(height * margin_fraction)
    _draw_document_card(
        image,
        (margin_x, margin_y, width - margin_x, height - margin_y),
        paper=paper,
        ink=ink,
        outline=outline,
        header_fill=header_fill,
    )
    return image


def good_document_bytes(
    width: int = 1600,
    height: int = 1000,
    *,
    image_format: str = "PNG",
) -> bytes:
    image = document_image(width, height)
    return _to_bytes(image, image_format, quality=95) if image_format != "PNG" else _to_bytes(image)


def blurry_document_bytes(width: int = 1600, height: int = 1000, radius: float = 5.0) -> bytes:
    image = document_image(width, height).filter(ImageFilter.GaussianBlur(radius))
    return _to_bytes(image)


def dark_document_bytes(width: int = 1600, height: int = 1000) -> bytes:
    image = document_image(
        width,
        height,
        paper=(55, 55, 55),
        ink=(12, 12, 12),
        outline=(80, 80, 80),
        header_fill=(45, 45, 45),
        background=(4, 4, 4),
    )
    return _to_bytes(image)


def overexposed_document_bytes(width: int = 1600, height: int = 1000) -> bytes:
    image = document_image(
        width,
        height,
        paper=(250, 250, 250),
        ink=(238, 238, 238),
        outline=(100, 100, 100),
        header_fill=(246, 246, 246),
        background=(180, 180, 180),
    )
    return _to_bytes(image)


def low_contrast_document_bytes(width: int = 1600, height: int = 1000) -> bytes:
    image = document_image(
        width,
        height,
        paper=(170, 170, 170),
        ink=(100, 100, 100),
        outline=(45, 45, 45),
        header_fill=(160, 160, 160),
        background=(90, 90, 90),
    )
    return _to_bytes(image)


def glare_document_bytes(width: int = 1600, height: int = 1000) -> bytes:
    image = document_image(width, height).convert("RGBA")
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    center_x = int(width * 0.45)
    center_y = int(height * 0.48)
    radius_x = int(width * 0.13)
    radius_y = int(height * 0.10)
    draw.ellipse(
        [center_x - radius_x, center_y - radius_y, center_x + radius_x, center_y + radius_y],
        fill=(255, 255, 255, 255),
    )
    overlay = overlay.filter(ImageFilter.GaussianBlur(8))
    merged = Image.alpha_composite(image, overlay).convert("RGB")
    return _to_bytes(merged)


def high_near_saturation_document_bytes(
    width: int = 1600,
    height: int = 1000,
) -> bytes:
    """Synthetic white-paper image with a local reflection.

    Most of the document is near-saturated white paper, so the configured
    near-saturation guard must suppress the local glare signal.  The lower gray
    band keeps the mean brightness below the overexposure rule so this image
    isolates the near-saturation guard.
    """

    image = document_image(
        width,
        height,
        paper=(252, 252, 252),
        ink=(30, 30, 30),
        header_fill=(252, 252, 252),
    )
    draw = ImageDraw.Draw(image)
    margin_x = int(width * 0.10)
    margin_y = int(height * 0.10)
    draw.rectangle(
        [
            margin_x,
            int(height * 0.60),
            width - margin_x,
            height - margin_y,
        ],
        fill=(170, 170, 170),
    )

    rgba = image.convert("RGBA")
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw_overlay = ImageDraw.Draw(overlay)
    center_x = int(width * 0.50)
    center_y = int(height * 0.74)
    radius_x = int(width * 0.10)
    radius_y = int(height * 0.09)
    draw_overlay.ellipse(
        [center_x - radius_x, center_y - radius_y, center_x + radius_x, center_y + radius_y],
        fill=(255, 255, 255, 255),
    )
    overlay = overlay.filter(ImageFilter.GaussianBlur(6))
    merged = Image.alpha_composite(rgba, overlay).convert("RGB")
    return _to_bytes(merged)


def cropped_document_bytes(width: int = 1500, height: int = 1000) -> bytes:
    image = Image.new("RGB", (width, height), (30, 30, 30))
    _draw_document_card(
        image,
        (-int(width * 0.10), -int(height * 0.10), int(width * 0.90), int(height * 0.90)),
        visible_marks=True,
    )
    return _to_bytes(image)


def rotated_document_bytes(
    width: int = 1400,
    height: int = 1000,
    angle: float = 12.0,
) -> bytes:
    base = Image.new("RGB", (width, height), (35, 35, 35))
    layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    _draw_document_card(
        layer,
        (int(width * 0.18), int(height * 0.18), int(width * 0.82), int(height * 0.82)),
    )
    rotated = layer.rotate(angle, resample=Image.Resampling.BICUBIC, expand=False)
    base.paste(rotated, (0, 0), rotated)
    return _to_bytes(base)


def uneven_light_document_bytes(width: int = 1600, height: int = 1000) -> bytes:
    image = document_image(width, height)
    array = np.asarray(image).astype(np.float32)
    gradient = np.linspace(1.20, 0.42, width, dtype=np.float32)[None, :, None]
    array = np.clip(array * gradient, 0, 255).astype(np.uint8)
    return _to_bytes(Image.fromarray(array, mode="RGB"))


def no_document_bytes(width: int = 1500, height: int = 1000, seed: int = 7) -> bytes:
    rng = np.random.default_rng(seed)
    horizontal = np.linspace(35.0, 72.0, width, dtype=np.float32)[None, :]
    vertical = np.linspace(0.0, 14.0, height, dtype=np.float32)[:, None]
    noise = rng.normal(0.0, 1.1, (height, width)).astype(np.float32)
    gray = np.clip(horizontal + vertical + noise, 0, 255).astype(np.uint8)
    image = Image.fromarray(np.dstack([gray, gray, gray]), mode="RGB")
    image = image.filter(ImageFilter.GaussianBlur(1.0))
    return _to_bytes(image)


def tiny_image_bytes(width: int = 32, height: int = 32) -> bytes:
    image = Image.new("RGB", (width, height), (240, 240, 240))
    draw = ImageDraw.Draw(image)
    draw.rectangle([0, 0, width - 1, height - 1], outline=(20, 20, 20), width=1)
    return _to_bytes(image)


def low_resolution_document_bytes(width: int = 320, height: int = 240) -> bytes:
    image = document_image(width, height, margin_fraction=0.08)
    return _to_bytes(image)


def grayscale_document_bytes() -> bytes:
    return _to_bytes(document_image(1600, 1000).convert("L"))


def webp_named_jpg_bytes() -> bytes:
    image = document_image(1600, 1000)
    return _to_bytes(image, "WEBP", quality=95)


def exif_rotated_jpeg_bytes() -> bytes:
    image = document_image(1600, 1000)
    exif = Image.Exif()
    exif[274] = 6  # rotate 90 degrees clockwise when displayed
    return _to_bytes(image, "JPEG", quality=95, exif=exif)


def huge_image_bytes(width: int = 3000, height: int = 3000) -> bytes:
    image = Image.new("RGB", (width, height), (200, 200, 200))
    draw = ImageDraw.Draw(image)
    draw.rectangle([100, 100, width - 100, height - 100], outline=(20, 20, 20), width=5)
    return _to_bytes(image)


def corrupt_bytes() -> bytes:
    return b"this is not an image and must never be decoded"


def selfie_image_bytes(
    width: int = 1200,
    height: int = 900,
    *,
    blur: float = 0.0,
) -> bytes:
    image = Image.new("RGB", (width, height), (120, 130, 140))
    draw = ImageDraw.Draw(image)
    draw.rectangle(
        [int(width * 0.08), int(height * 0.08), int(width * 0.92), int(height * 0.92)],
        fill=(185, 170, 155),
        outline=(40, 40, 40),
        width=3,
    )
    draw.text(
        (int(width * 0.18), int(height * 0.16)),
        "SPECIMEN / TEST",
        fill=(20, 20, 20),
        font=_font(max(18, int(height * 0.06))),
    )
    draw.text(
        (int(width * 0.18), int(height * 0.25)),
        "NO FACE - QUALITY TEST ONLY",
        fill=(30, 30, 30),
        font=_font(max(12, int(height * 0.04))),
    )
    for index in range(16):
        y = int(height * 0.38) + index * int(height * 0.028)
        draw.rectangle(
            [int(width * 0.18), y, int(width * (0.55 + 0.18 * (index % 3))), y + max(2, int(height * 0.008))],
            fill=(55, 65, 75),
        )
    if blur > 0:
        image = image.filter(ImageFilter.GaussianBlur(blur))
    return _to_bytes(image)


def write_bytes(tmp_path: Any, name: str, data: bytes) -> str:
    path = tmp_path / name
    path.write_bytes(data)
    return str(path)
