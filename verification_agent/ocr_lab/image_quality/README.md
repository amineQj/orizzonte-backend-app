# ORIZZONTE image-quality and document-classification module (Phases 2–3)

## Status

This is a standalone, offline **image-quality gate** and **upload-slot consistency
classifier**. The gate decides whether a photo is technically suitable for a later OCR
stage. The classifier then checks whether OCR text is consistent with the declared slot.
The core package does not bundle an OCR engine; the optional `backend_bridge.py` used by
Django can invoke a local Tesseract installation. It does not perform biometrics, identity
matching, or fraud detection.

The module is **not production-ready**:

- threshold layouts start as `UNCALIBRATED`;
- `calibrate --apply` only marks a layout `PROVISIONAL`, even after a documented review;
- only a human edit of `thresholds.json` can set `CALIBRATED`;
- the automated tests use only synthetic `SPECIMEN / TEST` images;
- the checked-in defaults have not been validated as production thresholds;
- synthetic pass results do not prove reliability on real phone photos;
- classification depends on OCR quality and public-title keywords, so a match is not
  proof of authenticity or document validity.

Do not treat a green test run as evidence of production accuracy. Calibration requires
real, consented specimens and an agreed label set. The current `id_card/default` and
`driving_license/card` layouts were adjusted from aggregate metrics of a local sample of
real phone photos and from a Youden-J sharpness review. They are marked `PROVISIONAL`
because those sample-specific adjustments have not been independently validated; every
layout remains non-production.

## Profiles and layouts

| Profile | Layouts | Document geometry | Notes |
| --- | --- | --- | --- |
| `id_card` | `default` | yes | Card-shaped ID document |
| `driving_license` | `card`, `paper` | yes | Default layout is `card`; `paper` is the older paper format |
| `vehicle_registration` | `paper` | yes | Paper document |
| `selfie` | `default` | no | Quality metrics; optional Haar face presence for slot consistency; no biometrics |

The actual backend upload slots are `selfie`, `id_card_front`, `id_card_back`,
`driving_license`, and `vehicle_registration`. The module profiles are independent of
those slot names and do not modify the Django application. Phase 3 maps the slots to
document types in `document_layouts.json`.

## Document classification (Phase 3)

The classifier runs only after the quality gate returns `ACCEPTABLE_QUALITY` or
`POOR_QUALITY`. A `NEEDS_RECAPTURE` result blocks classification with
`QUALITY_GATE_BLOCKED`.

The core classifier accepts OCR text from the caller. The Django bridge can obtain that
text from a local Tesseract installation; the classifier itself does not bundle an OCR
engine:

```python
from image_quality import classify_after_quality

result = classify_after_quality(quality_result, "id_card_front", ocr_text=text)
```

Text is normalized for accents, punctuation, and whitespace, then matched against
configurable public-title keywords in `document_layouts.json`. The default entries are:

| Type | Public keywords |
| --- | --- |
| `id_card` | `REPUBLIQUE ALGERIENNE DEMOCRATIQUE ET POPULAIRE`, `CARTE NATIONALE D'IDENTITE`, `الجمهورية الجزائرية الديمقراطية الشعبية`, `بطاقة التعريف الوطنية` |
| `driving_license` | `PERMIS DE CONDUIRE`, `REPUBLIQUE ALGERIENNE`, `رخصة السياقة`, `الجمهورية الجزائرية` |
| `vehicle_registration` | `CERTIFICAT D'IMMATRICULATION`, `CARTE GRISE`, `شهادة تسجيل`, `البطاقة الرمادية` |
| `selfie` | no text keywords; face presence only |

Each keyword has a configurable weight. Latin and Arabic keywords are scored
independently, so a document matching either language can reach `min_confidence`; a
close second candidate is treated as ambiguous. The result contains `declared_slot`,
`expected_type`, `detected_type`, `confidence`, `consistency` (`MATCH`, `MISMATCH`, or
`UNKNOWN`), `decision` (`MATCH`, `NEEDS_REVIEW`, or `QUALITY_GATE_BLOCKED`),
`manual_review_required`, and a machine-readable `reason_code`.

A mismatch is always `NEEDS_REVIEW`, never an automatic rejection. Low confidence, no
keyword, conflicting keywords, a missing selfie face, and an unavailable face detector
also return `NEEDS_REVIEW`. Selfie classification uses only optional OpenCV Haar face
presence; if the cascade is unavailable, the result is `NEEDS_REVIEW`. No colour,
texture, appearance, biometric, or identity matching is used.

