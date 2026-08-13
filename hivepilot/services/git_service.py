from __future__ import annotations

import math
import re
import subprocess
import threading
import uuid
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator

from git import GitCommandError, Repo  # type: ignore

from hivepilot import pr_attribution
from hivepilot.config import settings
from hivepilot.forges import resolve_forge
from hivepilot.models import GitActions, ProjectConfig
from hivepilot.services.pr_body import resolve_pr_body_file
from hivepilot.services.verification_service import Check, run_checks
from hivepilot.utils.logging import get_logger

if TYPE_CHECKING:
    # Import-only (never at runtime): `orchestrator.py` imports
    # `perform_git_actions`/`isolated_worktree` FROM this module, so a
    # top-level `from hivepilot.orchestrator import Verdict` here would be a
    # circular import. `is_blocking` below duck-types on the verdict's
    # `.decision`/`.confidence` attributes at runtime instead.
    from hivepilot.orchestrator import Verdict
    from hivepilot.services.verification_service import Check, VerificationReport

logger = get_logger(__name__)


def _render_verification(report: "VerificationReport") -> str:
    """The deterministic half of a blocked-gate report.

    Quotes each failing check's own output rather than describing it: the
    output IS the evidence, and a paraphrase would put a model back in the one
    place this feature exists to keep models out of.
    """
    lines = [
        "## HivePilot release gate: **deterministic check failed**",
        "",
        report.summary,
        "",
    ]
    for result in report.results:
        if not result.blocking:
            continue
        code = "no exit code" if result.exit_code is None else f"exit {result.exit_code}"
        lines.append(f"### `{result.name}` — {result.outcome} ({code})")
        lines.append("")
        lines.append("```")
        lines.append(result.output or "(no output)")
        lines.append("```")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


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


#: git's wording when a branch is checked out in a different worktree. A
#: branch can only live in one at a time, and git says so precisely.
_BRANCH_CLAIMED_MARKER = "is already used by worktree at"


def checkout_branch(path: Path, branch: str) -> None:
    """Check *branch* out in *path*, creating or resetting it.

    Retries once with ``--ignore-other-worktrees`` when git refuses because
    the branch is claimed elsewhere. Run 425 died on exactly that, at the
    second-to-last stage, after eleven stages and $25.78 of work:

        git checkout -B hivepilot/noxys
        fatal: 'hivepilot/noxys' is already used by worktree at '/root/noxys'

    Two features interacting, each right alone: `perform_git_actions` checks
    the branch out in `project.path`, and worktree isolation runs a stage
    somewhere else. The main clone was simply parked on the branch, left
    there by an earlier stage.

    The retry is safe HERE SPECIFICALLY because stages are serialised -- the
    parked clone is idle, not writing. It is scoped to this one stderr
    signature rather than applied to any failure, and logged: a genuine
    concurrent claim stays visible instead of being silently overridden.

    What it actually bought: that run never reached the PR-approval stage,
    which carries the review gate, so no verdict was produced at all.
    """
    repo = ensure_repo(path)
    git = repo.git
    try:
        git.checkout("-B", branch)
        return
    except GitCommandError as exc:
        if _BRANCH_CLAIMED_MARKER not in str(exc):
            raise RuntimeError(f"Failed to checkout {branch}: {exc}") from exc
        logger.warning(
            "git.branch_claimed_by_other_worktree",
            branch=branch,
            path=str(path),
            remediation="retrying with --ignore-other-worktrees; stages are serialised",
        )
    try:
        git.checkout("-B", branch, "--ignore-other-worktrees")
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


