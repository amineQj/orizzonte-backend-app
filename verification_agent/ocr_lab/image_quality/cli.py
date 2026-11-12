"""Command-line interface: analyze, calibrate, evaluate."""

from __future__ import annotations

import argparse
import copy
import csv
import json
import random
import statistics
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from time import perf_counter
from typing import Any, Sequence

from .analyzer import analyze_path
from .errors import ProfileError
from .metrics.boundaries import REJECTION_REASON_KEYS
from .output_schema import (
    ALGORITHM_VERSION,
    SCHEMA_VERSION,
    STATUSES,
    STATUS_ACCEPTABLE,
    STATUS_POOR,
    STATUS_RECAPTURE,
)
from .profiles import get_profile_config, load_thresholds
from .profiles import thresholds_path as default_thresholds_path
from .tools.phase2_1_calibration import wilson_interval

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
        "exploratory": len(acceptable) < 30 or len(recapture) < 30,
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


def _proportion(successes: int, total: int) -> dict[str, Any]:
    interval = wilson_interval(successes, total)
    return {
        "numerator": int(successes),
        "denominator": int(total),
        "rate": round(successes / total, 6) if total else None,
        "wilson_95": interval,
    }


def _gate_metrics(pairs: list[tuple[str, str]]) -> dict[str, Any]:
    """Gate metrics with human NEEDS_RECAPTURE as the positive class."""

    good_count = sum(
        1 for human, _predicted in pairs if human in (STATUS_ACCEPTABLE, STATUS_POOR)
    )
    bad_count = sum(1 for human, _predicted in pairs if human == STATUS_RECAPTURE)
    policies: dict[str, Any] = {}
    for name, pass_statuses in (
        ("pass_if_poor", frozenset({STATUS_ACCEPTABLE, STATUS_POOR})),
        ("strict", frozenset({STATUS_ACCEPTABLE})),
    ):
        good_passed = sum(
            1
            for human, predicted in pairs
            if human in (STATUS_ACCEPTABLE, STATUS_POOR) and predicted in pass_statuses
        )
        bad_passed = sum(
            1
            for human, predicted in pairs
            if human == STATUS_RECAPTURE and predicted in pass_statuses
        )
        good_blocked = good_count - good_passed
        bad_blocked = bad_count - bad_passed
        sensitivity = bad_blocked / bad_count if bad_count else None
        specificity = good_passed / good_count if good_count else None
        youden_j = (
            sensitivity + specificity - 1.0
            if sensitivity is not None and specificity is not None
            else None
        )
        policies[name] = {
            "pass_statuses": sorted(pass_statuses),
            "counts": {
                "good_passed": good_passed,
                "good_blocked": good_blocked,
                "bad_passed": bad_passed,
                "bad_blocked": bad_blocked,
            },
            "rates": {
                "good_passed": _proportion(good_passed, good_count),
                "good_blocked": _proportion(good_blocked, good_count),
                "bad_passed": _proportion(bad_passed, bad_count),
                "bad_blocked": _proportion(bad_blocked, bad_count),
            },
            "false_positive_rate": _proportion(good_blocked, good_count),
            "false_negative_rate": _proportion(bad_passed, bad_count),
            "sensitivity": _proportion(bad_blocked, bad_count),
            "specificity": _proportion(good_passed, good_count),
            "youden_j": round(youden_j, 6) if youden_j is not None else None,
        }
    return {
        "positive_label": STATUS_RECAPTURE,
        "good_labels": [STATUS_ACCEPTABLE, STATUS_POOR],
        "good_count": good_count,
        "bad_count": bad_count,
        "policies": policies,
    }


def _row_group(filename: str, group: str | None) -> tuple[str, str]:
    if group:
        return group, "labels.csv:group"
    stem = Path(filename).stem
    if stem:
        return stem, "filename_stem"
    return filename, "filename"


def _grouped_folds(
    groups: list[str],
    k: int,
    seed: int,
) -> list[tuple[list[int], list[int]]]:
    """Return deterministic grouped K folds; a group is never split."""

    unique = sorted(set(groups))
    random.Random(seed).shuffle(unique)
    fold_groups = [set() for _ in range(k)]
    for index, group in enumerate(unique):
        fold_groups[index % k].add(group)
    folds: list[tuple[list[int], list[int]]] = []
    all_indices = set(range(len(groups)))
    for test_groups in fold_groups:
        test_indices = sorted(
            index for index, group in enumerate(groups) if group in test_groups
        )
        train_indices = sorted(all_indices - set(test_indices))
        folds.append((train_indices, test_indices))
    return folds


