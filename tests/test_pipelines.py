"""Tests for hivepilot.pipelines — write_stage_artifact redacts registered
secret values from the vault note body before it is written (or returned in
the dry_run preview dict)."""

from __future__ import annotations

from pathlib import Path

import pytest

from hivepilot.pipelines import write_stage_artifact
from hivepilot.services import config_provenance


@pytest.fixture(autouse=True)
def _clean() -> None:
    config_provenance.clear_secret_values()
    yield
    config_provenance.clear_secret_values()


def test_none_vault_path_returns_none() -> None:
    assert write_stage_artifact(None, 1, "Stage", "output") is None


def test_dry_run_preview_content_is_redacted(tmp_path: Path) -> None:
    marker = "STAGE-MARKER-do-not-leak"
    config_provenance.register_secret_value(marker)
    result = write_stage_artifact(
        vault_path=tmp_path,
        run_id=1,
        stage_name="Stage One",
        output=f"echoed {marker}",
        dry_run=True,
    )
    assert result is not None
    assert marker not in result["content"]
    assert config_provenance.REDACTED in result["content"]


def test_written_note_is_redacted(tmp_path: Path) -> None:
    marker = "STAGE-WRITE-MARKER-do-not-leak"
    config_provenance.register_secret_value(marker)
    result = write_stage_artifact(
        vault_path=tmp_path,
        run_id=2,
        stage_name="Stage Two",
        output=f"echoed {marker}",
        dry_run=False,
    )
    assert result is not None
    written_path = Path(result["path"])
    written = written_path.read_text(encoding="utf-8")
    assert marker not in written
    assert config_provenance.REDACTED in written


def test_plain_output_unaffected(tmp_path: Path) -> None:
    result = write_stage_artifact(
        vault_path=tmp_path, run_id=3, stage_name="Stage Three", output="clean output", dry_run=True
    )
    assert result is not None
    assert result["content"] == "clean output" or "clean output" in result["content"]
