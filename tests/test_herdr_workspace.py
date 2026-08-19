"""One worktree per run, and every role's pane inside it.

Step 3. Step 2 put a step's process in a pane; it created that pane with
`pane split --current`, which means *whatever workspace the operator happens to
be looking at*. With one workspace that is invisible. With one per run it is
wrong in a way nobody would suspect: steps land in another run's workspace, and
merely focusing a different tab moves where agents appear.

So a run gets a workspace of its own, and panes are targeted at it by id.

Every argv shape below was read off herdr 0.8.0 on the box, not recalled:

    worktree create --cwd <repo> --branch <b> --base <ref> --label <l>
        -> result.workspace.workspace_id, result.root_pane.pane_id,
           result.worktree.path

    pane split --pane <ID>      -> lands in <ID>'s workspace even when the
                                   operator is focused elsewhere (measured
                                   against two workspaces, which is the only
                                   arrangement that can tell the two apart)

`--workspace` is deliberately NOT passed to `worktree create`: it creates one
and hands back its id, so a separate `workspace create` call would buy nothing
and add a failure mode.
"""

from __future__ import annotations

import subprocess

import pytest

from hivepilot.services.herdr_workspace import (
    RunWorkspace,
    WorkspaceError,
    close_run_workspace,
    current_pane,
    open_run_workspace,
    run_workspace,
    workspace_for_path,
)

CREATED = {
    "id": "cli:worktree:create",
    "result": {
        "type": "worktree_created",
        "root_pane": {"pane_id": "w3:p1", "workspace_id": "w3"},
        "workspace": {"workspace_id": "w3", "label": "noxys run 742"},
        "worktree": {
            "branch": "hivepilot/noxys/742",
            "path": "/home/h/.herdr/worktrees/noxys/hivepilot-noxys-742",
        },
    },
}


class FakeHerdr:
    def __init__(self, payload=CREATED, returncode=0):
        self.calls: list[list[str]] = []
        self.payload = payload
        self.returncode = returncode

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        import json

        return subprocess.CompletedProcess(argv, self.returncode, json.dumps(self.payload), "")

    def argv_for(self, verb):
        return next(c for c in self.calls if verb in c)


class TestClosingIt:
    def test_the_workspace_is_closed_by_id(self):
        herdr = FakeHerdr()

        close_run_workspace(RunWorkspace("w3", "w3:p1", "/co", "b"), run_cli=herdr)

        assert herdr.calls[-1] == ["herdr", "workspace", "close", "w3"]

    def test_closing_never_raises(self):
        """A workspace left open is a leak; a failure to close one is not
        worth losing a finished run's results over."""
        close_run_workspace(
            RunWorkspace("w3", "w3:p1", "/co", "b"), run_cli=FakeHerdr(returncode=1)
        )


OPENED = {
    "id": "cli:worktree:open",
    "result": {
        "type": "worktree_opened",
        "already_open": False,
        "root_pane": {"pane_id": "w9:p1", "cwd": "/srv/noxys/.hivepilot-wt/abc"},
        "workspace": {"workspace_id": "w9", "label": "noxys run 742"},
        "worktree": {"path": "/srv/noxys/.hivepilot-wt/abc", "is_detached": True},
    },
}