def refresh_vault(vault_path: Path) -> bool:
    """Bring the vault clone up to date before agents read from it.

    The vault is not only written, it is READ: `plugins/obsidian.py`'s
    `recall` pulls excerpts straight into the prompt. Nothing ever refreshed
    the clone, so the notes agents recalled were as old as the last time
    someone happened to push from that machine -- on the reference box, 54
    commits behind, for an unknown number of runs.

    That is worse than the push failure it sits beside. A rejected push at
    least logs a warning; a stale READ produces a confident answer built on
    superseded notes, and nothing anywhere says so.

    `--autostash` because a stage may already have committed or dirtied the
    tree: refreshing must never become a way to lose that. Nothing here
    resets or checks out.

    Never raises and never blocks. An unreachable remote is a reason to run
    on the notes we have, not a reason to fail a pipeline -- but the outcome
    is logged either way, because "the notes are current" and "the notes are
    whatever was here last time" look identical in every artifact
    downstream.
    """
    try:
        repo = Repo(vault_path, search_parent_directories=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("vault.refresh_skipped_not_git_repo", path=str(vault_path), error=str(exc))
        return False

    if repo.head.is_detached:
        # No branch to pull onto, and guessing one would move a deliberately
        # pinned checkout.
        logger.warning("vault.refresh_skipped_detached_head", path=str(vault_path))
        return False

    branch = repo.active_branch.name
    try:
        repo.git.pull("--rebase", "--autostash", "origin", branch)
    except Exception as exc:  # noqa: BLE001 — stale notes beat a failed run
        logger.warning("vault.refresh_failed", path=str(vault_path), branch=branch, error=str(exc))
        return False

    logger.info("vault.refreshed", path=str(vault_path), branch=branch)
    return True


def _push_with_rebase_retry(repo: Any, branch: str) -> None:
    """Push *branch*, and on rejection rebase onto the remote and push once more.

    The vault is a SHARED repository -- the operator writes notes from their
    own machine, and every other run writes to it too. A plain push therefore
    gets rejected the moment anyone else has pushed, and without this it stays
    rejected forever, because nothing here ever pulled.

    Observed on run 347: the remote held 54 commits the box did not have, the
    box held 20 it had never pushed, and the commit for EVERY stage of that
    run sat locally unreachable while the pipeline logged `vault.commit_failed`
    and carried on. Nothing was lost, which is precisely what made it quiet --
    a pipeline looks healthy while its entire written record stops leaving the
    machine.

    Rebase rather than merge: these are append-only note commits, and a merge
    bubble per run would bury the history the vault exists to hold.

    Once, not in a loop. If the second push also fails, something other than a
    moved remote is wrong; retrying will not discover what, and swallowing it
    would recreate the silence this exists to fix. The exception reaches the
    caller, which logs `vault.commit_failed` -- the honest outcome.
    """
    try:
        repo.git.push("origin", branch)
        return
    except Exception as exc:  # noqa: BLE001 — inspected, then retried once
        logger.info("vault.push_rejected_rebasing", branch=branch, error=str(exc))

    repo.git.pull("--rebase", "origin", branch)
    repo.git.push("origin", branch)
    logger.info("vault.pushed_after_rebase", branch=branch)


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
    # `--sparse` because the vault may be a cone checkout that excludes the
    # very directories HivePilot writes to. The operator's cone holds their own
    # notes; `HivePilot/` and `Artifacts/` were never in it, so `git add`
    # refused all 93 paths of run 507 and raised -- three days of run records on
    # disk and in no commit, behind one warning.
    #
    # This is not a cone override: nothing here changes the sparse definition.
    # It stages the files HivePilot itself wrote one line earlier, which is what
    # git's own hint recommends. A vault with no sparse checkout is unaffected.
    repo.git.add("-A", "--sparse", "--", pathspec)
    if not repo.git.diff("--cached", "--name-only", "--", pathspec).strip():
        return False  # nothing changed under the vault
    repo.git.commit("-m", message, "--", pathspec)  # commit only the vault's paths
    if push:
        if repo.head.is_detached:
            logger.warning("vault.detached_head_no_push", path=pathspec)
        else:
            _push_with_rebase_retry(repo, repo.active_branch.name)
    logger.info("vault.committed", path=pathspec, pushed=push)
    return True


# Known blocking verdicts (uppercased). A ``can_block`` role that stops the
# release gate reports one of these as its ``status:``. Everything else --
# PASS, APPROVE, APPROVED, CLEARED, ADVISORY, OK, and absent/empty/unparseable
# output -- means "proceed", so promote/merge run. NEEDS_HUMAN is blocking: it
# defers to a human, so the PR must stay a draft until that human acts.
#: Author identity for every unattended commit HivePilot makes. An automated
#: commit cannot depend on ambient git configuration: a freshly cloned repo has
#: no `user.name`, and systemd units run without HOME so `~/.gitconfig` is
#: invisible. Shared with the corrections writer, which learned this first.
COMMIT_IDENTITY_NAME = "HivePilot"
COMMIT_IDENTITY_EMAIL = "hivepilot@localhost"

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


# Characters git refuses in a ref, plus whitespace. A project name is
# operator-supplied and lands directly in a branch name.
_REF_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


#: Forges truncate or reject long titles; cut deliberately instead.
_MAX_PR_TITLE = 120


def build_pr_title(*, branch: str, commit_subjects: list[str]) -> str:
    """Describe the PR from the commits it actually contains.

    ``HivePilot: pipeline implementation`` told a reviewer nothing. The branch
    deliberately carries no description -- see `build_branch_name` -- so the
    title is the only place a human learns what a PR is for. Unlike the branch
    it is built AFTER the commits exist, so it can be accurate rather than
    aspirational.

    A PR with several commits says so: titling a three-commit change after one
    of them hides the other two.
    """
    subjects = [s.strip() for s in commit_subjects if s and s.strip()]
    if not subjects:
        return f"HivePilot: {branch}"

    title = subjects[0]
    extra = len(subjects) - 1
    if extra:
        title = f"{title} (+{extra} more)"
    if len(title) > _MAX_PR_TITLE:
        title = title[: _MAX_PR_TITLE - 1].rstrip() + "…"
    return title


def build_branch_name(*, prefix: str, project_name: str, run_id: int | None) -> str:
    """``<prefix>/<project>/<run_id>`` -- one branch, and one PR, per run.

    A single perpetual ``<prefix>/<project>`` branch made every review
    cumulative: a run pushed into the PR an earlier run had opened
    (``git.pr_already_open``), the body was never refreshed, and the reviewer
    of the newest run was implicitly reviewing every run before it. One
    production PR carried three runs from three separate days.

    **No slug.** This name is built before any code exists, so a descriptive
    part could only restate an intention -- and the objective is not persisted
    anywhere to restate. A descriptive name that is wrong is worse than a
    neutral one. The run id is the key to the verdicts, lessons, costs and the
    blocked-gate report, so a branch resolves to the whole record in one step.
    The description belongs in the PR title, which is written after the work
    exists and can be accurate.

    Falls back to the old ``<prefix>/<project>`` when no run is in scope, so
    callers outside a run keep working rather than inventing an id that
    resolves to nothing.
    """
    safe_project = _REF_UNSAFE.sub("-", project_name)
    # git rejects a ref containing "..", and the dot itself is legal, so the
    # character class above lets a traversal through untouched.
    safe_project = re.sub(r"\.{2,}", ".", safe_project).strip("-.") or "project"
    base = f"{prefix}/{safe_project}"
    return f"{base}/{run_id}" if run_id else base


def perform_git_actions(
    *,
    project_name: str,
    project: ProjectConfig,
    git: GitActions,
    task_result: str | None = None,
    verdict: "Verdict | None" = None,
    judge_gate_enabled: bool = False,
    confidence_threshold: float = 0.0,
    run_id: int | None = None,
    role: str | None = None,
    checks: "list[Check] | None" = None,
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
    branch = build_branch_name(prefix=git.branch_prefix, project_name=project_name, run_id=run_id)
    if git.commit or git.push:
        checkout_branch(project.path, branch)
        # The agent (e.g. claude) may have already committed its work; only commit
        # when there are uncommitted changes. The branch still carries the agent's
        # commits, so push/PR proceed either way.
        if git.commit and repo.is_dirty(untracked_files=True):
            repo.git.add("-A")
            message = git.commit_message or f"chore({project_name}): automated task run"
            # Explicit identity. A freshly cloned repo has no `user.name` and
            # systemd units have no HOME, so `git commit` dies with "Please
            # tell me who you are" -- which killed the developer stage of a
            # real greenfield run. The `noxys` checkout happens to carry a
            # LOCAL identity, which is why every earlier run committed fine and
            # this stayed invisible until a repo was cloned fresh.
            repo.git.commit(
                "-c",
                f"user.name={COMMIT_IDENTITY_NAME}",
                "-c",
                f"user.email={COMMIT_IDENTITY_EMAIL}",
                "-m",
                message,
            )
        if git.push:
            push(project.path, "origin", branch)
    if git.create_pr:
        # task_result (THIS stage's own output) is the richest fallback
        # content available for a declared-but-missing/empty pr_body_file --
        # see create_pr/resolve_pr_body_file for why that fallback exists.
        create_pr(project=project, branch=branch, git=git, task_result=task_result)
    # Promote-to-ready and merge are release-gate actions -- gate them on the
    # stage's own verdict (see _agent_verdict_blocked above). create_pr is NOT
    # gated: opening (or keeping) the draft PR must still happen even when the
    # gate blocks, so a human can see the review report on the PR itself.
    blocked = _agent_verdict_blocked(task_result)
    if judge_gate_enabled:
        blocked = blocked or is_blocking(verdict, confidence_threshold)

    # The one input to this gate that is not an opinion. Both LLM paths above
    # fail OPEN by design -- an unparseable verdict must not freeze every
    # pipeline -- so on the A/B where six review outputs missed a raw ESC byte
    # in the produced tool, a gate wired only to prose would have promoted it.
    # `run_checks` fails CLOSED, and its veto is additive: a green check never
    # rescues something an agent refused, it only ever adds a reason to refuse.

    verification = run_checks(list(checks or []), cwd=project.path)
    blocked = blocked or verification.blocking

    if blocked:
        # The gate has refused to promote. Say WHY, on the PR, where the person
        # who has to act will look -- see post_blocked_report for what the
        # silence cost.
        post_blocked_report(
            forge=resolve_forge(project),
            project=project,
            branch=branch,
            run_id=run_id,
            # per_role_stance is role -> stance straight from the judge; the
            # free-text summary is the fallback when the judge did not supply
            # one. Both are parsed the same way downstream.
            verdict_summary=_verdict_role_summary(verdict, task_result, role),
            verification=verification,
        )
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


def _publish_pr_ready_best_effort(project: ProjectConfig, branch: str, kind: str) -> None:
    """Publish a `pr_ready` swarm-bus event for *branch* (Swarm Phase 1,
    build item 7) -- called ONLY after the forge call already succeeded
    (`create_pr`/`promote_pr` below), so a publish failure NEVER retroactively
    breaks an already-completed PR action. Wraps the whole call (import,
    HEAD-sha lookup, publish) in a broad except: `swarm_service.
    publish_pr_ready` is already internally best-effort, but this is
    belt-and-suspenders so this helper itself can NEVER raise.
    """
    try:
        from hivepilot.services.swarm_service import publish_pr_ready

        try:
            sha = ensure_repo(project.path).head.commit.hexsha
        except Exception:  # noqa: BLE001 — an unresolvable HEAD sha still publishes (as "unknown")
            sha = "unknown"
        publish_pr_ready(
            project_name=project.path.name,
            owner_repo=project.owner_repo,
            branch=branch,
            sha=sha,
            kind=kind,
        )
    except Exception:  # noqa: BLE001 — best-effort: a bus failure never breaks a run
        logger.warning(
            "git.swarm_publish_failed",
            project=project.path.name,
            branch=branch,
            kind=kind,
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# PR-URL ledger (propose -> ratify -> dispatch PRD, spec §8)
#
# The partition journal wants the PR link for each dispatched task, but the
# code that OPENS the PR (`create_pr` below, called from
# `perform_git_actions` inside `Orchestrator._execute_task`) runs on a
# `ThreadPoolExecutor` worker thread that `run_pipeline` spawns -- and
# contextvars are NOT inherited across `ThreadPoolExecutor.submit` (the same
# fact `orchestrator.py` documents where it hand-propagates its OTel
# context). So a process-global, explicitly locked ledger carries the URLs
# themselves.
#
# WHICH task opened a given entry used to be INFERRED from a time window
# (`pr_ledger_mark` before the run, `pr_urls_since` after it), which meant
# two same-project tasks running at once each saw both entries and both
# honestly degraded to `NULL`. Every entry now also carries the ambient
# `hivepilot.pr_attribution` key -- an identity the dispatcher installs and
# `orchestrator.py` re-adopts across the pool boundary alongside the outward
# permission -- so the reader can filter on identity instead of on timing.
#
# The ledger stays deliberately DUMB and append-only: it records what a forge
# reported plus who was running when it did, and the READER decides what it
# may honestly attribute. See `pr_urls_since`, which returns every match and
# leaves the "exactly one, or nothing" rule to `partition_service` -- so any
# residual ambiguity still degrades to "no URL", never to a mis-attributed
# one.
# ---------------------------------------------------------------------------

_PR_LEDGER_MAX = 512


@dataclass(frozen=True)
class PrLedgerEntry:
    """One pull request a forge reported opening.

    ``attribution`` is the `hivepilot.pr_attribution` key in force on the
    thread that opened it, or ``None`` when nothing attributed it (every
    non-partition run: an ordinary ``hivepilot run``, a scheduled run, an API
    run). ``None`` is NOT a wildcard -- it never matches an attributed query.
    """

    seq: int
    project: str
    branch: str
    url: str
    attribution: str | None


_pr_ledger: deque[PrLedgerEntry] = deque(maxlen=_PR_LEDGER_MAX)
_pr_ledger_lock = threading.Lock()
_pr_ledger_seq = 0


def record_pr_opened(*, project: str, branch: str, url: str) -> int:
    """Append one entry to the ledger; return its sequence number.

    Called only with a URL a FORGE reported (see
    `hivepilot.forges.provider.extract_pr_url`) -- never a derived one.

    The attribution key is read from the AMBIENT scope rather than passed in,
    deliberately: every write to this ledger -- from `create_pr` below, and
    from anything that ledgers a PR in future -- is then labelled with
    whoever was running, without each call site having to remember to. Outside
    a scope the key is `None`, so a non-partition run records exactly what it
    recorded before this field existed.
    """
    global _pr_ledger_seq  # noqa: PLW0603 - module-level monotonic sequence, lock-guarded
    attribution = pr_attribution.current()
    with _pr_ledger_lock:
        _pr_ledger_seq += 1
        seq = _pr_ledger_seq
        _pr_ledger.append(
            PrLedgerEntry(seq=seq, project=project, branch=branch, url=url, attribution=attribution)
        )
    return seq


def pr_ledger_mark() -> int:
    """The current ledger high-water mark. Pass it to `pr_urls_since` to ask
    "which PRs did this process open AFTER this point"."""
    with _pr_ledger_lock:
        return _pr_ledger_seq


def pr_urls_since(
    mark: int, *, project: str | None = None, attribution: str | None = None
) -> tuple[str, ...]:
    """Every PR URL recorded after *mark*, narrowed by the given filters.

    *project* and *attribution* are independent NARROWING filters; `None`
    means "do not narrow on this". Passing an *attribution* selects only
    entries opened under exactly that key -- an unattributed entry
    (``attribution is None``) never matches an attributed query, so a PR
    opened by an unrelated concurrent run can never be claimed by a
    partition task.

    Returns ALL matches, in order: the caller is responsible for refusing to
    attribute an ambiguous result (see `partition_service._capture_pr_url`,
    which records `NULL` unless exactly one URL matched). A bounded `deque`
    means a very long-running process eventually forgets old entries -- which
    loses a link, never invents one.
    """
    with _pr_ledger_lock:
        entries = list(_pr_ledger)
    return tuple(
        entry.url
        for entry in entries
        if entry.seq > mark
        and (project is None or entry.project == project)
        and (attribution is None or entry.attribution == attribution)
    )


def _derived_pr_title(*, project: ProjectConfig, branch: str) -> str | None:
    """Title from the commits this branch adds over its base.

    Best-effort: a title is a nicety and must never stop a PR from opening.
    Returns ``None`` on any git trouble so the forge falls back to its own
    default.
    """
    base = getattr(project, "default_branch", None) or "main"
    try:
        repo = Repo(str(project.path))
        # `Commit.message` is typed `str | bytes`; normalise before splitting
        # so a bytes message cannot silently produce bytes subjects.
        subjects: list[str] = []
        for commit in repo.iter_commits(f"origin/{base}..{branch}", max_count=20):
            message = commit.message
            if isinstance(message, bytes):
                message = message.decode("utf-8", errors="replace")
            first_line = message.splitlines()[0].strip() if message.strip() else ""
            if first_line:
                subjects.append(first_line)
    except Exception as exc:  # noqa: BLE001 - never block opening a PR
        logger.debug("git.pr_title_derive_failed", branch=branch, error=str(exc))
        return None
    if not subjects:
        return None
    # iter_commits walks newest-first; the oldest commit is the one that
    # introduced the change, and reads best as the headline.
    return build_pr_title(branch=branch, commit_subjects=list(reversed(subjects)))


def create_pr(
    *, project: ProjectConfig, branch: str, git: GitActions, task_result: str | None = None
) -> str | None:
    """Open a pull request via the project's forge provider (developer-opens-PR flow).

    Returns the PR's URL when the forge reported one, else ``None``
    (propose -> ratify -> dispatch PRD, spec §8 -- `ForgeProvider.open_pr` is
    widened to ``-> str | None``). A provider that cannot cheaply produce the
    URL returns ``None`` and the partition journal shows "—";
    **never a fabricated URL**. The return value is additive: every
    pre-existing caller ignores it and is unaffected.

    Phase 1 (forge plugin type): thin wrapper around
    ``resolve_forge(project).open_pr`` -- for ``forge: "github"`` (every
    pre-existing project, since that's the default) this is byte-identical to
    the direct ``gh pr create`` subprocess call this function used to make
    inline (that logic now lives in
    ``hivepilot.forges.github.GitHubForge.open_pr``).

    ``pr_body_file`` fix: ``git.pr_body_file`` is a task-config-declared
    filename the engine itself never writes -- nothing upstream ever
    verified it actually existed before handing it to a forge, so a missing
    file crashed the whole PR-creation call (losing the stage's entire
    already-paid-for output; see ``resolve_pr_body_file`` for the full
    writeup). When a file IS declared, ``resolve_pr_body_file`` resolves it
    (relative to *project.path*, never the orchestrator process's cwd) if
    present and non-blank, and otherwise materializes a redacted,
    size-capped fallback body from *task_result* (falling further back to
    ``git.pr_title``/``git.commit_message``, then a generic message) --
    logging a warning whenever the declared file couldn't actually be used.
    When NO file is declared, ``git`` is passed through completely
    unchanged -- every forge (github/forgejo/gitlab) already has its own
    correct, tested "no custom body" default for that case, so this fix
    applies without touching any individual ``ForgeProvider`` implementation
    or altering pre-existing, already-correct behaviour.

    Swarm Phase 1: on a SUCCESSFUL open, best-effort publishes a `pr_ready`
    event (kind="opened") -- see `_publish_pr_ready_best_effort`. A raised
    forge error propagates unchanged (unaffected by this addition); nothing
    is ever published for a PR that failed to open.
    """
    with resolve_pr_body_file(
        base_dir=project.path, git=git, fallback_text=task_result
    ) as body_path:
        # `body_path` is None when `pr_body_file` was never declared -- leave
        # `git` untouched so every forge's own "no custom body" default stays
        # byte-identical (see resolve_pr_body_file's docstring).
        updates: dict[str, Any] = {}
        if body_path is not None:
            updates["pr_body_file"] = str(body_path)
        # Only fill a title the operator did not set. An explicit `pr_title`
        # is a deliberate choice and must win over anything derived here.
        if not git.pr_title:
            derived = _derived_pr_title(project=project, branch=branch)
            if derived:
                updates["pr_title"] = derived
        effective_git = git.model_copy(update=updates) if updates else git
        raw_url = resolve_forge(project).open_pr(project=project, branch=branch, git=effective_git)
    _publish_pr_ready_best_effort(project, branch, "opened")
    # A provider written against the PRE-widening `-> None` signature (an
    # out-of-tree forge plugin, or a test double) returns `None`, and a
    # `MagicMock` double returns a mock -- neither is a URL. Only a genuine
    # string is ever ledgered, so the journal can never learn a fabricated
    # link from a stale plugin.
    pr_url = raw_url if isinstance(raw_url, str) and raw_url.strip() else None
    if pr_url is not None:
        record_pr_opened(project=project.path.name, branch=branch, url=pr_url)
    return pr_url


def promote_pr(*, project: ProjectConfig, branch: str, git: GitActions) -> None:
    """Mark *branch*'s draft PR ready for review via the project's forge provider.

    Sibling to merge_pr: same subprocess/error-handling shape (now inside the
    resolved provider). Only called by perform_git_actions when the gating
    stage's own verdict did not block (see _agent_verdict_blocked).

    Swarm Phase 1: on a SUCCESSFUL promote, best-effort publishes a
    `pr_ready` event (kind="promoted") -- see `_publish_pr_ready_best_effort`.
    """
    resolve_forge(project).promote_pr(project=project, branch=branch, git=git)
    _publish_pr_ready_best_effort(project, branch, "promoted")


def merge_pr(*, project: ProjectConfig, branch: str, git: GitActions) -> None:
    """Merge the open PR for *branch* via the project's forge provider --
    Jules' autonomous final approval.

    Merge (not a review approval) because GitHub forbids approving your own PR, so
    the actionable autonomous step in a solo workflow is the merge itself.
    """
    resolve_forge(project).merge_pr(project=project, branch=branch, git=git)


def _verdict_role_summary(
    verdict: "Verdict | None", task_result: str | None, role: str | None = None
) -> str | None:
    """`role: STATUS` pairs for the report, from the judge or the stage output.

    Prefers the judge's structured `per_role_stance` and falls back to the
    stage's own `status:` line, so a run with no judge still explains itself.
    """
    stance = getattr(verdict, "per_role_stance", None) if verdict is not None else None
    if isinstance(stance, dict) and stance:
        return "; ".join(f"{role}: {value}" for role, value in stance.items())
    if not task_result:
        return None
    from hivepilot.services.agent_report import parse_agent_report

    status = parse_agent_report(task_result).status.strip().upper()
    if not status:
        return None
    # The STAGE'S ROLE, never the literal "stage". No interaction is recorded
    # under "stage", so a report built from it quotes nothing -- which is
    # exactly the bare "BLOCKED" this feature exists to replace. Seen on a real
    # PR before this line existed.
    return f"{role or 'stage'}: {status}"


def post_blocked_report(
    *,
    forge: Any,
    project: Any,
    branch: str,
    run_id: int | None,
    verdict_summary: str | None,
    verification: "VerificationReport | None" = None,
    post: Any = None,
) -> str | None:
    """Put the gate's reasoning on the pull request when it blocks.

    `create_pr` already runs on a blocked gate specifically "so a human can see
    the review report on the PR itself" -- but nothing ever wrote one. Measured
    on PR #428: 0 reviews, 0 comments, and a 43-character body, while the
    reviewer, CISO and QA had produced 31 KB of findings between them.

    Returns the posted body, or ``None`` when nothing blocked. Never raises:
    a comment that cannot be delivered must not take down the git actions that
    matter, and the gate has already done its job by refusing to promote.
    """
    from hivepilot.services import pr_verdict_report

    body = ""
    # The agent half needs a run id, because it reads each role's recorded
    # output back out of `interactions`. The deterministic half does not: a
    # failing check carries its own output, so a refusal is never left without
    # its reason merely because no run was recorded.
    if run_id is not None:
        try:
            body = (
                pr_verdict_report.build_report(run_id=run_id, verdict_summary=verdict_summary) or ""
            )
        except Exception as exc:  # noqa: BLE001 - reporting must not break the run
            logger.warning("git.blocked_report_failed", branch=branch, error=str(exc))

    if verification is not None and verification.blocking:
        body = (body + "\n\n" if body else "") + _render_verification(verification)

    if not body:
        return None

    sink = post
    if sink is None:
        if forge is None:
            return None

        def sink(text: str) -> None:  # type: ignore[misc]
            forge.comment_pr(project=project, branch=branch, body=text)

    try:
        sink(body)
        logger.info("git.blocked_report_posted", branch=branch, run_id=run_id, chars=len(body))
    except Exception as exc:  # noqa: BLE001
        logger.warning("git.blocked_report_post_failed", branch=branch, error=str(exc))
    return body
