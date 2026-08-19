"""One herdr worktree per run, so a run is a place the operator can look at.

Step 2 gave a step's process a pane. It created that pane with `pane split
--current` -- *whatever workspace the operator happens to be focused on*. With
a single workspace that is indistinguishable from correct. With one per run it
is wrong in a way nobody would suspect: a step lands in another run's
workspace, and merely focusing a different tab moves where agents appear.

So a run gets a workspace of its own, addressed by id, and every pane is
targeted at it explicitly.

Three facts, each read off herdr 0.8.0 on the box rather than recalled -- the
two times I recalled instead this week, I was wrong both times (`kill` for
`close`, `horizontal` for a direction that only accepts `right`/`down`):

* `worktree create` with no `--workspace` creates one AND returns it, so there
  is no reason to call `workspace create` first. It hands back the workspace
  id, the id of a root pane it opened, and the checkout path it chose.
* `pane split --pane <ID>` lands in <ID>'s workspace even when the operator is
  focused elsewhere. Measured against TWO workspaces, which is the only
  arrangement in which the right answer and `--current` differ.
* `worktree create --cwd <repo>` ALSO registers the repo itself as a
  workspace, so a run leaves two behind unless cleanup knows about both.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable

import structlog

logger = structlog.get_logger(__name__)


class WorkspaceError(RuntimeError):
    """The workspace could not be created, or herdr's answer did not name one.

    Always raised rather than degraded: every fallback available here -- the
    focused workspace, the project path, a guessed id -- puts a live agent
    somewhere other than where the run believes it is.
    """


@dataclass(frozen=True)
class RunWorkspace:
    """Where one run lives while it is running."""

    workspace_id: str
    #: The pane herdr opened with the workspace. The first role needs no split.
    root_pane_id: str
    #: herdr chooses this, under `~/.herdr/worktrees/<repo>/<branch-slug>`. It
    #: is the step's cwd -- NOT the project path a worktree-less run would use.
    checkout_path: str
    branch: str


def _default_run_cli(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def open_run_workspace(
    *,
    repo: str,
    worktree_path: str,
    label: str,
    run_cli: Callable[..., subprocess.CompletedProcess] | None = None,
) -> RunWorkspace:
    """Adopt the worktree HivePilot already made for this run.

    `git_service.isolated_worktree` creates one per run at
    `<repo>/.hivepilot-wt/<uuid>` with `git worktree add --detach`, and removes
    it on exit. herdr does not need to make a second one -- it needs to SHOW
    that one.

    Detached is also why there is no collision with the
    `hivepilot/<project>/<run_id>` branch `perform_git_actions` checks out in
    the project directory. Two worktrees cannot share a branch; a detached
    worktree claims none. I called that collision blocking before reading
    `isolated_worktree` -- it came from a design I had invented rather than
    from the code.

    Ownership stays with HivePilot, and the order matters: closing the
    workspace leaves the git worktree intact (probed), so the engine's own
    `finally` still removes it. Close the workspace FIRST, or panes are left
    sitting in a directory being deleted.

    `worktree open` is CONTEXTUAL, probed against 0.8.0: with `--path` alone it
    answers `not_git_worktree` -- "Herdr worktree actions require a workspace
    inside a Git work tree". `--cwd <repo>` selects that context; `--path`
    names the tree. Both are required.
    """
    cli = run_cli or _default_run_cli
    result = cli(
        [
            "herdr",
            "worktree",
            "open",
            "--cwd",
            repo,
            "--path",
            worktree_path,
            "--label",
            label,
            "--no-focus",
        ]
    )
    if result.returncode != 0:
        raise WorkspaceError(
            f"herdr worktree open failed for {worktree_path} (exit {result.returncode}): "
            f"{_tail(result.stderr or result.stdout)}"
        )

    payload = _loads(result.stdout)
    body = payload.get("result") if isinstance(payload, dict) else None
    body = body if isinstance(body, dict) else {}

    workspace_id = _dig(body, "workspace", "workspace_id")
    if not workspace_id:
        # Never fall back to the focused workspace: a live agent would land in
        # another run's tree and the run would never know.
        raise WorkspaceError(
            f"herdr worktree open named no workspace for {worktree_path}: {_tail(result.stdout)}"
        )

    logger.info(
        "herdr_workspace.opened",
        workspace=workspace_id,
        checkout=worktree_path,
        already_open=body.get("already_open"),
    )
    return RunWorkspace(
        workspace_id=workspace_id,
        root_pane_id=_dig(body, "root_pane", "pane_id") or "",
        # The tree the ENGINE made, not one herdr chose.
        checkout_path=_dig(body, "worktree", "path") or worktree_path,
        branch="",
    )


def workspace_for_path(
    *,
    path: str,
    label: str,
    run_cli: Callable[..., subprocess.CompletedProcess] | None = None,
) -> RunWorkspace:
    """A workspace over a plain directory, for a run with no worktree.

    The engine's worktree is CONDITIONAL -- `worktree_isolation`, not
    simulating, `auto_git`, the task actually commits or pushes, and the
    project is a git repo. A run failing any of those works directly in the
    project directory, and there is no tree to adopt.

    A NEW worktree is deliberately not created for that case. It would have to
    claim a branch, and the only branch that makes sense is the one
    `perform_git_actions` checks out in the project directory -- which git
    refuses to have in two worktrees at once. A plain workspace gives the same
    visibility with nothing to collide with.
    """
    cli = run_cli or _default_run_cli
    result = cli(["herdr", "workspace", "create", "--cwd", path, "--label", label])
    if result.returncode != 0:
        raise WorkspaceError(
            f"herdr workspace create failed for {path} (exit {result.returncode}): "
            f"{_tail(result.stderr or result.stdout)}"
        )
    payload = _loads(result.stdout)
    body = payload.get("result") if isinstance(payload, dict) else None
    body = body if isinstance(body, dict) else {}
    workspace_id = _dig(body, "workspace", "workspace_id") or _dig(body, "workspace_id")
    if not workspace_id:
        raise WorkspaceError(
            f"herdr workspace create named no workspace for {path}: {_tail(result.stdout)}"
        )
    logger.info("herdr_workspace.created_plain", workspace=workspace_id, path=path)
    return RunWorkspace(
        workspace_id=workspace_id,
        root_pane_id=_dig(body, "root_pane", "pane_id") or "",
        checkout_path=path,
        branch="",
    )


def close_run_workspace(
    workspace: RunWorkspace,
    *,
    run_cli: Callable[..., subprocess.CompletedProcess] | None = None,
) -> None:
    """Best effort. A workspace left open is a leak; failing to close one is
    not worth losing a finished run's results over, so this never raises."""
    cli = run_cli or _default_run_cli
    try:
        result = cli(["herdr", "workspace", "close", workspace.workspace_id])
    except OSError as exc:  # pragma: no cover - defensive
        logger.warning(
            "herdr_workspace.close_failed", workspace=workspace.workspace_id, error=str(exc)
        )
        return
    if result.returncode != 0:
        logger.warning(
            "herdr_workspace.close_failed",
            workspace=workspace.workspace_id,
            exit_code=result.returncode,
            detail=_tail(result.stderr or result.stdout),
        )


