"""Phase 2.1 calibration dataset preparation and annotation validation.

This tool is deliberately separate from the production decision path.  It
never imports the analyzer and never changes thresholds, profiles or output
schema.  It prepares opaque, group-aware calibration manifests and blind
annotation sheets, then validates them.

Only the Python standard library is used here.  Wilson intervals are
implemented directly so the later evaluation step does not need scipy.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import random
import re
import shutil
import sys
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Sequence


TOOL_VERSION = "phase2.1-calibration/1.0.0"

STATUS_VALUES = (
    "ACCEPTABLE_QUALITY",
    "POOR_QUALITY",
    "NEEDS_RECAPTURE",
)
PROBLEM_VALUES = (
    "LOW_RES",
    "TOO_SMALL",
    "BLURRY",
    "DARK",
    "OVEREXPOSED",
    "UNEVEN_LIGHT",
    "LOW_CONTRAST",
    "GLARE",
    "CROPPED",
    "ROTATED",
    "PERSPECTIVE",
    "NO_DOCUMENT_FOUND",
    "UNREADABLE_IMAGE",
    "NONE",
)
SPLIT_VALUES = ("development", "holdout")
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff"}

MANIFEST_FIELDS = (
    "image_id",
    "relative_path",
    "source_dataset",
    "source_group_id",
    "original_video_or_document_id",
    "split",
    "license_or_permission",
    "notes",
)
ANNOTATION_FIELDS = (
    "image_id",
    "expected_quality",
    "main_problem",
    "secondary_problems",
    "notes",
    "annotator",
)
FORBIDDEN_ANNOTATION_FIELDS = {
    "relative_path",
    "source_dataset",
    "source_group_id",
    "original_video_or_document_id",
    "split",
    "filename",
    "path",
    "video",
    "document_id",
}
OPAQUE_GROUP_RE = re.compile(r"^grp_[0-9a-f]{12,64}$")
OPAQUE_IMAGE_RE = re.compile(r"^img_[0-9]{4,}$")
ANNOTATOR_RE = re.compile(r"^A[0-9]{1,3}$")
FORBIDDEN_TEXT_PATTERNS = (
    re.compile(r"(?i)\b[A-Z]:\\"),
    re.compile(r"(?i)\\\\"),
    re.compile(r"(?i)\busers\b"),
    re.compile(r"(?i)\bsmartdoc\b"),
    re.compile(r"(?i)\.(avi|mp4|mov|mkv|m4v|webm|png|jpe?g|bmp|tiff?)\b"),
)


class CalibrationDataError(ValueError):
    """Raised for invalid calibration input without exposing source details."""


def wilson_interval(successes: int, total: int, z: float = 1.96) -> dict[str, Any] | None:
    """Return a Wilson score interval, or None when there are no observations."""
    if total <= 0:
        return None
    if successes < 0 or successes > total:
        raise ValueError("invalid_proportion")
    proportion = successes / total
    denominator = 1.0 + (z * z) / total
    center = (proportion + (z * z) / (2.0 * total)) / denominator
    margin = (
        z
        * math.sqrt(
            (proportion * (1.0 - proportion) / total)
            + (z * z) / (4.0 * total * total)
        )
        / denominator
    )
    return {
        "low": round(max(0.0, center - margin), 6),
        "high": round(min(1.0, center + margin), 6),
        "method": "wilson",
        "z": z,
    }


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _opaque_id(prefix: str, value: str, length: int = 12) -> str:
    return f"{prefix}_{hashlib.sha256(value.encode('utf-8')).hexdigest()[:length]}"


def _group_id(parent: Path, images_root: Path) -> str:
    if OPAQUE_GROUP_RE.match(parent.name):
        return parent.name
    try:
        relative = parent.relative_to(images_root).as_posix()
    except ValueError:
        relative = parent.name
    return _opaque_id("grp", relative)


def _iter_images(root: Path) -> list[Path]:
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
    )


def _deterministic_sample(paths: Sequence[Path], count: int, seed: str) -> list[Path]:
    ordered = sorted(paths, key=lambda path: path.name.lower())
    if count >= len(ordered):
        return ordered
    rng = random.Random(seed)
    chosen: list[Path] = []
    for index in range(count):
        start = (index * len(ordered)) // count
        end = ((index + 1) * len(ordered)) // count - 1
        if end < start:
            end = start
        chosen.append(ordered[rng.randint(start, end)])
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in chosen:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    if len(unique) < count:
        for path in ordered:
            if path not in seen:
                seen.add(path)
                unique.append(path)
                if len(unique) == count:
                    break
    return sorted(unique, key=lambda path: path.name.lower())


def _split_groups(group_ids: Iterable[str], seed: int) -> dict[str, str]:
    ordered = sorted(set(group_ids))
    if len(ordered) < 2:
        return {group_id: "development" for group_id in ordered}
    shuffled = ordered[:]
    random.Random(seed).shuffle(shuffled)
    development_count = max(
        1,
        min(len(shuffled) - 1, math.ceil(len(shuffled) * 0.6)),
    )
    return {
        group_id: ("development" if index < development_count else "holdout")
        for index, group_id in enumerate(shuffled)
    }


def _write_csv(path: Path, fields: Sequence[str], rows: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fields), lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def _read_csv(path: Path, required_fields: Sequence[str]) -> list[dict[str, str]]:
    if not path.is_file():
        raise CalibrationDataError("csv_not_found")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise CalibrationDataError("csv_has_no_header")
        fields = [field.strip() for field in reader.fieldnames]
        if tuple(fields) != tuple(required_fields):
            raise CalibrationDataError("unexpected_csv_fields")
        return [
            {str(key).strip(): (value or "").strip() for key, value in row.items()}
            for row in reader
        ]


def _relative_path(path: Path, path_base: Path) -> str:
    try:
        return path.relative_to(path_base).as_posix()
    except ValueError as exc:
        raise CalibrationDataError("path_outside_base") from exc


def prepare_dataset(
    *,
    images_root: Path,
    selected_dir: Path,
    path_base: Path,
    manifest_path: Path,
    annotations_path: Path,
    summary_path: Path,
    source_dataset: str,
    license_or_permission: str,
    seed: int,
    per_group: int,
    notes: str = "",
    copy_files: bool = True,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Prepare an opaque manifest and a blind annotation sheet."""
    if not images_root.is_dir():
        raise CalibrationDataError("images_root_not_a_directory")
    if per_group <= 0:
        raise CalibrationDataError("per_group_must_be_positive")
    images = _iter_images(images_root)
    if not images:
        raise CalibrationDataError("no_images_found")

    grouped: dict[str, list[Path]] = defaultdict(list)
    for image in images:
        grouped[_group_id(image.parent, images_root)].append(image)

    split_by_group = _split_groups(grouped, seed)
    manifest_rows: list[dict[str, str]] = []
    annotation_rows: list[dict[str, str]] = []
    image_counter = 0
    copied_files = 0

    if copy_files:
        selected_dir.mkdir(parents=True, exist_ok=True)

    for group_id in sorted(grouped):
        sample = _deterministic_sample(
            grouped[group_id],
            per_group,
            f"{seed}:{group_id}",
        )
        for source in sample:
            image_counter += 1
            image_id = f"img_{image_counter:04d}"
            suffix = source.suffix.lower()
            destination = selected_dir / f"{image_id}{suffix}"
            if copy_files:
                if destination.exists() and not overwrite:
                    raise CalibrationDataError("selected_file_already_exists")
                shutil.copyfile(source, destination)
                copied_files += 1
            manifest_rows.append(
                {
                    "image_id": image_id,
                    "relative_path": _relative_path(destination, path_base),
                    "source_dataset": source_dataset,
                    "source_group_id": group_id,
                    "original_video_or_document_id": group_id,
                    "split": split_by_group[group_id],
                    "license_or_permission": license_or_permission,
                    "notes": notes,
                }
            )
            annotation_rows.append(
                {
                    "image_id": image_id,
                    "expected_quality": "",
                    "main_problem": "",
                    "secondary_problems": "",
                    "notes": "",
                    "annotator": "",
                }
            )

    _write_csv(manifest_path, MANIFEST_FIELDS, manifest_rows)
    _write_csv(annotations_path, ANNOTATION_FIELDS, annotation_rows)

    split_counts = Counter(row["split"] for row in manifest_rows)
    summary = {
        "ok": True,
        "tool_version": TOOL_VERSION,
        "source_dataset": source_dataset,
        "seed": seed,
        "per_group": per_group,
        "group_count": len(grouped),
        "selected_count": len(manifest_rows),
        "split_counts": {
            "development": split_counts.get("development", 0),
            "holdout": split_counts.get("holdout", 0),
        },
        "copied_files": copied_files,
        "manifest_sha256": _sha256_file(manifest_path),
        "annotation_sha256": _sha256_file(annotations_path),
    }
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return summary


