"""Command-line interface: analyze, calibrate, evaluate."""

from __future__ import annotations

import argparse
import copy
import csv
import json
import statistics
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from time import perf_counter
from typing import Any, Sequence

from .analyzer import analyze_path
from .errors import ProfileError
from .output_schema import (
    ALGORITHM_VERSION,
    SCHEMA_VERSION,
    STATUSES,
    STATUS_ACCEPTABLE,
    STATUS_RECAPTURE,
)
from .profiles import get_profile_config, load_thresholds
from .profiles import thresholds_path as default_thresholds_path

_IMAGE_SUFFIXES = frozenset(
    {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
)


def _elapsed_ms(start: float) -> float:
    return round((perf_counter() - start) * 1000.0, 3)


def _command_error(
    *,
    command: str,
    profile: str,
    layout: str | None,
    category: str,
    reason_codes: list[str],
    start: float,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "algorithm_version": ALGORITHM_VERSION,
        "command": command,
        "profile": profile,
        "layout": layout,
        "status": STATUS_RECAPTURE,
        "confidence": 0.0,
        "thresholds_calibrated": False,
        "processing_time_ms": _elapsed_ms(start),
        "image": None,
        "metrics": {},
        "reason_codes": reason_codes,
        "decision_trace": [],
        "error_category": category,
    }


def _collect_numeric(prefix: str, value: Any, output: dict[str, list[float]]) -> None:
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, (int, float)):
        output[prefix].append(float(value))
        return
    if isinstance(value, dict):
        for key, child in value.items():
            _collect_numeric(f"{prefix}.{key}", child, output)


def _summary(values: list[float]) -> dict[str, Any]:
    return {
        "count": len(values),
        "min": round(min(values), 6),
        "median": round(float(statistics.median(values)), 6),
        "max": round(max(values), 6),
    }


def _percentile(values: list[float], fraction: float) -> float:
    """Linear-interpolation percentile used by the calibration diagnostic."""

    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    index = (len(ordered) - 1) * fraction
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    weight = index - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _metric_separation(
    acceptable: list[float],
    recapture: list[float],
) -> dict[str, Any] | None:
    """Return Youden-J separation statistics for one numeric metric.

    The direction (higher-is-better or lower-is-better) is chosen from the
    labels rather than hard-coded, because some metrics improve upward and
    others improve downward.
    """

    if len(acceptable) < 2 or len(recapture) < 2:
        return None
    values = sorted(set(acceptable + recapture))
    if len(values) < 2:
        return None
    thresholds = [
        (left + right) / 2.0 for left, right in zip(values, values[1:])
    ]

    best_j = -1.0
    best_thresholds: dict[str, list[float]] = {
        "higher_is_better": [],
        "lower_is_better": [],
    }
    for direction in ("higher_is_better", "lower_is_better"):
        for threshold in thresholds:
            if direction == "higher_is_better":
                true_positive = sum(1 for value in acceptable if value >= threshold)
                true_negative = sum(1 for value in recapture if value < threshold)
            else:
                true_positive = sum(1 for value in acceptable if value <= threshold)
                true_negative = sum(1 for value in recapture if value > threshold)
            sensitivity = true_positive / len(acceptable)
            specificity = true_negative / len(recapture)
            youden_j = sensitivity + specificity - 1.0
            if youden_j > best_j + 1e-12:
                best_j = youden_j
                best_thresholds = {
                    "higher_is_better": [],
                    "lower_is_better": [],
                }
                best_thresholds[direction].append(threshold)
            elif abs(youden_j - best_j) <= 1e-12:
                best_thresholds[direction].append(threshold)

    best_direction = next(
        direction
        for direction in ("higher_is_better", "lower_is_better")
        if best_thresholds[direction]
    )
    candidates = best_thresholds[best_direction]
    suggested_threshold = (min(candidates) + max(candidates)) / 2.0
    if best_direction == "higher_is_better":
        true_positive = sum(
            1 for value in acceptable if value >= suggested_threshold
        )
        true_negative = sum(
            1 for value in recapture if value < suggested_threshold
        )
    else:
        true_positive = sum(
            1 for value in acceptable if value <= suggested_threshold
        )
        true_negative = sum(
            1 for value in recapture if value > suggested_threshold
        )

    return {
        "acceptable_p5": round(_percentile(acceptable, 0.05), 6),
        "acceptable_median": round(float(statistics.median(acceptable)), 6),
        "needs_recapture_p95": round(_percentile(recapture, 0.95), 6),
        "overlap": best_j < 1.0 - 1e-12,
        "suggested_threshold": round(suggested_threshold, 6),
        "direction": best_direction,
        "youden_j": round(best_j, 6),
        "sensitivity": round(true_positive / len(acceptable), 6),
        "specificity": round(true_negative / len(recapture), 6),
        "acceptable_count": len(acceptable),
        "needs_recapture_count": len(recapture),
    }