def _dig(body: dict[str, Any], *path: str) -> str | None:
    """Walk a nested dict, returning a non-empty string or None.

    Never raises and never guesses: a missing key here is reported by the
    caller as a refusal, because every alternative places a live agent
    somewhere the run does not know about.
    """
    node: Any = body
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node if isinstance(node, str) and node else None


def _loads(raw: str) -> Any:
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {}


def _tail(text: str | None) -> str:
    return (text or "").strip()[-2000:]


@contextmanager
def run_workspace(
    *,
    enabled: bool,
    repo: str,
    exec_path: str,
    label: str,
    run_cli: Callable[..., subprocess.CompletedProcess] | None = None,
) -> Iterator[RunWorkspace | None]:
    """Give one run a workspace for its lifetime, or nothing at all.

    Nests inside `isolated_worktree`, so `exec_path` is the tree the agent will
    actually work in -- the worktree when isolation is on, the repo itself when
    it fell back. Opening the project path instead would show the operator a
    tree the agent is not in.

    Two polarities, and they point opposite ways on purpose.

    OFF is the default and must be indistinguishable from this code not
    existing: no herdr call at all. A box with no herdr server keeps working.

    And when it IS on, a herdr failure does NOT fail the run. This surface
    exists to be watched, not to decide anything -- unlike `checks:` or the
    review gate, where an unavailable input must block. A dead herdr server
    costs visibility; it must never cost a run. That is why every failure here
    yields `None` rather than raising.

    Closing happens on the way out including on an exception -- failures are
    exactly when an operator wants to look at the workspace, so leaking one per
    failed run would leave the most interesting ones unreclaimable. The
    WORKSPACE is closed, never the tree: `isolated_worktree`'s own `finally`
    removes that, and closing first is what stops panes sitting in a directory
    being deleted.
    """
    if not enabled:
        yield None
        return

    workspace: RunWorkspace | None = None
    try:
        if exec_path != repo:
            workspace = open_run_workspace(
                repo=repo, worktree_path=exec_path, label=label, run_cli=run_cli
            )
        else:
            # `isolated_worktree` fell back in place, or isolation is off:
            # there is no tree to adopt.
            workspace = workspace_for_path(path=exec_path, label=label, run_cli=run_cli)
    except Exception as exc:  # noqa: BLE001
        logger.warning("herdr_workspace.unavailable", path=exec_path, error=str(exc))
        workspace = None

    try:
        yield workspace
    finally:
        if workspace is not None:
            close_run_workspace(workspace, run_cli=run_cli)
