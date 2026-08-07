"""Bring the vault up to date before agents read from it.

The vault is not only written, it is READ: `plugins/obsidian.py`'s `recall`
pulls excerpts into the prompt, and run 347 shows it doing so —
`plugin.obsidian.recalled count=5` on the reviewer.

Nothing has ever refreshed the clone. Verified by grep: no fetch, no pull,
anywhere in the orchestrator's vault handling. So the notes agents recall
are as old as the last time someone happened to push from that machine — on
the reference box, 54 commits behind, for an unknown number of runs.

That is worse than the push failure it sits next to. A rejected push at
least logs a warning; a stale read produces a confident answer built on
superseded notes, and nothing anywhere says so.

So the vault is refreshed once at the start of a run, and **the outcome is
always reported**. A refresh that could not happen must be visible, because
"the notes are current" and "the notes are whatever was here last time" look
identical in every downstream artifact.

Never raises, never blocks: an unreachable remote is a reason to run on the
notes we have, not a reason to fail the pipeline. But it says which one it
did.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hivepilot.services import git_service


class _Git:
    def __init__(self, *, fails: str | None = None) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self._fails = fails

    def __getattr__(self, name: str):
        def _call(*args):
            self.calls.append((name, args))
            if self._fails == name:
                raise RuntimeError("could not reach origin")
            return ""

        return _call


class _Repo:
    def __init__(self, git: _Git, *, detached: bool = False) -> None:
        self.git = git
        self.head = type("H", (), {"is_detached": detached})()
        self.active_branch = type("B", (), {"name": "main"})()


def _names(git: _Git) -> list[str]:
    return [n for n, _ in git.calls]


class TestItBringsTheCloneForward:
    def test_it_pulls_before_anyone_reads(self, monkeypatch, tmp_path) -> None:
        git = _Git()
        monkeypatch.setattr(git_service, "Repo", lambda *a, **k: _Repo(git))

        assert git_service.refresh_vault(Path(tmp_path)) is True
        assert "pull" in _names(git)

    def test_it_rebases_rather_than_merging(self, monkeypatch, tmp_path) -> None:
        """Same reason as the push path: append-only note commits, and a
        merge bubble per run would bury the history the vault holds."""
        git = _Git()
        monkeypatch.setattr(git_service, "Repo", lambda *a, **k: _Repo(git))

        git_service.refresh_vault(Path(tmp_path))

        pull = next(args for name, args in git.calls if name == "pull")
        assert "--rebase" in pull

    def test_it_does_not_discard_local_work(self, monkeypatch, tmp_path) -> None:
        """A stage may have committed or dirtied the tree already. Refreshing
        must never be a way to lose that -- `--autostash` moves it aside and
        puts it back, and nothing here resets or checks out."""
        git = _Git()
        monkeypatch.setattr(git_service, "Repo", lambda *a, **k: _Repo(git))

        git_service.refresh_vault(Path(tmp_path))

        pull = next(args for name, args in git.calls if name == "pull")
        assert "--autostash" in pull
        assert "reset" not in _names(git)
        assert "checkout" not in _names(git)


class TestItNeverBlocksTheRun:
    def test_an_unreachable_remote_is_survived(self, monkeypatch, tmp_path) -> None:
        """A vault we cannot reach is a reason to run on the notes we have,
        not a reason to fail a pipeline."""
        git = _Git(fails="pull")
        monkeypatch.setattr(git_service, "Repo", lambda *a, **k: _Repo(git))

        assert git_service.refresh_vault(Path(tmp_path)) is False

    def test_a_non_repo_is_survived(self, monkeypatch, tmp_path) -> None:
        def _boom(*a, **k):
            raise RuntimeError("not a git repo")

        monkeypatch.setattr(git_service, "Repo", _boom)

        assert git_service.refresh_vault(Path(tmp_path)) is False

    def test_a_detached_head_is_not_pulled(self, monkeypatch, tmp_path) -> None:
        """There is no branch to pull onto, and guessing one would move a
        deliberately pinned checkout."""
        git = _Git()
        monkeypatch.setattr(git_service, "Repo", lambda *a, **k: _Repo(git, detached=True))

        assert git_service.refresh_vault(Path(tmp_path)) is False
        assert "pull" not in _names(git)


class TestTheOutcomeIsAlwaysVisible:
    @pytest.mark.parametrize("failure", [None, "pull"])
    def test_it_logs_either_way(self, monkeypatch, tmp_path, caplog, failure) -> None:
        """ "The notes are current" and "the notes are whatever was here last
        time" look identical in every downstream artifact. Only the log can
        tell them apart, so both outcomes have to reach it."""
        git = _Git(fails=failure)
        monkeypatch.setattr(git_service, "Repo", lambda *a, **k: _Repo(git))

        with caplog.at_level("INFO"):
            git_service.refresh_vault(Path(tmp_path))

        assert any("vault" in record.getMessage() for record in caplog.records)