class TestAdoptingTheWorktreeTheEngineAlreadyMade:
    """HivePilot does not need herdr to CREATE a worktree. It already makes one
    per run -- `git_service.isolated_worktree`, at `<repo>/.hivepilot-wt/<uuid>`,
    with `git worktree add --detach`.

    Detached is the whole reason there is no collision with the
    `hivepilot/<project>/<run_id>` branch that `perform_git_actions` checks out
    in the project directory. I described that collision as blocking before
    reading this function; it came from a design I had invented, not from the
    code. Two branches cannot share a worktree -- but a detached worktree
    claims no branch.

    So herdr ADOPTS the existing tree rather than making a second one, and
    HivePilot keeps sole ownership of creating and removing it.

    Probed on the box against 0.8.0: `worktree open` is contextual. It answers
    `not_git_worktree` unless the CALLING workspace is inside a git work tree,
    so `--cwd <repo>` selects that context and `--path <worktree>` names the
    tree. `--path` alone fails.
    """

    @staticmethod
    def _open(herdr, **kw):
        kw.setdefault("repo", "/srv/noxys")
        kw.setdefault("worktree_path", "/srv/noxys/.hivepilot-wt/abc")
        kw.setdefault("label", "noxys run 742")
        return open_run_workspace(run_cli=herdr, **kw)

    def test_it_adopts_rather_than_creating(self):
        herdr = FakeHerdr(payload=OPENED)

        self._open(herdr)

        assert herdr.calls[0][:3] == ["herdr", "worktree", "open"]
        assert not any("create" in c for c in herdr.calls)

    def test_both_the_context_and_the_path_are_sent(self):
        """`--path` alone answers `not_git_worktree` -- the command is
        contextual on the calling workspace, not on the path."""
        herdr = FakeHerdr(payload=OPENED)

        self._open(herdr)

        argv = herdr.calls[0]
        assert argv[argv.index("--cwd") + 1] == "/srv/noxys"
        assert argv[argv.index("--path") + 1] == "/srv/noxys/.hivepilot-wt/abc"

    def test_the_workspace_and_root_pane_come_back(self):
        ws = self._open(FakeHerdr(payload=OPENED))

        assert ws.workspace_id == "w9"
        assert ws.root_pane_id == "w9:p1"

    def test_the_checkout_is_the_tree_the_engine_made(self):
        """Not a path herdr chose. The step already runs there."""
        ws = self._open(FakeHerdr(payload=OPENED))

        assert ws.checkout_path == "/srv/noxys/.hivepilot-wt/abc"

    def test_a_refusal_raises_rather_than_falling_back(self):
        """Falling back to the focused workspace would put a live agent in
        another run's tree, and the run would never know."""
        with pytest.raises(WorkspaceError):
            self._open(FakeHerdr(payload={"error": {"code": "not_git_worktree"}}, returncode=1))

    def test_no_branch_is_named(self):
        """The tree is DETACHED. Naming a branch would either fail or, worse,
        move one."""
        herdr = FakeHerdr(payload=OPENED)

        self._open(herdr)

        assert "--branch" not in herdr.calls[0]


class TestARunWithNoWorktree:
    """The engine's worktree is CONDITIONAL: `worktree_isolation`, not
    simulating, `auto_git`, the task actually commits or pushes, and a git
    repo. A run failing any of those works in the project directory and has no
    tree to adopt."""

    def test_a_plain_workspace_is_created_over_the_project(self):
        payload = {"result": {"workspace": {"workspace_id": "w4"}}}
        herdr = FakeHerdr(payload=payload)

        ws = workspace_for_path(path="/srv/noxys", label="run 9", run_cli=herdr)

        assert herdr.calls[0][:3] == ["herdr", "workspace", "create"]
        assert ws.workspace_id == "w4"

    def test_no_worktree_is_invented_for_it(self):
        """A new worktree would have to claim a branch, and the only sensible
        branch is the one `perform_git_actions` checks out in the project
        directory -- which git refuses to have in two worktrees at once."""
        herdr = FakeHerdr(payload={"result": {"workspace": {"workspace_id": "w4"}}})

        workspace_for_path(path="/srv/noxys", label="run 9", run_cli=herdr)

        assert not any("worktree" in c for c in herdr.calls)
        assert not any("--branch" in c for c in herdr.calls)

    def test_it_refuses_rather_than_guessing(self):
        with pytest.raises(WorkspaceError):
            workspace_for_path(path="/srv/noxys", label="l", run_cli=FakeHerdr(returncode=1))