def _metric_separation_table(
    labeled_values: dict[str, dict[str, list[float]]],
) -> dict[str, dict[str, Any]]:
    acceptable = labeled_values.get(STATUS_ACCEPTABLE, {})
    recapture = labeled_values.get(STATUS_RECAPTURE, {})
    table: dict[str, dict[str, Any]] = {}
    for name in sorted(set(acceptable) & set(recapture)):
        separation = _metric_separation(acceptable[name], recapture[name])
        if separation is not None:
            table[name] = separation
    return table


def _iter_image_files(folder: Path) -> list[Path]:
    return sorted(
        path
        for path in folder.iterdir()
        if path.is_file() and path.suffix.lower() in _IMAGE_SUFFIXES
    )


def _normalize_filename(name: str) -> str:
    """Normalize display-only filename differences without guessing content."""

    normalized = unicodedata.normalize("NFKC", name).strip().casefold()
    return "".join(character for character in normalized if character.isalnum())


def _apply_calibration_status(
    data: dict[str, Any],
    *,
    profile: str,
    layout: str,
    image_count: int,
    analyzed_count: int,
    failed_count: int,
) -> dict[str, Any]:
    updated = copy.deepcopy(data)
    try:
        layout_data = updated["profiles"][profile]["layouts"][layout]
    except (KeyError, TypeError) as exc:
        raise ProfileError("invalid_configuration") from exc
    if not isinstance(layout_data, dict):
        raise ProfileError("invalid_configuration")
    layout_data["calibration_status"] = "CALIBRATED"
    layout_data["calibration"] = {
        "method": "aggregate_distribution_review",
        "sample_count": int(image_count),
        "analyzed_count": int(analyzed_count),
        "failed_count": int(failed_count),
    }
    return updated


def _write_thresholds(target: Path, data: dict[str, Any]) -> None:
    """Write atomically and validate the result before replacing the target."""

    if target.exists() and target.is_dir():
        raise OSError("threshold target is a directory")
    temp = target.with_name(f".{target.name}.tmp")
    try:
        temp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        load_thresholds(temp)
        temp.replace(target)
    finally:
        if temp.exists():
            temp.unlink()


