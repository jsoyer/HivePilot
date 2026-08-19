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
    create_run_workspace,
    run_branch_name,
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


def _create(herdr, **kw):
    kw.setdefault("repo", "/srv/noxys")
    kw.setdefault("branch", "hivepilot/noxys/742")
    kw.setdefault("label", "noxys run 742")
    return create_run_workspace(run_cli=herdr, **kw)


class TestTheRunGetsItsOwnWorktree:
    def test_the_workspace_id_comes_back(self):
        """Without it there is nothing to route later panes into, and nothing
        to close at the end of the run."""
        assert _create(FakeHerdr()).workspace_id == "w3"

    def test_the_root_pane_comes_back(self):
        """The workspace arrives with a pane already. The first role does not
        need a split."""
        assert _create(FakeHerdr()).root_pane_id == "w3:p1"

    def test_the_checkout_path_comes_back(self):
        """herdr chooses where the worktree lands, so the step's cwd is this
        path -- NOT the project path it would have used without a worktree."""
        ws = _create(FakeHerdr())

        assert ws.checkout_path.endswith("hivepilot-noxys-742")

    def test_no_workspace_is_created_beforehand(self):
        """`worktree create` makes one and names it. A separate `workspace
        create` would add a call and a failure mode and change nothing."""
        herdr = FakeHerdr()

        _create(herdr)

        assert not any("workspace" in c and "create" in c for c in herdr.calls)

    def test_the_label_carries_the_run_identity(self):
        """It is how the operator finds this run among the workspaces."""
        herdr = FakeHerdr()

        _create(herdr, label="noxys run 742")

        argv = herdr.argv_for("create")
        assert argv[argv.index("--label") + 1] == "noxys run 742"

    def test_the_base_ref_is_always_stated(self):
        """Omitted, the worktree forks from whatever HEAD happens to be, which
        on a box that just ran another agent is not main."""
        herdr = FakeHerdr()

        _create(herdr, base="main")

        argv = herdr.argv_for("create")
        assert argv[argv.index("--base") + 1] == "main"


class TestItRefusesToGuess:
    def test_a_failed_create_raises(self):
        with pytest.raises(WorkspaceError):
            _create(FakeHerdr(returncode=1))

    def test_a_response_naming_no_workspace_raises(self):
        """Never fall back to the focused workspace: the panes would land in
        another run, which is the exact failure this file exists to prevent."""
        payload = {"result": {"root_pane": {"pane_id": "w3:p1"}}}

        with pytest.raises(WorkspaceError):
            _create(FakeHerdr(payload=payload))

    def test_a_response_naming_no_checkout_raises(self):
        """A worktree whose path we do not know is a step that would run in
        the wrong tree -- silently, because the command would still succeed."""
        payload = {
            "result": {
                "workspace": {"workspace_id": "w3"},
                "root_pane": {"pane_id": "w3:p1"},
            }
        }

        with pytest.raises(WorkspaceError):
            _create(FakeHerdr(payload=payload))


class TestTheBranchName:
    def test_it_follows_the_house_convention(self):
        """`hivepilot/<project>/<run_id>`, no slug -- the description belongs
        in the PR title, and one branch per run is what the merge tooling
        already assumes."""
        assert run_branch_name("noxys", 742) == "hivepilot/noxys/742"

    def test_a_project_name_cannot_break_out_of_the_namespace(self):
        """Project names come from config, and a `..` or a leading slash would
        produce a ref that resolves somewhere else entirely."""
        name = run_branch_name("../../evil", 1)

        assert ".." not in name
        assert name.startswith("hivepilot/")

    def test_two_runs_never_collide(self):
        assert run_branch_name("noxys", 1) != run_branch_name("noxys", 2)


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