class TestBranchNamingHasOneSource:
    """The test that should have existed before I wrote a second namer.

    `git_service.build_branch_name` already builds `<prefix>/<project>/<run_id>`
    from the CONFIGURED `git.branch_prefix`. I added a `run_branch_name` here
    that hardcoded `hivepilot/` -- not merely duplication, a divergence: a
    deployment with a custom prefix would get two different names depending on
    which function was called, and one of them would be the branch nobody
    pushes.
    """

    def test_this_module_does_not_name_branches(self):
        import hivepilot.services.herdr_workspace as mod

        assert not [n for n in dir(mod) if "branch_name" in n]

    def test_the_one_namer_honours_the_configured_prefix(self):
        from hivepilot.services.git_service import build_branch_name

        assert (
            build_branch_name(prefix="acme", project_name="noxys", run_id=742) == "acme/noxys/742"
        )


class TestTheRunScope:
    """The context manager the orchestrator nests inside `isolated_worktree`.

    It yields the workspace (or None) and always closes it -- the workspace,
    never the tree. `isolated_worktree`'s own `finally` removes the tree, and
    closing first is what stops panes sitting in a directory being deleted.
    """

    def test_off_by_default_it_calls_herdr_at_all(self):
        """The discriminating case. A box with no herdr server must behave
        exactly as before -- and a display surface must never be able to fail
        a run."""
        herdr = FakeHerdr(payload=OPENED)

        with run_workspace(
            enabled=False, repo="/srv/n", exec_path="/srv/n", label="l", run_cli=herdr
        ) as ws:
            assert ws is None

        assert herdr.calls == []

    def test_on_it_adopts_the_exec_path_not_the_project_path(self):
        """The exec path IS the worktree when isolation is on. Opening the
        project path instead would show the operator a tree the agent is not
        working in."""
        herdr = FakeHerdr(payload=OPENED)

        with run_workspace(
            enabled=True,
            repo="/srv/n",
            exec_path="/srv/n/.hivepilot-wt/abc",
            label="l",
            run_cli=herdr,
        ) as ws:
            assert ws is not None

        argv = herdr.calls[0]
        assert argv[argv.index("--path") + 1] == "/srv/n/.hivepilot-wt/abc"

    def test_a_run_without_a_worktree_gets_a_plain_workspace(self):
        """`isolated_worktree` yields the repo path itself when it falls back,
        and there is then no tree to adopt."""
        herdr = FakeHerdr(payload={"result": {"workspace": {"workspace_id": "w4"}}})

        with run_workspace(
            enabled=True, repo="/srv/n", exec_path="/srv/n", label="l", run_cli=herdr
        ) as ws:
            assert ws is not None

        assert herdr.calls[0][:3] == ["herdr", "workspace", "create"]

    def test_it_closes_the_workspace_on_the_way_out(self):
        herdr = FakeHerdr(payload=OPENED)

        with run_workspace(
            enabled=True,
            repo="/srv/n",
            exec_path="/srv/n/.hivepilot-wt/a",
            label="l",
            run_cli=herdr,
        ):
            pass

        assert herdr.calls[-1][:3] == ["herdr", "workspace", "close"]

    def test_it_closes_even_when_the_run_raises(self):
        """Otherwise every failed run leaks a workspace, and failures are
        exactly when the operator wants to look at one."""
        herdr = FakeHerdr(payload=OPENED)

        with pytest.raises(ValueError):
            with run_workspace(
                enabled=True,
                repo="/srv/n",
                exec_path="/srv/n/.hivepilot-wt/a",
                label="l",
                run_cli=herdr,
            ):
                raise ValueError("step blew up")

        assert herdr.calls[-1][:3] == ["herdr", "workspace", "close"]

    def test_a_herdr_failure_does_not_fail_the_run(self):
        """Deliberate polarity, and the opposite of the security gates: this
        surface exists to be WATCHED, not to decide anything. A dead herdr
        server must cost visibility, never a run."""
        herdr = FakeHerdr(returncode=1)

        with run_workspace(
            enabled=True, repo="/srv/n", exec_path="/srv/n", label="l", run_cli=herdr
        ) as ws:
            assert ws is None