def calibrate_folder(
    folder: str | Path,
    profile: str,
    layout: str | None = None,
    *,
    max_side: int | None = None,
    max_megapixels: float | None = None,
    apply: bool = False,
    output: str | Path | None = None,
    labels: str | Path | None = None,
    thresholds_path: str | Path | None = None,
) -> dict[str, Any]:
    start = perf_counter()
    source_path = (
        Path(thresholds_path) if thresholds_path is not None else default_thresholds_path()
    )
    try:
        thresholds = load_thresholds(source_path)
        cfg = get_profile_config(profile, layout, thresholds)
    except ProfileError as exc:
        return _command_error(
            command="calibrate",
            profile=profile,
            layout=layout,
            category=exc.category,
            reason_codes=[],
            start=start,
        )

    if output is not None and not apply:
        return _command_error(
            command="calibrate",
            profile=profile,
            layout=layout,
            category="invalid_configuration",
            reason_codes=[],
            start=start,
        )

    folder_path = Path(folder)
    if not folder_path.exists():
        return _command_error(
            command="calibrate",
            profile=profile,
            layout=layout,
            category="folder_not_found",
            reason_codes=[],
            start=start,
        )
    if not folder_path.is_dir():
        return _command_error(
            command="calibrate",
            profile=profile,
            layout=layout,
            category="not_a_folder",
            reason_codes=[],
            start=start,
        )

    files = _iter_image_files(folder_path)
    labels_source = Path(labels) if labels is not None else folder_path / "labels.csv"
    label_rows: list[tuple[str, str]] | None = None
    label_error: str | None = None
    if labels is not None or labels_source.is_file():
        label_rows, label_error = _read_labels(labels_source)

    path_to_label: dict[Path, str] = {}
    if label_rows:
        by_name = {path.name: path for path in files}
        by_normalized_name: dict[str, list[Path]] = defaultdict(list)
        for path in files:
            by_normalized_name[_normalize_filename(path.name)].append(path)
        for filename, expected in label_rows:
            path = by_name.get(filename)
            if path is None:
                matches = by_normalized_name.get(_normalize_filename(filename), [])
                if len(matches) == 1:
                    path = matches[0]
            if path is not None:
                path_to_label[path] = expected

    numeric_values: dict[str, list[float]] = defaultdict(list)
    labeled_values: dict[str, dict[str, list[float]]] = defaultdict(
        lambda: defaultdict(list)
    )
    unknown_counts: Counter[str] = Counter()
    failure_counts: Counter[str] = Counter()
    analyzed_count = 0

    for path in files:
        result = analyze_path(
            path,
            profile,
            layout,
            thresholds=thresholds,
            max_side=max_side,
            max_megapixels=max_megapixels,
        )
        if result.get("error_category") is not None:
            failure_counts[str(result["error_category"])] += 1
            continue
        analyzed_count += 1
        expected = path_to_label.get(path)
        for metric_name, metric in result["metrics"].items():
            if metric.get("nature") == "unknown":
                unknown_counts[metric_name] += 1
                continue
            value = metric.get("value")
            _collect_numeric(metric_name, value, numeric_values)
            if expected in (STATUS_ACCEPTABLE, STATUS_RECAPTURE):
                _collect_numeric(metric_name, value, labeled_values[expected])

    failed_count = len(files) - analyzed_count
    calibrated = cfg.thresholds_calibrated
    written = False
    modified = False
    calibration_skipped: str | None = None

    if apply:
        if analyzed_count == 0:
            calibration_skipped = "no_analyzed_images"
        else:
            target = Path(output) if output is not None else source_path
            try:
                updated = _apply_calibration_status(
                    thresholds,
                    profile=cfg.profile,
                    layout=cfg.layout,
                    image_count=len(files),
                    analyzed_count=analyzed_count,
                    failed_count=failed_count,
                )
                _write_thresholds(target, updated)
                written = True
                modified = True
                calibrated = get_profile_config(
                    profile,
                    layout,
                    load_thresholds(target),
                ).thresholds_calibrated
            except OSError:
                return _command_error(
                    command="calibrate",
                    profile=profile,
                    layout=layout,
                    category="invalid_output_path",
                    reason_codes=[],
                    start=start,
                )
            except ProfileError:
                return _command_error(
                    command="calibrate",
                    profile=profile,
                    layout=layout,
                    category="invalid_configuration",
                    reason_codes=[],
                    start=start,
                )

    payload = {
        "schema_version": SCHEMA_VERSION,
        "algorithm_version": ALGORITHM_VERSION,
        "command": "calibrate",
        "profile": profile,
        "layout": layout,
        "image_count": len(files),
        "analyzed_count": analyzed_count,
        "failed_count": failed_count,
        "failure_categories": dict(sorted(failure_counts.items())),
        "unknown_metrics": dict(sorted(unknown_counts.items())),
        "metrics": {
            name: _summary(values) for name, values in sorted(numeric_values.items())
        },
        "metric_separation": _metric_separation_table(labeled_values),
        "metric_separation_scope": "analyzed_images_only",
        "thresholds_calibrated": calibrated,
        "thresholds_modified": modified,
        "thresholds_written": written,
        "processing_time_ms": _elapsed_ms(start),
    }
    if label_error is not None:
        payload["metric_separation_error"] = label_error
    if calibration_skipped is not None:
        payload["calibration_skipped"] = calibration_skipped
    return payload


