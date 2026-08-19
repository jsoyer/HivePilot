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
import re
import subprocess
from dataclasses import dataclass
from typing import Any, Callable

import structlog

logger = structlog.get_logger(__name__)

#: What survives in a branch name. Project names come from config, and a `..`
#: or a leading slash would produce a ref that resolves somewhere else.
_UNSAFE_REF = re.compile(r"[^A-Za-z0-9._-]+")


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


def run_branch_name(project: str, run_id: int | str) -> str:
    """`hivepilot/<project>/<run_id>`, the house convention.

    No slug: the description belongs in the PR title, and one branch per run is
    what the merge tooling already assumes.
    """
    safe = _UNSAFE_REF.sub("-", str(project)).strip("-.") or "project"
    return f"hivepilot/{safe}/{run_id}"


def _default_run_cli(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def create_run_workspace(
    *,
    repo: str,
    branch: str,
    label: str,
    base: str = "main",
    run_cli: Callable[..., subprocess.CompletedProcess] | None = None,
) -> RunWorkspace:
    """Open a git worktree for this run and return where it lives.

    `--base` is always sent. Omitted, the worktree forks from whatever HEAD
    happens to be, and on a box that just finished another agent's run that is
    not the trunk.

    `--no-focus` so starting a run does not yank the operator's screen away
    from whatever they were reading.
    """
    cli = run_cli or _default_run_cli
    result = cli(
        [
            "herdr",
            "worktree",
            "create",
            "--cwd",
            repo,
            "--branch",
            branch,
            "--base",
            base,
            "--label",
            label,
            "--no-focus",
        ]
    )
    if result.returncode != 0:
        raise WorkspaceError(
            f"herdr worktree create failed for {branch} (exit {result.returncode}): "
            f"{_tail(result.stderr or result.stdout)}"
        )

    payload = _loads(result.stdout)
    body = payload.get("result") if isinstance(payload, dict) else None
    body = body if isinstance(body, dict) else {}

    workspace_id = _dig(body, "workspace", "workspace_id")
    root_pane_id = _dig(body, "root_pane", "pane_id")
    checkout = _dig(body, "worktree", "path") or _dig(
        body, "workspace", "worktree", "checkout_path"
    )

    if not workspace_id:
        raise WorkspaceError(
            f"herdr worktree create named no workspace for {branch}: {_tail(result.stdout)}"
        )
    if not checkout:
        # A worktree whose path we do not know is a step that would run in the
        # wrong tree -- silently, because the command itself succeeded.
        raise WorkspaceError(
            f"herdr worktree create named no checkout path for {branch}: {_tail(result.stdout)}"
        )

    logger.info(
        "herdr_workspace.created",
        workspace=workspace_id,
        branch=branch,
        checkout=checkout,
    )
    return RunWorkspace(
        workspace_id=workspace_id,
        root_pane_id=root_pane_id or "",
        checkout_path=checkout,
        branch=branch,
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
