# ORIZZONTE image-quality module (Phase 2)

## Status

This is a standalone, offline **image-quality gate**. It decides whether a photo is
technically suitable for a later OCR stage. It does not perform OCR, face detection,
biometrics, identity matching, or fraud detection.

The module is **not production-ready**:

- every threshold is marked `UNCALIBRATED`;
- the automated tests use only synthetic `SPECIMEN / TEST` images;
- no real document or selfie has been used to validate the defaults;
- synthetic pass results do not prove reliability on real phone photos.

Do not mark the thresholds as calibrated, and do not treat a green test run as evidence
of production accuracy. Calibration requires real, consented specimens and an agreed
label set.

## Profiles and layouts

| Profile | Layouts | Document geometry | Notes |
| --- | --- | --- | --- |
| `id_card` | `default` | yes | Card-shaped ID document |
| `driving_license` | `card`, `paper` | yes | Default layout is `card`; `paper` is the older paper format |
| `vehicle_registration` | `paper` | yes | Paper document |
| `selfie` | `default` | no | Quality metrics only; no face detection, no biometrics |

The actual backend upload slots are `selfie`, `id_card_front`, `id_card_back`,
`driving_license`, and `vehicle_registration`. The module profiles are independent of
those slot names and do not modify the Django application.

## Install and test (from `verification_agent/`)

```powershell
Set-Location "C:\Users\THINKPAD\Desktop\orizzonte-backend-app\verification_agent"
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --dry-run --only-binary=:all: -r requirements.txt -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest
```

The runtime pins are:

```text
opencv-python-headless==5.0.0.93
numpy==2.5.4
Pillow==12.3.0
```

The development pin is `pytest==9.2.0`. Installation needs network access; **analysis at
runtime does not**.

## CLI

Run the commands from `verification_agent/ocr_lab` so that `image_quality` is importable:

```powershell
Set-Location "C:\Users\THINKPAD\Desktop\orizzonte-backend-app\verification_agent\ocr_lab"
..\.venv\Scripts\python.exe -m image_quality analyze C:\path\to\image.jpg --profile id_card
..\.venv\Scripts\python.exe -m image_quality calibrate C:\path\to\folder --profile id_card
..\.venv\Scripts\python.exe -m image_quality evaluate C:\path\to\folder --labels C:\path\to\labels.csv --profile id_card
```

Alternatively, from `verification_agent/`:

```powershell
$env:PYTHONPATH = (Resolve-Path .\ocr_lab).Path
.\.venv\Scripts\python.exe -m image_quality analyze C:\path\to\image.jpg --profile id_card
```

`analyze` prints JSON to stdout and nothing else. It never prints the input path, file
name, image content, or any identity data. Errors are structured JSON; the CLI does not
print tracebacks. Exit codes:

| Code | Meaning |
| --- | --- |
| `0` | Analysis completed, regardless of `ACCEPTABLE_QUALITY`, `POOR_QUALITY`, or `NEEDS_RECAPTURE` |
| `2` | Invalid profile/layout or invalid command configuration |
| `3` | Unreadable, unsupported, empty, or over-limit image (`UNREADABLE_IMAGE`) |
| `4` | Unexpected internal error; no traceback and no path are printed |

`calibrate` reads every file in a folder and prints aggregate per-metric distributions
(count, min, median, max). It does **not** write or modify `thresholds.json`. It reports
`"thresholds_calibrated": false` and `"thresholds_modified": false`.

`evaluate` reads a local labels CSV with this exact shape:

```csv
filename,label
good.jpg,ACCEPTABLE_QUALITY
blurry.jpg,NEEDS_RECAPTURE
```

`filename` must be a plain name inside the evaluated folder; path traversal is rejected.
`label` must be one of the three statuses. `evaluate` prints a confusion matrix,
false-acceptance count/rate, false-rejection count/rate, and an
`acceptable_but_poor` count. An unreadable image is counted as `NEEDS_RECAPTURE`.

