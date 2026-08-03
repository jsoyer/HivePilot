"""An agent that changed nothing must not kill the pipeline.

When the developer stage produces no changes, its branch has no commits
ahead of the base and GitHub refuses the pull request:

    No commits between staging and hivepilot/noxys (createPullRequest)

That refusal used to raise, which failed the stage, which failed the whole
run — **6 of 8 noxys pipeline runs died exactly there**, after every agent
had already done and been billed for its work. The pipeline completed once
in four, and the recorded reason was `returned non-zero exit status 1`,
because stderr was never captured.

Both halves are under test here: the outcome (nothing to open is not a
failure) and the diagnosability (a real failure says what happened).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hivepilot.forges.github import GitHubForge
from hivepilot.models import GitActions, ProjectConfig


@pytest.fixture
def forge() -> GitHubForge:
    return GitHubForge()


@pytest.fixture
def project(tmp_path: Path) -> ProjectConfig:
    return ProjectConfig(path=tmp_path)


def _raise(stderr: str):
    def _run(*_args, **_kwargs):
        raise subprocess.CalledProcessError(1, ["gh", "pr", "create"], output="", stderr=stderr)

    return _run


def test_no_commits_between_is_not_a_failure(
    forge: GitHubForge, project: ProjectConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The production case: the agent found nothing to change."""
    monkeypatch.setattr(
        subprocess,
        "run",
        _raise("pull request create failed: GraphQL: No commits between staging and hp/x"),
    )

    url = forge.open_pr(project=project, branch="hp/x", git=GitActions(create_pr=True))

    assert url is None, "nothing to open means no URL, not an exception"


def test_the_marker_is_matched_case_insensitively(
    forge: GitHubForge, project: ProjectConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Branch names are interpolated into the message, so it is matched as a
    substring — and GitHub's capitalisation is not a contract."""
    monkeypatch.setattr(subprocess, "run", _raise("GraphQL: NO COMMITS BETWEEN main and hp/x"))

    assert forge.open_pr(project=project, branch="hp/x", git=GitActions(create_pr=True)) is None


def test_a_real_failure_still_raises_and_now_says_why(
    forge: GitHubForge, project: ProjectConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Swallowing every `gh` error would trade one silent failure for another.

    Only the nothing-to-open case is tolerated. Everything else raises — and
    carries the stderr, which it did not before: the operator used to get
    `returned non-zero exit status 1` and nothing else, which is why this
    bug survived eight runs.
    """
    monkeypatch.setattr(subprocess, "run", _raise("HTTP 403: Resource not accessible"))

    with pytest.raises(RuntimeError) as excinfo:
        forge.open_pr(project=project, branch="hp/x", git=GitActions(create_pr=True))

    assert "403" in str(excinfo.value)
    assert "Resource not accessible" in str(excinfo.value)


def test_a_successful_creation_is_unaffected(
    forge: GitHubForge, project: ProjectConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Ok:
        stdout = "https://github.com/o/r/pull/42\n"
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: _Ok())

    url = forge.open_pr(project=project, branch="hp/x", git=GitActions(create_pr=True))

    assert url == "https://github.com/o/r/pull/42"
