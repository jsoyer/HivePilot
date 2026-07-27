"""Tests for hivepilot.services.pr_body -- the shared PR/MR body resolver
used by git_service.create_pr for EVERY forge (github/forgejo/gitlab).

`GitActions.pr_body_file` is a task-config-declared filename the engine
itself never writes -- it exists so an agent stage can hand the forge a
richer PR description as a side effect of its own work. Before this module
existed, nothing verified the declared file actually existed: a missing file
either crashed `gh pr create --body-file <path>` (github) or raised an
unhandled `FileNotFoundError` reading the path directly (forgejo/gitlab) --
either way losing the stage's entire output after the full cost of running
it. These tests prove: a present, non-blank file is used untouched; an
absent/blank one degrades gracefully to a redacted, size-capped fallback
body (never an empty PR); and the substitution is always logged loudly, not
swallowed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hivepilot.models import GitActions
from hivepilot.services.config_provenance import (
    clear_secret_values,
    register_secret_value,
)
from hivepilot.services.pr_body import DEFAULT_PR_BODY, resolve_pr_body_file


@pytest.fixture(autouse=True)
def _clean_secret_registry():
    clear_secret_values()
    yield
    clear_secret_values()


def test_present_nonblank_file_used_unchanged(tmp_path: Path) -> None:
    """Regression guard: a valid declared file's content reaches the forge
    completely unchanged (no redaction, no truncation, no substitution)."""
    body_file = tmp_path / "PR_BODY.md"
    original = "## Summary\nActual PR body content written by the agent stage.\n"
    body_file.write_text(original, encoding="utf-8")
    git = GitActions(pr_body_file="PR_BODY.md")

    with resolve_pr_body_file(base_dir=tmp_path, git=git, fallback_text="ignored") as resolved:
        assert resolved is not None
        assert resolved.read_text(encoding="utf-8") == original


def test_absent_declared_file_falls_back(tmp_path: Path) -> None:
    """The declared file was never written by the agent -- PR creation must
    still get a meaningful, non-empty body instead of crashing."""
    git = GitActions(pr_body_file="PR_BODY.md")  # never written to tmp_path

    with resolve_pr_body_file(
        base_dir=tmp_path, git=git, fallback_text="Stage produced this real output."
    ) as resolved:
        assert resolved is not None
        content = resolved.read_text(encoding="utf-8")

    assert "Stage produced this real output." in content


def test_absent_declared_file_warns_loudly(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Requirement: never silently degrade -- a warning must name the missing
    file, the directory searched, and that a substitution happened."""
    git = GitActions(pr_body_file="PR_BODY.md")

    with caplog.at_level("WARNING"):
        with resolve_pr_body_file(base_dir=tmp_path, git=git, fallback_text="output") as _resolved:
            pass

    joined = "\n".join(
        str(r.getMessage()) + str(getattr(r, "__dict__", {})) for r in caplog.records
    )
    assert "PR_BODY.md" in joined
    assert str(tmp_path) in joined


def test_empty_declared_file_treated_as_absent(tmp_path: Path) -> None:
    """A whitespace-only declared file must NOT become an empty PR body --
    it degrades to the fallback exactly like a missing file."""
    body_file = tmp_path / "PR_BODY.md"
    body_file.write_text("   \n\n   \t\n", encoding="utf-8")
    git = GitActions(pr_body_file="PR_BODY.md")

    with resolve_pr_body_file(
        base_dir=tmp_path, git=git, fallback_text="real stage output here"
    ) as resolved:
        assert resolved is not None
        content = resolved.read_text(encoding="utf-8")

    assert content.strip() == "real stage output here"


def test_fallback_redacts_registered_secret(tmp_path: Path) -> None:
    """Agent-produced fallback content flows through the established
    redaction choke point (config_provenance.redact_text) before it can ever
    reach a (possibly public) PR."""
    secret = "sk-supersecrettoken1234567890"
    register_secret_value(secret)
    git = GitActions(pr_body_file="PR_BODY.md")  # declared but never written

    with resolve_pr_body_file(
        base_dir=tmp_path,
        git=git,
        fallback_text=f"Ran successfully using token {secret} against the API.",
    ) as resolved:
        assert resolved is not None
        content = resolved.read_text(encoding="utf-8")

    assert secret not in content
    assert "REDACTED" in content


def test_oversized_fallback_is_capped_with_in_band_notice(tmp_path: Path) -> None:
    huge = "x" * 200_000
    git = GitActions(pr_body_file="PR_BODY.md")  # declared but never written

    with resolve_pr_body_file(base_dir=tmp_path, git=git, fallback_text=huge) as resolved:
        assert resolved is not None
        content = resolved.read_text(encoding="utf-8")

    assert len(content) < 200_000
    assert "truncat" in content.lower()


def test_declared_file_missing_no_fallback_text_uses_default_message(tmp_path: Path) -> None:
    git = GitActions(pr_body_file="PR_BODY.md")  # declared but never written

    with resolve_pr_body_file(base_dir=tmp_path, git=git, fallback_text=None) as resolved:
        assert resolved is not None
        content = resolved.read_text(encoding="utf-8")

    assert content == DEFAULT_PR_BODY


def test_no_declared_file_yields_none_and_never_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A task that never opted into pr_body_file shouldn't get a spurious
    'missing file' warning, NOR should its `git` be touched at all -- every
    forge already has its own correct, tested "no custom body" default for
    this case (see docstring on resolve_pr_body_file)."""
    git = GitActions()

    with caplog.at_level("WARNING"):
        with resolve_pr_body_file(base_dir=tmp_path, git=git, fallback_text="stuff") as resolved:
            assert resolved is None

    assert not caplog.records


def test_fallback_temp_file_cleaned_up_on_exit(tmp_path: Path) -> None:
    git = GitActions(pr_body_file="PR_BODY.md")  # declared but never written

    with resolve_pr_body_file(base_dir=tmp_path, git=git, fallback_text="hello") as resolved:
        assert resolved is not None
        path = resolved
        assert path.exists()

    assert not path.exists()


def test_present_file_not_deleted_on_exit(tmp_path: Path) -> None:
    """Only fallback tempfiles we created ourselves get cleaned up -- the
    agent's own declared file must never be touched."""
    body_file = tmp_path / "PR_BODY.md"
    body_file.write_text("agent content", encoding="utf-8")
    git = GitActions(pr_body_file="PR_BODY.md")

    with resolve_pr_body_file(base_dir=tmp_path, git=git, fallback_text=None) as resolved:
        assert resolved is not None
        path = resolved

    assert path.exists()
    assert path.read_text(encoding="utf-8") == "agent content"


def test_relative_declared_path_resolved_against_base_dir_not_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sibling-risk fix: forgejo/gitlab used to read `Path(pr_body_file)`
    directly, which resolves relative paths against the ORCHESTRATOR
    process's cwd, not the project's own working directory. A relative
    `pr_body_file` must resolve against `base_dir` regardless of the
    process's current working directory."""
    other_dir = tmp_path / "somewhere-else"
    other_dir.mkdir()
    monkeypatch.chdir(other_dir)

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "PR_BODY.md").write_text("project-local content", encoding="utf-8")
    git = GitActions(pr_body_file="PR_BODY.md")

    with resolve_pr_body_file(base_dir=project_dir, git=git, fallback_text=None) as resolved:
        assert resolved is not None
        content = resolved.read_text(encoding="utf-8")

    assert content == "project-local content"
