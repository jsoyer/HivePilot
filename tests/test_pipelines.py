"""Tests for hivepilot.pipelines — write_stage_artifact redacts registered
secret values from the vault note body before it is written (or returned in
the dry_run preview dict); also covers the canonical `<artifacts>/<role>/`
copy (vault-canonical-artifacts PRD)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import conftest
import pytest

from hivepilot.pipelines import write_stage_artifact
from hivepilot.services import config_provenance


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
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


# ---------------------------------------------------------------------------
# Canonical `<artifacts>/<role>/` copy (vault-canonical-artifacts PRD)
# ---------------------------------------------------------------------------


class TestCanonicalArtifactCopy:
    def test_role_writes_artifact_under_role_subfolder(self, tmp_path: Path) -> None:
        result = write_stage_artifact(
            vault_path=tmp_path,
            run_id=43,
            stage_name="CTO Review",
            output="the technical spec deliverable",
            dry_run=False,
            role="cto",
        )
        assert result is not None  # run-copy return value untouched

        artifact_files = list(
            (tmp_path / conftest.TEST_VAULT_ARTIFACTS_FOLDER / "cto").glob("*.md")
        )
        assert len(artifact_files) == 1, f"Expected 1 artifact file, found: {artifact_files}"
        content = artifact_files[0].read_text(encoding="utf-8")
        assert "the technical spec deliverable" in content
        assert "type: artifact" in content
        assert "role: cto" in content
        assert "run_id: 43" in content
        assert "stage: CTO Review" in content

    def test_no_role_skips_artifact_copy(self, tmp_path: Path) -> None:
        write_stage_artifact(
            vault_path=tmp_path,
            run_id=1,
            stage_name="Stage One",
            output="output",
            dry_run=False,
            role=None,
        )
        assert not (tmp_path / conftest.TEST_VAULT_ARTIFACTS_FOLDER).exists()

    def test_developer_role_skips_artifact_copy(self, tmp_path: Path) -> None:
        """The developer stage's deliverable is a code diff for the repo, not
        the vault -- excluded from the canonical artifact copy."""
        write_stage_artifact(
            vault_path=tmp_path,
            run_id=1,
            stage_name="Implementation",
            output="diff --git a/foo.py b/foo.py",
            dry_run=False,
            role="developer",
        )
        assert not (tmp_path / conftest.TEST_VAULT_ARTIFACTS_FOLDER).exists()

    def test_blank_output_skips_artifact_copy(self, tmp_path: Path) -> None:
        """Whitespace-only output must not produce an empty artifact file."""
        write_stage_artifact(
            vault_path=tmp_path,
            run_id=1,
            stage_name="Empty Stage",
            output="   \n  \t\n",
            dry_run=False,
            role="pm",
        )
        assert not (tmp_path / conftest.TEST_VAULT_ARTIFACTS_FOLDER).exists()

    def test_artifact_copy_redacts_secret(self, tmp_path: Path) -> None:
        marker = "ARTIFACT-MARKER-do-not-leak"
        config_provenance.register_secret_value(marker)
        write_stage_artifact(
            vault_path=tmp_path,
            run_id=9,
            stage_name="PM Brief",
            output=f"echoed {marker}",
            dry_run=False,
            role="pm",
        )
        artifact_files = list((tmp_path / conftest.TEST_VAULT_ARTIFACTS_FOLDER / "pm").glob("*.md"))
        assert len(artifact_files) == 1
        content = artifact_files[0].read_text(encoding="utf-8")
        assert marker not in content
        assert config_provenance.REDACTED in content

    def test_artifact_write_failure_is_best_effort(self, tmp_path: Path) -> None:
        """A vault-write error on the artifact copy must never raise or
        prevent the run-copy return value from being produced."""
        with patch(
            "hivepilot.services.obsidian_service.ObsidianService.write_artifact",
            side_effect=OSError("disk full"),
        ):
            result = write_stage_artifact(
                vault_path=tmp_path,
                run_id=1,
                stage_name="Stage One",
                output="output",
                dry_run=False,
                role="cto",
            )
        # Run-copy still succeeds; no exception propagated.
        assert result is not None
        assert result["dry_run"] is False

    def test_dry_run_artifact_copy_no_file_written(self, tmp_path: Path) -> None:
        write_stage_artifact(
            vault_path=tmp_path,
            run_id=1,
            stage_name="CTO Review",
            output="deliverable",
            dry_run=True,
            role="cto",
        )
        assert not (tmp_path / conftest.TEST_VAULT_ARTIFACTS_FOLDER).exists()
