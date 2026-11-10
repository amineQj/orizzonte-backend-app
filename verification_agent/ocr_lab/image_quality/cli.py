"""Command-line interface: analyze, calibrate, evaluate."""

from __future__ import annotations

import argparse
import csv
import json
import statistics
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
from .profiles import get_profile_config


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


def calibrate_folder(
    folder: str | Path,
    profile: str,
    layout: str | None = None,
    *,
    max_side: int | None = None,
    max_megapixels: float | None = None,
) -> dict[str, Any]:
    start = perf_counter()
    try:
        get_profile_config(profile, layout)
    except ProfileError as exc:
        return _command_error(
            command="calibrate",
            profile=profile,
            layout=layout,
            category=exc.category,
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

    files = sorted(path for path in folder_path.iterdir() if path.is_file())
    numeric_values: dict[str, list[float]] = defaultdict(list)
    unknown_counts: Counter[str] = Counter()
    failure_counts: Counter[str] = Counter()
    analyzed_count = 0

    for path in files:
        result = analyze_path(
            path,
            profile,
            layout,
            max_side=max_side,
            max_megapixels=max_megapixels,
        )
        if result.get("error_category") is not None:
            failure_counts[str(result["error_category"])] += 1
            continue
        analyzed_count += 1
        for metric_name, metric in result["metrics"].items():
            if metric.get("nature") == "unknown":
                unknown_counts[metric_name] += 1
                continue
            _collect_numeric(metric_name, metric.get("value"), numeric_values)

    return {
        "schema_version": SCHEMA_VERSION,
        "algorithm_version": ALGORITHM_VERSION,
        "command": "calibrate",
        "profile": profile,
        "layout": layout,
        "image_count": len(files),
        "analyzed_count": analyzed_count,
        "failed_count": len(files) - analyzed_count,
        "failure_categories": dict(sorted(failure_counts.items())),
        "unknown_metrics": dict(sorted(unknown_counts.items())),
        "metrics": {
            name: _summary(values) for name, values in sorted(numeric_values.items())
        },
        "thresholds_calibrated": False,
        "thresholds_modified": False,
        "processing_time_ms": _elapsed_ms(start),
    }


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
) -> dict[str, Any]:
    start = perf_counter()
    try:
        get_profile_config(profile, layout)
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

    confusion: dict[str, dict[str, int]] = {
        expected: {predicted: 0 for predicted in STATUSES} for expected in STATUSES
    }
    unreadable_count = 0
    for filename, expected in rows:
        result = analyze_path(
            folder_path / filename,
            profile,
            layout,
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
        "thresholds_calibrated": False,
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