def _read_labels(csv_path: Path) -> tuple[list[tuple[str, str]] | None, str | None]:
    if not csv_path.exists():
        return None, "labels_not_found"
    if not csv_path.is_file():
        return None, "labels_not_a_file"
    try:
        with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None:
                return None, "invalid_labels"
            fields = {field.strip().lower() for field in reader.fieldnames}
            if "filename" not in fields or "label" not in fields:
                return None, "invalid_labels"
            rows: list[tuple[str, str]] = []
            for row in reader:
                lowered = {str(key).strip().lower(): value for key, value in row.items()}
                filename = str(lowered.get("filename", "")).strip()
                label = str(lowered.get("label", "")).strip()
                if not filename or label not in STATUSES:
                    return None, "invalid_labels"
                if Path(filename).name != filename:
                    return None, "invalid_labels"
                rows.append((filename, label))
            return rows, None
    except (OSError, UnicodeError, csv.Error):
        return None, "invalid_labels"


def evaluate_folder(
    folder: str | Path,
    labels: str | Path,
    profile: str,
    layout: str | None = None,
    *,
    max_side: int | None = None,
    max_megapixels: float | None = None,
    thresholds_path: str | Path | None = None,
) -> dict[str, Any]:
    start = perf_counter()
    source_path = (
        Path(thresholds_path) if thresholds_path is not None else default_thresholds_path()
    )
    try:
        thresholds = load_thresholds(source_path)
        cfg = get_profile_config(profile, layout, thresholds)
    except ProfileError as exc:
        return _command_error(
            command="evaluate",
            profile=profile,
            layout=layout,
            category=exc.category,
            reason_codes=[],
            start=start,
        )

    folder_path = Path(folder)
    if not folder_path.exists():
        return _command_error(
            command="evaluate",
            profile=profile,
            layout=layout,
            category="folder_not_found",
            reason_codes=[],
            start=start,
        )
    if not folder_path.is_dir():
        return _command_error(
            command="evaluate",
            profile=profile,
            layout=layout,
            category="not_a_folder",
            reason_codes=[],
            start=start,
        )

    rows, label_error = _read_labels(Path(labels))
    if label_error is not None or rows is None:
        return _command_error(
            command="evaluate",
            profile=profile,
            layout=layout,
            category=label_error or "invalid_labels",
            reason_codes=[],
            start=start,
        )

    files = _iter_image_files(folder_path)
    by_name = {path.name: path for path in files}
    by_normalized_name: dict[str, list[Path]] = defaultdict(list)
    for path in files:
        by_normalized_name[_normalize_filename(path.name)].append(path)

    confusion: dict[str, dict[str, int]] = {
        expected: {predicted: 0 for predicted in STATUSES} for expected in STATUSES
    }
    unreadable_count = 0
    warnings: list[str] = []
    missing_files: list[str] = []
    ambiguous_files: list[str] = []

    for filename, expected in rows:
        path = by_name.get(filename)
        if path is None:
            matches = by_normalized_name.get(_normalize_filename(filename), [])
            if len(matches) == 1:
                path = matches[0]
                warnings.append(f"normalized: {filename} -> {path.name}")
            elif len(matches) > 1:
                ambiguous_files.append(filename)
            else:
                missing_files.append(filename)

        if path is None:
            unreadable_count += 1
            confusion[expected][STATUS_RECAPTURE] += 1
            continue

        result = analyze_path(
            path,
            profile,
            layout,
            thresholds=thresholds,
            max_side=max_side,
            max_megapixels=max_megapixels,
        )
        predicted = str(result["status"])
        if predicted not in STATUSES:
            predicted = STATUS_RECAPTURE
        if result.get("error_category") is not None:
            unreadable_count += 1
        confusion[expected][predicted] += 1

    expected_acceptable = sum(confusion[STATUS_ACCEPTABLE].values())
    expected_recapture = sum(confusion[STATUS_RECAPTURE].values())
    false_acceptance = confusion[STATUS_RECAPTURE][STATUS_ACCEPTABLE]
    false_rejection = confusion[STATUS_ACCEPTABLE][STATUS_RECAPTURE]
    acceptable_but_poor = confusion[STATUS_ACCEPTABLE]["POOR_QUALITY"]

    return {
        "schema_version": SCHEMA_VERSION,
        "algorithm_version": ALGORITHM_VERSION,
        "command": "evaluate",
        "profile": profile,
        "layout": layout,
        "sample_count": len(rows),
        "unreadable_count": unreadable_count,
        "confusion_matrix": confusion,
        "false_acceptance": false_acceptance,
        "false_acceptance_rate": (
            round(false_acceptance / expected_recapture, 6) if expected_recapture else None
        ),
        "false_rejection": false_rejection,
        "false_rejection_rate": (
            round(false_rejection / expected_acceptable, 6) if expected_acceptable else None
        ),
        "acceptable_but_poor": acceptable_but_poor,
        "warnings": sorted(warnings),
        "missing_files": sorted(missing_files),
        "ambiguous_files": sorted(ambiguous_files),
        "thresholds_calibrated": cfg.thresholds_calibrated,
        "processing_time_ms": _elapsed_ms(start),
    }


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False))


