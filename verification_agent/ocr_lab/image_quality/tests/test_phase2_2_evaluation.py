from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

import image_quality.cli as cli
from image_quality.output_schema import (
    STATUS_ACCEPTABLE,
    STATUS_POOR,
    STATUS_RECAPTURE,
)
from image_quality.profiles import load_thresholds

from . import synthetic as s

OCR_LAB_DIR = Path(__file__).resolve().parents[2]


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "image_quality", *args],
        cwd=OCR_LAB_DIR,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
        check=False,
    )


def _write_eval_folder(tmp_path: Path) -> tuple[Path, Path]:
    labels: list[tuple[str, str, str]] = [
        ("alpha-1.png", STATUS_ACCEPTABLE, "group-alpha"),
        ("alpha-2.png", STATUS_POOR, "group-alpha"),
        ("alpha-3.png", STATUS_RECAPTURE, "group-alpha"),
        ("beta-1.png", STATUS_ACCEPTABLE, "group-beta"),
        ("beta-2.png", STATUS_POOR, "group-beta"),
        ("beta-3.png", STATUS_RECAPTURE, "group-beta"),
        ("gamma-1.png", STATUS_ACCEPTABLE, "group-gamma"),
        ("gamma-2.png", STATUS_POOR, "group-gamma"),
        ("gamma-3.png", STATUS_RECAPTURE, "group-gamma"),
    ]
    image_bytes = s.good_document_bytes(800, 600)
    for filename, _label, _group in labels:
        (tmp_path / filename).write_bytes(image_bytes)
    labels_path = tmp_path / "labels.csv"
    lines = ["filename,label,group"]
    lines.extend(f"{filename},{label},{group}" for filename, label, group in labels)
    labels_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return tmp_path, labels_path


def test_gate_metrics_counts_rates_wilson_and_na() -> None:
    pairs = [
        (STATUS_ACCEPTABLE, STATUS_ACCEPTABLE),
        (STATUS_POOR, STATUS_POOR),
        (STATUS_RECAPTURE, STATUS_RECAPTURE),
        (STATUS_ACCEPTABLE, STATUS_RECAPTURE),
        (STATUS_RECAPTURE, STATUS_ACCEPTABLE),
    ]
    gate = cli._gate_metrics(pairs)
    assert gate["good_count"] == 3
    assert gate["bad_count"] == 2
    pass_if_poor = gate["policies"]["pass_if_poor"]
    assert pass_if_poor["counts"] == {
        "good_passed": 2,
        "good_blocked": 1,
        "bad_passed": 1,
        "bad_blocked": 1,
    }
    assert pass_if_poor["rates"]["bad_passed"]["rate"] == 0.5
    assert pass_if_poor["rates"]["bad_passed"]["wilson_95"] == cli.wilson_interval(1, 2)
    assert pass_if_poor["youden_j"] == round(1 / 2 + 2 / 3 - 1, 6)
    strict = gate["policies"]["strict"]
    assert strict["counts"] == {
        "good_passed": 1,
        "good_blocked": 2,
        "bad_passed": 1,
        "bad_blocked": 1,
    }
    assert strict["youden_j"] == round(1 / 2 + 1 / 3 - 1, 6)

    single = cli._gate_metrics([(STATUS_ACCEPTABLE, STATUS_ACCEPTABLE)])
    absent = single["policies"]["strict"]["rates"]["bad_passed"]
    assert absent["denominator"] == 0
    assert absent["rate"] is None
    assert absent["wilson_95"] is None
    assert single["policies"]["strict"]["youden_j"] is None


