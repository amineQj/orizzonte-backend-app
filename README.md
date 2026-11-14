# ORIZZONTE — Driver Document Verification Backend

Offline image-quality gate, document classifier, and asynchronous REST API for
Algerian driver documents.

ORIZZONTE verifies that an uploaded document is technically usable and that the
document type matches the upload slot declared by the driver. The pipeline
handles:

- Algerian national ID card (`id_card_front`, `id_card_back`)
- Algerian driving licence (`driving_license`)
- Algerian vehicle registration certificate (`vehicle_registration`)
- Driver selfie (`selfie`, face-presence check only)

The pipeline is intentionally conservative. It does not prove that a document is
authentic, valid, or belongs to a particular person. A match only means that the
image passed the quality checks and that public document-title keywords are
consistent with the declared slot. Final acceptance remains a backend or human
review decision.

> **Production note:** the image-quality thresholds are calibrated for specific
> local layouts, but this project is not a certified identity-verification system.
> It performs no biometrics, no face matching, and no identity lookup. Do not use
> real identity documents in development or automated tests; use synthetic
> `SPECIMEN / TEST` images.

---

## Table of contents

1. [Project overview](#1-project-overview)
2. [Architecture diagram](#2-architecture-diagram)
3. [Prerequisites](#3-prerequisites)
4. [Installation](#4-installation)
5. [How to run](#5-how-to-run)
6. [API endpoints](#6-api-endpoints)
7. [Module structure](#7-module-structure)
8. [Configuration files](#8-configuration-files)
9. [Testing](#9-testing)
10. [Troubleshooting](#10-troubleshooting)
11. [Contributing](#11-contributing)
12. [License / credits](#12-license--credits)

---

## 1. Project overview

### What ORIZZONTE does

ORIZZONTE is a Django REST backend for driver onboarding. For every uploaded
document it:

1. validates and re-encodes the image, removing EXIF metadata;
2. writes it to a private temporary file for asynchronous processing (the profile
   upload endpoint stores long-lived documents under `PRIVATE_MEDIA_ROOT`);
3. runs an OpenCV quality gate to decide whether the photo is usable;
4. extracts text with EasyOCR (Arabic document slots) or Tesseract;
5. classifies the public document title against configurable keywords;
6. compares the detected type with the declared upload slot;
7. returns a safe final decision: `ACCEPT`, `MANUAL_REVIEW`, or `REJECT`.

The verification work runs asynchronously. The upload endpoint returns immediately
with a `task_id`; the mobile app polls a status endpoint for the result.

### The problem it solves

Algerian driver documents are frequently photographed in poor conditions: motion
blur, low light, glare, perspective skew, or a document that is partially outside
the frame. Running OCR on those images wastes time and produces unreliable
classification. ORIZZONTE first rejects unusable photos, then checks whether the
visible public title is consistent with the slot the driver selected.

This catches common upload mistakes such as:

- a driving licence uploaded into the ID-card slot;
- a vehicle registration uploaded into the licence slot;
- a photo that is too blurred, dark, cropped, or glare-affected for OCR;
- an image containing only a generic republic header and no specific title.

### Architecture

The repository contains two cooperating Python projects:

| Component | Location | Responsibility |
| --- | --- | --- |
| Django REST API | `demenagement/`, `accounts/`, `drivers/`, `authentication/`, `missions/` | Authentication, driver profiles, private uploads, async task API, final decision |
| Verification agent | `verification_agent/ocr_lab/image_quality/` | OpenCV quality gate, OCR bridge, keyword classification |
| EasyOCR server | `verification_agent/easyocr_server.py` | Persistent FastAPI process that keeps the EasyOCR model warm |
| Tesseract | external CLI | Fallback OCR and primary OCR for non-EasyOCR slots |
| SQLite | `db.sqlite3` (development) | Users, profiles, review logs |
| Private media | `PRIVATE_MEDIA_ROOT` | Identity documents, outside static/media web roots |

The Django virtual environment deliberately does **not** contain OpenCV, NumPy,
PyTorch, or EasyOCR. The verification module runs through its own interpreter
(see `VERIFICATION_AGENT_PYTHON`). EasyOCR and PyTorch run only in the isolated
environment at `C:\orizzonte_easyocr_env`.

---

## 2. Architecture diagram

### Request and data flow

```text
┌──────────────────────────────┐
│  Mobile app (Flutter, etc.)  │
│  JWT + multipart upload      │
└──────────────┬───────────────┘
               │
               │ 1. POST /api/token/                    (email + password)
               │ 2. POST /api/verification/check/       (slot + image)
               ▼
┌──────────────────────────────────────────────────────────────┐
│ Django REST API                                              │
│                                                              │
│  accounts/          JWT auth, roles, email verification      │
│  drivers/documents  JPEG/PNG/WebP validation, EXIF stripping │
│  drivers/views      profile and document storage              │
│  drivers/verification_views.py                               │
│      ├─ validate upload                                      │
│      ├─ create task_id                                       │
│      ├─ return 202 Accepted                                  │
│      └─ ThreadPoolExecutor worker                            │
└──────────────────────────────┬───────────────────────────────┘
                               │
                               │ subprocess: JSON over stdin/stdout
                               ▼
┌──────────────────────────────────────────────────────────────┐
│ verification_agent/.venv                                     │
│   python -m image_quality.backend_bridge                     │
│                                                              │
│   ┌──────────────────────────────────────────────────────┐   │
│   │ image_quality                                        │   │
│   │  1. OpenCV quality gate                               │   │
│   │  2. OCR selection                                     │   │
│   │  3. keyword classification                            │   │
│   └───────────────┬──────────────────────┬───────────────┘   │
│                   │                      │                   │
│                   │ EasyOCR slots        │ other / fallback  │
│                   ▼                      ▼                   │
│       POST http://127.0.0.1:8001/ocr    tesseract CLI       │
│                   │                      │                   │
└───────────────────┼──────────────────────┼───────────────────┘
                    │                      │
                    ▼                      │
┌──────────────────────────────────────┐   │
│ EasyOCR FastAPI server               │   │
│ easyocr.Reader(["ar", "en"])         │   │
│ loaded once at startup, CPU mode     │   │
└──────────────────────────────────────┘   │
                                           ▼
                              fra+ara traineddata required
```

### Asynchronous flow

The verification endpoint does not block the HTTP request while OCR runs:

```text
Client                                  Django
  │                                       │
  │ POST /api/verification/check/         │
  │──────────────────────────────────────>│
  │                                       │ validate + queue
  │ 202 Accepted                          │
  │ {"task_id": "...", "status":"pending"}│
  │<──────────────────────────────────────│
  │                                       │
  │ GET /api/verification/status/{id}/    │
  │──────────────────────────────────────>│
  │ 202 Accepted                          │
  │ {"task_id":"...","status":"processing"}│
  │<──────────────────────────────────────│
  │                                       │ worker completes
  │ GET /api/verification/status/{id}/    │
  │──────────────────────────────────────>│
  │ 200 OK                                │
  │ { quality, classification, ocr,       │
  │   final_decision, task_id, status }   │
  │<──────────────────────────────────────│
```

Task states are `pending`, `processing`, `completed`, and `failed`.

> **Operational limitation:** the task registry and `ThreadPoolExecutor` are
> in-memory and process-local. A Django restart loses pending/completed tasks, and
> a multi-process deployment would need a shared queue (Celery/RQ/Redis) and a
> persistent result store. The current implementation is suitable for development
> and a single-process demo.

---

## 3. Prerequisites

| Requirement | Version / notes |
| --- | --- |
| Git | Any recent version |
| Python for Django | 3.11 or newer; Python 3.13 was used in this workspace |
| Python for `verification_agent` | 3.11 or newer; use the interpreter available on your machine |
| Python for EasyOCR | **Python 3.12** (the supported PyTorch/EasyOCR combination) |
| Tesseract OCR | 5.x with `fra.traineddata` and `ara.traineddata` |
| EasyOCR | 1.7.2 + PyTorch CPU wheels in `C:\orizzonte_easyocr_env` |
| FastAPI + Uvicorn | Installed in the EasyOCR environment |
| RAM | 4 GB+ recommended for the EasyOCR model |
| Disk | Model cache plus private media space |
| OS | Windows 10/11 commands are shown; Linux/macOS equivalents are noted |

The first EasyOCR startup downloads its detection and recognition models. Runtime
analysis is otherwise offline. Internet access is needed for installation and the
first model download.

PowerShell examples below use Windows paths. On Linux/macOS:

```bash
python3 -m venv venv
source venv/bin/activate
```

---

## 4. Installation

### 4.1 Clone the repository

```powershell
git clone https://github.com/amineQj/orizzonte-backend-app.git
Set-Location .\orizzonte-backend-app
```

### 4.2 Django backend environment

Use one Django environment name consistently. The commands below use `venv`.

```powershell
py -3.11 -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

# demenagement/settings.py imports corsheaders, but the current
# requirements.txt does not pin django-cors-headers. Install it explicitly
# until the requirements file is updated.
python -m pip install "django-cors-headers==4.9.0"
```

If PowerShell blocks activation:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\venv\Scripts\Activate.ps1
```

Linux/macOS:

```bash
python3 -m venv venv
source venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install "django-cors-headers==4.9.0"
```

The Django environment must not contain `torch`, `torchvision`, or `easyocr`.

### 4.3 Verification agent environment

The verification agent has its own environment. Do not install EasyOCR or PyTorch
into it.

```powershell
Set-Location .\verification_agent
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install --dry-run --only-binary=:all: -r requirements.txt -r requirements-dev.txt
python -m pip install -r requirements.txt -r requirements-dev.txt
Set-Location ..
```

The runtime pins are:

```text
opencv-python-headless==5.0.0.93
numpy==2.5.4
Pillow==12.3.0
pytest==9.2.0   (development only)
```

### 4.4 EasyOCR environment

Create the environment outside the project so the Django and verification
environments stay lightweight:

```powershell
py -3.12 -m venv C:\orizzonte_easyocr_env
C:\orizzonte_easyocr_env\Scripts\python.exe -m pip install --upgrade pip
C:\orizzonte_easyocr_env\Scripts\python.exe -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
C:\orizzonte_easyocr_env\Scripts\python.exe -m pip install easyocr==1.7.2 fastapi uvicorn
```

Verify the environment:

```powershell
C:\orizzonte_easyocr_env\Scripts\python.exe -c "import easyocr, torch; print('EasyOCR', easyocr.__version__); print('PyTorch', torch.__version__)"
```

`verification_agent\start_easyocr_server.bat` assumes the environment is exactly at
`C:\orizzonte_easyocr_env\Scripts\python.exe`. If you use another path, edit the
batch file or start Uvicorn with the full path to your interpreter.

### 4.5 Tesseract OCR

Tesseract is a command-line program, not a server. It is invoked on demand by
`backend_bridge.py`.

Windows:

1. Download the UB-Mannheim installer:
   `https://github.com/UB-Mannheim/tesseract/releases/download/v5.4.0.20240606/tesseract-ocr-w64-setup-5.4.0.20240606.exe`
2. Install it, or extract it with 7-Zip into
   `%LOCALAPPDATA%\Programs\Tesseract-OCR` if you cannot elevate.
3. Add the installation directory to `PATH`, or set `TESSERACT_CMD`.
4. Put `fra.traineddata` and `ara.traineddata` in
   `C:\Program Files\Tesseract-OCR\tessdata`:

```powershell
Invoke-WebRequest `
  -Uri "https://github.com/tesseract-ocr/tessdata/raw/main/fra.traineddata" `
  -OutFile "C:\Program Files\Tesseract-OCR\tessdata\fra.traineddata"

Invoke-WebRequest `
  -Uri "https://github.com/tesseract-ocr/tessdata/raw/main/ara.traineddata" `
  -OutFile "C:\Program Files\Tesseract-OCR\tessdata\ara.traineddata"
```

5. Set `TESSDATA_PREFIX=C:\Program Files\Tesseract-OCR\tessdata`.
6. Verify:

```powershell
tesseract --version
tesseract --list-langs
```

`--list-langs` must include both `fra` and `ara`.

Linux:

```bash
sudo apt-get update
sudo apt-get install tesseract-ocr tesseract-ocr-fra tesseract-ocr-ara
```

### 4.6 Configure environment variables

Copy the template and edit it:

```powershell
Copy-Item .env.example .env
```

`.env` is already in `.gitignore`. Never commit secrets or real identity data.

At minimum, set the private media directory. Django refuses to start without it.

```dotenv
# --- Django ---
SECRET_KEY=replace-with-a-generated-secret
DEBUG=True
ALLOWED_HOSTS=*
SITE_URL=http://127.0.0.1:8000

# --- Private identity documents (ABSOLUTE path, outside static files) ---
PRIVATE_MEDIA_ROOT=C:/orizzonte/private_media
MAX_UPLOAD_MB=5

# --- Verification agent ---
# Empty = auto-detect verification_agent/.venv
VERIFICATION_AGENT_PYTHON=
VERIFICATION_AGENT_TIMEOUT_SECONDS=180
VERIFICATION_AGENT_DISABLE_OCR=False
VERIFICATION_AGENT_DEBUG_OCR=False

# --- Persistent EasyOCR server ---
EASYOCR_SERVER_URL=http://127.0.0.1:8001

# Legacy/one-shot EasyOCR runner only. The persistent bridge uses
# EASYOCR_SERVER_URL and does not require this variable.
EASYOCR_PYTHON_PATH=C:\orizzonte_easyocr_env\Scripts\python.exe

# --- Tesseract fallback ---
TESSDATA_PREFIX=C:\Program Files\Tesseract-OCR\tessdata
TESSERACT_CMD=C:\Program Files\Tesseract-OCR\tesseract.exe
TESSERACT_LANGUAGE=fra+ara
```

Important variables:

| Variable | Purpose |
| --- | --- |
| `PRIVATE_MEDIA_ROOT` | Absolute directory for identity documents. Mandatory; Django check `drivers.E001` fails if missing. |
| `VERIFICATION_AGENT_PYTHON` | Interpreter for `verification_agent`. Empty means auto-detect `.venv/Scripts/python.exe`. |
| `VERIFICATION_AGENT_TIMEOUT_SECONDS` | Maximum time for the whole bridge subprocess. Default `180`. |
| `VERIFICATION_AGENT_DISABLE_OCR` | When `True`, classification runs without OCR. Useful for diagnostics only. |
| `VERIFICATION_AGENT_DEBUG_OCR` | Must be `True` together with `DEBUG=True` and `?debug=1` to expose raw OCR text. Never enable in production. |
| `EASYOCR_SERVER_URL` | URL of the persistent EasyOCR server. Default `http://127.0.0.1:8001`. |
| `EASYOCR_PYTHON_PATH` | Compatibility variable for legacy one-shot runner setups. The current bridge reads `EASYOCR_SERVER_URL` instead. |
| `TESSERACT_CMD` | Optional absolute path to `tesseract.exe`. |
| `TESSERACT_LANGUAGE` | Tesseract language string. Default `fra+ara`. |
| `TESSDATA_PREFIX` | Directory containing `fra.traineddata` and `ara.traineddata`. |
| `MAX_UPLOAD_MB` | Maximum upload size in megabytes. Default `5`. |

The `.env` file is loaded by `demenagement/settings.py` from the project root.
Restart Django after changing it.

### 4.7 Prepare the database and boss account

Return to the project root and use the Django environment explicitly:

```powershell
Set-Location C:\path\to\orizzonte-backend-app
.\venv\Scripts\python.exe manage.py migrate
.\venv\Scripts\python.exe manage.py check
.\venv\Scripts\python.exe manage.py create_boss --email boss@example.com
```

`create_boss` prompts for a password interactively. Never put a real password in
source control. For a non-interactive script, use `BOSS_PASSWORD` only in a
private shell environment.

---

## 5. How to run

Start the components in this order.

### 5.1 Start the EasyOCR server

Open a dedicated terminal:

```powershell
Set-Location .\verification_agent
.\start_easyocr_server.bat
```

Or start it directly:

```powershell
C:\orizzonte_easyocr_env\Scripts\python.exe -m uvicorn easyocr_server:app --host 127.0.0.1 --port 8001
```

Check that it is alive:

```powershell
Invoke-RestMethod http://127.0.0.1:8001/health
```

Expected response:

```json
{"status": "ok"}
```

The server loads `easyocr.Reader(["ar", "en"], gpu=False)` once at startup. The
first startup may take a while and may download models. Subsequent requests reuse
the warm model.

### 5.2 Start the Django server

In another terminal, from the project root:

```powershell
.\venv\Scripts\Activate.ps1
python manage.py runserver 127.0.0.1:8000
```

To let a phone on the same Wi-Fi reach the API:

```powershell
python manage.py runserver 0.0.0.0:8000
```

Then set:

```dotenv
ALLOWED_HOSTS=*
SITE_URL=http://192.168.1.20:8000
```

Use the actual IPv4 address of the PC (`ipconfig`). Allow port `8000` in the
Windows firewall if needed. Emulator note: an Android emulator usually reaches
the host at `http://10.0.2.2:8000`.

### 5.3 Tesseract fallback

There is no Tesseract server to start. The bridge invokes the CLI automatically
when needed. Verify the installation instead:

```powershell
tesseract --version
tesseract --list-langs
```

If EasyOCR is running and returns text, Tesseract is not used for the three
EasyOCR slots. It remains the primary OCR for `vehicle_registration` and the
fallback for `id_card_front`, `id_card_back`, and `driving_license`.

### 5.4 OCR selection and fallback behavior

EasyOCR is the primary engine for:

- `id_card_front`
- `id_card_back`
- `driving_license`

Tesseract is used:

- for `vehicle_registration`;
- for the EasyOCR slots when the EasyOCR server is unavailable, returns a
  non-200 response, returns invalid JSON, times out, or returns no text.

If neither engine is available, the classification is `NEEDS_REVIEW` with
`reason_code: "NO_OCR"`.

If EasyOCR returns non-empty text but no specific document title matches, the
pipeline does **not** fall back to Tesseract. It returns the EasyOCR-based result
with `decision: "NEEDS_REVIEW"` so that a human can review it. The reason code is
`"AMBIGUOUS_KEYWORDS"` when only generic republic wording is present, or
`"NO_KEYWORD"` when no configured title keyword is found at all.

### 5.5 Health checklist

```text
[ ] EasyOCR /health returns 200 and {"status":"ok"}
[ ] Django starts without system check errors
[ ] PRIVATE_MEDIA_ROOT exists and is writable
[ ] tesseract --list-langs lists fra and ara
[ ] verification_agent/.venv/Scripts/python.exe exists
[ ] verification_agent can import image_quality
```

---

## 6. API endpoints

Base URL for local development:

```text
http://127.0.0.1:8000
```

All verification endpoints require a JWT access token:

```http
Authorization: Bearer <jwt-access-token>
```

### 6.1 `POST /api/token/` — JWT login

Credentials are **email + password**. The account email must be verified before
login succeeds.

Request:

```powershell
curl.exe -X POST "http://127.0.0.1:8000/api/token/" `
  -H "Content-Type: application/json" `
  -d '{"email":"driver@example.com","password":"REPLACE_ME"}'
```

Body:

```json
{
  "email": "driver@example.com",
  "password": "REPLACE_ME"
}
```

Success response:

```json
{
  "id": 1,
  "username": "driver",
  "email": "driver@example.com",
  "role": "driver",
  "access": "<jwt-access-token>",
  "refresh": "<jwt-refresh-token>"
}
```

Related authentication endpoints:

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `POST` | `/api/token/refresh/` | Exchange a refresh token for a new access token |
| `POST` | `/api/logout/` | Blacklist the refresh token |
| `GET` | `/api/users/me/` | Current user profile and role |

Example refresh body:

```json
{
  "refresh": "<jwt-refresh-token>"
}
```

### 6.2 `POST /api/verification/check/` — submit a document

Role: `driver`.

Multipart fields:

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `slot` | string | yes | `selfie`, `id_card_front`, `id_card_back`, `driving_license`, or `vehicle_registration` |
| `file` | file | yes | JPEG, PNG, or WebP; maximum `MAX_UPLOAD_MB` (default 5 MB) |

The uploaded file is validated with Pillow, re-encoded to remove EXIF metadata,
and stored only in a temporary file for the worker. The verification endpoint
does not retain the upload after processing.

Request:

```powershell
curl.exe -X POST "http://127.0.0.1:8000/api/verification/check/" `
  -H "Authorization: Bearer <jwt-access-token>" `
  -F "slot=driving_license" `
  -F "file=@C:\path\to\specimen.jpg"
```

Immediate response — `202 Accepted`:

```json
{
  "task_id": "8f14e45fceea167a5a36dedd4bea2543",
  "status": "pending"
}
```

Possible errors:

| Status | Meaning | Example body |
| --- | --- | --- |
| `400` | Invalid slot or rejected image | `{"detail": "...", "code": "invalid_document"}` |
| `401` | Missing or invalid access token | `{"detail": "..."}` |
| `403` | Authenticated user is not a driver | `{"detail": "..."}` |
| `413` | Upload exceeds the allowed body size | Framework-level error |

### 6.3 `GET /api/verification/status/<task_id>/` — poll the result

Role: `driver`. A driver can only read their own tasks.

Request:

```powershell
curl.exe "http://127.0.0.1:8000/api/verification/status/8f14e45fceea167a5a36dedd4bea2543/" `
  -H "Authorization: Bearer <jwt-access-token>"
```

While the task is queued or running — `202 Accepted`:

```json
{
  "task_id": "8f14e45fceea167a5a36dedd4bea2543",
  "status": "processing"
}
```

When the task is complete — `200 OK`:

```json
{
  "slot": "driving_license",
  "quality_status": "ACCEPTABLE_QUALITY",
  "reason_codes": [],
  "quality": {
    "schema_version": "1.0.0",
    "algorithm_version": "1.0.0",
    "profile": "driving_license",
    "layout": "card",
    "status": "ACCEPTABLE_QUALITY",
    "confidence": 0.93,
    "thresholds_calibrated": true,
    "processing_time_ms": 141.2,
    "image": {
      "is_grayscale": false,
      "original_width_px": 1600,
      "original_height_px": 1000,
      "original_megapixels": 1.6,
      "oriented_width_px": 1600,
      "oriented_height_px": 1000,
      "working_width_px": 1600,
      "working_height_px": 1000,
      "resized_for_analysis": false,
      "working_side_limit_px": 1600
    },
    "metrics": {
      "resolution_original": {
        "value": {
          "width_px": 1600,
          "height_px": 1000,
          "megapixels": 1.6,
          "oriented_width_px": 1600,
          "oriented_height_px": 1000,
          "working_width_px": 1600,
          "working_height_px": 1000,
          "resized_for_analysis": false,
          "exif_orientation_applied": true
        },
        "nature": "measured",
        "confidence": 1.0,
        "metadata": {}
      },
      "sharpness_laplacian": {
        "value": 312.4,
        "nature": "measured",
        "confidence": 0.95,
        "metadata": {}
      }
    },
    "reason_codes": [],
    "decision_trace": [],
    "error_category": null
  },
  "classification": {
    "declared_slot": "driving_license",
    "expected_type": "driving_license",
    "detected_type": "driving_license",
    "confidence": 1.0,
    "consistency": "MATCH",
    "decision": "MATCH",
    "manual_review_required": false,
    "reason_code": "KEYWORD_MATCH",
    "matched_keywords": ["رخصة السياقة"],
    "face_detected": null,
    "quality_gate_blocked": false
  },
  "consistency": "MATCH",
  "ocr": {
    "used": true,
    "available": true,
    "source": "easyocr",
    "reason": "OCR_USED"
  },
  "final_decision": "ACCEPT",
  "task_id": "8f14e45fceea167a5a36dedd4bea2543",
  "status": "completed"
}
```

The `quality.metrics` object above is abbreviated for readability. A real
document-profile response also includes `sharpness_tenengrad`, `brightness`,
`clipping`, `uneven_illumination`, `contrast`, `glare`, `document_boundary`,
`rotation`, and `perspective`.

If the task failed — `503 Service Unavailable`:

```json
{
  "task_id": "8f14e45fceea167a5a36dedd4bea2543",
  "status": "failed",
  "detail": "Verification service is unavailable.",
  "code": "verification_python_missing"
}
```

If the task ID is unknown or belongs to another user — `404 Not Found`:

```json
{
  "detail": "Unknown verification task.",
  "code": "unknown_task"
}
```

Remember that the task registry is in memory. Restarting Django makes old task IDs
unknown.

### 6.4 Decision semantics

`final_decision` is computed by `drivers/verification.py`:

| Quality | Classification | Final decision |
| --- | --- | --- |
| `NEEDS_RECAPTURE` | any | `REJECT` |
| `ACCEPTABLE_QUALITY` | `MATCH` | `ACCEPT` |
| everything else | any | `MANUAL_REVIEW` |

`POOR_QUALITY` is usable for classification but never produces an automatic
`ACCEPT`.

Quality statuses:

| Status | Meaning |
| --- | --- |
| `ACCEPTABLE_QUALITY` | Photo is technically suitable |
| `POOR_QUALITY` | Usable but weak; manual review is appropriate |
| `NEEDS_RECAPTURE` | Must be corrected or re-uploaded |

Classification decisions:

| Decision | Meaning |
| --- | --- |
| `MATCH` | Detected public title matches the declared slot |
| `NEEDS_REVIEW` | Ambiguous, low-confidence, missing, or mismatched evidence |
| `QUALITY_GATE_BLOCKED` | Quality gate returned `NEEDS_RECAPTURE`; classification was skipped |

`OCR` metadata:

| Field | Values |
| --- | --- |
| `used` | `true` / `false` |
| `available` | `true` / `false` |
| `source` | `easyocr`, `tesseract`, `provided`, `none` |
| `reason` | `OCR_USED`, `NOT_REQUIRED`, `NO_OCR`, `OCR_FAILED` |

### 6.5 Debug OCR text

Raw OCR text is never returned by default. It is exposed only when all three
conditions are true:

1. `DEBUG=True`
2. `VERIFICATION_AGENT_DEBUG_OCR=True`
3. the request uses `?debug=1`

Example:

```powershell
curl.exe -X POST "http://127.0.0.1:8000/api/verification/check/?debug=1" `
  -H "Authorization: Bearer <jwt-access-token>" `
  -F "slot=driving_license" `
  -F "file=@C:\path\to\specimen.jpg"
```

The completed response may then include `ocr_debug_text`. Never enable this in
production and never log real identity data.

---

## 7. Module structure

```text
orizzonte-backend-app/
├── manage.py
├── requirements.txt
├── .env.example
├── demenagement/
│   ├── settings.py                 Django settings, .env loading, verification settings
│   └── urls.py                     Mounts accounts, missions, drivers under /api/
├── accounts/                       JWT login, signup, email verification, roles
├── authentication/                 Token/logout helpers
├── missions/                       Mission management API
├── drivers/
│   ├── constants.py                DOCUMENT_TYPES and required profile fields
│   ├── documents.py                Image validation, EXIF stripping, private storage
│   ├── verification.py             Django-to-agent subprocess bridge, final decision
│   ├── verification_views.py       Async upload/status views and task registry
│   ├── urls.py                     /api/verification/... routes
│   ├── models.py                   DriverProfile, DriverReviewLog
│   ├── serializers.py              Driver, boss, agent, verification request shapes
│   └── tests/                      Backend tests, including async verification
└── verification_agent/
    ├── easyocr_server.py           Persistent FastAPI + EasyOCR server
    ├── easyocr_runner.py           Legacy one-shot EasyOCR runner
    ├── start_easyocr_server.bat    Windows launcher
    ├── requirements.txt            OpenCV, NumPy, Pillow
    ├── requirements-dev.txt        pytest
    ├── pytest.ini
    └── ocr_lab/
        └── image_quality/
            ├── analyzer.py          Quality-gate orchestration and output schema
            ├── backend_bridge.py    JSON bridge, EasyOCR HTTP client, Tesseract fallback
            ├── document_classification.py
            ├── document_layouts.json
            ├── decision_engine.py
            ├── image_loader.py
            ├── normalization.py
            ├── profiles.py
            ├── thresholds.json
            ├── output_schema.py
            ├── messages.json
            ├── cli.py               analyze / calibrate / evaluate commands
            ├── metrics/             Resolution, sharpness, exposure, contrast, glare, boundary
            ├── tools/               Calibration/evaluation helpers
            └── tests/               Module tests (synthetic images only)
```

Key files:

| File | What it does |
| --- | --- |
| `drivers/verification.py` | Launches `image_quality.backend_bridge` in the verification environment. Forwards only `EASYOCR_DIAG` stderr lines when `DEBUG` is on. |
| `drivers/verification_views.py` | `POST /api/verification/check/`, `GET /api/verification/status/<task_id>/`, in-memory task registry, `ThreadPoolExecutor(max_workers=4)`. |
| `verification_agent/ocr_lab/image_quality/backend_bridge.py` | Reads base64 image JSON, runs the quality gate, selects EasyOCR/Tesseract, classifies, and prints JSON. |
| `verification_agent/easyocr_server.py` | FastAPI `/health` and `/ocr`; one warm EasyOCR reader. |
| `verification_agent/easyocr_runner.py` | One-shot EasyOCR runner for legacy/manual use; not used by the persistent bridge. |
| `verification_agent/ocr_lab/image_quality/document_classification.py` | Normalization, keyword scoring, slot consistency, selfie face-presence check. |

---

## 8. Configuration files

### 8.1 `thresholds.json`

Location: `verification_agent/ocr_lab/image_quality/thresholds.json`

The file contains profiles (`id_card`, `driving_license`, `vehicle_registration`,
`selfie`) and layouts. A profile can have several layouts. The current bridge
passes only the profile, so `profiles.py` uses the default layout:
`id_card/default`, `driving_license/card`, `vehicle_registration/paper`, and
`selfie/default`. The `driving_license/paper` layout is available for CLI work but
is not selected by the API bridge today.

Current calibration markers:

| Profile / layout | Status |
| --- | --- |
| `id_card/default` | `CALIBRATED` |
| `driving_license/card` | `CALIBRATED` |
| `driving_license/paper` | `UNCALIBRATED` |
| `vehicle_registration/paper` | `UNCALIBRATED` |
| `selfie/default` | `UNCALIBRATED` |

The top-level `calibration_status` is informational; the per-layout marker
controls how a layout is treated. A `CALIBRATED` marker means the values were
adjusted from a documented local sample. It does not make the system a certified
identity-verification product.

Key parameters:

| Key | Meaning |
| --- | --- |
| `document_analysis` | Whether geometric document checks run. `false` for `selfie`. |
| `max_megapixels` | Decode limit; larger images are refused. |
| `max_working_side` | Longest side of the downscaled analysis copy. |
| `min_original_width`, `min_original_height`, `min_original_megapixels` | Minimum accepted original image size. |
| `min_document_width`, `min_document_height`, `min_document_megapixels` | Minimum detected document region. |
| `evaluation_region.document_inset_fraction` | Inset from the detected document boundary. |
| `evaluation_region.central_fraction` | Central crop used for quality metrics. |
| `blur.laplacian_variance_poor` | Laplacian variance below this value indicates poor sharpness. |
| `blur.laplacian_variance_severe` | Hard blur threshold. |
| `blur.tenengrad_poor` | Tenengrad gradient-energy threshold for poor sharpness. |
| `blur.tenengrad_severe` | Hard tenengrad threshold. |
| `exposure.dark_mean` | Mean luminance below this value indicates a dark image. |
| `exposure.overexposed_mean` | Mean luminance above this value indicates overexposure. |
| `exposure.dark_clip_level`, `exposure.bright_clip_level` | Pixel levels treated as clipped. |
| `exposure.dark_clip_fraction`, `exposure.bright_clip_fraction` | Maximum clipped-pixel fractions. |
| `exposure.uneven_illumination_ratio` | Bright/dark block ratio indicating uneven lighting. |
| `exposure.block_grid` | Grid size used for illumination analysis. |
| `contrast.p5_p95_min_range` | Minimum spread between the 5th and 95th percentiles. |
| `contrast.rms_min` | Minimum RMS contrast. |
| `contrast.p5_min` | Minimum 5th-percentile luminance. |
| `contrast.rms_secondary_min` | Secondary RMS guard used by the decision engine. |
| `glare.near_saturated_threshold` | Pixel level considered near-saturated. |
| `glare.low_saturation_threshold` | Low-saturation threshold for glare candidates. |
| `glare.max_suspected_fraction` | Maximum suspected-glare area fraction. |
| `glare.max_component_fraction` | Maximum single glare-component fraction. |
| `boundary.min_confidence` | Minimum confidence for accepting a document boundary. |
| `boundary.fallback_min_area_ratio` | Minimum area ratio for a fallback boundary. |
| `boundary.fallback_confidence` | Confidence assigned to the fallback boundary. |
| `boundary.min_candidate_area_ratio` | Minimum candidate contour area. |
| `boundary.aspect_ratio_range` | Accepted document aspect ratios; `null` disables the check. |
| `boundary.cropping_min_sides` | Number of cut sides required for a cropping decision. |
| `boundary.cropping_min_confidence` | Confidence required for a cropping decision. |
| `boundary.no_document_min_edge_density` | Edge density below which no document is assumed. |
| `boundary.min_solidity` | Minimum contour solidity. |
| `rotation.max_abs_angle_deg` | Maximum absolute rotation angle. |
| `rotation.max_perspective_ratio` | Maximum perspective distortion ratio. |

Values differ by profile. Do not copy `id_card` thresholds into
`vehicle_registration`; paper documents have different geometry and resolution.

### 8.2 `document_layouts.json`

Location: `verification_agent/ocr_lab/image_quality/document_layouts.json`

It defines the upload-slot mapping and public-title keywords.

Global classifier settings:

```json
{
  "min_confidence": 0.6,
  "min_confidence_margin": 0.2
}
```

Slots:

| Slot | Expected type | Mode |
| --- | --- | --- |
| `selfie` | `selfie` | `face` |
| `id_card_front` | `id_card` | `text` |
| `id_card_back` | `id_card` | `text` |
| `driving_license` | `driving_license` | `text` |
| `vehicle_registration` | `vehicle_registration` | `text` |

Keywords:

| Type | Specific public titles (weight) | Generic context (weight) |
| --- | --- | --- |
| `id_card` | `CARTE NATIONALE D'IDENTITE` (3.0), `بطاقة التعريف الوطنية` (5.0) | `REPUBLIQUE ALGERIENNE DEMOCRATIQUE ET POPULAIRE` (0.5), `الجمهورية الجزائرية الديمقراطية الشعبية` (0.5) |
| `driving_license` | `PERMIS DE CONDUIRE` (3.0), `رخصة السياقة` (5.0), `السياقة` (4.0), `رخصت السياقة` (3.0, OCR variant) | `REPUBLIQUE ALGERIENNE` (0.5), `الجمهورية الجزائرية` (0.5) |
| `vehicle_registration` | `CERTIFICAT D'IMMATRICULATION` (3.0), `CARTE GRISE` (2.0), `شهادة تسجيل` (5.0), `البطاقة الرمادية` (3.0) | none |
| `selfie` | No text keywords; use face presence | none |

Scoring rules:

- Text is accent-folded, punctuation-normalized, and uppercased.
- Latin and Arabic keywords are scored independently by script.
- A specific keyword is required to determine a document type.
- Generic republic phrases are context only. A generic-only match returns
  `detected_type: null`, `decision: "NEEDS_REVIEW"`, and
  `reason_code: "AMBIGUOUS_KEYWORDS"`.
- The winner must reach `min_confidence` and beat the second specific candidate by
  at least `min_confidence_margin`; otherwise the result is `NEEDS_REVIEW`.
- A detected type that differs from the slot's expected type is
  `consistency: "MISMATCH"` and `decision: "NEEDS_REVIEW"`, never an automatic
  rejection.
- Selfie classification uses an optional OpenCV Haar face detector. It is a
  presence check, not biometrics.

### 8.3 Other configuration

| File | Purpose |
| --- | --- |
| `.env` | Local secrets and runtime paths; ignored by Git |
| `.env.example` | Template for local configuration |
| `requirements.txt` | Django/DRF dependencies |
| `verification_agent/requirements.txt` | OpenCV, NumPy, Pillow |
| `verification_agent/requirements-dev.txt` | pytest |
| `verification_agent/pytest.ini` | Module test discovery |
| `verification_agent/ocr_lab/image_quality/messages.json` | CLI/error message text |

---

## 9. Testing

All automated tests use synthetic `SPECIMEN / TEST` images or mocked OCR
responses. Do not add real names, document numbers, photos, or OCR text to the
test suite.

### 9.1 Verification module tests

From the project root:

```powershell
Set-Location .\verification_agent
$env:PYTHONPATH = (Resolve-Path .\ocr_lab).Path
.\.venv\Scripts\python.exe -m pytest -q
```

Expected current result:

```text
201 passed
```

You can also run from `verification_agent/` without setting `PYTHONPATH` if the
`pytest.ini` test paths are used; setting `PYTHONPATH` explicitly is the safest
option when invoking from other directories.

### 9.2 Django backend tests

From the project root:

```powershell
.\venv\Scripts\python.exe manage.py test --settings=demenagement.settings
```

Expected current result:

```text
Ran 164 tests
OK (skipped=1)
```

If the virtual environment is already activated:

```powershell
python manage.py test --settings=demenagement.settings
```

### 9.3 Targeted test commands

```powershell
# Only the EasyOCR bridge/fallback tests
Set-Location .\verification_agent
.\.venv\Scripts\python.exe -m pytest ocr_lab/image_quality/tests/test_easyocr_bridge.py -q

# Only the async verification API tests
Set-Location ..
.\venv\Scripts\python.exe manage.py test drivers.tests.test_verification --settings=demenagement.settings
```

### 9.4 Manual smoke test

Use a synthetic specimen, never a real identity document:

```powershell
curl.exe -X POST "http://127.0.0.1:8000/api/verification/check/" `
  -H "Authorization: Bearer <jwt-access-token>" `
  -F "slot=driving_license" `
  -F "file=@C:\path\to\synthetic-specimen.jpg"
```

Poll the returned `task_id` until it returns `200`. Expected useful fields:

```text
quality_status: ACCEPTABLE_QUALITY
detected_type:  driving_license
decision:       MATCH
reason_code:    KEYWORD_MATCH
final_decision: ACCEPT
ocr.source:     easyocr
```

A green test suite does not prove production accuracy. Calibration requires real,
consented specimens, an agreed label set, and a documented review.

---

## 10. Troubleshooting

### Django will not start: `drivers.E001` / `PRIVATE_MEDIA_ROOT`

**Cause:** `PRIVATE_MEDIA_ROOT` is empty or invalid.

**Fix:**

```dotenv
PRIVATE_MEDIA_ROOT=C:/orizzonte/private_media
```

Create the directory, ensure it is writable, and run:

```powershell
python manage.py check
```

The directory must be absolute and outside `STATIC_ROOT`/`STATICFILES_DIRS`.

### `ModuleNotFoundError: No module named 'corsheaders'`

**Cause:** `demenagement/settings.py` includes `corsheaders`, but the current
`requirements.txt` does not pin it.

**Fix:**

```powershell
.\venv\Scripts\python.exe -m pip install "django-cors-headers==4.9.0"
```

### `verification_python_missing`

**Cause:** Django cannot find the verification agent interpreter.

**Fix:** create `verification_agent/.venv`, or set an explicit path:

```dotenv
VERIFICATION_AGENT_PYTHON=C:/path/to/verification_agent/.venv/Scripts/python.exe
```

Run `python manage.py check` and restart Django.

### `verification_bridge_missing`

**Cause:** `verification_agent/ocr_lab/image_quality/backend_bridge.py` is missing
or the project was moved without its module files.

**Fix:** restore the `verification_agent/ocr_lab` tree from the repository and
retry.

### EasyOCR server is unavailable

Symptoms:

```text
EASYOCR_DIAG server_unavailable url=http://127.0.0.1:8001
```

Check:

```powershell
Invoke-RestMethod http://127.0.0.1:8001/health
```

If it fails:

1. Start `verification_agent\start_easyocr_server.bat`.
2. Confirm the batch file points to the real EasyOCR interpreter.
3. Check that port `8001` is free.
4. Confirm `EASYOCR_SERVER_URL` matches the running server.

If another service uses port `8001`, run Uvicorn on another port and update
`EASYOCR_SERVER_URL`:

```powershell
C:\orizzonte_easyocr_env\Scripts\python.exe -m uvicorn easyocr_server:app --host 127.0.0.1 --port 9001
```

```dotenv
EASYOCR_SERVER_URL=http://127.0.0.1:9001
```

### EasyOCR fails to import or PyTorch errors

**Cause:** EasyOCR/PyTorch were installed into the Django or verification agent
environment, or the EasyOCR environment uses an unsupported Python version.

**Fix:** use the isolated Python 3.12 environment:

```powershell
C:\orizzonte_easyocr_env\Scripts\python.exe -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
C:\orizzonte_easyocr_env\Scripts\python.exe -m pip install easyocr==1.7.2 fastapi uvicorn
```

Do not install `torch`, `torchvision`, or `easyocr` into `venv` or
`verification_agent/.venv`.

### First EasyOCR request is very slow or times out

**Cause:** the model is being downloaded or loaded for the first time, or the
machine is CPU-bound.

**Fix:**

1. Start the EasyOCR server and wait for startup to complete.
2. Run one warm-up request.
3. Increase `VERIFICATION_AGENT_TIMEOUT_SECONDS` if needed.
4. Check that `EASYOCR_SERVER_URL` points to a warm server, not a per-request
   subprocess.

### Tesseract not found

Symptoms:

```text
ocr.available: false
ocr.reason:    NO_OCR
```

**Fix:**

```powershell
tesseract --version
tesseract --list-langs
```

Set one of:

```dotenv
TESSERACT_CMD=C:\Program Files\Tesseract-OCR\tesseract.exe
TESSDATA_PREFIX=C:\Program Files\Tesseract-OCR\tessdata
```

The bridge also auto-detects `%LOCALAPPDATA%\Programs\Tesseract-OCR` and the
standard `C:\Program Files\Tesseract-OCR` locations.

### `fra` or `ara` is missing from Tesseract

**Cause:** the traineddata files are not in the tessdata directory.

**Fix:** download both files into
`C:\Program Files\Tesseract-OCR\tessdata` and verify:

```powershell
tesseract --list-langs
```

`TESSERACT_LANGUAGE` defaults to `fra+ara`.

### `NEEDS_REVIEW` with `AMBIGUOUS_KEYWORDS`

This is expected when EasyOCR returns text but only generic republic wording is
present, or when two document types are too close in confidence. The pipeline
deliberately does not fall back to Tesseract after EasyOCR returns text.

**What to check:**

- Use `?debug=1` with `DEBUG=True` and `VERIFICATION_AGENT_DEBUG_OCR=True` to
  inspect the OCR text locally, but never log real identity data.
- Add missing public titles to `document_layouts.json` if they are legitimate
  document titles.
- Do not lower `min_confidence` blindly; add tests for the new keyword.

### `OCR_FAILED` or `NO_OCR`

- `OCR_FAILED`: an OCR engine was available but failed.
- `NO_OCR`: neither EasyOCR nor Tesseract was available, or both returned no text.

Check the EasyOCR health endpoint, Tesseract installation, and the
`TESSERACT_LANGUAGE` setting.

### Status endpoint returns `404 unknown_task`

**Cause:** the task ID is unknown, was created by another user, or Django was
restarted. The task registry is in-memory and process-local.

**Fix:** submit the document again. For production, replace the in-memory store
with a persistent task backend.

### Status endpoint returns `503` after a long wait

**Cause:** the bridge subprocess exceeded `VERIFICATION_AGENT_TIMEOUT_SECONDS`, or
the verification process failed.

**Fix:**

1. Confirm the EasyOCR server is reachable.
2. Confirm Tesseract is installed if EasyOCR is down.
3. Increase `VERIFICATION_AGENT_TIMEOUT_SECONDS` (for example `300`).
4. Inspect the Django console for `EASYOCR_DIAG` lines when `DEBUG=True`.
5. Run the module tests to separate Django wiring from OCR/environment issues.

### `413 Request Entity Too Large`

**Cause:** the upload exceeds `MAX_UPLOAD_MB` or Django's body limits.

**Fix:** reduce the image size or increase `MAX_UPLOAD_MB` and the related
`DATA_UPLOAD_MAX_MEMORY_SIZE` setting. Keep identity uploads as small as practical.

### Phone cannot reach the backend

**Fix:**

```powershell
python manage.py runserver 0.0.0.0:8000
```

Set `ALLOWED_HOSTS=*` (development) and `SITE_URL=http://<PC-LAN-IP>:8000`.
Allow port `8000` in Windows Firewall. Do not use `127.0.0.1` from the phone.

### `EASYOCR_DIAG` lines are missing

Diagnostics are written to stderr by the bridge and forwarded by Django only when
`DEBUG=True`. They contain HTTP status, byte counts, JSON keys, text length, and
exception type/message. They never contain OCR text. Enable `DEBUG` temporarily if
you need to diagnose the bridge.

---

## 11. Contributing

### Add a new document type

1. Add the upload slot to `DOCUMENT_TYPES` in `drivers/constants.py`.
2. Add the corresponding model field and migration if the document is stored on
   the driver profile.
3. Expose the field in the relevant serializer and upload view.
4. Add a slot entry in `verification_agent/ocr_lab/image_quality/document_layouts.json`:

```json
{
  "new_document": {
    "expected_type": "new_document",
    "mode": "text"
  }
}
```

5. Add a `types.new_document` keyword list with public titles.
6. Add a profile/layout in `thresholds.json` if the document has different
   geometry or resolution requirements. If you add a new profile, also register
   it in `PROFILE_LAYOUTS` and `DEFAULT_LAYOUTS` in `profiles.py`.
7. Add synthetic tests for:
   - a matching title;
   - a wrong-slot title;
   - generic-only text;
   - low confidence;
   - unavailable OCR.
8. Update the API documentation and this README.

### Update keywords

Edit `verification_agent/ocr_lab/image_quality/document_layouts.json`.

Rules:

- `text` must be a public document title, never a person's name or number.
- `weight` must be greater than zero.
- Use `"specific": true` for titles that identify a document type.
- Use `"specific": false` only for context such as the republic header.
- A generic phrase alone must never select a type.
- Latin and Arabic variants are scored independently.
- Keep `min_confidence` and `min_confidence_margin` conservative.
- Add regression tests for every new keyword or weight change.

After editing, run both test suites.

### Adjust thresholds

Edit `verification_agent/ocr_lab/image_quality/thresholds.json` per profile and
layout. Prefer the calibration workflow for evidence:

```powershell
Set-Location .\verification_agent\ocr_lab
..\.venv\Scripts\python.exe -m image_quality calibrate C:\path\to\consented-samples --profile id_card
```

`calibrate --apply` only marks a layout `PROVISIONAL`. A human must review the
evidence and explicitly set `CALIBRATED`; do not treat an automated write as
production approval.

Calibration requirements:

- use real, consented specimens only;
- keep labels and groups in a local CSV, never in Git if they contain identity
  data;
- group multiple photos of the same person/document into one fold;
- review false-accept and false-reject rates, not only accuracy;
- run the module and backend tests after changes;
- document the sample count and analysis date.

### Development checklist

```text
[ ] No real identity data in code, tests, fixtures, logs, or screenshots
[ ] No secrets in Git
[ ] Module tests pass: 201 passed
[ ] Backend tests pass: 164 tests, OK (skipped=1)
[ ] EasyOCR and PyTorch remain isolated in C:\orizzonte_easyocr_env
[ ] README and .env.example updated for new variables
[ ] Changes reviewed before commit
```

Do not commit or push changes until they have been reviewed and verified.

---

## 12. License / credits

### License

No `LICENSE` file is currently included in this repository. Unless a separate
license is added by the project owner, treat the code as internal/proprietary to
the ORIZZONTE project. Contact the owner before redistributing it.

### Credits and upstream projects

This project builds on:

- **Django** and **Django REST Framework** — web API and ORM.
- **SimpleJWT** — JWT authentication.
- **OpenCV** — image loading, normalization, blur/contrast/exposure/glare and
  boundary analysis.
- **NumPy** — numerical image operations.
- **Pillow** — image validation and re-encoding.
- **Tesseract OCR** — French/Arabic fallback OCR (`fra+ara`).
- **EasyOCR** — primary Arabic/English OCR for ID cards and driving licences.
- **PyTorch** — EasyOCR runtime (CPU mode).
- **FastAPI** and **Uvicorn** — persistent EasyOCR HTTP server.

Refer to each upstream project for its license terms before distributing a
combined product.

---

**ORIZZONTE** — offline-first document quality and classification for Algerian
driver onboarding.
