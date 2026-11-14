"""Standalone EasyOCR runner for the verification bridge.

This script is executed by the separate EasyOCR virtual environment, never by
the Django backend or the verification_agent virtual environment.  It prints
one JSON object to stdout and never prints recognized text to logs.
"""

from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path

LANGUAGES = ("ar", "en")


def _write(status: str, text: str = "") -> None:
    print(
        json.dumps(
            {"status": status, "text": text},
            ensure_ascii=True,
            separators=(",", ":"),
        )
    )


def main() -> int:
    if len(sys.argv) != 2:
        _write("FAILED")
        return 2

    image_path = Path(sys.argv[1])
    if not image_path.is_file():
        _write("FAILED")
        return 2

    try:
        with contextlib.redirect_stdout(sys.stderr):
            import easyocr

            reader = easyocr.Reader(
                list(LANGUAGES),
                gpu=False,
                verbose=False,
            )
            results = reader.readtext(str(image_path), detail=0)
    except Exception:
        _write("FAILED")
        return 2

    text = "\n".join(
        item.strip()
        for item in results
        if isinstance(item, str) and item.strip()
    ).strip()
    _write("USED" if text else "EMPTY", text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