def _validate_manifest(
    rows: Sequence[dict[str, str]],
    *,
    path_base: Path | None,
) -> tuple[list[str], dict[str, Any]]:
    errors: list[str] = []
    image_ids: list[str] = []
    group_splits: dict[str, set[str]] = defaultdict(set)
    group_ids: set[str] = set()
    split_counts: Counter[str] = Counter()
    missing_files = 0

    for index, row in enumerate(rows, start=2):
        image_id = row["image_id"]
        image_ids.append(image_id)
        if not OPAQUE_IMAGE_RE.match(image_id):
            errors.append(f"manifest_row_{index}:invalid_image_id")
        relative = row["relative_path"]
        pure = PurePosixPath(relative)
        if (
            not relative
            or pure.is_absolute()
            or "\\" in relative
            or any(part in ("", ".", "..") for part in pure.parts)
            or ":" in pure.parts[0]
        ):
            errors.append(f"manifest_row_{index}:unsafe_relative_path")
        elif path_base is not None:
            candidate = (path_base / Path(*pure.parts)).resolve()
            base_resolved = path_base.resolve()
            if not candidate.is_relative_to(base_resolved):
                errors.append(f"manifest_row_{index}:path_escapes_base")
            elif not candidate.is_file():
                missing_files += 1
                errors.append(f"manifest_row_{index}:selected_file_missing")
        group_id = row["source_group_id"]
        group_ids.add(group_id)
        if not OPAQUE_GROUP_RE.match(group_id):
            errors.append(f"manifest_row_{index}:non_opaque_group_id")
        split = row["split"]
        if split not in SPLIT_VALUES:
            errors.append(f"manifest_row_{index}:invalid_split")
        else:
            split_counts[split] += 1
            group_splits[group_id].add(split)
        if not row["source_dataset"]:
            errors.append(f"manifest_row_{index}:missing_source_dataset")
        if not row["license_or_permission"]:
            errors.append(f"manifest_row_{index}:missing_license")
        if row["original_video_or_document_id"] != group_id:
            errors.append(f"manifest_row_{index}:original_id_mismatch")

    duplicates = [image_id for image_id, count in Counter(image_ids).items() if count > 1]
    if duplicates:
        errors.append("manifest_duplicate_image_ids")
    for group_id, splits in group_splits.items():
        if len(splits) != 1:
            errors.append(f"group_crosses_split:{group_id}")

    counts = {
        "manifest_rows": len(rows),
        "unique_image_ids": len(set(image_ids)),
        "groups": len(group_ids),
        "development_rows": split_counts.get("development", 0),
        "holdout_rows": split_counts.get("holdout", 0),
        "missing_files": missing_files,
    }
    return errors, counts