## Tesseract OCR bridge

`backend_bridge.py` invokes Tesseract only when `allow_ocr` is true and no trusted
`ocr_text` is supplied. It uses `fra+ara` by default, honors `TESSERACT_CMD`,
`TESSERACT_LANGUAGE`, and `TESSDATA_PREFIX`, and auto-detects a per-user install at
`%LOCALAPPDATA%\Programs\Tesseract-OCR` or a standard install at
`C:\Program Files\Tesseract-OCR`.

Windows setup:

1. Download the UB-Mannheim installer:
   `https://github.com/UB-Mannheim/tesseract/releases/download/v5.4.0.20240606/tesseract-ocr-w64-setup-5.4.0.20240606.exe`
2. Run the installer, or extract it with 7-Zip into
   `%LOCALAPPDATA%\Programs\Tesseract-OCR` when elevation is unavailable.
3. Put the install directory on `PATH`.
4. Download `fra.traineddata` and `ara.traineddata` from
   `https://github.com/tesseract-ocr/tessdata/raw/main/fra.traineddata` and
   `https://github.com/tesseract-ocr/tessdata/raw/main/ara.traineddata` into
   `<install>\tessdata`.
5. Set `TESSDATA_PREFIX=<install>\tessdata`.
6. Verify with `tesseract --version` and `tesseract --list-langs` (it must list
   both `fra` and `ara`).

