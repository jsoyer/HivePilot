from __future__ import annotations

import math
import subprocess
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

from git import GitCommandError, Repo  # type: ignore

from hivepilot.config import settings
from hivepilot.models import GitActions, ProjectConfig
from hivepilot.utils.logging import get_logger

if TYPE_CHECKING:
    # Import-only (never at runtime): `orchestrator.py` imports
    # `perform_git_actions`/`isolated_worktree` FROM this module, so a
    # top-level `from hivepilot.orchestrator import Verdict` here would be a
    # circular import. `is_blocking` below duck-types on the verdict's
    # `.decision`/`.confidence` attributes at runtime instead.
    from hivepilot.orchestrator import Verdict

logger = get_logger(__name__)

# Explicit APPROVE whitelist for the fail-closed judge/arbiter PR gate (Debate
# Judge & Consensus PRD, Sprint 3). Unlike `_BLOCKING_VERDICTS` below (a
# blacklist -- safe there because a `can_block` role's free-text status
# vocabulary is heterogeneous and mostly means "proceed"), a judge/arbiter
# Verdict has ONE narrow, controlled decision vocabulary (see
# `orchestrator._adjudicate_challenge`'s prompt: "ACCEPT" or "DEFEND"), so a
# WHITELIST is both safe and correct here: only an explicit approval decision
# proceeds, and everything else -- DEFEND, MAINTAIN, NEEDS_HUMAN, DECLINE, a
# free-text synthesis paragraph, empty, or unparseable -- fails closed (blocks).
_APPROVE_VERDICTS: frozenset[str] = frozenset({"ACCEPT", "ACCEPTED", "APPROVE", "APPROVED"})


def is_blocking(verdict: "Verdict | None", threshold: float) -> bool:
    """Fail-CLOSED default-deny gate for a judge/arbiter :class:`Verdict`.

    Returns ``True`` (block) on ANY empty/``None``/missing/unparseable/
    non-finite/below-threshold/non-approval verdict. Returns ``False``
    (proceed) ONLY for an explicit approval decision (see
    ``_APPROVE_VERDICTS``) with a present, finite confidence ``>=``
    *threshold*.

    Duck-types on ``verdict.decision``/``verdict.confidence`` (via
    ``getattr``) rather than importing :class:`Verdict` at runtime, so this
    module never actually imports ``orchestrator.py`` (see the
    ``TYPE_CHECKING`` import above) -- avoiding a circular import, since
    ``orchestrator.py`` imports ``perform_git_actions`` FROM this module.

    Null-guards run BEFORE any ``.strip()``/``>=`` comparison, and
    ``confidence == 0.0`` is a valid (not falsy-empty) value -- there is no
    "0.0-is-falsy" hole. Non-finite confidence (``NaN``/``inf``, which a
    judge's raw JSON can smuggle in) is explicitly rejected via
    ``math.isfinite`` -- never treated as "confident enough".
    """
    decision = getattr(verdict, "decision", None)
    confidence = getattr(verdict, "confidence", None)
    approved = (
        verdict is not None
        and isinstance(decision, str)
        and decision.strip().upper() in _APPROVE_VERDICTS
        and isinstance(confidence, (int, float))
        and not isinstance(confidence, bool)
        and math.isfinite(confidence)
        and confidence >= threshold
    )
    return not approved