def _stratified_folds(
    labels: list[str],
    k: int,
    seed: int,
) -> list[tuple[list[int], list[int]]]:
    by_class: dict[str, list[int]] = defaultdict(list)
    for index, label in enumerate(labels):
        by_class[label].append(index)
    rng = random.Random(seed)
    test_sets = [set() for _ in range(k)]
    for label in sorted(by_class):
        indices = list(by_class[label])
        rng.shuffle(indices)
        for offset, index in enumerate(indices):
            test_sets[offset % k].add(index)
    all_indices = set(range(len(labels)))
    return [
        (sorted(all_indices - test_set), sorted(test_set)) for test_set in test_sets
    ]


_DERIVED_THRESHOLD_PATHS: dict[str, tuple[str, ...]] = {
    "sharpness_laplacian": ("blur", "laplacian_variance_poor"),
    "sharpness_tenengrad": ("blur", "tenengrad_poor"),
    "contrast.p5": ("contrast", "p5_min"),
    "contrast.rms_contrast": ("contrast", "rms_min"),
    "document_boundary.edge_density": ("boundary", "no_document_min_edge_density"),
}


def _derive_thresholds(
    base_thresholds: dict[str, Any],
    *,
    profile: str,
    layout: str,
    training_rows: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, float]]:
    """Derive in-memory soft thresholds from training rows only."""

    labeled_values: dict[str, dict[str, list[float]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for row in training_rows:
        result = row.get("result")
        if not isinstance(result, dict) or result.get("error_category") is not None:
            continue
        label = str(row.get("label", ""))
        if label not in (STATUS_ACCEPTABLE, STATUS_RECAPTURE):
            continue
        for metric_name, metric in result.get("metrics", {}).items():
            if metric.get("nature") == "unknown":
                continue
            _collect_numeric(metric_name, metric.get("value"), labeled_values[label])

    table = _metric_separation_table(labeled_values)
    derived = copy.deepcopy(base_thresholds)
    layout_data = derived["profiles"][profile]["layouts"][layout]
    applied: dict[str, float] = {}
    for metric_name, path in _DERIVED_THRESHOLD_PATHS.items():
        separation = table.get(metric_name)
        if separation is None or separation.get("direction") != "higher_is_better":
            continue
        value = float(separation["suggested_threshold"])
        target = layout_data
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        applied[".".join(path)] = value
    return derived, {key: round(applied[key], 6) for key in sorted(applied)}


def _boundary_diagnostics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    detector_used = {label: Counter() for label in STATUSES}
    candidate_rejections = {
        label: Counter({key: 0 for key in REJECTION_REASON_KEYS})
        for label in STATUSES
    }
    for row in rows:
        label = str(row["label"])
        result = row.get("result")
        boundary = None
        if isinstance(result, dict):
            metric = result.get("metrics", {}).get("document_boundary")
            if isinstance(metric, dict):
                boundary = metric.get("value")
        if not isinstance(boundary, dict):
            detector_used[label]["not_available"] += 1
            candidate_rejections[label]["not_available"] += 1
            continue
        detector_used[label][str(boundary.get("detector_used", "unknown"))] += 1
        counts = boundary.get("rejection_counts", {})
        if not isinstance(counts, dict):
            counts = {}
        for key in REJECTION_REASON_KEYS:
            candidate_rejections[label][key] += int(counts.get(key, 0))
    return {
        "detector_used": {
            label: dict(sorted(detector_used[label].items())) for label in STATUSES
        },
        "candidate_rejections": {
            label: dict(sorted(candidate_rejections[label].items()))
            for label in STATUSES
        },
    }


def _diagnosis_no_candidate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    diagnosed: list[dict[str, Any]] = []
    rejection_totals: Counter[str] = Counter()
    for row in rows:
        if row.get("label") != STATUS_ACCEPTABLE:
            continue
        result = row.get("result")
        if not isinstance(result, dict):
            continue
        metrics = result.get("metrics")
        if not isinstance(metrics, dict):
            continue
        boundary_metric = metrics.get("document_boundary")
        boundary = (
            boundary_metric.get("value")
            if isinstance(boundary_metric, dict)
            else None
        )
        if (
            not isinstance(boundary, dict)
            or boundary.get("valid") is not False
            or boundary.get("candidate_count") != 0
        ):
            continue
        diagnosed.append(metrics)
        reasons = boundary.get("rejection_counts")
        if isinstance(reasons, dict):
            rejection_totals.update(
                {
                    str(reason): int(count)
                    for reason, count in reasons.items()
                    if isinstance(count, int) and count > 0
                }
            )

    def mean_metric(metric_name: str, value_name: str | None = None) -> float | None:
        values: list[float] = []
        for metrics in diagnosed:
            metric = metrics.get(metric_name)
            value = metric.get("value") if isinstance(metric, dict) else None
            if value_name is not None and isinstance(value, dict):
                value = value.get(value_name)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                values.append(float(value))
        return round(statistics.mean(values), 5) if values else None

    top_reasons = sorted(
        rejection_totals.items(),
        key=lambda item: (-item[1], item[0]),
    )[:3]
    return {
        "count": len(diagnosed),
        "mean_edge_density": mean_metric("document_boundary", "edge_density"),
        "mean_contrast_p5": mean_metric("contrast", "p5"),
        "mean_brightness": mean_metric("brightness"),
        "top_rejection_reasons": [
            {"reason": reason, "count": count} for reason, count in top_reasons
        ],
    }


_REASON_METRIC_PATHS: dict[str, tuple[tuple[str, ...], ...]] = {
    "BLURRY": (("sharpness_laplacian",), ("sharpness_tenengrad",)),
    "CROPPED": (
        ("document_boundary", "area_ratio"),
        ("document_boundary", "border_sides_touching"),
    ),
    "DARK": (("brightness",), ("clipping", "dark_fraction")),
    "GLARE": (
        ("glare", "suspected_fraction"),
        ("glare", "largest_component_fraction"),
        ("glare", "near_saturated_fraction"),
    ),
    "LOW_CONTRAST": (
        ("contrast", "p5"),
        ("contrast", "p5_p95_range"),
        ("contrast", "rms_contrast"),
    ),
    "LOW_RES": (
        ("resolution_document", "width_px"),
        ("resolution_document", "height_px"),
        ("resolution_document", "megapixels"),
    ),
    "NO_DOCUMENT_FOUND": (
        ("document_boundary", "edge_density"),
        ("document_boundary", "no_document_signals"),
        ("document_boundary", "candidate_count"),
    ),
    "OVEREXPOSED": (
        ("brightness",),
        ("clipping", "bright_fraction"),
        ("contrast", "p5"),
        ("contrast", "rms_contrast"),
    ),
    "PERSPECTIVE": (("perspective", "ratio"),),
    "ROTATED": (("rotation", "angle_deg"),),
    "TOO_SMALL": (
        ("resolution_original", "width_px"),
        ("resolution_original", "height_px"),
        ("resolution_original", "megapixels"),
    ),
    "UNEVEN_LIGHT": (("uneven_illumination", "ratio"),),
    "UNREADABLE_IMAGE": (),
}


def _reason_codes_by_label(rows: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, dict[str, Any]] = {
        label: {
            "count": 0,
            "reason_counts": Counter(),
            "metric_values": defaultdict(lambda: defaultdict(list)),
        }
        for label in STATUSES
    }

    for row in rows:
        label = str(row.get("label", ""))
        if label not in grouped:
            continue
        group = grouped[label]
        group["count"] += 1
        result = row.get("result")
        if not isinstance(result, dict):
            continue
        reason_codes = result.get("reason_codes")
        if not isinstance(reason_codes, list):
            continue
        metrics = result.get("metrics")
        if not isinstance(metrics, dict):
            metrics = {}
        for reason_code in set(reason_codes):
            if not isinstance(reason_code, str):
                continue
            group["reason_counts"][reason_code] += 1
            for path in _REASON_METRIC_PATHS.get(reason_code, ()):
                metric: Any = metrics.get(path[0])
                metric_value = metric.get("value") if isinstance(metric, dict) else None
                for part in path[1:]:
                    if not isinstance(metric_value, dict):
                        metric_value = None
                        break
                    metric_value = metric_value.get(part)
                if isinstance(metric_value, (int, float)) and not isinstance(
                    metric_value, bool
                ):
                    group["metric_values"][reason_code][".".join(path)].append(
                        float(metric_value)
                    )

    payload: dict[str, Any] = {}
    for label in STATUSES:
        group = grouped[label]
        means_by_reason: dict[str, dict[str, float | None]] = {}
        for reason_code in sorted(group["reason_counts"]):
            means_by_reason[reason_code] = {
                metric_name: (
                    round(
                        statistics.mean(
                            group["metric_values"][reason_code].get(metric_name, [])
                        ),
                        5,
                    )
                    if group["metric_values"][reason_code].get(metric_name)
                    else None
                )
                for metric_name in sorted(
                    ".".join(path)
                    for path in _REASON_METRIC_PATHS.get(reason_code, ())
                )
            }
        payload[label] = {
            "count": group["count"],
            "reason_code_counts": dict(sorted(group["reason_counts"].items())),
            "mean_metrics_by_reason": means_by_reason,
        }
    return payload


class _WhatIfError(ValueError):
    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


def _parse_what_if_value(raw: str) -> Any:
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


def _resolve_what_if_target(
    data: dict[str, Any],
    parts: list[str],
) -> tuple[dict[str, Any], str] | None:
    target: Any = data
    for part in parts[:-1]:
        if not isinstance(target, dict) or part not in target:
            return None
        target = target[part]
    last = parts[-1]
    if not isinstance(target, dict) or last not in target:
        return None
    return target, last


def _apply_what_if(
    data: dict[str, Any],
    assignments: Sequence[str] | None,
    *,
    profile: str | None = None,
    layout: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    updated = copy.deepcopy(data)
    applied: dict[str, Any] = {}
    layout_prefix = (
        ["profiles", profile, "layouts", layout]
        if profile is not None and layout is not None
        else None
    )
    for assignment in assignments or ():
        if "=" not in assignment:
            raise _WhatIfError("invalid_what_if")
        key, raw_value = assignment.split("=", 1)
        key = key.strip()
        raw_value = raw_value.strip()
        if not key or not raw_value:
            raise _WhatIfError("invalid_what_if")
        parts = key.split(".")
        resolved = _resolve_what_if_target(updated, parts)
        if resolved is None and layout_prefix is not None:
            resolved = _resolve_what_if_target(updated, [*layout_prefix, *parts])
        if (
            resolved is None
            and layout_prefix is not None
            and parts == ["boundary", "no_document_severity"]
        ):
            layout_target = _resolve_what_if_target(updated, layout_prefix)
            if layout_target is not None:
                layout_data = layout_target[0][layout_target[1]]
                boundary = layout_data.get("boundary")
                if isinstance(boundary, dict):
                    boundary.setdefault("no_document_severity", "critical")
                    resolved = (boundary, "no_document_severity")
        if resolved is None:
            raise _WhatIfError("unknown_what_if_key")
        target, last = resolved
        old_value = target[last]
        new_value = _parse_what_if_value(raw_value)
        if isinstance(old_value, bool):
            valid_type = isinstance(new_value, bool)
        elif isinstance(old_value, (int, float)):
            valid_type = isinstance(new_value, (int, float)) and not isinstance(
                new_value, bool
            )
        elif isinstance(old_value, str):
            valid_type = isinstance(new_value, str)
        elif isinstance(old_value, list):
            valid_type = isinstance(new_value, list)
        else:
            valid_type = True
        if not valid_type:
            raise _WhatIfError("invalid_what_if_value")
        target[last] = new_value
        applied[key] = new_value
    return updated, {key: applied[key] for key in sorted(applied)}


def _cross_validation(
    evaluated: list[dict[str, Any]],
    *,
    k: int,
    seed: int,
    thresholds: dict[str, Any],
    profile: str,
    layout: str,
    max_side: int | None,
    max_megapixels: float | None,
    in_sample_gate: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    labels = [str(row["label"]) for row in evaluated]
    class_counts = Counter(labels)
    for label in STATUSES:
        if class_counts.get(label, 0) < k:
            return None, {
                "category": "insufficient_cv_samples",
                "class_counts": {
                    status: int(class_counts.get(status, 0)) for status in STATUSES
                },
                "k": k,
            }

    explicit_groups = any(
        row.get("group_source") == "labels.csv:group" for row in evaluated
    )
    groups = [str(row["group"]) for row in evaluated]
    unique_groups = set(groups)
    warnings: list[str] = []
    folds: list[tuple[list[int], list[int]]]
    group_count: int | None
    group_leakage: bool | None
    if explicit_groups:
        if len(unique_groups) < 2:
            return None, {
                "category": "insufficient_cv_groups",
                "group_count": len(unique_groups),
                "k": k,
            }
        if len(unique_groups) < k:
            return None, {
                "category": "insufficient_cv_groups",
                "group_count": len(unique_groups),
                "k": k,
            }
        group_source = (
            "labels.csv:group"
            if all(row.get("group_source") == "labels.csv:group" for row in evaluated)
            else "mixed_group_column_and_filename_stem"
        )
        folds = _grouped_folds(groups, k, seed)
        group_count = len(unique_groups)
        group_leakage = False
    elif len(unique_groups) >= k:
        group_source = "filename_stem"
        folds = _grouped_folds(groups, k, seed)
        group_count = len(unique_groups)
        group_leakage = False
    else:
        group_source = "stratified_folds"
        warnings.append("cv_group_fallback: stratified_folds")
        folds = _stratified_folds(labels, k, seed)
        group_count = None
        group_leakage = None

    oof_predictions: list[tuple[int, str]] = []
    derived_by_fold: list[dict[str, Any]] = []
    fold_sizes: list[int] = []
    for fold_index, (train_indices, test_indices) in enumerate(folds, start=1):
        training_rows = [evaluated[index] for index in train_indices]
        derived, applied = _derive_thresholds(
            thresholds,
            profile=profile,
            layout=layout,
            training_rows=training_rows,
        )
        derived_by_fold.append(
            {
                "fold": fold_index,
                "train_count": len(train_indices),
                "test_count": len(test_indices),
                "thresholds": applied,
            }
        )
        fold_sizes.append(len(test_indices))
        for index in test_indices:
            row = evaluated[index]
            path = row.get("path")
            if path is None:
                predicted = STATUS_RECAPTURE
            else:
                result = analyze_path(
                    path,
                    profile,
                    layout,
                    thresholds=derived,
                    max_side=max_side,
                    max_megapixels=max_megapixels,
                )
                predicted = str(result.get("status", STATUS_RECAPTURE))
                if predicted not in STATUSES:
                    predicted = STATUS_RECAPTURE
            oof_predictions.append((index, predicted))

    ordered_predictions = dict(oof_predictions)
    pooled = _gate_metrics(
        [
            (str(row["label"]), ordered_predictions[index])
            for index, row in enumerate(evaluated)
        ]
    )
    return (
        {
            "k": k,
            "seed": seed,
            "group_source": group_source,
            "group_count": group_count,
            "group_leakage": group_leakage,
            "fold_sizes": fold_sizes,
            "warnings": sorted(set(warnings)),
            "in_sample_gate": in_sample_gate,
            "pooled_out_of_fold_gate": pooled,
            "derived_thresholds_by_fold": derived_by_fold,
        },
        None,
    )


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
    class_counts: dict[str, int] | None = None,
    group_count: int = 0,
) -> dict[str, Any]:
    updated = copy.deepcopy(data)
    try:
        layout_data = updated["profiles"][profile]["layouts"][layout]
    except (KeyError, TypeError) as exc:
        raise ProfileError("invalid_configuration") from exc
    if not isinstance(layout_data, dict):
        raise ProfileError("invalid_configuration")
    if "thresholds_calibrated" in updated:
        updated["thresholds_calibrated"] = False
    if "thresholds_calibrated" in layout_data:
        layout_data["thresholds_calibrated"] = False
    status_counts = {
        status: int((class_counts or {}).get(status, 0)) for status in STATUSES
    }
    layout_data["calibration_status"] = "PROVISIONAL"
    layout_data["calibration"] = {
        "method": "aggregate_distribution_review",
        "sample_count": int(image_count),
        "analyzed_count": int(analyzed_count),
        "failed_count": int(failed_count),
    }
    layout_data["calibration_metadata"] = {
        "method": "aggregate_distribution_review",
        "sample_count": int(image_count),
        "analyzed_count": int(analyzed_count),
        "failed_count": int(failed_count),
        "class_counts": status_counts,
        "group_count": int(group_count),
        "status": "PROVISIONAL",
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
    label_rows: list[tuple[str, str, str | None]] | None = None
    label_error: str | None = None
    if labels is not None or labels_source.is_file():
        label_rows, label_error = _read_labels(labels_source)

    path_to_label: dict[Path, str] = {}
    if label_rows:
        by_name = {path.name: path for path in files}
        by_normalized_name: dict[str, list[Path]] = defaultdict(list)
        for path in files:
            by_normalized_name[_normalize_filename(path.name)].append(path)
        for filename, expected, _group in label_rows:
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
    analyzed_label_counts: Counter[str] = Counter()
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
        if expected in STATUSES:
            analyzed_label_counts[expected] += 1
        for metric_name, metric in result["metrics"].items():
            if metric.get("nature") == "unknown":
                unknown_counts[metric_name] += 1
                continue
            value = metric.get("value")
            _collect_numeric(metric_name, value, numeric_values)
            if expected in (STATUS_ACCEPTABLE, STATUS_RECAPTURE):
                _collect_numeric(metric_name, value, labeled_values[expected])

    failed_count = len(files) - analyzed_count
    label_class_counts = Counter(
        expected for _filename, expected, _group in (label_rows or [])
    )
    label_group_count = len(
        {
            _row_group(_filename, group)[0]
            for _filename, _expected, group in (label_rows or [])
        }
    )
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
                    class_counts=dict(label_class_counts),
                    group_count=label_group_count,
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

    metric_separation_n_by_class = {
        status: int(analyzed_label_counts.get(status, 0)) for status in STATUSES
    }
    metric_separation_table = _metric_separation_table(labeled_values)
    if any(count < 30 for count in metric_separation_n_by_class.values()):
        for entry in metric_separation_table.values():
            entry["exploratory"] = True
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
        "metric_separation": metric_separation_table,
        "metric_separation_scope": "analyzed_images_only",
        "metric_separation_n_by_class": metric_separation_n_by_class,
        "thresholds_calibrated": calibrated,
        "thresholds_modified": modified,
        "thresholds_written": written,
        "processing_time_ms": _elapsed_ms(start),
    }
    if any(count < 30 for count in metric_separation_n_by_class.values()):
        payload["selection_bias_warning"] = (
            "At least one human class has n < 30 analysed images; "
            "metric separation suggestions are exploratory."
        )
    if label_error is not None:
        payload["metric_separation_error"] = label_error
    if calibration_skipped is not None:
        payload["calibration_skipped"] = calibration_skipped
    return payload


def _read_labels(
    csv_path: Path,
) -> tuple[list[tuple[str, str, str | None]] | None, str | None]:
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
            rows: list[tuple[str, str, str | None]] = []
            for row in reader:
                lowered = {str(key).strip().lower(): value for key, value in row.items()}
                filename = str(lowered.get("filename", "")).strip()
                label = str(lowered.get("label", "")).strip()
                group_value = str(lowered.get("group", "")).strip()
                group = group_value or None
                if not filename or label not in STATUSES:
                    return None, "invalid_labels"
                if Path(filename).name != filename:
                    return None, "invalid_labels"
                rows.append((filename, label, group))
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
    cv: int | None = None,
    seed: int = 20261110,
    what_if: Sequence[str] | None = None,
) -> dict[str, Any]:
    start = perf_counter()
    source_path = (
        Path(thresholds_path) if thresholds_path is not None else default_thresholds_path()
    )
    try:
        thresholds = load_thresholds(source_path)
        base_cfg = get_profile_config(profile, layout, thresholds)
        defaulted_optional_what_if = any(
            assignment.partition("=")[0].strip()
            == "boundary.no_document_severity"
            and "no_document_severity" not in base_cfg.data.get("boundary", {})
            for assignment in what_if or ()
        )
        thresholds, applied_what_if = _apply_what_if(
            thresholds,
            what_if,
            profile=base_cfg.profile,
            layout=base_cfg.layout,
        )
        cfg = get_profile_config(profile, layout, thresholds)
    except _WhatIfError as exc:
        return _command_error(
            command="evaluate",
            profile=profile,
            layout=layout,
            category=exc.category,
            reason_codes=[],
            start=start,
        )
    except ProfileError as exc:
        return _command_error(
            command="evaluate",
            profile=profile,
            layout=layout,
            category=exc.category,
            reason_codes=[],
            start=start,
        )

    if cv is not None and cv < 2:
        return _command_error(
            command="evaluate",
            profile=profile,
            layout=layout,
            category="invalid_cv",
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
    warnings: set[str] = set()
    if defaulted_optional_what_if:
        warnings.add("what_if_defaulted_optional_key: boundary.no_document_severity")
    missing_count = 0
    ambiguous_count = 0
    normalized_count = 0
    evaluated: list[dict[str, Any]] = []

    for filename, expected, group in rows:
        resolved_group, group_source = _row_group(filename, group)
        path = by_name.get(filename)
        if path is None:
            matches = by_normalized_name.get(_normalize_filename(filename), [])
            if len(matches) == 1:
                path = matches[0]
                normalized_count += 1
                warnings.add("normalized_filename_match")
            elif len(matches) > 1:
                ambiguous_count += 1
                warnings.add("ambiguous_files")
            else:
                missing_count += 1
                warnings.add("missing_files")

        if path is None:
            unreadable_count += 1
            confusion[expected][STATUS_RECAPTURE] += 1
            evaluated.append(
                {
                    "label": expected,
                    "group": resolved_group,
                    "group_source": group_source,
                    "path": None,
                    "result": None,
                    "predicted": STATUS_RECAPTURE,
                }
            )
            continue

        result = analyze_path(
            path,
            profile,
            cfg.layout,
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
        evaluated.append(
            {
                "label": expected,
                "group": resolved_group,
                "group_source": group_source,
                "path": path,
                "result": result,
                "predicted": predicted,
            }
        )

    expected_acceptable = sum(confusion[STATUS_ACCEPTABLE].values())
    expected_recapture = sum(confusion[STATUS_RECAPTURE].values())
    false_acceptance = confusion[STATUS_RECAPTURE][STATUS_ACCEPTABLE]
    false_rejection = confusion[STATUS_ACCEPTABLE][STATUS_RECAPTURE]
    acceptable_but_poor = confusion[STATUS_ACCEPTABLE][STATUS_POOR]

    no_document_by_label = {status: 0 for status in STATUSES}
    for row in evaluated:
        result = row.get("result")
        if isinstance(result, dict) and "NO_DOCUMENT_FOUND" in result.get(
            "reason_codes", []
        ):
            no_document_by_label[str(row["label"])] += 1

    gate = _gate_metrics(
        [(str(row["label"]), str(row["predicted"])) for row in evaluated]
    )
    gate["no_document_by_label"] = no_document_by_label

    payload: dict[str, Any] = {
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
        "gate": gate,
        "boundary_diagnostics": _boundary_diagnostics(evaluated),
        "diagnosis_no_candidate": _diagnosis_no_candidate(evaluated),
        "reason_codes_by_label": _reason_codes_by_label(evaluated),
        "warnings": sorted(warnings),
        "missing_files_count": missing_count,
        "ambiguous_files_count": ambiguous_count,
        "normalized_matches_count": normalized_count,
        "what_if": applied_what_if,
        "thresholds_calibrated": cfg.thresholds_calibrated,
    }

    if cv is not None:
        cv_payload, cv_error = _cross_validation(
            evaluated,
            k=cv,
            seed=seed,
            thresholds=thresholds,
            profile=cfg.profile,
            layout=cfg.layout,
            max_side=max_side,
            max_megapixels=max_megapixels,
            in_sample_gate=gate,
        )
        if cv_error is not None:
            error = _command_error(
                command="evaluate",
                profile=profile,
                layout=layout,
                category=str(cv_error["category"]),
                reason_codes=[],
                start=start,
            )
            error["cv_error"] = cv_error
            return error
        payload["cross_validation"] = cv_payload
        payload["warnings"] = sorted(
            set(payload["warnings"]) | set(cv_payload.get("warnings", []))
        )
    return payload


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
        help="Mark the selected layout PROVISIONAL and write the threshold file.",
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
    evaluate_parser.add_argument(
        "--cv",
        type=int,
        nargs="?",
        const=5,
        default=None,
        help="Grouped K-fold cross-validation; omit to use the default 5 when the flag is present.",
    )
    evaluate_parser.add_argument(
        "--seed",
        type=int,
        default=20261110,
        help="Deterministic cross-validation seed (default: 20261110).",
    )
    evaluate_parser.add_argument(
        "--what-if",
        action="append",
        default=None,
        metavar="KEY=VALUE",
        help="Apply a threshold override in memory; repeatable and never written.",
    )

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
                cv=args.cv,
                seed=args.seed,
                what_if=args.what_if,
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