## Output contract

Every successful analysis is versioned JSON with these top-level fields:

```text
schema_version, algorithm_version, profile, layout, status, confidence,
thresholds_calibrated, processing_time_ms, image, metrics, reason_codes,
decision_trace, error_category
```

`status` is one of:

- `ACCEPTABLE_QUALITY`
- `POOR_QUALITY`
- `NEEDS_RECAPTURE`

Each metric has this shape:

```json
{
  "value": 0.0,
  "nature": "measured | heuristic | unknown",
  "confidence": 0.0,
  "metadata": {}
}
```

`nature` is part of the contract:

- `measured`: a direct pixel measurement, or a measurement derived from a detected region;
- `heuristic`: an estimate that can be wrong;
- `unknown`: not enough reliable information; it never rejects an image on its own.

`processing_time_ms` is the measured in-process analysis time in milliseconds with three decimal places.
`thresholds_calibrated` is `true` only when the selected layout's `calibration_status` is
`CALIBRATED` in `thresholds.json`.

Reason codes are stable strings:

`LOW_RES`, `TOO_SMALL`, `BLURRY`, `DARK`, `OVEREXPOSED`, `UNEVEN_LIGHT`,
`LOW_CONTRAST`, `GLARE`, `CROPPED`, `ROTATED`, `PERSPECTIVE`, `NO_DOCUMENT_FOUND`,
`UNREADABLE_IMAGE`.

Human-readable messages live only in `messages.json`. Every code has `en` and `fr`
filled; `ar` is intentionally empty in this phase. No message contains a path, personal
data, or an identity claim.

## Metrics and defaults

The values below are the actual defaults in `thresholds.json`. They are provisional and
**UNCALIBRATED**. `D` = `id_card/default`; `LC` = `driving_license/card`;
`LP/VP` = `driving_license/paper` and `vehicle_registration/paper`;
`S` = `selfie/default`.

### Global profile defaults

| Setting | D | LC | LP/VP | S |
| --- | ---: | ---: | ---: | ---: |
| `max_megapixels` | 40.0 | 40.0 | 40.0 | 40.0 |
| `max_working_side` | 1600 | 1600 | 1600 | 1600 |
| `min_original_width` | 640 | 800 | 1000 | 640 |
| `min_original_height` | 480 | 500 | 700 | 480 |
| `min_original_megapixels` | 0.30 | 0.40 | 0.70 | 0.30 |
| `min_document_width` | 600 | 700 | 800 | n/a |
| `min_document_height` | 400 | 450 | 600 | n/a |
| `min_document_megapixels` | 0.24 | 0.315 | 0.48 | n/a |
| `evaluation_region.document_inset_fraction` | 0.03 | 0.03 | 0.03 | n/a |
| `evaluation_region.central_fraction` | 0.8 | 0.8 | 0.8 | 0.7 |
| `blur.laplacian_variance_poor` | 120 | 120 | 120 | 100 |
| `blur.laplacian_variance_severe` | 10 | 10 | 10 | 10 |
| `blur.tenengrad_poor` | 2500 | 2500 | 2500 | 2500 |
| `blur.tenengrad_severe` | 1200 | 1200 | 1200 | 1200 |
| `exposure.dark_mean` | 60 | 60 | 60 | 60 |
| `exposure.overexposed_mean` | 215 | 215 | 215 | 215 |
| `exposure.dark_clip_level` | 10 | 10 | 10 | 10 |
| `exposure.bright_clip_level` | 245 | 245 | 245 | 245 |
| `exposure.dark_clip_fraction` | 0.30 | 0.30 | 0.30 | 0.30 |
| `exposure.bright_clip_fraction` | 0.35 | 0.35 | 0.35 | 0.35 |
| `exposure.uneven_illumination_ratio` | 0.50 | 0.50 | 0.50 | 0.50 |
| `exposure.block_grid` | 8 | 8 | 8 | 8 |
| `contrast.p5_p95_min_range` | 90 | 90 | 90 | 80 |
| `contrast.rms_min` | 30 | 30 | 30 | 28 |

