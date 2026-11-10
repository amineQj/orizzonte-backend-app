from __future__ import annotations

import csv
import json
import subprocess
import sys
from pathlib import Path

import image_quality.cli as cli
from image_quality.output_schema import STATUS_ACCEPTABLE, STATUS_RECAPTURE

from . import synthetic as s

OCR_LAB_DIR = Path(__file__).resolve().parents[2]


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "image_quality", *args],
        cwd=OCR_LAB_DIR,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
        check=False,
    )


def _write_image(folder: Path, name: str, data: bytes) -> Path:
    path = folder / name
    path.write_bytes(data)
    return path


def test_analyze_prints_json_only_and_no_path(tmp_path) -> None:
    path = _write_image(tmp_path, "specimen-good.png", s.good_document_bytes())
    completed = _run_cli("analyze", str(path), "--profile", "id_card")
    assert completed.returncode == 0
    assert "Traceback" not in completed.stderr
    result = json.loads(completed.stdout)
    assert result["status"] == STATUS_ACCEPTABLE
    assert str(path) not in completed.stdout
    assert path.name not in completed.stdout
    assert "SPECIMEN" not in completed.stdout


def test_analyze_unreadable_image_is_structured(tmp_path) -> None:
    path = _write_image(tmp_path, "corrupt.png", s.corrupt_bytes())
    completed = _run_cli("analyze", str(path), "--profile", "id_card")
    assert completed.returncode == 3
    assert "Traceback" not in completed.stderr
    result = json.loads(completed.stdout)
    assert result["reason_codes"] == ["UNREADABLE_IMAGE"]
    assert result["error_category"] == "corrupt_image"
    assert str(path) not in completed.stdout
    assert path.name not in completed.stdout


def test_analyze_invalid_profile_is_structured() -> None:
    completed = _run_cli("analyze", "does-not-exist.png", "--profile", "passport")
    assert completed.returncode == 2
    assert "Traceback" not in completed.stderr
    result = json.loads(completed.stdout)
    assert result["error_category"] == "invalid_profile"
    assert result["image"] is None


def test_calibrate_reports_distributions_without_filenames(tmp_path) -> None:
    _write_image(tmp_path, "good.png", s.good_document_bytes())
    _write_image(tmp_path, "blurry.png", s.blurry_document_bytes())
    completed = _run_cli("calibrate", str(tmp_path), "--profile", "id_card")
    assert completed.returncode == 0
    result = json.loads(completed.stdout)
    assert result["command"] == "calibrate"
    assert result["image_count"] == 2
    assert result["analyzed_count"] == 2
    assert result["failed_count"] == 0
    assert result["thresholds_calibrated"] is False
    assert result["thresholds_modified"] is False
    sharpness = result["metrics"]["sharpness_laplacian"]
    assert sharpness["count"] == 2
    assert sharpness["min"] <= sharpness["median"] <= sharpness["max"]
    assert "good.png" not in completed.stdout
    assert "blurry.png" not in completed.stdout


def test_calibrate_missing_folder_is_structured(tmp_path) -> None:
    missing = tmp_path / "missing"
    completed = _run_cli("calibrate", str(missing), "--profile", "id_card")
    assert completed.returncode == 2
    result = json.loads(completed.stdout)
    assert result["error_category"] == "folder_not_found"


def test_evaluate_reports_confusion_matrix_and_rates(tmp_path) -> None:
    _write_image(tmp_path, "good.png", s.good_document_bytes())
    _write_image(tmp_path, "blurry.png", s.blurry_document_bytes())
    _write_image(tmp_path, "dark.png", s.dark_document_bytes())
    labels = tmp_path / "labels.csv"
    with labels.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["filename", "label"])
        writer.writerow(["good.png", STATUS_ACCEPTABLE])
        writer.writerow(["blurry.png", STATUS_RECAPTURE])
        writer.writerow(["dark.png", "POOR_QUALITY"])
    completed = _run_cli(
        "evaluate",
        str(tmp_path),
        "--labels",
        str(labels),
        "--profile",
        "id_card",
    )
    assert completed.returncode == 0
    result = json.loads(completed.stdout)
    assert result["command"] == "evaluate"
    assert result["sample_count"] == 3
    assert result["unreadable_count"] == 0
    assert result["false_acceptance"] == 0
    assert result["false_rejection"] == 0
    assert sum(sum(row.values()) for row in result["confusion_matrix"].values()) == 3
    assert "good.png" not in completed.stdout
    assert str(labels) not in completed.stdout


def test_evaluate_invalid_label_is_structured(tmp_path) -> None:
    _write_image(tmp_path, "good.png", s.good_document_bytes())
    labels = tmp_path / "labels.csv"
    labels.write_text("filename,label\ngood.png,BOGUS\n", encoding="utf-8")
    completed = _run_cli(
        "evaluate",
        str(tmp_path),
        "--labels",
        str(labels),
        "--profile",
        "id_card",
    )
    assert completed.returncode == 2
    result = json.loads(completed.stdout)
    assert result["error_category"] == "invalid_labels"


def test_evaluate_missing_labels_file_is_structured(tmp_path) -> None:
    _write_image(tmp_path, "good.png", s.good_document_bytes())
    completed = _run_cli(
        "evaluate",
        str(tmp_path),
        "--labels",
        str(tmp_path / "missing.csv"),
        "--profile",
        "id_card",
    )
    assert completed.returncode == 2
    result = json.loads(completed.stdout)
    assert result["error_category"] == "labels_not_found"


def test_false_acceptance_and_false_rejection_math(tmp_path, monkeypatch) -> None:
    for name in ("a.png", "b.png", "c.png", "d.png"):
        _write_image(tmp_path, name, b"not analyzed because analyze_path is mocked")
    labels = tmp_path / "labels.csv"
    labels.write_text(
        "\n".join(
            [
                "filename,label",
                f"a.png,{STATUS_ACCEPTABLE}",
                f"b.png,{STATUS_RECAPTURE}",
                f"c.png,{STATUS_RECAPTURE}",
                f"d.png,{STATUS_ACCEPTABLE}",
            ]
        ),
        encoding="utf-8",
    )

    predictions = {
        "a.png": STATUS_ACCEPTABLE,
        "b.png": STATUS_ACCEPTABLE,
        "c.png": STATUS_RECAPTURE,
        "d.png": STATUS_RECAPTURE,
    }

    def fake_analyze(path, profile, layout=None, **kwargs):
        return {"status": predictions[Path(path).name], "error_category": None}

    monkeypatch.setattr(cli, "analyze_path", fake_analyze)
    result = cli.evaluate_folder(tmp_path, labels, "id_card")
    assert result["false_acceptance"] == 1
    assert result["false_acceptance_rate"] == 0.5
    assert result["false_rejection"] == 1
    assert result["false_rejection_rate"] == 0.5
    assert result["acceptable_but_poor"] == 0
