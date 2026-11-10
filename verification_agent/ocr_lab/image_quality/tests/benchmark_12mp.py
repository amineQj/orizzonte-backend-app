"""Local 12-megapixel timing benchmark.

This is a reporting script, not a pass/fail test.  It generates its synthetic
image in memory and never writes an image to disk.
"""

from __future__ import annotations

import json
import os
import platform
import sys
from time import perf_counter

import cv2
import numpy
import PIL

from image_quality import analyze_bytes

from . import synthetic as s


def main(repetitions: int = 5) -> int:
    width, height = 4000, 3000
    image_bytes = s.good_document_bytes(width, height, image_format="JPEG")

    # Warm up imports, allocation paths, and CPU caches.
    analyze_bytes(image_bytes, "id_card")

    timings: list[float] = []
    for _ in range(repetitions):
        start = perf_counter()
        result = analyze_bytes(image_bytes, "id_card")
        timings.append((perf_counter() - start) * 1000.0)

    payload = {
        "image_width_px": width,
        "image_height_px": height,
        "image_megapixels": round((width * height) / 1_000_000.0, 3),
        "image_format": "JPEG",
        "profile": "id_card",
        "status": result["status"],
        "repetitions": repetitions,
        "methodology": (
            "Synthetic SPECIMEN JPEG generated and encoded in memory; one warm-up "
            "analysis; generation and JPEG encoding excluded from timings."
        ),
        "times_ms": [round(value, 3) for value in timings],
        "min_ms": round(min(timings), 3),
        "median_ms": round(sorted(timings)[len(timings) // 2], 3),
        "max_ms": round(max(timings), 3),
        "hardware": {
            "platform": platform.platform(),
            "processor": platform.processor(),
            "cpu_count": os.cpu_count(),
        },
        "software": {
            "python": sys.version.split()[0],
            "opencv": cv2.__version__,
            "numpy": numpy.__version__,
            "pillow": PIL.__version__,
        },
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