@contextmanager
def isolated_worktree(repo_path: Path, base_ref: str | None = None) -> Iterator[Path]:
    """Create a throwaway git worktree for `repo_path`, yield its Path, then remove it.

    The worktree is placed under `<repo_path>/.hivepilot-wt/<uuid>` (never under
    .claude/worktrees). On exit — even if the body raises — the worktree is removed
    with `git worktree remove --force`. Removal failures are logged as warnings and
    never re-raised, so cleanup never masks the original exception.

    Falls back to yielding `repo_path` itself when `git worktree add` fails (not a
    git repo, old git version, etc.) — the run continues in-place with a warning.
    """
    wt_base = repo_path / ".hivepilot-wt"
    wt_path = wt_base / str(uuid.uuid4())
    wt_path.mkdir(parents=True, exist_ok=True)
    git_ref = base_ref or "HEAD"
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), "worktree", "add", "--detach", str(wt_path), git_ref],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            logger.warning(
                "worktree.add_failed",
                repo=str(repo_path),
                error=result.stderr.strip(),
                fallback="in_place",
            )
            # Clean up the empty dir we created, fall back to real path
            try:
                wt_path.rmdir()
            except Exception:  # noqa: BLE001
                pass
            yield repo_path
            return
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "worktree.add_exception", repo=str(repo_path), error=str(exc), fallback="in_place"
        )
        try:
            wt_path.rmdir()
        except Exception:  # noqa: BLE001
            pass
        yield repo_path
        return

    logger.info("worktree.created", path=str(wt_path), repo=str(repo_path))
    try:
        yield wt_path
    finally:
        try:
            subprocess.run(
                ["git", "-C", str(repo_path), "worktree", "remove", "--force", str(wt_path)],
                capture_output=True,
                text=True,
                check=False,
            )
            logger.info("worktree.removed", path=str(wt_path))
        except Exception as exc:  # noqa: BLE001
            logger.warning("worktree.remove_failed", path=str(wt_path), error=str(exc))


def ensure_repo(path: Path) -> Repo:
    try:
        return Repo(path)
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(f"{path} is not a git repository: {exc}") from exc


def checkout_branch(path: Path, branch: str) -> None:
    repo = ensure_repo(path)
    git = repo.git
    try:
        git.checkout("-B", branch)
    except GitCommandError as exc:
        raise RuntimeError(f"Failed to checkout {branch}: {exc}") from exc


def push(path: Path, remote: str, branch: str) -> None:
    repo = ensure_repo(path)
    try:
        repo.git.push("-u", remote, branch)
    except GitCommandError as exc:
        raise RuntimeError(f"Failed to push {branch}: {exc}") from exc


def status(path: Path) -> str:
    repo = ensure_repo(path)
    return repo.git.status("--short")


def run_git_command(args: list[str], cwd: Path) -> None:
    subprocess.run([settings.git_command, *args], cwd=str(cwd), check=True)