### Document-only defaults

| Setting | D | LC | LP/VP |
| --- | ---: | ---: | ---: |
| `glare.near_saturated_threshold` | 250 | 250 | 250 |
| `glare.low_saturation_threshold` | 40 | 40 | 40 |
| `glare.component_min_area_px` | 80 | 80 | 80 |
| `glare.max_suspected_fraction` | 0.015 | 0.015 | 0.015 |
| `glare.max_component_fraction` | 0.008 | 0.008 | 0.008 |
| `glare.max_background_fraction` | 0.25 | 0.25 | 0.25 |
| `glare.max_near_saturated_fraction` | 0.30 | 0.30 | 0.30 |
| `boundary.min_confidence` | 0.65 | 0.65 | 0.65 |
| `boundary.min_analysis_side_px` | 64 | 64 | 64 |
| `boundary.fallback_min_area_ratio` | 0.25 | 0.25 | 0.25 |
| `boundary.fallback_confidence` | 0.72 | 0.72 | 0.72 |
| `boundary.min_candidate_area_ratio` | 0.04 | 0.04 | 0.04 |
| `boundary.border_tolerance_fraction` | 0.012 | 0.012 | 0.012 |
| `boundary.cropping_min_sides` | 2 | 2 | 2 |
| `boundary.cropping_min_confidence` | 0.80 | 0.80 | 0.80 |
| `boundary.no_document_min_edge_density` | 0.004 | 0.004 | 0.004 |
| `boundary.min_solidity` | 0.85 | 0.85 | 0.85 |
| `boundary.canny_low` / `canny_high` | 50 / 150 | 50 / 150 | 50 / 150 |
| `boundary.approx_epsilon_fraction` | 0.02 | 0.02 | 0.02 |
| `boundary.dilate_iterations` | 2 | 2 | 2 |
| `boundary.aspect_ratio_range` | `[1.35, 1.85]` | `[1.35, 1.85]` | `null` |
| `rotation.max_abs_angle_deg` | 5.0 | 5.0 | 5.0 |
| `rotation.max_perspective_ratio` | 0.08 | 0.08 | 0.08 |

### What each metric means

#### `resolution_original`

Reports the decoded original width, height, megapixels, oriented dimensions, and working
copy dimensions. It is `measured`, confidence `1.0`. The original file is never
upscaled; `normalize_image` only downscales so that blur cannot be hidden by resampling.
If width, height, or megapixels is below the profile minimum, the result adds
`TOO_SMALL` as a **critical** problem.

#### `resolution_document`

Reports the detected document bounding-box width and height in oriented pixel units,
plus megapixels. It is `measured` when a boundary is valid and `unknown` otherwise.
Perspective and cropping can make the bounding box a conservative estimate. If any of
the document minimums is not met, the result adds `LOW_RES` as a **critical** problem.

#### `sharpness_laplacian`

Variance of the Laplacian on the evaluation region. It is `measured` with confidence
`0.95`. This is the primary focus indicator. A value at or below
`laplacian_variance_poor` is a weak blur signal; a value at or below
`laplacian_variance_severe` is a severe signal.

#### `sharpness_tenengrad`

Mean squared Sobel gradient energy: `mean(Gx² + Gy²)`. It is `measured` with confidence
`0.90`. It is the independent second focus indicator. A value at or below
`tenengrad_poor` is a weak blur signal; at or below `tenengrad_severe` is severe.

`BLURRY` is only `critical` when **both** indicators are severe. One poor indicator, or
one severe indicator without the other, is only a moderate problem. Low contrast can
depress both indicators, so blur output must be read together with `contrast`.

#### `brightness`

Mean gray level of the evaluation region, scale 0-255. It is `measured`, confidence
`0.95`. Below `dark_mean` adds `DARK` (moderate). Very high brightness combined with high
bright clipping and low contrast adds `OVEREXPOSED` (moderate).