def _analyze_exit_code(result: dict[str, Any]) -> int:
    category = result.get("error_category")
    if category is None:
        return 0
    if category in ("invalid_profile", "invalid_layout", "invalid_configuration"):
        return 2
    return 3


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m image_quality",
        description="Standalone, offline document-image quality analysis.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    analyze_parser = subparsers.add_parser("analyze", help="Analyze one image.")
    analyze_parser.add_argument("path", help="Image path (never printed).")
    analyze_parser.add_argument("--profile", required=True)
    analyze_parser.add_argument("--layout", default=None)
    analyze_parser.add_argument("--max-side", type=int, default=None)
    analyze_parser.add_argument("--max-megapixels", type=float, default=None)

    calibrate_parser = subparsers.add_parser(
        "calibrate",
        help="Print per-metric distributions for a folder.",
    )
    calibrate_parser.add_argument("folder")
    calibrate_parser.add_argument("--profile", required=True)
    calibrate_parser.add_argument("--layout", default=None)
    calibrate_parser.add_argument("--max-side", type=int, default=None)
    calibrate_parser.add_argument("--max-megapixels", type=float, default=None)
    calibrate_parser.add_argument(
        "--labels",
        default=None,
        help=(
            "Optional labels CSV for metric separation; defaults to labels.csv "
            "in the calibrated folder when present."
        ),
    )
    calibrate_parser.add_argument(
        "--apply",
        action="store_true",
        help="Mark the selected layout CALIBRATED and write the threshold file.",
    )
    calibrate_parser.add_argument(
        "--output",
        default=None,
        help="Optional alternate threshold file; requires --apply.",
    )

    evaluate_parser = subparsers.add_parser(
        "evaluate",
        help="Compare predictions with a local labels CSV.",
    )
    evaluate_parser.add_argument("folder")
    evaluate_parser.add_argument("--labels", required=True)
    evaluate_parser.add_argument("--profile", required=True)
    evaluate_parser.add_argument("--layout", default=None)
    evaluate_parser.add_argument("--max-side", type=int, default=None)
    evaluate_parser.add_argument("--max-megapixels", type=float, default=None)

    args = parser.parse_args(argv)

    try:
        if args.command == "analyze":
            result = analyze_path(
                args.path,
                args.profile,
                args.layout,
                max_side=args.max_side,
                max_megapixels=args.max_megapixels,
            )
            _print_json(result)
            return _analyze_exit_code(result)

        if args.command == "calibrate":
            result = calibrate_folder(
                args.folder,
                args.profile,
                args.layout,
                max_side=args.max_side,
                max_megapixels=args.max_megapixels,
                apply=args.apply,
                output=args.output,
                labels=args.labels,
            )
            _print_json(result)
            return 0 if result.get("error_category") is None else 2

        if args.command == "evaluate":
            result = evaluate_folder(
                args.folder,
                args.labels,
                args.profile,
                args.layout,
                max_side=args.max_side,
                max_megapixels=args.max_megapixels,
            )
            _print_json(result)
            return 0 if result.get("error_category") is None else 2

        parser.error("unknown command")
        return 2
    except Exception:
        # Never expose a traceback, file path, or image content.
        _print_json(
            _command_error(
                command=str(args.command),
                profile=str(getattr(args, "profile", "")),
                layout=getattr(args, "layout", None),
                category="processing_error",
                reason_codes=[],
                start=perf_counter(),
            )
        )
        return 4