def commit_vault(
    vault_path: Path, message: str = "HivePilot: update Obsidian notes", *, push: bool = True
) -> bool:
    """git add/commit/push changes under the Obsidian *vault_path*.

    Best-effort and self-contained: returns False (no raise) if the vault is not a
    git work tree or has nothing to commit. Only the vault's own changes are staged.
    """
    try:
        repo = Repo(vault_path, search_parent_directories=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("vault.not_git_repo", path=str(vault_path), error=str(exc))
        return False
    # Scope every operation to the vault pathspec so we never stage/commit/push
    # unrelated changes that happen to be in the enclosing repo's index.
    pathspec = str(vault_path)
    repo.git.add("-A", "--", pathspec)
    if not repo.git.diff("--cached", "--name-only", "--", pathspec).strip():
        return False  # nothing changed under the vault
    repo.git.commit("-m", message, "--", pathspec)  # commit only the vault's paths
    if push:
        if repo.head.is_detached:
            logger.warning("vault.detached_head_no_push", path=pathspec)
        else:
            repo.git.push("origin", repo.active_branch.name)  # explicit remote + branch
    logger.info("vault.committed", path=pathspec, pushed=push)
    return True


# Known blocking verdicts (uppercased). A ``can_block`` role that stops the
# release gate reports one of these as its ``status:``. Everything else --
# PASS, APPROVE, APPROVED, CLEARED, ADVISORY, OK, and absent/empty/unparseable
# output -- means "proceed", so promote/merge run. NEEDS_HUMAN is blocking: it
# defers to a human, so the PR must stay a draft until that human acts.
_BLOCKING_VERDICTS: frozenset[str] = frozenset(
    {
        "BLOCK",
        "BLOCKED",
        "REJECT",
        "REJECTED",
        "REQUEST_CHANGES",
        "CHANGES_REQUESTED",
        "NEEDS_HUMAN",
        "FAIL",
        "FAILED",
        "DENY",
        "DENIED",
    }
)


def _agent_verdict_blocked(task_result: str | None) -> bool:
    """True iff *task_result*'s parsed ``status:`` is an explicit blocking verdict.

    CORRECTNESS GATE (draft-PR-then-promote): a ``can_block`` role (reviewer,
    cto, ciso, qa, developer, release_manager -- see ``prompts/agents/*.md``)
    reports a free-text ``status:`` verdict in its stage output. Investigation
    for this feature found that **nothing upstream of ``perform_git_actions``
    turns that verdict into a hard stop**: ``orchestrator.py``'s pipeline stage
    loop does call ``parse_agent_report(stage_output)`` (near the
    ``_execute_task`` call site), but only to detect ``.challenge`` for the
    agent-to-agent challenge feature -- it never reads ``.status``.
    ``RunResult.success`` (which DOES gate ``stage_failed``/pipeline fail-fast)
    reflects only whether the runner raised an exception, never the agent's
    semantic judgement. ``role.can_block`` itself is descriptive metadata only
    (CLI/scaffold display) and is not read anywhere in the orchestrator's
    execution path. So a release-gate stage that free-text BLOCKS would, without
    this gate, still have its PR promoted/merged.

    The agent status vocabulary is HETEROGENEOUS: the release gate approves with
    ``status: APPROVE``, code roles pass with ``PASS``, security clears with
    ``CLEARED``, advisory roles emit ``ADVISORY`` -- all of which mean "proceed".
    A PASS-only whitelist would therefore wrongly block the release gate on its
    own approval. So we use a BLOCKING-VERDICT BLACKLIST (``_BLOCKING_VERDICTS``)
    instead: block only on a known explicit stop verdict; treat every other
    value -- including all the "proceed" synonyms and absent/empty/unparseable
    output -- as non-blocking. Most tasks are not ``can_block`` roles and emit no
    structured status, so they keep working unchanged.

    Evaluated on the CURRENT stage's own ``task_result`` (the text
    ``_execute_task`` just produced for THIS stage, passed in before this call),
    using the same tolerant parser the orchestrator already uses.
    """
    if not task_result:
        return False
    from hivepilot.services.agent_report import parse_agent_report

    status = parse_agent_report(task_result).status.strip().upper()
    return status in _BLOCKING_VERDICTS


def perform_git_actions(
    *,
    project_name: str,
    project: ProjectConfig,
    git: GitActions,
    task_result: str | None = None,
    verdict: "Verdict | None" = None,
    judge_gate_enabled: bool = False,
    confidence_threshold: float = 0.0,
) -> None:
    """Perform the configured git actions for a completed task/stage.

    ``verdict``/``judge_gate_enabled``/``confidence_threshold`` are additive
    (Debate Judge & Consensus PRD, Sprint 3) and default to the pre-Sprint-3
    values (``None``/``False``/``0.0``) -- when ``judge_gate_enabled`` is
    ``False`` (the default), this function is BYTE-IDENTICAL to pre-Sprint-3
    behaviour: only ``_agent_verdict_blocked(task_result)`` governs the gate.

    When ``judge_gate_enabled`` is ``True``, the gate ALSO fails closed on
    ``is_blocking(verdict, confidence_threshold)`` -- a missing/empty/
    unparseable/low-confidence/non-approval judge or challenge-arbiter
    ``Verdict`` blocks ``promote_pr``/``merge_pr`` exactly like an explicit
    ``_BLOCKING_VERDICTS`` status does. Either condition blocking is
    sufficient to block (OR, never AND) -- this is a strictly stricter
    superset of the legacy gate, never a relaxation of it.
    """
    repo = ensure_repo(project.path)
    branch = f"{git.branch_prefix}/{project_name}"
    if git.commit or git.push:
        checkout_branch(project.path, branch)
        # The agent (e.g. claude) may have already committed its work; only commit
        # when there are uncommitted changes. The branch still carries the agent's
        # commits, so push/PR proceed either way.
        if git.commit and repo.is_dirty(untracked_files=True):
            repo.git.add("-A")
            message = git.commit_message or f"chore({project_name}): automated task run"
            repo.git.commit("-m", message)
        if git.push:
            push(project.path, "origin", branch)
    if git.create_pr:
        create_pr(project=project, branch=branch, git=git)
    # Promote-to-ready and merge are release-gate actions -- gate them on the
    # stage's own verdict (see _agent_verdict_blocked above). create_pr is NOT
    # gated: opening (or keeping) the draft PR must still happen even when the
    # gate blocks, so a human can see the review report on the PR itself.
    blocked = _agent_verdict_blocked(task_result)
    if judge_gate_enabled:
        blocked = blocked or is_blocking(verdict, confidence_threshold)
    if git.promote_pr:
        if blocked:
            logger.warning("git.promote_skipped_blocked", project=project_name, branch=branch)
        else:
            promote_pr(project=project, branch=branch, git=git)
    if git.merge_pr:
        if blocked:
            logger.warning("git.merge_skipped_blocked", project=project_name, branch=branch)
        else:
            merge_pr(project=project, branch=branch, git=git)


def create_pr(*, project: ProjectConfig, branch: str, git: GitActions) -> None:
    """Open a pull request via the gh CLI (run from the project repo)."""
    base = project.default_branch or "main"
    title = git.pr_title or f"HivePilot: {branch}"
    cmd = [settings.gh_command, "pr", "create", "--base", base, "--head", branch, "--title", title]
    if git.draft:
        cmd.append("--draft")
    if git.pr_body_file:
        cmd += ["--body-file", git.pr_body_file]
    else:
        cmd += ["--body", "Automated pull request opened by HivePilot."]
    try:
        subprocess.run(cmd, cwd=str(project.path), check=True, text=True)
        logger.info(
            "git.pr_created", project=project.path.name, branch=branch, base=base, draft=git.draft
        )
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Failed to create PR for {project.path.name}: {exc}") from exc


def promote_pr(*, project: ProjectConfig, branch: str, git: GitActions) -> None:
    """Mark *branch*'s draft PR ready for review via gh -- release-gate promotion.

    Sibling to merge_pr: same subprocess/error-handling shape. Only called by
    perform_git_actions when the gating stage's own verdict did not block
    (see _agent_verdict_blocked).
    """
    cmd = [settings.gh_command, "pr", "ready", branch]
    try:
        subprocess.run(cmd, cwd=str(project.path), check=True, text=True)
        logger.info("git.pr_promoted", project=project.path.name, branch=branch)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Failed to promote PR for {project.path.name}: {exc}") from exc


def merge_pr(*, project: ProjectConfig, branch: str, git: GitActions) -> None:
    """Merge the open PR for *branch* via gh -- Jules' autonomous final approval.

    Merge (not a review approval) because GitHub forbids approving your own PR, so
    the actionable autonomous step in a solo workflow is the merge itself.
    """
    method = git.merge_method if git.merge_method in {"merge", "squash", "rebase"} else "merge"
    cmd = [settings.gh_command, "pr", "merge", branch, f"--{method}"]
    try:
        subprocess.run(cmd, cwd=str(project.path), check=True, text=True)
        logger.info("git.pr_merged", project=project.path.name, branch=branch, method=method)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Failed to merge PR for {project.path.name}: {exc}") from exc