class TestTheAmbientPane:
    """How the root pane id reaches the runner.

    An ambient rather than a payload field, because `RunnerPayload` is built at
    a dozen sites and threading a display concern through all of them would put
    it in the way of everything.

    NO `adopt` at the thread boundary, unlike `outward` and `pr_attribution`.
    Those are set on the SUBMITTING thread and read on the pool worker, so they
    must be captured and re-adopted. This one is set inside
    `_execute_task_body` -- already on the worker -- and read downstream in the
    same `with` block on the same thread. I asserted a fourth `adopt` was
    needed by analogy with the other three, before checking whether this value
    crosses anything. It does not.

    The invariant is pinned below, so that hoisting the workspace up to
    `run_pipeline` fails a test instead of silently falling back to
    `pane split --current`.
    """

    def test_there_is_no_pane_by_default(self):
        assert current_pane() is None

    def test_the_pane_is_readable_inside_the_scope(self):
        herdr = FakeHerdr(payload=OPENED)

        with run_workspace(
            enabled=True,
            repo="/srv/n",
            exec_path="/srv/n/.hivepilot-wt/a",
            label="l",
            run_cli=herdr,
        ):
            assert current_pane() == "w9:p1"

    def test_it_is_cleared_on_the_way_out(self):
        """A leaked pane id would send the NEXT run's steps into a workspace
        that has been closed."""
        herdr = FakeHerdr(payload=OPENED)

        with run_workspace(
            enabled=True,
            repo="/srv/n",
            exec_path="/srv/n/.hivepilot-wt/a",
            label="l",
            run_cli=herdr,
        ):
            pass

        assert current_pane() is None

    def test_it_is_cleared_even_when_the_run_raises(self):
        herdr = FakeHerdr(payload=OPENED)

        with pytest.raises(ValueError):
            with run_workspace(
                enabled=True,
                repo="/srv/n",
                exec_path="/srv/n/.hivepilot-wt/a",
                label="l",
                run_cli=herdr,
            ):
                raise ValueError("boom")

        assert current_pane() is None

    def test_the_flag_off_path_sets_nothing(self):
        with run_workspace(
            enabled=False,
            repo="/srv/n",
            exec_path="/srv/n",
            label="l",
            run_cli=FakeHerdr(payload=OPENED),
        ):
            assert current_pane() is None

    def test_an_unavailable_herdr_leaves_no_pane(self):
        """Rather than a stale one from an earlier run."""
        with run_workspace(
            enabled=True,
            repo="/srv/n",
            exec_path="/srv/n",
            label="l",
            run_cli=FakeHerdr(returncode=1),
        ):
            assert current_pane() is None


class TestTheSameThreadInvariant:
    """The reason no `adopt` is needed -- pinned, so it cannot quietly stop
    being true.

    A contextvar set on one thread is invisible on another. Today the
    workspace is opened inside `_execute_task_body`, on the pool worker, and
    the runner dispatches downstream in the same `with` block. Hoist the
    opening to `run_pipeline` and every step would read `None` and fall back
    to `pane split --current` -- no error, just the defect back."""

    def test_a_different_thread_does_not_see_the_pane(self):
        import threading

        seen: list[str | None] = []
        herdr = FakeHerdr(payload=OPENED)

        with run_workspace(
            enabled=True,
            repo="/srv/n",
            exec_path="/srv/n/.hivepilot-wt/a",
            label="l",
            run_cli=herdr,
        ):
            t = threading.Thread(target=lambda: seen.append(current_pane()))
            t.start()
            t.join()

        assert seen == [None], (
            "a fresh thread must NOT inherit the pane -- if this ever passes, "
            "the ambient is being set somewhere that needs an explicit adopt"
        )
