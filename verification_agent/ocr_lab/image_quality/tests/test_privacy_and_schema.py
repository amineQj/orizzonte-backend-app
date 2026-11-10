from __future__ import annotations

import copy
import hashlib
import json
import socket
from pathlib import Path

from image_quality import analyze_bytes, analyze_path
from image_quality.output_schema import validate_output

from . import synthetic as s


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def test_file_is_not_modified_and_no_files_are_created(tmp_path) -> None:
    path = tmp_path / "specimen-good.png"
    path.write_bytes(s.good_document_bytes())
    before_hash = _sha256(path)
    before_mtime = path.stat().st_mtime_ns
    before_files = sorted(item.name for item in tmp_path.iterdir())

    result = analyze_path(path, "id_card")

    assert result["status"] == "ACCEPTABLE_QUALITY"
    assert _sha256(path) == before_hash
    assert path.stat().st_mtime_ns == before_mtime
    assert sorted(item.name for item in tmp_path.iterdir()) == before_files


def test_bytes_analysis_does_not_write_any_file(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    before_files = list(tmp_path.iterdir())
    result = analyze_bytes(s.good_document_bytes(), "id_card")
    assert result["status"] == "ACCEPTABLE_QUALITY"
    assert list(tmp_path.iterdir()) == before_files


def test_output_contains_no_path_or_image_content(tmp_path) -> None:
    path = tmp_path / "private-name-should-not-leak.png"
    path.write_bytes(s.good_document_bytes())
    result = analyze_path(path, "id_card")
    serialized = json.dumps(result, ensure_ascii=False)
    assert str(path) not in serialized
    assert path.name not in serialized
    assert "SPECIMEN" not in serialized
    assert "FICTIONAL" not in serialized
    assert "private-name" not in serialized


def test_unreadable_output_has_no_path_or_traceback(tmp_path) -> None:
    path = tmp_path / "private-corrupt-name.png"
    path.write_bytes(s.corrupt_bytes())
    result = analyze_path(path, "id_card")
    serialized = json.dumps(result, ensure_ascii=False)
    assert result["reason_codes"] == ["UNREADABLE_IMAGE"]
    assert str(path) not in serialized
    assert path.name not in serialized
    assert "Traceback" not in serialized


def test_analysis_works_with_network_sockets_blocked(monkeypatch) -> None:
    def blocked_socket(*args, **kwargs):
        raise AssertionError("network access is forbidden")

    monkeypatch.setattr(socket, "socket", blocked_socket)
    result = analyze_bytes(s.good_document_bytes(), "id_card")
    assert result["status"] == "ACCEPTABLE_QUALITY"


def test_output_is_json_serializable_and_schema_valid() -> None:
    result = analyze_bytes(s.good_document_bytes(), "id_card")
    json.dumps(result, ensure_ascii=False)
    assert validate_output(result) == []
    serialized = json.dumps(result, ensure_ascii=False).lower()
    assert "fraud" not in serialized
    assert "identity" not in serialized
    assert "passport" not in serialized


def test_validate_output_catches_broken_contracts() -> None:
    result = analyze_bytes(s.good_document_bytes(), "id_card")

    missing = copy.deepcopy(result)
    del missing["schema_version"]
    assert validate_output(missing)

    bad_status = copy.deepcopy(result)
    bad_status["status"] = "FRAUD"
    assert validate_output(bad_status)

    bad_reason = copy.deepcopy(result)
    bad_reason["reason_codes"].append("NOT_A_REASON")
    assert validate_output(bad_reason)

    duplicate_reason = copy.deepcopy(result)
    duplicate_reason["reason_codes"] = ["BLURRY", "BLURRY"]
    assert validate_output(duplicate_reason)

    bad_confidence = copy.deepcopy(result)
    bad_confidence["confidence"] = 2.0
    assert validate_output(bad_confidence)

    bad_metric = copy.deepcopy(result)
    bad_metric["metrics"]["sharpness_laplacian"]["nature"] = "magic"
    assert validate_output(bad_metric)

    bad_trace = copy.deepcopy(result)
    bad_trace["decision_trace"] = [{"reason_code": "BLURRY", "severity": "magic", "metrics": []}]
    assert validate_output(bad_trace)
