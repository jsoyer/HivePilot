"""A pipeline cannot iterate on its own PR, and this is why.

Run 404 was the second pass over the same objective. It completed intake,
spec, synthesis, CTO, design and implementation — $16.30 of work — and then
died at the review stage:

    Failed to create PR for noxys: Command '[... 'gh','pr','create',
    '--head','hivepilot/noxys' ...]' returned non-zero exit status 1:
    a pull request for branch "hivepilot/noxys" into branch "staging"
    already exists:
    https://github.com/noxys-eu/noxys/pull/425

Security, QA, design review, documentation and the release gate never ran.
Nothing was wrong: the branch was pushed, the PR was updated, and #425 is
exactly where the work landed. `gh pr create` simply refuses to create a
second PR for a head branch that already has one — correctly.

This is the same shape the file already handles one line above, for
`_NO_COMMITS_MARKER`: an agent that changed nothing is "an ordinary outcome,
not a failure", and that comment records 6 of 8 runs dying there before it
was fixed. An agent that changed something on a branch whose PR is already
open is equally ordinary — and it is the *normal* case for any second pass,
which makes it a hard block on iterating autonomously rather than an edge
case.

Better than returning None: `gh` prints the existing PR's URL in the very
message it refuses with, so the forge itself reports it. That satisfies
`extract_pr_url`'s standing contract — a URL the forge reported, never one
this codebase assembled from a slug and a guessed number.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from hivepilot.forges.github import GitHubForge
from hivepilot.models import GitActions, ProjectConfig

_EXISTING = (
    'a pull request for branch "hivepilot/noxys" into branch "staging" already exists:\n'
    "https://github.com/noxys-eu/noxys/pull/425\n"
)


@pytest.fixture
def forge() -> GitHubForge:
    return GitHubForge()


def _refuse(stderr: str):
    return subprocess.CalledProcessError(returncode=1, cmd=["gh", "pr", "create"], stderr=stderr)


class TestAnExistingPrIsNotAFailure:
    def test_it_does_not_raise(self, forge: GitHubForge, tmp_path: Path) -> None:
        """The stage that opens the PR must not kill the five stages after
        it for a PR that is already open."""
        git = GitActions(create_pr=True)
        with patch("hivepilot.forges.github.subprocess.run", side_effect=_refuse(_EXISTING)):
            url = forge.open_pr(
                project=ProjectConfig(path=tmp_path), branch="hivepilot/noxys", git=git
            )

        assert url == "https://github.com/noxys-eu/noxys/pull/425"

    def test_the_url_comes_from_gh_not_from_us(self, forge: GitHubForge, tmp_path: Path) -> None:
        """`extract_pr_url`'s contract: a URL the forge reported, never one
        assembled from a slug and a guessed number. When `gh` refuses without
        naming a URL there is nothing to report, and None is the honest
        answer — the run continues either way."""
        git = GitActions(create_pr=True)
        bare = "a pull request for branch X into branch Y already exists:"
        with patch("hivepilot.forges.github.subprocess.run", side_effect=_refuse(bare)):
            url = forge.open_pr(project=ProjectConfig(path=tmp_path), branch="hivepilot/x", git=git)

        assert url is None

    def test_it_is_case_insensitive(self, forge: GitHubForge, tmp_path: Path) -> None:
        """Matched the same way `_NO_COMMITS_MARKER` is: `gh`'s wording is
        not a stable API and capitalisation is the cheapest thing to get
        wrong."""
        git = GitActions(create_pr=True)
        shouty = (
            'A Pull Request For Branch "b" Into Branch "staging" ALREADY EXISTS:\nhttps://x/pull/9'
        )
        with patch("hivepilot.forges.github.subprocess.run", side_effect=_refuse(shouty)):
            url = forge.open_pr(project=ProjectConfig(path=tmp_path), branch="b", git=git)

        assert url == "https://x/pull/9"


class TestEveryOtherFailureStillFails:
    def test_an_unrecognised_gh_error_still_raises(
        self, forge: GitHubForge, tmp_path: Path
    ) -> None:
        """The point is to stop treating one ordinary outcome as fatal, not
        to stop reporting real breakage. An auth failure must still stop the
        pipeline."""
        git = GitActions(create_pr=True)
        with patch(
            "hivepilot.forges.github.subprocess.run",
            side_effect=_refuse("HTTP 401: Bad credentials"),
        ):
            with pytest.raises(RuntimeError, match="Failed to create PR"):
                forge.open_pr(project=ProjectConfig(path=tmp_path), branch="b", git=git)

    def test_a_non_subprocess_error_still_raises(self, forge: GitHubForge, tmp_path: Path) -> None:
        git = GitActions(create_pr=True)
        with patch("hivepilot.forges.github.subprocess.run", side_effect=OSError("boom")):
            with pytest.raises(RuntimeError, match="Failed to create PR"):
                forge.open_pr(project=ProjectConfig(path=tmp_path), branch="b", git=git)

    def test_no_commits_is_still_handled_separately(
        self, forge: GitHubForge, tmp_path: Path
    ) -> None:
        """The pre-existing case must keep its own behaviour: nothing was
        opened, so there is no URL to report."""
        git = GitActions(create_pr=True)
        with patch(
            "hivepilot.forges.github.subprocess.run",
            side_effect=_refuse("No commits between staging and hivepilot/x"),
        ):
            url = forge.open_pr(project=ProjectConfig(path=tmp_path), branch="hivepilot/x", git=git)

        assert url is None