#### `clipping`

Reports `dark_fraction` (pixels at or below `dark_clip_level`) and `bright_fraction`
(pixels at or above `bright_clip_level`). It is `measured`, confidence `0.95`. It supports
`DARK` and `OVEREXPOSED`; it does not reject by itself. Black text can create a large dark
fraction on a well-lit document, and white paper can create a large bright fraction.

#### `uneven_illumination`

Divides the evaluation region into an `8 × 8` grid and reports
`(max_block_mean - min_block_mean) / 255`. It is `heuristic`, confidence `0.65`. Above
the configured ratio it adds `UNEVEN_LIGHT` (moderate). Text layout and dark graphics can
mimic uneven lighting.

#### `contrast`

Reports the robust `p5_p95_range` and `rms_contrast` of the gray evaluation region. It is
`measured`, confidence `0.90`. If either the percentile range or RMS is below its
minimum, it adds `LOW_CONTRAST` (moderate). It also supports the `OVEREXPOSED` rule.

#### `glare`

Converts the region to HSV and marks pixels that are near-saturated
(`V >= near_saturated_threshold`) and low-saturation (`S <= low_saturation_threshold`).
It measures connected components, ignores very large components that are likely the
document background, and reports `suspected_fraction`, `largest_component_fraction`,
`component_count`, `near_saturated_fraction`, and `low_saturation_fraction`. It is
`heuristic`.

`GLARE` is moderate when both the total suspected fraction and the largest component
fraction exceed their limits. The rule is suppressed when the image is already
overexposed and low-contrast, or when most of the region is near-saturated (for example,
white paper). This reduces false positives but can also hide a real reflection on a fully
white document. Plastic sleeves, holograms, printed highlights, and white paper remain
known confounders.

#### `document_boundary`

A heuristic geometric detector. It uses Canny edges, dilation, external contours,
convexity, solidity, and `approxPolyDP` to propose a quadrilateral. A second Otsu
bright-region fallback handles blurred cards and cards that touch the image border. A
rectangle is **not** proof of a document.

Reported fields include `valid`, `area_ratio`, `bounding_box_working`, `quad_working`,
`border_sides_touching`, `cropping_suspected`, `cropping_confirmed`, `aspect_ratio`,
`aspect_ratio_in_range`, `edge_density`, `frame_border_ambiguous`,
`no_document_signals`, `candidate_count`, and `solidity`.

- Validity requires the candidate confidence to meet `min_confidence`.
- Cropping is suspected when the quadrilateral touches at least `cropping_min_sides`
  image borders. It is confirmed only for a valid boundary with confidence at least
  `cropping_min_confidence`, and then adds `CROPPED` as **critical**.
- `NO_DOCUMENT_FOUND` is **critical** only when at least two independent no-document
  signals are present: no reliable quadrilateral, an area below
  `min_candidate_area_ratio`, an edge density below `no_document_min_edge_density`, or
  no candidate contours at all.
- `min_analysis_side_px` prevents boundary conclusions on very small working images.
- `aspect_ratio_range` is checked only for card layouts. Paper layouts report
  `aspect_ratio_in_range: null` because paper sizes vary.

The document region is perspective-warped in memory to a rectangle before sharpness,
exposure, contrast, and glare metrics. The warp is for metric computation only; no image
is saved or returned.

#### `rotation`

Estimates the in-plane angle from the longer quadrilateral edge, normalized to
`[-45, 45]`. It is `heuristic`. An absolute angle above `max_abs_angle_deg` adds
`ROTATED` (moderate). The metric deliberately reports
`orientation_90_180: "unknown"`; this phase does not guess 90-degree or 180-degree
orientation.

#### `perspective`

Reports the maximum imbalance between opposite sides:
`max(|top-bottom|/max(top,bottom), |left-right|/max(left,right))`. It is `heuristic`.
A value above `max_perspective_ratio` adds `PERSPECTIVE` (moderate). This is a simple
skew indicator, not a full camera calibration.

