"""A vault push must survive a remote that moved.

`commit_vault` committed and then pushed, with nothing in between. The vault
is a shared Obsidian repository: the operator writes notes from their laptop,
and any other run writes to it too. The moment anyone else pushes, every
subsequent HivePilot push is rejected —

    ! [rejected]  main -> main (fetch first)
    Updates were rejected because the remote contains work that you do not
    have locally.

— and it stays rejected forever, because nothing ever pulls. Observed on
run 347: the remote had 54 commits the box did not, the box had 20 it had
never pushed, and the commits for EVERY stage of that run sat locally
unreachable while both Implementation and Review logged `vault.commit_failed`.

The commits were never lost, which is exactly what made it quiet: the stage
logs a warning and carries on, so a pipeline looks healthy while its whole
written record stops leaving the machine.

So a rejected push pulls with rebase and tries once more. Rebase rather than
merge because these are append-only note commits and a merge bubble per run
would bury the history the vault exists to hold. Once more rather than in a
loop: if the second push also fails, something other than a moved remote is
wrong and retrying will not discover what.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hivepilot.services import git_service


class _Git:
    """Records git calls and fails the first push, like a moved remote."""

    def __init__(self, *, pushes_before_success: int = 1) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self._push_failures = pushes_before_success

    def __getattr__(self, name: str):
        # kwargs too: `commit_all` calls `repo.git.update_environment(
        # GIT_AUTHOR_NAME=...)`, and a fake that only models positional args
        # stops modelling the object it stands in for.
        def _call(*args, **kwargs):
            self.calls.append((name, args))
            if name == "push" and self._push_failures > 0:
                self._push_failures -= 1
                raise RuntimeError(
                    "! [rejected] main -> main (fetch first)\n"
                    "Updates were rejected because the remote contains work"
                )
            if name == "diff":
                return "note.md"
            return ""

        return _call


class _Repo:
    head = type("H", (), {"is_detached": False})()
    active_branch = type("B", (), {"name": "main"})()

    def __init__(self, git: _Git) -> None:
        self.git = git


@pytest.fixture
def repo(monkeypatch, tmp_path):
    git = _Git()
    monkeypatch.setattr(git_service, "Repo", lambda *a, **k: _Repo(git))
    return git


def _names(git: _Git) -> list[str]:
    return [name for name, _ in git.calls]


class TestARejectedPushRecovers:
    def test_it_pulls_with_rebase_and_pushes_again(self, repo, tmp_path) -> None:
        git_service.commit_vault(Path(tmp_path), "msg")

        assert _names(repo).count("push") == 2
        assert "pull" in _names(repo)

    def test_the_pull_rebases_rather_than_merges(self, repo, tmp_path) -> None:
        """Append-only note commits: a merge bubble per run would bury the
        history the vault exists to hold."""
        git_service.commit_vault(Path(tmp_path), "msg")

        pull = next(args for name, args in repo.calls if name == "pull")
        assert "--rebase" in pull

    def test_it_reports_success(self, repo, tmp_path) -> None:
        assert git_service.commit_vault(Path(tmp_path), "msg") is True


class TestItDoesNotRetryForever:
    def test_a_second_failure_is_raised_not_swallowed(self, monkeypatch, tmp_path) -> None:
        """If the retry also fails, something other than a moved remote is
        wrong. Looping would not discover what, and swallowing it would
        recreate the silence this fixes — the caller logs it as
        `vault.commit_failed`, which is the honest outcome.
        """
        git = _Git(pushes_before_success=2)
        monkeypatch.setattr(git_service, "Repo", lambda *a, **k: _Repo(git))

        with pytest.raises(RuntimeError):
            git_service.commit_vault(Path(tmp_path), "msg")

        assert _names(git).count("push") == 2


class TestTheHappyPathIsUnchanged:
    def test_a_push_that_works_never_pulls(self, monkeypatch, tmp_path) -> None:
        """No extra network round-trip on the normal path."""
        git = _Git(pushes_before_success=0)
        monkeypatch.setattr(git_service, "Repo", lambda *a, **k: _Repo(git))

        git_service.commit_vault(Path(tmp_path), "msg")

        assert "pull" not in _names(git)
        assert _names(git).count("push") == 1

    def test_nothing_to_commit_still_returns_false(self, monkeypatch, tmp_path) -> None:
        git = _Git(pushes_before_success=0)
        git.__dict__["_empty"] = True
        monkeypatch.setattr(git_service, "Repo", lambda *a, **k: _Repo(git))
        monkeypatch.setattr(
            _Git, "__getattr__", lambda self, n: lambda *a: "" if n == "diff" else ""
        )

        assert git_service.commit_vault(Path(tmp_path), "msg") is False