def _problem_set(value: str) -> set[str]:
    if not value:
        return set()
    return {part.strip() for part in value.split(";") if part.strip()}


def _check_forbidden_text(value: str) -> bool:
    return any(pattern.search(value) for pattern in FORBIDDEN_TEXT_PATTERNS)


def _validate_annotations(
    rows: Sequence[dict[str, str]],
    manifest_rows: Sequence[dict[str, str]],
    *,
    require_complete: bool,
) -> tuple[list[str], dict[str, Any]]:
    errors: list[str] = []
    annotation_ids: list[str] = []
    manifest_ids = {row["image_id"] for row in manifest_rows}
    nonempty = 0
    complete = 0

    for index, row in enumerate(rows, start=2):
        image_id = row["image_id"]
        annotation_ids.append(image_id)
        if not OPAQUE_IMAGE_RE.match(image_id):
            errors.append(f"annotation_row_{index}:invalid_image_id")

        quality = row["expected_quality"]
        main_problem = row["main_problem"]
        secondary = _problem_set(row["secondary_problems"])
        annotator = row["annotator"]

        if quality:
            nonempty += 1
            if quality not in STATUS_VALUES:
                errors.append(f"annotation_row_{index}:invalid_expected_quality")
        if main_problem and main_problem not in PROBLEM_VALUES:
            errors.append(f"annotation_row_{index}:invalid_main_problem")
        if any(problem not in PROBLEM_VALUES for problem in secondary):
            errors.append(f"annotation_row_{index}:invalid_secondary_problem")
        if "NONE" in secondary and len(secondary) > 1:
            errors.append(f"annotation_row_{index}:none_must_be_alone")
        if main_problem == "NONE" and secondary:
            errors.append(f"annotation_row_{index}:none_with_secondary")
        if annotator and not ANNOTATOR_RE.match(annotator):
            errors.append(f"annotation_row_{index}:invalid_annotator_code")

        fields_to_scan = (
            quality,
            main_problem,
            row["secondary_problems"],
            row["notes"],
            annotator,
        )
        if any(_check_forbidden_text(value) for value in fields_to_scan):
            errors.append(f"annotation_row_{index}:forbidden_text")

        if require_complete:
            if not quality or not main_problem or not annotator:
                errors.append(f"annotation_row_{index}:incomplete")
            else:
                complete += 1

    duplicates = [
        image_id for image_id, count in Counter(annotation_ids).items() if count > 1
    ]
    if duplicates:
        errors.append("annotation_duplicate_image_ids")
    annotation_id_set = set(annotation_ids)
    if manifest_ids - annotation_id_set:
        errors.append("manifest_images_missing_from_annotations")
    if annotation_id_set - manifest_ids:
        errors.append("annotations_not_in_manifest")

    counts = {
        "annotation_rows": len(rows),
        "unique_image_ids": len(annotation_id_set),
        "nonempty_quality_labels": nonempty,
        "complete_rows": complete,
    }
    return errors, counts


