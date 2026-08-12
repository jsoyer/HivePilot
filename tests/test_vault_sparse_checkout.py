"""A vault on a sparse checkout silently dropped everything HivePilot wrote.

Measured on the production box, run 507::

    git add -A -- /var/lib/hivepilot/home/jsoyer-obsidian-vault
    The following paths and/or pathspecs matched paths that exist outside of
    your sparse-checkout definition, so will not be updated in the index:
      HivePilot/Interactions/2026-08-12-gustave-developer-completed-stage.md
      HivePilot/Runs/2026-08-12-run496-review.md
      Artifacts/ciso/2026-08-12-run496-security.md
      ... 90 more

The operator's vault uses a cone limited to their own notes directory.
HivePilot writes under ``HivePilot/`` and ``Artifacts/`` -- outside it. So
``git add`` refused every path, raised, and the caller logged
``vault.commit_failed`` and carried on.

Three days of run records were on disk and in no commit. The pipeline looked
healthy throughout, which is what made it quiet: the written record is the one
artifact whose absence nothing else notices.

The existing vault tests all drive a *fake* git object, so none of them could
see this -- a stub answers ``add`` the same whatever the cone says. These use a
real repository with a real sparse-checkout, because the defect lives entirely
in git's own behaviour.

git's hint names the fix itself: "If you intend to update such entries, try
--sparse". HivePilot wrote these files deliberately, one line before asking to
commit them; refusing to stage them destroys the operator's record rather than
protecting it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hivepilot.services import git_service


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def sparse_vault(tmp_path: Path) -> Path:
    """A vault whose cone excludes the paths HivePilot writes to.

    This is the operator's real layout: the cone holds their own notes, and
    HivePilot's directories were never part of it.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    _git(vault, "init", "-q", "-b", "main")
    _git(vault, "config", "user.name", "Test")
    _git(vault, "config", "user.email", "test@example.invalid")

    owner_notes = vault / "Jsoyer"
    owner_notes.mkdir()
    (owner_notes / "note.md").write_text("the operator's own note\n")
    hivepilot_dir = vault / "HivePilot"
    hivepilot_dir.mkdir()
    (hivepilot_dir / "seed.md").write_text("seed\n")
    _git(vault, "add", "-A")
    _git(vault, "commit", "-qm", "initial")

    # The cone the operator actually set: their notes only.
    _git(vault, "sparse-checkout", "set", "Jsoyer")

    # What the notifier writes, one line before asking for a commit.
    runs = vault / "HivePilot" / "Runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "2026-08-12-run507-ceo-intake.md").write_text("the run record\n")
    artifacts = vault / "Artifacts" / "ciso"
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "2026-08-12-run507-security.md").write_text("the findings\n")
    return vault


def _committed_paths(vault: Path) -> set[str]:
    return set(_git(vault, "show", "--pretty=", "--name-only", "HEAD").split())


class TestASparseVaultStillRecordsTheRun:
    def test_the_run_record_reaches_a_commit(self, sparse_vault: Path) -> None:
        """The whole defect in one assertion: written, and never committed."""
        assert git_service.commit_vault(sparse_vault, "HivePilot: run 507", push=False) is True

        committed = _committed_paths(sparse_vault)
        assert "HivePilot/Runs/2026-08-12-run507-ceo-intake.md" in committed
        assert "Artifacts/ciso/2026-08-12-run507-security.md" in committed

    def test_the_working_tree_keeps_the_files(self, sparse_vault: Path) -> None:
        """Staging outside the cone must not make git prune them back out.

        A "fix" that committed the note and then removed it from disk would
        trade a silent loss for a louder one.
        """
        git_service.commit_vault(sparse_vault, "msg", push=False)

        assert (sparse_vault / "HivePilot" / "Runs" / "2026-08-12-run507-ceo-intake.md").exists()

    def test_nothing_outside_the_vault_is_touched(self, sparse_vault: Path) -> None:
        """The cone is the operator's; HivePilot only adds its own writes."""
        git_service.commit_vault(sparse_vault, "msg", push=False)

        assert _git(sparse_vault, "sparse-checkout", "list").split() == ["Jsoyer"]


class TestAnOrdinaryVaultIsUnchanged:
    """Most vaults are not sparse. Their behaviour must be byte-identical."""

    def test_a_full_checkout_still_commits(self, tmp_path: Path) -> None:
        vault = tmp_path / "plain"
        vault.mkdir()
        _git(vault, "init", "-q", "-b", "main")
        _git(vault, "config", "user.name", "Test")
        _git(vault, "config", "user.email", "test@example.invalid")
        (vault / "seed.md").write_text("seed\n")
        _git(vault, "add", "-A")
        _git(vault, "commit", "-qm", "initial")
        (vault / "note.md").write_text("a note\n")

        assert git_service.commit_vault(vault, "msg", push=False) is True
        assert "note.md" in _committed_paths(vault)

    def test_no_change_still_reports_nothing_to_commit(self, tmp_path: Path) -> None:
        vault = tmp_path / "quiet"
        vault.mkdir()
        _git(vault, "init", "-q", "-b", "main")
        _git(vault, "config", "user.name", "Test")
        _git(vault, "config", "user.email", "test@example.invalid")
        (vault / "seed.md").write_text("seed\n")
        _git(vault, "add", "-A")
        _git(vault, "commit", "-qm", "initial")

        assert git_service.commit_vault(vault, "msg", push=False) is False
