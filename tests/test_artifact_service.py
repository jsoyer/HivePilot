"""Tests for ArtifactManager (artifact_service), incl. the lazy boto3 path."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from hivepilot.services import config_provenance
from hivepilot.services.artifact_service import ArtifactManager


@pytest.fixture(autouse=True)
def _clean_secret_registry() -> Iterator[None]:
    config_provenance.clear_secret_values()
    yield
    config_provenance.clear_secret_values()


def test_write_file_and_json(tmp_path: Path) -> None:
    mgr = ArtifactManager(tmp_path)
    p = mgr.write_file("a.txt", "hello")
    assert p.read_text(encoding="utf-8") == "hello"
    j = mgr.write_json("b.json", {"k": 1})
    assert '"k": 1' in j.read_text(encoding="utf-8")


def test_write_file_redacts_registered_secret(tmp_path: Path) -> None:
    marker = "ARTIFACT-MARKER-do-not-leak"
    config_provenance.register_secret_value(marker)
    mgr = ArtifactManager(tmp_path)
    p = mgr.write_file("a.txt", f"output was {marker}")
    written = p.read_text(encoding="utf-8")
    assert marker not in written
    assert config_provenance.REDACTED in written


def test_write_json_redacts_registered_secret(tmp_path: Path) -> None:
    marker = "RESULTS-JSON-MARKER-do-not-leak"
    config_provenance.register_secret_value(marker)
    mgr = ArtifactManager(tmp_path)
    j = mgr.write_json("results.json", {"results": [{"detail": marker}]})
    written = j.read_text(encoding="utf-8")
    assert marker not in written
    assert config_provenance.REDACTED in written


def test_export_local_is_noop(tmp_path: Path) -> None:
    ArtifactManager(tmp_path).export([{"target": "local"}])  # must not raise


def test_export_routes_s3_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mgr = ArtifactManager(tmp_path)
    seen: dict = {}
    monkeypatch.setattr(mgr, "_export_s3", lambda cfg: seen.setdefault("cfg", cfg))
    mgr.export([{"target": "s3", "bucket": "b"}])
    assert seen["cfg"]["bucket"] == "b"


def test_export_s3_without_boto3_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Drop the conftest boto3 stub so the lazy import genuinely fails (boto3 is
    # not installed) → ArtifactManager should surface a clear, actionable error.
    monkeypatch.delitem(sys.modules, "boto3", raising=False)
    monkeypatch.delitem(sys.modules, "boto3.session", raising=False)
    mgr = ArtifactManager(tmp_path)
    with pytest.raises(RuntimeError, match=r"hivepilot\[cloud\]"):
        mgr.export([{"target": "s3", "bucket": "b"}])