def validate_dataset(
    *,
    manifest_path: Path,
    annotations_path: Path,
    path_base: Path | None,
    require_complete: bool = False,
) -> dict[str, Any]:
    """Validate manifest, split integrity and the blind annotation sheet."""
    errors: list[str] = []
    try:
        manifest_rows = _read_csv(manifest_path, MANIFEST_FIELDS)
        annotation_rows = _read_csv(annotations_path, ANNOTATION_FIELDS)
    except CalibrationDataError as exc:
        return {
            "ok": False,
            "tool_version": TOOL_VERSION,
            "errors": [str(exc)],
            "checks": {},
            "counts": {},
        }

    if any(
        field.strip().lower() in FORBIDDEN_ANNOTATION_FIELDS
        for field in ANNOTATION_FIELDS
    ):
        errors.append("annotation_header_contains_source_fields")

    manifest_errors, manifest_counts = _validate_manifest(
        manifest_rows,
        path_base=path_base,
    )
    annotation_errors, annotation_counts = _validate_annotations(
        annotation_rows,
        manifest_rows,
        require_complete=require_complete,
    )
    errors.extend(manifest_errors)
    errors.extend(annotation_errors)

    checks = {
        "manifest_fields": not any(e.startswith("unexpected_csv_fields") for e in errors),
        "annotation_fields": not any(
            e.startswith("annotation_header") or e.startswith("unexpected_csv_fields")
            for e in errors
        ),
        "unique_image_ids": "manifest_duplicate_image_ids" not in errors
        and "annotation_duplicate_image_ids" not in errors,
        "all_selected_images_present": manifest_counts.get("missing_files", 1) == 0,
        "all_manifest_ids_annotated": "manifest_images_missing_from_annotations"
        not in errors,
        "no_extra_annotations": "annotations_not_in_manifest" not in errors,
        "allowed_values_only": not any(
            "invalid_expected_quality" in error
            or "invalid_main_problem" in error
            or "invalid_secondary_problem" in error
            or "none_must_be_alone" in error
            or "none_with_secondary" in error
            or "invalid_annotator_code" in error
            for error in errors
        ),
        "no_group_crosses_split": not any(
            error.startswith("group_crosses_split") for error in errors
        ),
        "no_source_in_annotation": not any(
            "forbidden_text" in error or "annotation_header" in error
            for error in errors
        ),
        "complete_labels": not require_complete
        or annotation_counts.get("complete_rows", 0) == len(annotation_rows),
    }
    return {
        "ok": not errors,
        "tool_version": TOOL_VERSION,
        "checks": checks,
        "counts": {**manifest_counts, **annotation_counts},
        "errors": errors,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Phase 2.1 calibration preparation and annotation validation."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--images", required=True)
    prepare.add_argument("--selected-dir", required=True)
    prepare.add_argument("--path-base", required=True)
    prepare.add_argument("--manifest", required=True)
    prepare.add_argument("--annotations", required=True)
    prepare.add_argument("--summary", required=True)
    prepare.add_argument("--source-dataset", required=True)
    prepare.add_argument("--license", required=True)
    prepare.add_argument("--seed", type=int, required=True)
    prepare.add_argument("--per-group", type=int, default=10)
    prepare.add_argument("--notes", default="")
    prepare.add_argument("--no-copy", action="store_true")
    prepare.add_argument("--overwrite", action="store_true")

    validate = subparsers.add_parser("validate")
    validate.add_argument("--manifest", required=True)
    validate.add_argument("--annotations", required=True)
    validate.add_argument("--path-base")
    validate.add_argument("--require-complete", action="store_true")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            summary = prepare_dataset(
                images_root=Path(args.images),
                selected_dir=Path(args.selected_dir),
                path_base=Path(args.path_base),
                manifest_path=Path(args.manifest),
                annotations_path=Path(args.annotations),
                summary_path=Path(args.summary),
                source_dataset=args.source_dataset,
                license_or_permission=args.license,
                seed=args.seed,
                per_group=args.per_group,
                notes=args.notes,
                copy_files=not args.no_copy,
                overwrite=args.overwrite,
            )
        else:
            summary = validate_dataset(
                manifest_path=Path(args.manifest),
                annotations_path=Path(args.annotations),
                path_base=Path(args.path_base) if args.path_base else None,
                require_complete=args.require_complete,
            )
    except CalibrationDataError as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "tool_version": TOOL_VERSION,
                    "error": str(exc),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary.get("ok") else 3


if __name__ == "__main__":
    sys.exit(main())