## Decision logic

The decision engine is rule-based:

| Condition | Severity | Status contribution |
| --- | --- | --- |
| Original resolution below profile minimum | critical | `TOO_SMALL` |
| Document resolution below profile minimum | critical | `LOW_RES` |
| Both sharpness indicators at severe thresholds | critical | `BLURRY` |
| Cropping confirmed | critical | `CROPPED` |
| At least two no-document signals | critical | `NO_DOCUMENT_FOUND` |
| One poor or one severe sharpness indicator | moderate | `BLURRY` |
| Mean brightness below dark threshold, or dark clipping with mean below 100 | moderate | `DARK` |
| High mean brightness, high bright clipping, and low contrast | moderate | `OVEREXPOSED` |
| Uneven block illumination above threshold | moderate | `UNEVEN_LIGHT` |
| Percentile range or RMS contrast below minimum | moderate | `LOW_CONTRAST` |
| Glare fractions above limits and not suppressed | moderate | `GLARE` |
| Absolute in-plane angle above threshold | moderate | `ROTATED` |
| Perspective imbalance above threshold | moderate | `PERSPECTIVE` |

- Any critical reason makes the status `NEEDS_RECAPTURE`.
- Otherwise, any moderate reason makes it `POOR_QUALITY`.
- No reasons makes it `ACCEPTABLE_QUALITY`.
- `unknown` metrics never reject by themselves.
- `confidence = min(0.99, 0.5 + 0.5 × known_applicable_metrics / applicable_metrics)`.
  It is a coverage indicator, not a probability that the decision is correct. Structured
  errors use confidence `0.0`.

Reason codes are sorted by priority:
`UNREADABLE_IMAGE`, `TOO_SMALL`, `LOW_RES`, `CROPPED`, `NO_DOCUMENT_FOUND`, `BLURRY`,
`DARK`, `OVEREXPOSED`, `UNEVEN_LIGHT`, `LOW_CONTRAST`, `GLARE`, `PERSPECTIVE`, `ROTATED`.

## Calibration

`calibrate` only reports distributions. It never edits thresholds and always reports
`thresholds_calibrated: false` and `thresholds_modified: false`.

To mark a layout calibrated, the owner must deliberately edit that layout's
`calibration_status` in `thresholds.json` from `UNCALIBRATED` to `CALIBRATED`. The
top-level `calibration_status` and `notes` are informational. A synthetic test run is not
a calibration event and must not be used to change the status.

Recommended calibration process:

1. Collect real, consented specimens with clear ownership and usage permission.
2. Label each with the agreed status and reason codes.
3. Run `calibrate` and inspect the distributions.
4. Run `evaluate` against the labels and review false acceptances and false rejections.
5. Have a human owner review and document every threshold change.
6. Only then mark the layout `CALIBRATED`.
7. Re-run `evaluate` and keep the report with the threshold version.

Never copy, display, print, upload, or commit real specimens. Report aggregate metrics
only.

## Privacy and offline behavior

- Images are decoded and processed entirely in memory.
- The input file is never modified.
- No debug images, resized copies, crops, or intermediate files are written.
- Output contains no file path, file name, image content, base64, OCR text, face data, or
  identity data.
- There is no OCR, no face detection, no biometrics, no identity comparison, and no fraud
  decision.
- There is no network access during analysis. The test suite blocks `socket.socket` and
  confirms analysis still succeeds.
- The CLI prints JSON only to stdout. Structured errors do not include tracebacks or
  paths.

## Architecture and files

