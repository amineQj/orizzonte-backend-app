from __future__ import annotations

import csv
import json
from pathlib import Path

from PIL import Image

from image_quality.tools.phase2_1_calibration import (
    ANNOTATION_FIELDS,
    MANIFEST_FIELDS,
    prepare_dataset,
    validate_dataset,
    wilson_interval,
)


def _make_image(path: Path, color: tuple[int, int, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (24, 16), color).save(path)


def _build_inputs(root: Path, group_count: int = 3, images_per_group: int = 5) -> Path:
    images = root / "images"
    for group_index in range(group_count):
        group = images / f"grp_{group_index:012x}"
        for image_index in range(images_per_group):
            _make_image(
                group / f"source_video_frame_{group_index}_{image_index}.png",
                (20 + group_index * 20, 30 + image_index * 10, 40),
            )
    return images


def _prepare(
    *,
    images: Path,
    workspace: Path,
    manifest: Path,
    annotations: Path,
    summary: Path,
    copy_files: bool,
    per_group: int = 3,
    seed: int = 1234,
) -> dict:
    return prepare_dataset(
        images_root=images,
        selected_dir=workspace / "datasets" / "synthetic_test" / "selected",
        path_base=workspace,
        manifest_path=manifest,
        annotations_path=annotations,
        summary_path=summary,
        source_dataset="synthetic_test",
        license_or_permission="test-only",
        seed=seed,
        per_group=per_group,
        notes="deterministic test selection",
        copy_files=copy_files,
    )


def _read_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _write_rows(path: Path, fields: tuple[str, ...], rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fields), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def test_wilson_interval_is_pure_python_and_bounded() -> None:
    assert wilson_interval(0, 0) is None
    zero = wilson_interval(0, 10)
    ten = wilson_interval(10, 10)
    half = wilson_interval(5, 10)
    assert zero is not None and zero["low"] == 0.0 and 0.0 < zero["high"] < 1.0
    assert ten is not None and 0.0 < ten["low"] < 1.0 and ten["high"] == 1.0
    assert half is not None and half["low"] < 0.5 < half["high"]
    assert half["method"] == "wilson"


def test_prepare_is_reproducible_and_blind(tmp_path: Path) -> None:
    images = _build_inputs(tmp_path)
    workspace = tmp_path / "workspace"
    manifest_a = workspace / "annotations" / "manifest_a.csv"
    manifest_b = workspace / "annotations" / "manifest_b.csv"
    annotations_a = workspace / "annotations" / "labels_a.csv"
    annotations_b = workspace / "annotations" / "labels_b.csv"

    first = _prepare(
        images=images,
        workspace=workspace,
        manifest=manifest_a,
        annotations=annotations_a,
        summary=workspace / "reports" / "summary_a.json",
        copy_files=False,
    )
    second = _prepare(
        images=images,
        workspace=workspace,
        manifest=manifest_b,
        annotations=annotations_b,
        summary=workspace / "reports" / "summary_b.json",
        copy_files=False,
    )

    assert first["manifest_sha256"] == second["manifest_sha256"]
    assert first["annotation_sha256"] == second["annotation_sha256"]
    assert first["selected_count"] == 9
    assert first["group_count"] == 3
    assert first["split_counts"] == {"development": 6, "holdout": 3}

    manifest_text = manifest_a.read_text(encoding="utf-8")
    annotation_text = annotations_a.read_text(encoding="utf-8")
    assert "source_video" not in manifest_text
    assert "source_video" not in annotation_text
    assert "C:\\" not in manifest_text
    assert "C:\\" not in annotation_text
    assert "SMARTDOC" not in annotation_text.upper()

    manifest_rows = _read_rows(manifest_a)
    annotation_rows = _read_rows(annotations_a)
    assert tuple(manifest_rows[0].keys()) == MANIFEST_FIELDS
    assert tuple(annotation_rows[0].keys()) == ANNOTATION_FIELDS
    assert len(manifest_rows) == 9
    assert len(annotation_rows) == 9
    assert {row["image_id"] for row in manifest_rows} == {
        row["image_id"] for row in annotation_rows
    }
    assert all(row["image_id"].startswith("img_") for row in manifest_rows)
    assert all(row["expected_quality"] == "" for row in annotation_rows)
    assert all(row["main_problem"] == "" for row in annotation_rows)
    assert all(row["secondary_problems"] == "" for row in annotation_rows)
    assert all(row["annotator"] == "" for row in annotation_rows)


def test_prepare_copies_opaque_files_and_validates(tmp_path: Path) -> None:
    images = _build_inputs(tmp_path)
    workspace = tmp_path / "workspace"
    manifest = workspace / "annotations" / "manifest.csv"
    annotations = workspace / "annotations" / "labels.csv"
    _prepare(
        images=images,
        workspace=workspace,
        manifest=manifest,
        annotations=annotations,
        summary=workspace / "reports" / "summary.json",
        copy_files=True,
    )

    selected_dir = workspace / "datasets" / "synthetic_test" / "selected"
    assert selected_dir.is_dir()
    selected_names = sorted(path.name for path in selected_dir.iterdir())
    assert len(selected_names) == 9
    assert all(name.startswith("img_") for name in selected_names)
    assert all("source_video" not in name for name in selected_names)

    result = validate_dataset(
        manifest_path=manifest,
        annotations_path=annotations,
        path_base=workspace,
    )
    assert result["ok"] is True
    assert result["checks"]["all_selected_images_present"] is True
    assert result["checks"]["no_group_crosses_split"] is True
    assert result["checks"]["no_source_in_annotation"] is True
    assert result["counts"]["manifest_rows"] == 9
    assert result["counts"]["annotation_rows"] == 9


def test_validate_rejects_invalid_values_and_source_leak(tmp_path: Path) -> None:
    images = _build_inputs(tmp_path)
    workspace = tmp_path / "workspace"
    manifest = workspace / "annotations" / "manifest.csv"
    annotations = workspace / "annotations" / "labels.csv"
    _prepare(
        images=images,
        workspace=workspace,
        manifest=manifest,
        annotations=annotations,
        summary=workspace / "reports" / "summary.json",
        copy_files=True,
    )

    rows = _read_rows(annotations)
    rows[0]["expected_quality"] = "BAD"
    rows[0]["main_problem"] = "GLARE"
    rows[0]["secondary_problems"] = "NONE;BLURRY"
    rows[0]["annotator"] = "Alice"
    rows[0]["notes"] = "C:\\example\\private\\source_video.png"
    bad_path = workspace / "annotations" / "bad_labels.csv"
    _write_rows(bad_path, ANNOTATION_FIELDS, rows)

    result = validate_dataset(
        manifest_path=manifest,
        annotations_path=bad_path,
        path_base=workspace,
    )
    assert result["ok"] is False
    assert any("invalid_expected_quality" in error for error in result["errors"])
    assert any("none_must_be_alone" in error for error in result["errors"])
    assert any("invalid_annotator_code" in error for error in result["errors"])
    assert any("forbidden_text" in error for error in result["errors"])

    serialized = json.dumps(result)
    assert "Alice" not in serialized
    assert "example" not in serialized
    assert "source_video" not in serialized


def test_validate_requires_complete_labels_when_asked(tmp_path: Path) -> None:
    images = _build_inputs(tmp_path)
    workspace = tmp_path / "workspace"
    manifest = workspace / "annotations" / "manifest.csv"
    annotations = workspace / "annotations" / "labels.csv"
    _prepare(
        images=images,
        workspace=workspace,
        manifest=manifest,
        annotations=annotations,
        summary=workspace / "reports" / "summary.json",
        copy_files=True,
    )

    result = validate_dataset(
        manifest_path=manifest,
        annotations_path=annotations,
        path_base=workspace,
        require_complete=True,
    )
    assert result["ok"] is False
    assert any("incomplete" in error for error in result["errors"])


def test_validate_rejects_group_crossing_split(tmp_path: Path) -> None:
    images = _build_inputs(tmp_path)
    workspace = tmp_path / "workspace"
    manifest = workspace / "annotations" / "manifest.csv"
    annotations = workspace / "annotations" / "labels.csv"
    _prepare(
        images=images,
        workspace=workspace,
        manifest=manifest,
        annotations=annotations,
        summary=workspace / "reports" / "summary.json",
        copy_files=True,
    )

    rows = _read_rows(manifest)
    first_group = rows[0]["source_group_id"]
    first_split = rows[0]["split"]
    replacement = "holdout" if first_split == "development" else "development"
    rows[0]["split"] = replacement
    bad_manifest = workspace / "annotations" / "bad_manifest.csv"
    _write_rows(bad_manifest, MANIFEST_FIELDS, rows)

    result = validate_dataset(
        manifest_path=bad_manifest,
        annotations_path=annotations,
        path_base=workspace,
    )
    assert result["ok"] is False
    assert any(error.startswith("group_crosses_split") for error in result["errors"])


def test_validate_rejects_missing_selected_file(tmp_path: Path) -> None:
    images = _build_inputs(tmp_path)
    workspace = tmp_path / "workspace"
    manifest = workspace / "annotations" / "manifest.csv"
    annotations = workspace / "annotations" / "labels.csv"
    _prepare(
        images=images,
        workspace=workspace,
        manifest=manifest,
        annotations=annotations,
        summary=workspace / "reports" / "summary.json",
        copy_files=True,
    )

    selected_file = sorted(
        (workspace / "datasets" / "synthetic_test" / "selected").iterdir()
    )[0]
    selected_file.unlink()

    result = validate_dataset(
        manifest_path=manifest,
        annotations_path=annotations,
        path_base=workspace,
    )
    assert result["ok"] is False
    assert any("selected_file_missing" in error for error in result["errors"])


def test_single_group_has_no_holdout(tmp_path: Path) -> None:
    images = _build_inputs(tmp_path, group_count=1, images_per_group=4)
    workspace = tmp_path / "workspace"
    summary = _prepare(
        images=images,
        workspace=workspace,
        manifest=workspace / "annotations" / "manifest.csv",
        annotations=workspace / "annotations" / "labels.csv",
        summary=workspace / "reports" / "summary.json",
        copy_files=False,
        per_group=2,
    )
    assert summary["group_count"] == 1
    assert summary["split_counts"] == {"development": 2, "holdout": 0}