def test_cv_thresholds_derive_only_soft_keys_from_training_rows() -> None:
    base = load_thresholds()
    before = base["profiles"]["id_card"]["layouts"]["default"]
    severe_before = before["blur"]["laplacian_variance_severe"]

    def row(label: str, value: float | None) -> dict:
        metrics = {}
        if value is not None:
            metrics["sharpness_laplacian"] = {
                "value": value,
                "nature": "measured",
            }
        return {
            "label": label,
            "result": {"error_category": None, "metrics": metrics},
        }

    training_rows = [
        row(STATUS_ACCEPTABLE, 100.0),
        row(STATUS_ACCEPTABLE, 110.0),
        row(STATUS_RECAPTURE, 10.0),
        row(STATUS_RECAPTURE, 20.0),
    ]
    derived, applied = cli._derive_thresholds(
        base,
        profile="id_card",
        layout="default",
        training_rows=training_rows,
    )
    layout = derived["profiles"]["id_card"]["layouts"]["default"]
    assert applied["blur.laplacian_variance_poor"] == 60.0
    assert layout["blur"]["laplacian_variance_poor"] == 60.0
    assert layout["blur"]["laplacian_variance_severe"] == severe_before
    assert before["blur"]["laplacian_variance_poor"] != 60.0


def test_grouped_folds_never_split_a_group() -> None:
    groups = ["a", "a", "b", "b", "c", "c", "d", "d"]
    folds = cli._grouped_folds(groups, 4, 20261110)
    assert len(folds) == 4
    all_indices = set(range(len(groups)))
    for train_indices, test_indices in folds:
        assert set(train_indices) | set(test_indices) == all_indices
        assert not (set(train_indices) & set(test_indices))
        train_groups = {groups[index] for index in train_indices}
        test_groups = {groups[index] for index in test_indices}
        assert not (train_groups & test_groups)


def test_cli_cv_flag_defaults_to_k_five_and_seed(monkeypatch, tmp_path) -> None:
    captured: dict = {}

    def fake_evaluate_folder(folder, labels, profile, layout=None, **kwargs):
        captured.update(kwargs)
        return {"error_category": None}

    monkeypatch.setattr(cli, "evaluate_folder", fake_evaluate_folder)
    exit_code = cli.main(
        [
            "evaluate",
            str(tmp_path),
            "--labels",
            str(tmp_path / "labels.csv"),
            "--profile",
            "id_card",
            "--cv",
        ]
    )
    assert exit_code == 0
    assert captured["cv"] == 5
    assert captured["seed"] == 20261110

    captured.clear()
    exit_code = cli.main(
        [
            "evaluate",
            str(tmp_path),
            "--labels",
            str(tmp_path / "labels.csv"),
            "--profile",
            "id_card",
            "--cv",
            "3",
            "--seed",
            "42",
        ]
    )
    assert exit_code == 0
    assert captured["cv"] == 3
    assert captured["seed"] == 42


def test_stratified_folds_are_deterministic_and_cover_each_row_once() -> None:
    labels = [
        STATUS_ACCEPTABLE,
        STATUS_POOR,
        STATUS_RECAPTURE,
        STATUS_ACCEPTABLE,
        STATUS_POOR,
        STATUS_RECAPTURE,
        STATUS_ACCEPTABLE,
        STATUS_POOR,
        STATUS_RECAPTURE,
    ]
    first = cli._stratified_folds(labels, 3, 777)
    second = cli._stratified_folds(labels, 3, 777)
    assert first == second
    seen: list[int] = []
    for train_indices, test_indices in first:
        assert not (set(train_indices) & set(test_indices))
        assert set(train_indices) | set(test_indices) == set(range(len(labels)))
        seen.extend(test_indices)
    assert sorted(seen) == list(range(len(labels)))


def _cv_rows(group_source: str, groups: list[str]) -> list[dict]:
    labels = [STATUS_ACCEPTABLE, STATUS_POOR, STATUS_RECAPTURE] * 3
    return [
        {
            "label": label,
            "group": groups[index],
            "group_source": group_source,
            "path": None,
            "result": None,
            "predicted": STATUS_RECAPTURE,
        }
        for index, label in enumerate(labels)
    ]


def test_cross_validation_uses_filename_stems_without_group_column() -> None:
    cv, error = cli._cross_validation(
        _cv_rows("filename_stem", ["a", "a", "a", "b", "b", "b", "c", "c", "c"]),
        k=3,
        seed=20261110,
        thresholds=load_thresholds(),
        profile="id_card",
        layout="default",
        max_side=None,
        max_megapixels=None,
        in_sample_gate={"mode": "in-sample"},
    )
    assert error is None
    assert cv is not None
    assert cv["group_source"] == "filename_stem"
    assert cv["group_leakage"] is False
    assert cv["group_count"] == 3