The bridge returns only OCR metadata (`used`, `available`, `source`, `reason`) by
default. Raw Tesseract text is returned only when the internal `debug_ocr` flag is
explicitly set; the HTTP endpoint enables it only with `DEBUG=True`,
`VERIFICATION_AGENT_DEBUG_OCR=True`, and `?debug=1`.

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
..\.venv\Scripts\python.exe -m image_quality calibrate C:\path\to\folder --profile id_card --layout default --apply
..\.venv\Scripts\python.exe -m image_quality evaluate C:\path\to\folder --labels C:\path\to\labels.csv --profile id_card
..\.venv\Scripts\python.exe -m image_quality evaluate C:\path\to\folder --labels C:\path\to\labels.csv --profile id_card --cv 5 --seed 20261110
..\.venv\Scripts\python.exe -m image_quality evaluate C:\path\to\folder --labels C:\path\to\labels.csv --profile id_card --what-if boundary.no_document_severity=moderate
```

`--what-if` is repeatable and never writes the threshold file. The key may be relative to
the selected layout (`blur.laplacian_variance_poor`) or a full dotted path.

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
| `3` | Unreadable, unsupported, empty, over-limit, or loaded image with no document (`UNREADABLE_IMAGE` / `NO_DOCUMENT_FOUND`) |
| `4` | Unexpected internal error; no traceback and no path are printed |

`error_category` is `null` for ordinary quality decisions. `file_not_found` means the
path does not resolve to a readable file; `corrupt_image` means the bytes cannot be
decoded; `no_document_found` means the image loaded and was analyzed, but the boundary
and decision rules found no usable document. A `no_document_found` result still contains
image dimensions, metrics, and a decision trace.

`calibrate` reads image files in a folder and prints aggregate per-metric distributions
(count, min, median, max). It ignores non-image files such as the labels CSV. By default
it does **not** write or modify `thresholds.json`; it reports
`"thresholds_calibrated"` from the selected layout and `"thresholds_modified": false`.

When a `labels.csv` is present in the folder, `calibrate` also prints
`metric_separation` for every numeric metric that has both `ACCEPTABLE_QUALITY` and
`NEEDS_RECAPTURE` labels. Use `--labels PATH` to point at a different CSV. The diagnostic
uses only images that produced metrics: a `no_document_found` image is already an
unreadable `NEEDS_RECAPTURE` for a non-sharpness reason and is excluded from the
sharpness comparison. The per-metric counts show the actual analyzed sample. The
diagnostic reports the acceptable p5/median, the needs-recapture p95, whether the two
distributions overlap, and the Youden-J-optimal threshold. The direction
(higher-is-better or lower-is-better) is chosen from the labels. `calibrate` also reports
`metric_separation_n_by_class` after excluding unreadable images and marks every
suggestion with `exploratory: true` when any human class has fewer than 30 analyzed
images. A top-level `selection_bias_warning` appears in the same condition. Example from
the local 18-analyzed-image sample:

```json
"metric_separation": {
  "sharpness_tenengrad": {
    "acceptable_p5": 2549.60987,
    "acceptable_median": 4283.1372,
    "needs_recapture_p95": 2696.437875,
    "overlap": true,
    "suggested_threshold": 2121.10215,
    "direction": "higher_is_better",
    "youden_j": 0.9,
    "sensitivity": 1.0,
    "specificity": 0.9,
    "acceptable_count": 7,
    "needs_recapture_count": 10,
    "exploratory": true
  }
}
```

The suggested threshold is the midpoint of the Youden-J plateau, not an automatic write
target. A human owner still reviews and sets the actual soft/hard bands. Labels that
cannot be read produce `metric_separation_error` while the distribution report still
succeeds.

`calibrate --apply` deliberately marks the selected profile/layout `PROVISIONAL` and
records the sample/analyzed/failed counts plus `calibration_metadata` (per-class sample
counts and group count) in the threshold file. Without `--output`, it writes the default
`thresholds.json`, which `analyze` and `evaluate` read by default. `--output PATH` writes
an alternate threshold file instead and requires `--apply`; an alternate file is not used
by `evaluate` unless it is copied over the default file. The command reports
`"thresholds_calibrated": false`, `"thresholds_modified": true`, and
`"thresholds_written": true` after a successful write. It does not invent threshold
values from unlabeled images: the owner must review and set the values first, then apply
the provisional calibration decision. Only a human edit can change the layout to
`CALIBRATED`; legacy `thresholds_calibrated: true` files migrate in memory to
`PROVISIONAL` without deleting any threshold value.

`evaluate` reads a local labels CSV with this shape:

```csv
filename,label,group
good.jpg,ACCEPTABLE_QUALITY,person-001
blurry.jpg,NEEDS_RECAPTURE,person-001
```

`filename` must be a plain name inside the evaluated folder; path traversal is rejected.
`label` must be one of the three statuses. The optional third `group` column keeps
multiple photos of the same person or document in one cross-validation fold.

`evaluate` prints a confusion matrix, false-acceptance count/rate, false-rejection
count/rate, and an `acceptable_but_poor` count. An unreadable or no-document image is
counted as `NEEDS_RECAPTURE`. It also prints a `gate` block that treats human
`NEEDS_RECAPTURE` as the positive class and compares two operating policies:

- `pass_if_poor`: `ACCEPTABLE_QUALITY` and `POOR_QUALITY` pass;
- `strict`: only `ACCEPTABLE_QUALITY` passes.

Each policy reports counts, rates with 95% Wilson intervals, sensitivity, specificity,
false-positive/false-negative rates, and Youden J. A denominator of zero produces
`null`, not a fabricated zero. `no_document_by_label` counts `NO_DOCUMENT_FOUND` per
human label.

`evaluate --cv K --seed S` runs grouped K-fold cross-validation. The defaults are
`K=5` and `S=20261110` when the flag is used without a value. Folds never split a group:
the explicit `group` column is preferred, then the filename stem. Cross-validation
refuses to run when any status class has fewer than K samples or when an explicit group
column has fewer than K groups. When the group column is absent and filename stems yield
fewer than K groups, the command falls back to stratified folds and emits
`cv_group_fallback: stratified_folds`.
In each fold, soft thresholds are derived from the training rows only, applied in
memory, and evaluated on the held-out rows. The output includes the in-sample gate, the
pooled out-of-fold gate, fold sizes, group leakage status, and the derived thresholds by
fold. Thresholds are never written by cross-validation.

Before a label is treated as missing, `evaluate` normalizes filename differences such as
spaces, punctuation, brackets, Unicode width, and case. A unique match is analyzed and
reported only as the generic warning code `normalized_filename_match`:

```json
"warnings": ["normalized_filename_match"]
```

If normalization finds no match, `missing_files_count` is incremented; if it finds more
than one candidate, `ambiguous_files_count` is incremented. Both are counted as
unreadable rather than silently discarded. Filenames are not included in the JSON. The
`boundary_diagnostics` block reports `detector_used` and candidate rejection counts
(`aspect_ratio`, `area_ratio`, `solidity`, `confidence`, `border_contact`,
`none_found`) split by human label.

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

`processing_time_ms` is the measured in-process analysis time in milliseconds with three
decimal places for `analyze`. `evaluate` omits timing so the same `--cv` seed produces
byte-for-byte identical JSON. `thresholds_calibrated` is
`true` only when the selected layout's `calibration_status` is `CALIBRATED` in
`thresholds.json`; `PROVISIONAL` keeps it `false`.

Reason codes are stable strings:

`LOW_RES`, `TOO_SMALL`, `BLURRY`, `DARK`, `OVEREXPOSED`, `UNEVEN_LIGHT`,
`LOW_CONTRAST`, `GLARE`, `CROPPED`, `ROTATED`, `PERSPECTIVE`, `NO_DOCUMENT_FOUND`,
`UNREADABLE_IMAGE`.

Human-readable messages live only in `messages.json`. Every code has `en` and `fr`
filled; `ar` is intentionally empty in this phase. No message contains a path, personal
data, or an identity claim.

## Metrics and defaults

The values below are the actual defaults in `thresholds.json`. They are provisional;
layout status is tracked per profile/layout. `id_card/default` and
`driving_license/card` are currently `PROVISIONAL` from automated calibration, and the
other layouts are `UNCALIBRATED`.
`D` = `id_card/default`; `LC` = `driving_license/card`;
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
| `min_document_width` | 500 | 700 | 800 | n/a |
| `min_document_height` | 400 | 450 | 600 | n/a |
| `min_document_megapixels` | 0.24 | 0.315 | 0.48 | n/a |
| `evaluation_region.document_inset_fraction` | 0.03 | 0.03 | 0.03 | n/a |
| `evaluation_region.central_fraction` | 0.8 | 0.8 | 0.8 | 0.7 |
| `blur.laplacian_variance_poor` | 25 | 74.2 | 120 | 100 |
| `blur.laplacian_variance_severe` | 20 | 20 | 10 | 10 |
| `blur.tenengrad_poor` | 2500 | 6400.5 | 2500 | 2500 |
| `blur.tenengrad_severe` | 2000 | 2000 | 1200 | 1200 |
| `exposure.dark_mean` | 60 | 60 | 60 | 60 |
| `exposure.overexposed_mean` | 215 | 215 | 215 | 215 |
| `exposure.dark_clip_level` | 10 | 10 | 10 | 10 |
| `exposure.bright_clip_level` | 245 | 245 | 245 | 245 |
| `exposure.dark_clip_fraction` | 0.30 | 0.30 | 0.30 | 0.30 |
| `exposure.bright_clip_fraction` | 0.35 | 0.35 | 0.35 | 0.35 |
| `exposure.uneven_illumination_ratio` | 0.65 | 0.50 | 0.50 | 0.50 |
| `exposure.block_grid` | 8 | 8 | 8 | 8 |
| `contrast.p5_p95_min_range` | 90 | 90 | 90 | 80 |
| `contrast.rms_min` | 30 | 30 | 30 | 28 |
| `contrast.p5_min` | 40 | 28.5 | 40 | 40 |
| `contrast.rms_secondary_min` | 20 | 25 | 20 | 20 |

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
| `boundary.min_confidence` | 0.55 | 0.45 | 0.65 |
| `boundary.no_document_severity` | `critical` | `critical` | `critical` |
| `boundary.min_analysis_side_px` | 64 | 64 | 64 |
| `boundary.fallback_min_area_ratio` | 0.25 | 0.25 | 0.25 |
| `boundary.fallback_confidence` | 0.72 | 0.72 | 0.72 |
| `boundary.min_candidate_area_ratio` | 0.04 | 0.04 | 0.04 |
| `boundary.border_tolerance_fraction` | 0.012 | 0.012 | 0.012 |
| `boundary.cropping_min_sides` | 2 | 2 | 2 |
| `boundary.cropping_min_confidence` | 0.80 | 0.80 | 0.80 |
| `boundary.no_document_min_edge_density` | 0.004 | 0.004 | 0.004 |
| `boundary.border_cut_full_frame_edge_density` | n/a | 0.068 | n/a |
| `boundary.min_solidity` | 0.85 | 0.85 | 0.85 |
| `boundary.canny_low` / `canny_high` | 50 / 150 | 50 / 150 | 50 / 150 |
| `boundary.approx_epsilon_fraction` | 0.02 | 0.02 | 0.02 |
| `boundary.dilate_iterations` | 2 | 2 | 2 |
| `boundary.aspect_ratio_range` | `[1.35, 1.85]` | `[1.30, 2.00]` | `null` |
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
`0.95`. This is the secondary focus indicator. A value at or below
`laplacian_variance_poor` is a weak blur signal; a value strictly below
`laplacian_variance_severe` is a severe signal.

#### `sharpness_tenengrad`

Mean squared Sobel gradient energy: `mean(Gx² + Gy²)`. It is `measured` with confidence
`0.90`. It is the primary focus indicator. A value at or below `tenengrad_poor` is a weak
blur signal; a value strictly below `tenengrad_severe` is severe.

For `id_card/default`, the soft bands are `20 <= laplacian <= 25` and
`2000 <= tenengrad <= 2500`. The soft rule is an **AND** rule: a value in either band
alone is accepted; only a value in **both** bands is a moderate `BLURRY` reason
(`POOR_QUALITY`). The hard rule is independent: either indicator strictly below its
severe edge is a critical `BLURRY` (`NEEDS_RECAPTURE`), so Tenengrad can reject on its
own and Laplacian is a secondary hard veto. The soft-band edges are inclusive because
only the hard comparison is strict. Tenengrad is the better separator on the labeled
sample (Youden J = 0.947) than Laplacian (J = 0.783). The Laplacian soft upper edge was
lowered from 35 to 25 after a well-framed specimen with Laplacian 26.8 and Tenengrad
2865 was falsely rejected; Tenengrad remains the primary blur guard. A would-be critical
`BLURRY` is downgraded to moderate when the same image is `DARK` or `LOW_CONTRAST`,
because those conditions depress both sharpness indicators.

For `driving_license/card`, the provisional soft bands are `20 <= laplacian <= 74.2` and
`2000 <= tenengrad <= 6400.5`, with severe edges at 20 and 2000. Driving-license captures
are sharper and higher-contrast than ID cards, so the same AND rule is used with
profile-specific edges.

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

Reports the robust `p5_p95_range`, `rms_contrast`, `p5`, and `p95` of the gray evaluation
region. It is `measured`, confidence `0.90`. It adds `LOW_CONTRAST` (moderate) when the
percentile range or RMS is below its primary minimum, or when the secondary signal
`p5 < contrast.p5_min` or `rms_contrast < contrast.rms_secondary_min` is present. The
secondary signal is intentionally redundant with the RMS check and adds a dark-end check
for low-contrast captures. It also supports the `OVEREXPOSED` rule.

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

A heuristic geometric detector. It first uses Canny edges, dilation, external contours,
convexity, solidity, and `approxPolyDP` to propose a quadrilateral. An Otsu bright-region
fallback handles blurred cards and cards that touch the image border. If neither finds a
candidate, `HoughLinesP` reconstructs a rectangle from straight edge segments, which
recovers dashed or shadow-broken borders. If the area outside the central crop is
featureless, the central crop becomes the document region. For `driving_license/card`,
when there is no contour candidate but high-contrast content reaches at least two frame
edges and central edge density exceeds `border_cut_full_frame_edge_density`, the full
frame becomes the document region with `warning: border_cut_but_high_contrast`. Finally,
a detected card whose area exceeds 80% of the frame is accepted as the full frame; frame
borders are not treated as cropping evidence in either full-frame fallback. A rectangle
is **not** proof of a document.

Reported fields include `valid`, `area_ratio`, `bounding_box_working`, `quad_working`,
`border_sides_touching`, `cropping_suspected`, `cropping_confirmed`, `aspect_ratio`,
`aspect_ratio_in_range`, `edge_density`, `frame_border_ambiguous`,
`no_document_signals`, `candidate_count`, `solidity`, `detector_used`, `warning`, and
`rejection_counts`. `detector_used` is one of `contour_quadrilateral`,
`otsu_bright_region_quadrilateral`, `otsu_bright_region_min_area_rect`, `hough_lines`,
`center_crop_fallback`, `full_frame`, or `no_candidate`. `rejection_counts` records
candidate-level failures for `aspect_ratio`, `area_ratio`, `solidity`, `confidence`,
`border_contact`, and `none_found`.

- Validity requires the candidate confidence to meet `min_confidence`.
- Cropping is suspected when the quadrilateral touches at least `cropping_min_sides`
  image borders. It is confirmed only for a valid boundary with confidence at least
  `cropping_min_confidence`, and then adds `CROPPED` as **critical**.
- `NO_DOCUMENT_FOUND` appears when at least two independent no-document signals are
  present: no reliable quadrilateral, an area below `min_candidate_area_ratio`, an edge
  density below `no_document_min_edge_density`, or no candidate contours at all. Its
  severity is `critical` by default. If `boundary.no_document_severity` is `moderate`, it
  becomes moderate and the status can downgrade to `POOR_QUALITY` only when sharpness,
  contrast, and resolution all pass; otherwise it remains critical.
- `min_analysis_side_px` prevents boundary conclusions on very small working images.
- `aspect_ratio_range` is checked only for card layouts. Paper layouts report
  `aspect_ratio_in_range: null` because paper sizes vary. It is diagnostic only: aspect
  ratio alone does not add a rejection reason.

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
| Either sharpness indicator strictly below its severe threshold, without dark or low-contrast exposure | critical | `BLURRY` |
| Cropping confirmed | critical | `CROPPED` |
| At least two no-document signals | critical by default; configurable moderate | `NO_DOCUMENT_FOUND` |
| Both sharpness indicators inside their soft bands | moderate | `BLURRY` |
| Mean brightness below dark threshold, or dark clipping with mean below 100 | moderate | `DARK` |
| High mean brightness, high bright clipping, and low contrast | moderate | `OVEREXPOSED` |
| Uneven block illumination above threshold | moderate | `UNEVEN_LIGHT` |
| Percentile range or RMS contrast below primary minimum, or `p5`/secondary RMS below its limit | moderate | `LOW_CONTRAST` |
| Glare fractions above limits and not suppressed | moderate | `GLARE` |
| Absolute in-plane angle above threshold | moderate | `ROTATED` |
| Perspective imbalance above threshold | moderate | `PERSPECTIVE` |

- Any critical reason makes the status `NEEDS_RECAPTURE`.
- Otherwise, any moderate reason makes it `POOR_QUALITY`.
- No reasons makes it `ACCEPTABLE_QUALITY`.
- For `id_card/default`, a single soft blur indicator is accepted. A hard `BLURRY` is
  triggered by either indicator strictly below its severe edge; a moderate warning is
  triggered only when both indicators are in their soft bands. Tenengrad is the primary
  hard indicator and Laplacian is the secondary hard veto.
- `unknown` metrics never reject by themselves.
- `confidence = min(0.99, 0.5 + 0.5 × known_applicable_metrics / applicable_metrics)`.
  It is a coverage indicator, not a probability that the decision is correct. Structured
  errors use confidence `0.0`.

Reason codes are sorted by priority:
`UNREADABLE_IMAGE`, `TOO_SMALL`, `LOW_RES`, `CROPPED`, `NO_DOCUMENT_FOUND`, `BLURRY`,
`DARK`, `OVEREXPOSED`, `UNEVEN_LIGHT`, `LOW_CONTRAST`, `GLARE`, `PERSPECTIVE`, `ROTATED`.

## Calibration

Without `--apply`, `calibrate` only reports distributions and never edits thresholds. It
reports the selected layout's `thresholds_calibrated` value and
`thresholds_modified: false`.

`calibrate --apply` marks that profile/layout `PROVISIONAL`, records the sample counts,
per-class counts, and group count under `calibration` / `calibration_metadata`, and
writes the threshold file. `--output PATH` writes an alternate file instead; it requires
`--apply`. After a successful write the command reports `thresholds_calibrated: false`,
`thresholds_modified: true`, and `thresholds_written: true`. The top-level
`calibration_status` and `notes` remain informational; the per-layout status is
authoritative. A synthetic test run is not a calibration event and must not be used to
change the status.

The three statuses are:

- `UNCALIBRATED`: no calibration evidence;
- `PROVISIONAL`: automated calibration output exists but is not human-validated;
- `CALIBRATED`: a human owner explicitly set the status after documented review.

Legacy files with `thresholds_calibrated: true` are migrated in memory to `PROVISIONAL`
and `thresholds_calibrated: false`; threshold values are never deleted or rewritten by
the migration.

Recommended calibration process:

1. Collect real, consented specimens with clear ownership and usage permission.
2. Label each with the agreed status and reason codes.
3. Run `calibrate` and inspect the distributions and `metric_separation`.
4. Run `evaluate --cv 5` and review in-sample and pooled out-of-fold gate metrics.
5. Have a human owner review and document every threshold change.
6. Run `calibrate ... --apply` to record a `PROVISIONAL` calibration.
7. A human owner may then edit `calibration_status` to `CALIBRATED` only after an
   independent validation. No automated command does that.
8. Re-run `evaluate` and keep the report with the threshold version.

Use `evaluate --what-if KEY=VALUE` to test a threshold change in memory before editing
the file. The override is echoed under `what_if` and is never written.

Never copy, display, print, upload, or commit real specimens. Report aggregate metrics
only.

## Privacy and offline behavior

- Images are decoded and processed entirely in memory.
- The input file is never modified.
- No debug images, resized copies, crops, or intermediate files are written.
- `analyze`, `calibrate`, and `evaluate` output contains no file path, file name, image
  content, base64, OCR text, face data, or identity data.
- `evaluate` reports only aggregate counts (`missing_files_count`,
  `ambiguous_files_count`, `normalized_matches_count`) and generic warning codes; it does
  not echo label filenames or group names.
- There is no bundled OCR engine, no biometrics, no identity comparison, and no fraud
  decision. The optional bridge can call a separately installed Tesseract binary.
- Classification consumes OCR text and returns only public keyword matches, not the raw
  OCR text. The bridge never returns recognized text. A selfie slot may run an optional
  OpenCV Haar face-presence check; no face data or embeddings are stored.
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
    document_classification.py      # Phase 3 slot/type keyword consistency
    document_layouts.json           # configurable public keywords and slot map
    errors.py                       # structured error categories
    image_loader.py                 # format detection, EXIF, in-memory decode
    normalization.py                # bounded working copy, no upscaling
    output_schema.py                # versions, statuses, reason codes, validation
    profiles.py                     # profile/layout and threshold loading
    thresholds.json                 # per-profile thresholds and calibration metadata
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
  -> versioned quality JSON
  -> classify_after_quality (usable statuses only)
  -> slot/type consistency JSON (MATCH / NEEDS_REVIEW)
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
only, structured errors, schema validation, path/privacy checks for analyze, calibrate,
and evaluate, gate metrics with Wilson intervals and Youden J, grouped and stratified
cross-validation determinism, group-leakage prevention, missing/ambiguous aggregate
counts, provisional calibration status and legacy migration, in-memory `--what-if`,
boundary rejection diagnostics, `no_document_severity`, calibrated-threshold
persistence, Youden-J metric separation, soft-blur threshold bands,
false-acceptance/false-rejection math, Phase 3 keyword classification for every slot,
slot/type mismatch to `NEEDS_REVIEW`, low confidence, no keyword, ambiguous keywords,
selfie face fallback, configurable layouts, quality-gate blocking, OCR-text privacy,
Tesseract bridge integration with synthetic known and wrong document text, file
immutability, no debug files, and blocked network sockets.

A 12-megapixel timing benchmark is available from `verification_agent/ocr_lab`:

```powershell
..\.venv\Scripts\python.exe -m image_quality.tests.benchmark_12mp
```

It generates a synthetic 4000 × 3000 JPEG in memory, warms up once, then times repeated
`analyze_bytes` calls. Generation and JPEG encoding are excluded. On the Phase 2
development machine the median was about 0.23 seconds for the `id_card` profile. This is
a synthetic local measurement, not a production latency guarantee.

## Limitations and non-goals

- Defaults are not production-validated. The `id_card/default` and
  `driving_license/card` values were adjusted from aggregate metrics of local samples
  and a Youden-J sharpness review. They are marked `PROVISIONAL`; this is still a
  sample-specific calibration, not independent validation. The other layouts remain
  `UNCALIBRATED`.
- No production validation has been performed for any real ID, licence, or registration.
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
- The selfie profile measures quality and may use optional Haar face presence for slot
  consistency. It performs no face comparison, liveness check, or biometric analysis.
- Classification depends on OCR text quality and public-title keywords. It does not
  verify authenticity, extract personal fields, or prove document validity.
- The module does not decide identity, authenticity, fraud, or document validity.
- The core module is not wired into Django; the separate `backend_bridge.py` is the
  read-only stdin/stdout integration point. No models, database, `.env`, frontend, or
  backend `requirements.txt` are modified.
- Classification stops at slot/type consistency. OCR execution is limited to the optional
  bridge; biometrics, identity matching, and fraud decisions remain out of scope.
