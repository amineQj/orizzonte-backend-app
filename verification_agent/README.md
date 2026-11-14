# ORIZZONTE verification agent workspace

This workspace contains the standalone Phase 2/3 image-quality and
document-classification module:

- module: `ocr_lab/image_quality/`
- documentation: `ocr_lab/image_quality/README.md`
- tests: `ocr_lab/image_quality/tests/`
- isolated OCR runner: `easyocr_runner.py`

The core module is offline, in-memory, and uncalibrated. OCR is available only through
the optional `ocr_lab/image_quality/backend_bridge.py`, which calls isolated EasyOCR and
Tesseract installations. EasyOCR and PyTorch are not installed in this workspace's
virtual environment. The module is not production-ready.

## Environment and tests

Run these commands from this directory:

```powershell
Set-Location "C:\Users\THINKPAD\Desktop\orizzonte-backend-app\verification_agent"
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --dry-run --only-binary=:all: -r requirements.txt -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest
```

Runtime pins: `opencv-python-headless==5.0.0.93`, `numpy==2.5.4`, `Pillow==12.3.0`.
Development pin: `pytest==9.2.0`.

## CLI

```powershell
Set-Location "C:\Users\THINKPAD\Desktop\orizzonte-backend-app\verification_agent\ocr_lab"
..\.venv\Scripts\python.exe -m image_quality analyze C:\path\to\image.jpg --profile id_card
..\.venv\Scripts\python.exe -m image_quality calibrate C:\path\to\folder --profile id_card
..\.venv\Scripts\python.exe -m image_quality evaluate C:\path\to\folder --labels C:\path\to\labels.csv --profile id_card
```

`analyze` prints JSON only and never prints the input path. See
`ocr_lab/image_quality/README.md` for the output contract, every metric/default,
calibration rules, privacy behavior, test coverage, benchmark, and limitations.

## Scope boundary

Do not install EasyOCR or PyTorch in this virtual environment. The bridge is the only
Django integration point, and OCR engines remain separate subprocesses. Do not call the
module production-ready based on synthetic tests. It remains `UNCALIBRATED` until a human
owner validates real, consented specimens.
