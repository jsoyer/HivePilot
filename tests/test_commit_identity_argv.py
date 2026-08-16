"""`-c user.name=...` belongs to `git`, not to `git commit`.

The identity fix — added because systemd units have no HOME and a freshly
cloned repo has no `user.name` — was implemented as:

    repo.git.commit("-c", "user.name=HivePilot", "-m", message)

GitPython puts every argument AFTER the subcommand, so that runs

    git commit -c user.name=HivePilot -m "feat: ..."

and `-c` after `commit` is commit's own `--reedit-message` flag. Git refuses:

    fatal: options '-m' and '-c' cannot be used together

So HivePilot's own commit has been failing outright. Deliveries only ever
succeeded where the AGENT had already committed its work — the branch carries
those commits, so push and PR still proceeded and the breakage stayed invisible
until three forage runs in a row were marked failed with an empty step detail.

The fix is placement, not content: the identity flags must precede the
subcommand.
"""

from __future__ import annotations

import subprocess

import pytest

from hivepilot.services import git_service


@pytest.fixture
def repo(tmp_path):
    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=tmp_path, check=True, capture_output=True, text=True
        ).stdout

    git("init", "-q", "-b", "main")
    # No user.name / user.email on purpose: that absence is the whole reason
    # the identity flags exist, and a repo carrying a local identity is exactly
    # what hid this bug on the noxys checkout.
    (tmp_path / "file.txt").write_text("content\n")
    # Staging belongs to the caller in both real call sites; `commit_all`
    # only commits.
    git("add", "-A")
    return tmp_path, git


class TestTheIdentityFlagsPrecedeTheSubcommand:
    def test_a_commit_succeeds_without_any_configured_identity(self, repo):
        """The whole defect in one assertion: this raised
        `options '-m' and '-c' cannot be used together`."""
        path, git = repo

        git_service.commit_all(path, "feat: a message")

        assert git("log", "-1", "--format=%s").strip() == "feat: a message"

    def test_the_commit_carries_the_declared_identity(self, repo):
        path, git = repo

        git_service.commit_all(path, "feat: identity")

        assert git("log", "-1", "--format=%an").strip() == git_service.COMMIT_IDENTITY_NAME
        assert git("log", "-1", "--format=%ae").strip() == git_service.COMMIT_IDENTITY_EMAIL

    def test_it_does_not_reuse_an_existing_commit_message(self, repo):
        """`-c <commit>` after the subcommand means 'reedit that commit's
        message'. Placement is the bug, so assert the message is OURS."""
        path, git = repo
        git("-c", "user.name=X", "-c", "user.email=x@y.invalid", "commit", "-qam", "seed")
        (path / "file.txt").write_text("changed\n")
        git("add", "-A")

        git_service.commit_all(path, "feat: not the seed message")

        assert git("log", "-1", "--format=%s").strip() == "feat: not the seed message"
