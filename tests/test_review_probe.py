"""Tests for the on-demand review probe.

The review gate runs when the whole `noxys` pipeline runs, which happened
twice in five weeks — and only 2 of 8 attempts survived to the last stage.
Verifying a change to that gate by waiting for a real pipeline run means a
two-to-three week feedback loop with a 1-in-4 hit rate. That is the loop this
module exists to replace.

Two properties matter more than the plumbing:

- **A dry run must call nobody.** An exerciser that costs three opus
  dispatches every time it is used is one nobody uses, which defeats it.
- **A real run must report what it cost.** The question that prompted this
  ("do all three reviewers need to be on opus?") was unanswerable because
  reviewer dispatches were never recorded. A probe that cannot answer it
  would replace one blind spot with a faster one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hivepilot.services import review_probe, state_service


class _FakeCompleted:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def test_fetch_pr_diff_asks_gh_for_the_right_pr(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, list[str]] = {}

    def fake_run(cmd, **_kwargs):
        seen["cmd"] = cmd
        return _FakeCompleted("diff --git a/x b/x\n+line\n")

    monkeypatch.setattr(review_probe.subprocess, "run", fake_run)

    diff = review_probe.fetch_pr_diff(424, repo="noxys-eu/noxys")

    assert "diff --git" in diff
    assert seen["cmd"][:3] == ["gh", "pr", "diff"]
    assert "424" in seen["cmd"]
    assert "noxys-eu/noxys" in seen["cmd"]


def test_fetch_pr_diff_fails_loudly_rather_than_returning_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty subject is what makes a review block on nothing.

    Returning "" from a failed fetch would feed the exact fail-closed path
    this probe exists to diagnose, and the operator would read a real gh
    failure as "the gate blocked".
    """
    monkeypatch.setattr(
        review_probe.subprocess,
        "run",
        lambda *_a, **_k: _FakeCompleted("", returncode=1, stderr="no such PR"),
    )

    with pytest.raises(review_probe.ReviewProbeError) as excinfo:
        review_probe.fetch_pr_diff(999999, repo="noxys-eu/noxys")

    assert "no such PR" in str(excinfo.value)


class TestBranchPrDiff:
    """`review_target: github_pr` must review the PR, not the working tree.

    The two targets used to differ only in where the block was enforced.
    Since the gate has to sit on the stage that owns `promote_pr`, and that
    stage commits nothing, `git diff` there was empty every single time — so
    the reviewers were handed nothing and blocked on it.
    """

    def test_it_asks_gh_from_the_stages_own_checkout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict[str, object] = {}

        def fake_run(cmd, **kwargs):
            seen["cmd"] = cmd
            seen["cwd"] = kwargs.get("cwd")
            return _FakeCompleted("diff --git a/x b/x\n+line\n")

        monkeypatch.setattr(review_probe.subprocess, "run", fake_run)

        diff = review_probe.fetch_pr_diff_for_branch("/repo/here")

        assert "diff --git" in diff
        # No PR number and no --repo: a pipeline stage knows its checkout,
        # not a PR number, and `gh pr diff` resolves both from the branch.
        assert seen["cmd"] == ["gh", "pr", "diff"]
        assert seen["cwd"] == "/repo/here"

    def test_no_open_pr_raises_instead_of_returning_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Silently returning "" would read as "the reviewers blocked this".

        The caller turns this into a recorded blocking verdict naming the
        cause, which is a different and far more useful signal.
        """
        monkeypatch.setattr(
            review_probe.subprocess,
            "run",
            lambda *_a, **_k: _FakeCompleted("", returncode=1, stderr="no pull requests found"),
        )

        with pytest.raises(review_probe.ReviewProbeError) as excinfo:
            review_probe.fetch_pr_diff_for_branch("/repo/here")

        assert "no pull requests found" in str(excinfo.value)

    def test_an_empty_pr_diff_raises_too(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            review_probe.subprocess, "run", lambda *_a, **_k: _FakeCompleted("   \n")
        )

        with pytest.raises(review_probe.ReviewProbeError):
            review_probe.fetch_pr_diff_for_branch("/repo/here")


def test_a_dry_run_dispatches_nobody(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    called = []

    monkeypatch.setattr(
        review_probe,
        "_run_review_for",
        lambda **kwargs: called.append(kwargs),
    )

    result = review_probe.run_probe(
        subject="diff --git a/x b/x\n+line",
        reviewers=["reviewer", "ciso", "qa"],
        project="noxys",
        confidence_threshold=0.7,
        dry_run=True,
    )

    assert called == [], "a dry run must not dispatch a single reviewer"
    assert result.dry_run is True
    assert result.reviewers == ["reviewer", "ciso", "qa"]
    assert result.subject_bytes == len("diff --git a/x b/x\n+line")


def test_a_dry_run_reports_the_model_behind_each_reviewer(tmp_path: Path) -> None:
    """The cost question is about models, so a dry run has to name them.

    Without this the operator has to go read roles.yaml to find out what a
    real run would cost them.
    """
    result = review_probe.run_probe(
        subject="diff",
        reviewers=["reviewer", "qa"],
        project="noxys",
        confidence_threshold=0.7,
        dry_run=True,
    )

    planned = {r.role: r for r in result.planned}
    assert set(planned) == {"reviewer", "qa"}, "no reviewer may be silently dropped"
    # A role may legitimately declare no model (it inherits the runner's
    # default), so the assertion is that each one RESOLVED — an unresolvable
    # reviewer surfaces as runner "?" and still blocks, which the operator
    # has to be able to see.
    assert all(p.runner != "?" for p in planned.values()), f"unresolved reviewer: {planned}"


def test_a_real_run_reports_what_it_cost(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The payoff. Cost per reviewer is the number that was never measured."""

    def fake_review(*, run_id: int, **_kwargs) -> None:
        state_service.record_step(
            run_id,
            "review:reviewer",
            "success",
            provider="claude",
            model="opus",
            cost_usd=0.62,
            role="reviewer",
        )
        state_service.record_step(
            run_id,
            "review:qa",
            "success",
            provider="claude",
            model="sonnet",
            cost_usd=0.25,
            role="qa",
        )

    monkeypatch.setattr(review_probe, "_run_review_for", fake_review)

    result = review_probe.run_probe(
        subject="diff --git a/x b/x\n+line",
        reviewers=["reviewer", "qa"],
        project="noxys",
        confidence_threshold=0.7,
        dry_run=False,
    )

    assert result.dry_run is False
    by_role = {s.role: s for s in result.dispatched}
    assert by_role["reviewer"].cost_usd == pytest.approx(0.62)
    assert by_role["qa"].cost_usd == pytest.approx(0.25)
    assert result.total_cost_usd == pytest.approx(0.87)


def test_a_real_run_never_touches_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A probe that could promote a PR would be a gate, not a probe.

    `_run_review` is called directly, never `perform_git_actions`, so there
    is no path from running this to changing a PR's state.
    """
    import hivepilot.services.review_probe as mod

    source = Path(mod.__file__).read_text(encoding="utf-8")

    # Call syntax, not any mention — the module's own docstring explains why
    # it stays away from these, and a bare substring match would flag that
    # explanation as the very thing it warns against.
    for forbidden in ("perform_git_actions(", "promote_pr=", "merge_pr="):
        assert forbidden not in source, f"the probe must never reach {forbidden}"