def test_cross_validation_falls_back_to_stratified_folds() -> None:
    cv, error = cli._cross_validation(
        _cv_rows("filename_stem", ["a", "a", "a", "b", "b", "b", "a", "b", "a"]),
        k=3,
        seed=20261110,
        thresholds=load_thresholds(),
        profile="id_card",
        layout="default",
        max_side=None,
        max_megapixels=None,
        in_sample_gate={"mode": "in-sample"},
    )
    assert error is None
    assert cv is not None
    assert cv["group_source"] == "stratified_folds"
    assert cv["group_leakage"] is None
    assert cv["warnings"] == ["cv_group_fallback: stratified_folds"]


def test_evaluate_cv_same_seed_is_deterministic(tmp_path) -> None:
    folder, labels = _write_eval_folder(tmp_path)
    first = _run_cli(
        "evaluate",
        str(folder),
        "--labels",
        str(labels),
        "--profile",
        "id_card",
        "--cv",
        "3",
        "--seed",
        "12345",
    )
    second = _run_cli(
        "evaluate",
        str(folder),
        "--labels",
        str(labels),
        "--profile",
        "id_card",
        "--cv",
        "3",
        "--seed",
        "12345",
    )
    assert first.returncode == 0, first.stdout
    assert second.returncode == 0, second.stdout
    first_payload = json.loads(first.stdout)
    second_payload = json.loads(second.stdout)
    assert first_payload == second_payload
    assert "processing_time_ms" not in first_payload

    cv = first_payload["cross_validation"]
    assert cv["group_source"] == "labels.csv:group"
    assert cv["group_count"] == 3
    assert cv["group_leakage"] is False
    assert sum(cv["fold_sizes"]) == first_payload["sample_count"]
    assert cv["in_sample_gate"] == first_payload["gate"]
    assert "pooled_out_of_fold_gate" in cv


def test_evaluate_cv_refuses_a_class_below_k(tmp_path) -> None:
    _, labels = _write_eval_folder(tmp_path)
    rows = labels.read_text(encoding="utf-8").splitlines()
    labels.write_text("\n".join(rows[:4]) + "\n", encoding="utf-8")
    completed = _run_cli(
        "evaluate",
        str(tmp_path),
        "--labels",
        str(labels),
        "--profile",
        "id_card",
        "--cv",
        "3",
    )
    assert completed.returncode == 2
    payload = json.loads(completed.stdout)
    assert payload["error_category"] == "insufficient_cv_samples"
    assert payload["cv_error"]["k"] == 3


