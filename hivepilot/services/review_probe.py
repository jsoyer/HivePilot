"""Run the adversarial review on demand, against one PR, without a pipeline.

The review gate only runs as part of a full pipeline run. In practice that
was **twice in five weeks** on `noxys`, and only 2 of 8 attempts survived to
the stage that carries it. Verifying any change to that gate by waiting for a
real run meant a two-to-three week feedback loop with a 1-in-4 hit rate —
long enough that a fail-closed bug would sit in production unnoticed between
attempts.

This module collapses that loop to one command. It resolves the same
`EffectiveDebateConfig` the pipeline resolves, fetches a real subject, and
calls the same `Orchestrator._run_review` the pipeline calls. Exercising a
*copy* of the review path would prove nothing about the real one.

**It never touches git.** `_run_review` is invoked directly and
`perform_git_actions` is never reached, so no run of this can promote, merge,
or otherwise change a PR's state. A test asserts that by reading this file.

**A dry run dispatches nobody.** Two of the three noxys reviewers resolve to
opus, so an exerciser that costs a full panel every invocation is one that
gets used once. The dry run names each reviewer and the model behind it,
which is also what makes the cost visible before spending it.

**A real run reports what it cost.** Reviewer dispatches only became
measurable in the change immediately before this one; until then a review
round appeared in no figure anywhere. A probe that could not answer "what did
that cost" would replace a blind spot with a faster blind spot.
"""

from __future__ import annotations

import subprocess  # noqa: S404 — `gh` invocation, argument list is never shell-interpolated
from dataclasses import dataclass, field
from typing import Any

from hivepilot.services import db, state_service
from hivepilot.services.diff_filter import summarise_noisy_files
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

# `gh pr diff` on a large PR is still a single HTTP round trip; this bound is
# about a hung network, not about diff size.
_GH_TIMEOUT_SECONDS = 60


class ReviewProbeError(RuntimeError):
    """A probe could not be run. Raised rather than returning an empty subject.

    An empty subject is precisely what makes `_run_review` fail closed, so a
    failed `gh` call that returned `""` would be indistinguishable from a
    genuine "the reviewers blocked this" — and the operator would go looking
    for a review problem that does not exist.
    """


@dataclass(frozen=True)
class PlannedReviewer:
    """A reviewer the probe would dispatch, and what it would cost to."""

    role: str
    display_name: str | None
    runner: str
    model: str | None
    effort: str | None


@dataclass(frozen=True)
class DispatchedReviewer:
    """A reviewer the probe actually dispatched, with its recorded cost."""

    role: str
    model: str | None
    status: str
    cost_usd: float


@dataclass(frozen=True)
class ReviewProbeResult:
    reviewers: list[str]
    # The raw diff length. Kept even after filtering lands, because it is
    # still the proof that a rebuild happened — losing it would hide that
    # fact from an operator who only ever reads `reviewed_bytes`.
    subject_bytes: int
    # What `summarise_noisy_files` leaves once generated files are stripped
    # out — this is what the reviewer actually reads, and so what the run
    # actually costs.
    reviewed_bytes: int
    dry_run: bool
    planned: list[PlannedReviewer] = field(default_factory=list)
    dispatched: list[DispatchedReviewer] = field(default_factory=list)
    run_id: int | None = None

    @property
    def total_cost_usd(self) -> float:
        return round(sum(d.cost_usd for d in self.dispatched), 6)


