from __future__ import annotations

import sys
from pathlib import Path

OCR_LAB_DIR = Path(__file__).resolve().parents[2]
if str(OCR_LAB_DIR) not in sys.path:
    sys.path.insert(0, str(OCR_LAB_DIR))