```text
verification_agent/
  requirements.txt
  requirements-dev.txt
  pytest.ini
  README.md                         # short setup pointer
  ocr_lab/image_quality/
    __init__.py                     # public analyze_bytes / analyze_path
    __main__.py                     # python -m image_quality
    analyzer.py                     # orchestration and structured errors
    cli.py                          # analyze / calibrate / evaluate
    decision_engine.py              # rule-based decision and trace
    errors.py                       # structured error categories
    image_loader.py                 # format detection, EXIF, in-memory decode
    normalization.py                # bounded working copy, no upscaling
    output_schema.py                # versions, statuses, reason codes, validation
    profiles.py                     # profile/layout and threshold loading
    thresholds.json                 # every threshold, all UNCALIBRATED
    messages.json                   # en/fr messages; ar empty
    metrics/
      __init__.py
      boundaries.py                 # boundary, cropping, rotation, perspective
      contrast.py                   # percentile range and RMS
      exposure.py                   # brightness, clipping, uneven illumination
      glare.py                      # near-saturated reflection components
      region.py                     # document warp or central crop
      resolution.py                 # original and document resolution
      sharpness.py                  # Laplacian variance and Tenengrad
    tests/
      conftest.py
      synthetic.py                  # marked SPECIMEN / TEST generators
      test_*.py
      benchmark_12mp.py
```

Data flow:

```text
bytes/path
  -> image_loader (format, EXIF, RGB array)
  -> normalization (max side 1600, no upscaling)
  -> boundary detection (document profiles only)
  -> evaluation region (warped document or central crop)
  -> metrics (resolution, sharpness, exposure, contrast, glare, geometry)
  -> decision engine (critical / moderate / acceptable)
  -> schema validation
  -> versioned JSON
```

## Tests

Run from `verification_agent/`:

```powershell
.\.venv\Scripts\python.exe -m pytest
```

The suite uses pytest `tmp_path` for any file-based test, generates synthetic images at
test time, and never commits generated images. The synthetic generators are marked
`SPECIMEN / TEST` and contain fictional layouts only. They do not replicate real Algerian
documents and contain no personal data. The no-document generator uses a fixed seed.

Coverage includes: good document, blurry, low resolution, dark, overexposed, uneven
light, low contrast, glare, cropped, rotated, no document, tiny image, EXIF orientation,
WebP saved with a `.jpg` name, grayscale, corrupt, empty, over-megapixel, selfie quality
only, structured errors, schema validation, CLI JSON/path privacy, calibrate/evaluate,
false-acceptance/false-rejection math, file immutability, no debug files, and blocked
network sockets.

A 12-megapixel timing benchmark is available from `verification_agent/ocr_lab`:

```powershell
..\.venv\Scripts\python.exe -m image_quality.tests.benchmark_12mp
```

It generates a synthetic 4000 × 3000 JPEG in memory, warms up once, then times repeated
`analyze_bytes` calls. Generation and JPEG encoding are excluded. On the Phase 2
development machine the median was about 0.23 seconds for the `id_card` profile. This is
a synthetic local measurement, not a production latency guarantee.

## Limitations and non-goals

- Defaults are uncalibrated and were tuned only against synthetic test images.
- No real phone photo, real ID, real licence, or real registration has been validated.
- Boundary detection is heuristic; rounded corners, plastic sleeves, holograms, UI
  overlays, white backgrounds, and textured backgrounds can cause false positives or
  false negatives.
- A detected rectangle is not treated as proof of a document by itself; still, a small
  bright rectangle in a photo of another object can be misread.
- Glare detection can confuse white paper, plastic, holograms, and printed highlights.
  The near-saturated suppression can hide a reflection on a fully white document.
- Blur indicators are contrast-sensitive; dark or low-contrast images can look blurry.
- 90-degree and 180-degree orientation are reported as unknown.
- Perspective is a simple side-length imbalance, not a full lens/pose model.
- The selfie profile measures quality only. It performs no face detection, no face
  comparison, no liveness check, and no biometric analysis.
- The module does not decide identity, authenticity, fraud, or document validity.
- The module is not wired into Django, models, serializers, endpoints, settings, auth,
  database, `.env`, frontend, or backend `requirements.txt`.
- Phase 2 stops here. OCR and Phase 3 work must not be started without the exact next
  instruction.