def fetch_pr_diff(pr: int, *, repo: str, timeout: int = _GH_TIMEOUT_SECONDS) -> str:
    """The PR's diff, via `gh pr diff`.

    Raises `ReviewProbeError` on any failure — see that class for why an
    empty string would be the wrong answer here.
    """
    cmd = ["gh", "pr", "diff", str(pr), "--repo", repo]
    try:
        completed = subprocess.run(  # noqa: S603 — fixed argv, no shell
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except Exception as exc:  # noqa: BLE001 — surfaced to the operator, not swallowed
        raise ReviewProbeError(f"could not run `gh pr diff`: {exc}") from exc

    if completed.returncode != 0:
        detail = (completed.stderr or "").strip() or f"exit {completed.returncode}"
        raise ReviewProbeError(f"`gh pr diff {pr} --repo {repo}` failed: {detail}")

    diff = completed.stdout or ""
    if not diff.strip():
        raise ReviewProbeError(
            f"PR {pr} in {repo} produced an empty diff — there is nothing to review, "
            "which is a different problem from the reviewers blocking it"
        )
    return diff


def fetch_pr_diff_for_branch(repo_path: str, *, timeout: int = _GH_TIMEOUT_SECONDS) -> str:
    """The diff of the PR open on *repo_path*'s current branch.

    This is what `review_target: "github_pr"` was always supposed to review.
    Until now the only subject the orchestrator ever computed was `git diff`
    on the working tree, so `github_pr` and `internal` differed solely in
    *where the block was enforced* — never in *what was reviewed*. On the
    noxys pipeline the review sits on a stage that commits nothing, so the
    working tree was clean and the reviewers were handed an empty subject
    every single time.

    `gh pr diff` with no arguments resolves the repository and the current
    branch's PR by itself, which is why neither is passed: the pipeline stage
    knows its checkout, not a PR number.

    Raises `ReviewProbeError` rather than returning "" — see that class.
    """
    cmd = ["gh", "pr", "diff"]
    try:
        completed = subprocess.run(  # noqa: S603 — fixed argv, no shell
            cmd, capture_output=True, text=True, timeout=timeout, check=False, cwd=repo_path
        )
    except Exception as exc:  # noqa: BLE001 — surfaced, not swallowed
        raise ReviewProbeError(f"could not run `gh pr diff` in {repo_path}: {exc}") from exc

    if completed.returncode != 0:
        detail = (completed.stderr or "").strip() or f"exit {completed.returncode}"
        raise ReviewProbeError(f"`gh pr diff` in {repo_path} failed: {detail}")

    diff = completed.stdout or ""
    if not diff.strip():
        raise ReviewProbeError(
            f"the PR for {repo_path}'s current branch has an empty diff — nothing to review"
        )
    return diff


def _plan_each_reviewer(reviewers: list[str]) -> list[PlannedReviewer]:
    """Resolve each reviewer to the runner and model it would dispatch on.

    Unresolvable roles are surfaced with `model=None` rather than dropped —
    a reviewer that cannot be resolved is counted as blocking by
    `_run_review`, so hiding it here would make a blocked review look
    unexplained.
    """
    from hivepilot.roles import get_role, resolve_runner

    planned: list[PlannedReviewer] = []
    for role_name in reviewers:
        try:
            role = get_role(role_name)
            runner, model, effort = resolve_runner(role_name, None)
            planned.append(
                PlannedReviewer(
                    role=role_name,
                    display_name=role.display_name,
                    runner=runner,
                    model=model,
                    effort=str(effort) if effort is not None else None,
                )
            )
        except Exception as exc:  # noqa: BLE001 — reported, never silently dropped
            logger.warning("review_probe.role_unresolved", role=role_name, error=str(exc))
            planned.append(
                PlannedReviewer(
                    role=role_name, display_name=None, runner="?", model=None, effort=None
                )
            )
    return planned


def plan_reviewers(subject: str, *, reviewers: list[str]) -> ReviewProbeResult:
    """What a dry run reports: each reviewer's dispatch plan, plus what
    *subject* costs both before and after the noise filter — see
    `ReviewProbeResult.reviewed_bytes` for why both numbers are kept.
    """
    return ReviewProbeResult(
        reviewers=list(reviewers),
        subject_bytes=len(subject),
        reviewed_bytes=len(summarise_noisy_files(subject)),
        dry_run=True,
        planned=_plan_each_reviewer(reviewers),
    )


def _run_review_for(
    *,
    orchestrator: Any,
    subject: str,
    reviewers: list[str],
    project_path: Any,
    confidence_threshold: float,
    run_id: int,
) -> None:
    """Call the pipeline's own `_run_review`. Seam for tests to stub."""
    from pathlib import Path

    from hivepilot.models import EffectiveDebateConfig, ProjectConfig

    effective = EffectiveDebateConfig(
        enable_judge=False,
        enable_arbiter=False,
        runner="claude",
        model=None,
        confidence_threshold=confidence_threshold,
        reviewers=list(reviewers),
        # `internal` would raise `ReviewBlockedError` on a blocking verdict.
        # A probe reports; it does not abort. `github_pr` lets the verdict be
        # registered and read back instead of thrown.
        review_target="github_pr",
    )
    orchestrator._run_review(
        stage=None,
        pipeline=None,
        effective=effective,
        project=ProjectConfig(path=Path(project_path)),
        policy=None,
        subject=subject,
        simulate=False,
        run_id=run_id,
    )


def _recorded_dispatches(run_id: int) -> list[DispatchedReviewer]:
    state_service.init_db()
    with db.connect() as conn:
        rows = conn.execute(
            db.ph(
                "SELECT step, role, model, status, cost_usd FROM steps "
                "WHERE run_id = ? AND step LIKE 'review:%' ORDER BY id"
            ),
            (run_id,),
        ).fetchall()
    return [
        DispatchedReviewer(
            role=str(r["role"] or str(r["step"]).removeprefix("review:")),
            model=r["model"],
            status=str(r["status"]),
            cost_usd=float(r["cost_usd"] or 0.0),
        )
        for r in rows
    ]


def run_probe(
    *,
    subject: str,
    reviewers: list[str],
    project: str,
    confidence_threshold: float = 0.7,
    dry_run: bool = True,
    project_path: str = ".",
    orchestrator: Any | None = None,
) -> ReviewProbeResult:
    """Run (or plan) one adversarial review round over *subject*.

    Defaults to `dry_run=True`: the expensive thing should be the one you ask
    for explicitly, not the one you get by forgetting a flag.
    """
    if dry_run:
        return plan_reviewers(subject, reviewers=reviewers)

    planned = _plan_each_reviewer(reviewers)

    if orchestrator is None:
        from hivepilot.orchestrator import Orchestrator

        orchestrator = Orchestrator()

    # A run of its own, so the dispatches are attributable and this probe's
    # spend never lands on an unrelated pipeline run.
    #
    # It must also be CLOSED. The first version opened a run and never
    # completed it, so every probe left a row stuck at `running` forever —
    # cluttering the Runs view and making "what is executing right now"
    # unanswerable. `try/finally` because a reviewer raising must not leave
    # the row open either.
    run_id = state_service.record_run_start(project, "review-probe")
    try:
        _run_review_for(
            orchestrator=orchestrator,
            subject=subject,
            reviewers=reviewers,
            project_path=project_path,
            confidence_threshold=confidence_threshold,
            run_id=run_id,
        )
    except Exception as exc:  # noqa: BLE001 — record the outcome, then re-raise
        state_service.complete_run(run_id, "failed", str(exc)[:200])
        raise
    state_service.complete_run(run_id, "complete")
    return ReviewProbeResult(
        reviewers=list(reviewers),
        subject_bytes=len(subject),
        reviewed_bytes=len(summarise_noisy_files(subject)),
        dry_run=False,
        planned=planned,
        dispatched=_recorded_dispatches(run_id),
        run_id=run_id,
    )