def test_evaluate_cv_refuses_too_few_groups(tmp_path) -> None:
    folder, _labels = _write_eval_folder(tmp_path)
    labels = tmp_path / "labels_two_groups.csv"
    labels.write_text(
        "\n".join(
            ["filename,label,group"]
            + [
                f"alpha-1.png,{STATUS_ACCEPTABLE},g1",
                f"alpha-2.png,{STATUS_POOR},g1",
                f"alpha-3.png,{STATUS_RECAPTURE},g1",
                f"beta-1.png,{STATUS_ACCEPTABLE},g1",
                f"beta-2.png,{STATUS_POOR},g2",
                f"beta-3.png,{STATUS_RECAPTURE},g2",
                f"gamma-1.png,{STATUS_ACCEPTABLE},g2",
                f"gamma-2.png,{STATUS_POOR},g2",
                f"gamma-3.png,{STATUS_RECAPTURE},g2",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    completed = _run_cli(
        "evaluate",
        str(folder),
        "--labels",
        str(labels),
        "--profile",
        "id_card",
        "--cv",
        "3",
    )
    assert completed.returncode == 2
    payload = json.loads(completed.stdout)
    assert payload["error_category"] == "insufficient_cv_groups"


def test_evaluate_outputs_never_contain_filenames_or_paths(tmp_path) -> None:
    folder, labels = _write_eval_folder(tmp_path)
    with labels.open("a", encoding="utf-8") as handle:
        handle.write(f"missing-extra.png,{STATUS_ACCEPTABLE},group-delta\n")
    completed = _run_cli(
        "evaluate",
        str(folder),
        "--labels",
        str(labels),
        "--profile",
        "id_card",
    )
    assert completed.returncode == 0, completed.stdout
    serialized = completed.stdout.lower()
    assert not re.search(r"\.(jpeg|png|jpg)", serialized)
    assert "c:\\\\" not in serialized
    assert "alpha-1" not in serialized


def test_what_if_is_layout_relative_and_in_memory_only() -> None:
    base = load_thresholds()
    original = base["profiles"]["id_card"]["layouts"]["default"]["blur"][
        "laplacian_variance_poor"
    ]
    updated, applied = cli._apply_what_if(
        base,
        ["blur.laplacian_variance_poor=9999"],
        profile="id_card",
        layout="default",
    )
    assert updated["profiles"]["id_card"]["layouts"]["default"]["blur"][
        "laplacian_variance_poor"
    ] == 9999
    assert base["profiles"]["id_card"]["layouts"]["default"]["blur"][
        "laplacian_variance_poor"
    ] == original
    assert applied == {"blur.laplacian_variance_poor": 9999}

    with pytest.raises(cli._WhatIfError) as invalid_type:
        cli._apply_what_if(
            base,
            ["blur.laplacian_variance_poor=not-a-number"],
            profile="id_card",
            layout="default",
        )
    assert invalid_type.value.category == "invalid_what_if_value"


def test_evaluate_echoes_what_if_and_rejects_unknown_key(tmp_path) -> None:
    folder, labels = _write_eval_folder(tmp_path)
    completed = _run_cli(
        "evaluate",
        str(folder),
        "--labels",
        str(labels),
        "--profile",
        "id_card",
        "--what-if",
        "boundary.no_document_severity=moderate",
    )
    assert completed.returncode == 0, completed.stdout
    payload = json.loads(completed.stdout)
    assert payload["what_if"] == {"boundary.no_document_severity": "moderate"}

    unknown = _run_cli(
        "evaluate",
        str(folder),
        "--labels",
        str(labels),
        "--profile",
        "id_card",
        "--what-if",
        "boundary.not_a_key=1",
    )
    assert unknown.returncode == 2
    assert json.loads(unknown.stdout)["error_category"] == "unknown_what_if_key"


def test_evaluate_reports_gate_boundary_diagnostics_and_n_by_class(tmp_path) -> None:
    folder, labels = _write_eval_folder(tmp_path)
    completed = _run_cli(
        "evaluate",
        str(folder),
        "--labels",
        str(labels),
        "--profile",
        "id_card",
    )
    assert completed.returncode == 0, completed.stdout
    payload = json.loads(completed.stdout)
    assert set(payload["gate"]["policies"]) == {"pass_if_poor", "strict"}
    assert set(payload["gate"]["no_document_by_label"]) == {
        STATUS_ACCEPTABLE,
        STATUS_POOR,
        STATUS_RECAPTURE,
    }
    assert set(payload["diagnosis_no_candidate"]) == {
        "count",
        "mean_edge_density",
        "mean_contrast_p5",
        "mean_brightness",
        "top_rejection_reasons",
    }
    assert set(payload["reason_codes_by_label"]) == {
        STATUS_ACCEPTABLE,
        STATUS_POOR,
        STATUS_RECAPTURE,
    }
    diagnostics = payload["boundary_diagnostics"]
    assert set(diagnostics["detector_used"]) == {
        STATUS_ACCEPTABLE,
        STATUS_POOR,
        STATUS_RECAPTURE,
    }
    for label in (STATUS_ACCEPTABLE, STATUS_POOR, STATUS_RECAPTURE):
        assert "none_found" in diagnostics["candidate_rejections"][label]

    calibrate = _run_cli(
        "calibrate",
        str(folder),
        "--labels",
        str(labels),
        "--profile",
        "id_card",
    )
    assert calibrate.returncode == 0, calibrate.stdout
    calibration_payload = json.loads(calibrate.stdout)
    assert set(calibration_payload["metric_separation_n_by_class"]) == {
        STATUS_ACCEPTABLE,
        STATUS_POOR,
        STATUS_RECAPTURE,
    }
    assert calibration_payload["selection_bias_warning"]
    for entry in calibration_payload["metric_separation"].values():
        assert entry["exploratory"] is True

    small_separation = cli._metric_separation([1.0, 2.0, 3.0], [4.0, 5.0, 6.0])
    assert small_separation is not None
    assert small_separation["exploratory"] is True

    large_acceptable = [float(value) for value in range(30)]
    large_recapture = [float(value) for value in range(30, 60)]
    separation = cli._metric_separation(large_acceptable, large_recapture)
    assert separation is not None
    assert separation["exploratory"] is False


def test_diagnosis_no_candidate_reports_aggregate_acceptable_failures() -> None:
    def row(label, candidate_count, edge_density, p5, brightness, reasons):
        return {
            "label": label,
            "result": {
                "metrics": {
                    "document_boundary": {
                        "value": {
                            "valid": candidate_count != 0,
                            "candidate_count": candidate_count,
                            "edge_density": edge_density,
                            "rejection_counts": reasons,
                        }
                    },
                    "contrast": {"value": {"p5": p5}},
                    "brightness": {"value": brightness},
                }
            },
        }

    diagnosis = cli._diagnosis_no_candidate(
        [
            row(
                STATUS_ACCEPTABLE,
                0,
                0.02,
                20.0,
                100.0,
                {"area_ratio": 1, "none_found": 1},
            ),
            row(
                STATUS_ACCEPTABLE,
                0,
                0.04,
                40.0,
                120.0,
                {"area_ratio": 1, "none_found": 1, "solidity": 1},
            ),
            row(STATUS_POOR, 0, 0.08, 60.0, 180.0, {"none_found": 1}),
            row(STATUS_ACCEPTABLE, 1, 0.06, 50.0, 150.0, {}),
        ]
    )

    assert diagnosis == {
        "count": 2,
        "mean_edge_density": 0.03,
        "mean_contrast_p5": 30.0,
        "mean_brightness": 110.0,
        "top_rejection_reasons": [
            {"reason": "area_ratio", "count": 2},
            {"reason": "none_found", "count": 2},
            {"reason": "solidity", "count": 1},
        ],
    }


def test_reason_codes_by_label_reports_reason_counts_and_associated_means() -> None:
    rows = [
        {
            "label": STATUS_ACCEPTABLE,
            "result": {
                "reason_codes": ["LOW_CONTRAST", "BLURRY"],
                "metrics": {
                    "contrast": {
                        "value": {
                            "p5": 30.0,
                            "p5_p95_range": 70.0,
                            "rms_contrast": 18.0,
                        }
                    },
                    "sharpness_laplacian": {"value": 10.0},
                    "sharpness_tenengrad": {"value": 800.0},
                },
            },
        },
        {
            "label": STATUS_ACCEPTABLE,
            "result": {
                "reason_codes": ["LOW_CONTRAST"],
                "metrics": {
                    "contrast": {
                        "value": {
                            "p5": 40.0,
                            "p5_p95_range": 90.0,
                            "rms_contrast": 22.0,
                        }
                    }
                },
            },
        },
        {"label": STATUS_RECAPTURE, "result": None},
    ]

    diagnostic = cli._reason_codes_by_label(rows)

    assert diagnostic[STATUS_ACCEPTABLE] == {
        "count": 2,
        "reason_code_counts": {"BLURRY": 1, "LOW_CONTRAST": 2},
        "mean_metrics_by_reason": {
            "BLURRY": {
                "sharpness_laplacian": 10.0,
                "sharpness_tenengrad": 800.0,
            },
            "LOW_CONTRAST": {
                "contrast.p5": 35.0,
                "contrast.p5_p95_range": 80.0,
                "contrast.rms_contrast": 20.0,
            },
        },
    }
    assert diagnostic[STATUS_RECAPTURE] == {
        "count": 1,
        "reason_code_counts": {},
        "mean_metrics_by_reason": {},
    }
