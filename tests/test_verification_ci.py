"""Consult the forge's checks instead of running the suite on the box.

Why this exists rather than a toolchain install on the orchestration VM:

* **The checks execute code the agents just wrote.** On the box that is
  arbitrary code running as the service user, beside the secrets, the state
  database and the vault. In CI it is an ephemeral container.
* **The environment already exists and is correct.** Installing the same
  toolchain on the box creates a SECOND copy that drifts -- the shared
  `node_modules` ahead of its lockfile, again.

It does not replace local checks, and must not. CI runs what
`.github/workflows` contains, and the agent can write that file: a control the
subject can weaken is not a control. So the adversarial probes stay local and
operator-declared, and `ci:` carries the heavy product suite.

Fail-closed, and one case matters more than the rest: **"no checks reported" is
not a pass.** Reading a subset is the same mistake -- `gh pr checks | tail -3`
once hid a mypy failure and put two red pull requests on the trunk.
"""

from __future__ import annotations

import pytest

from hivepilot.services.verification_service import Check, CheckRun, run_checks


def _probe(*runs, calls=None):
    """A CI probe returning *runs*, recording how many times it was polled."""

    def probe():
        if calls is not None:
            calls.append(1)
        return list(runs)

    return probe


def _ci(name="ci", **kw):
    return Check(name=name, command=None, ci=True, **kw)


class TestTheConclusionIsTheVerdict:
    def test_all_successful_passes(self, tmp_path):
        report = run_checks(
            [_ci()],
            cwd=tmp_path,
            ci_probe=_probe(
                CheckRun("pytest", "completed", "success"),
                CheckRun("mypy", "completed", "success"),
            ),
        )

        assert report.blocking is False

    def test_one_failure_among_successes_blocks(self, tmp_path):
        """The `| tail -3` mistake, pinned: every run is read, not the last few."""
        report = run_checks(
            [_ci()],
            cwd=tmp_path,
            ci_probe=_probe(
                CheckRun("pytest", "completed", "success"),
                CheckRun("mypy", "completed", "failure"),
                CheckRun("ruff", "completed", "success"),
            ),
        )

        assert report.blocking is True
        assert "mypy" in report.results[0].output

    @pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out", "action_required"])
    def test_every_non_success_conclusion_blocks(self, tmp_path, conclusion):
        report = run_checks(
            [_ci()], cwd=tmp_path, ci_probe=_probe(CheckRun("suite", "completed", conclusion))
        )

        assert report.blocking is True

    @pytest.mark.parametrize("conclusion", ["success", "skipped", "neutral"])
    def test_only_these_conclusions_pass(self, tmp_path, conclusion):
        report = run_checks(
            [_ci()], cwd=tmp_path, ci_probe=_probe(CheckRun("suite", "completed", conclusion))
        )

        assert report.blocking is False


class TestNoChecksReportedIsNotAPass:
    def test_an_empty_result_blocks(self, tmp_path):
        """The single most important case. A repository with no workflow, a
        workflow that did not trigger, a wrong branch -- all produce silence,
        and silence has already been read as success in this codebase."""
        report = run_checks([_ci()], cwd=tmp_path, ci_probe=_probe())

        assert report.blocking is True
        assert "no check" in report.results[0].output.lower()

    def test_a_probe_that_raises_blocks(self, tmp_path):
        def boom():
            raise RuntimeError("gh: could not resolve to a Repository")

        report = run_checks([_ci()], cwd=tmp_path, ci_probe=boom)

        assert report.blocking is True

    def test_no_probe_at_all_blocks(self, tmp_path):
        """A `ci:` check declared where nothing can consult a forge verifies
        nothing."""
        report = run_checks([_ci()], cwd=tmp_path)

        assert report.blocking is True


class TestWaitingIsNotPassingAndNotFailingForever:
    def test_it_polls_until_the_runs_complete(self, tmp_path):
        states = [
            [CheckRun("suite", "in_progress", None)],
            [CheckRun("suite", "completed", "success")],
        ]

        def probe():
            return states.pop(0) if len(states) > 1 else states[0]

        report = run_checks(
            [_ci(timeout_seconds=30)], cwd=tmp_path, ci_probe=probe, sleep=lambda _s: None
        )

        assert report.blocking is False

    def test_still_queued_at_the_timeout_blocks(self, tmp_path):
        """Waiting is not passing. A queue that never drains must not promote."""
        report = run_checks(
            [_ci(timeout_seconds=0)],
            cwd=tmp_path,
            ci_probe=_probe(CheckRun("suite", "queued", None)),
            sleep=lambda _s: None,
        )

        assert report.blocking is True
        assert "still running" in report.results[0].output.lower()


class TestItStaysBesideTheLocalChecks:
    def test_a_local_check_and_a_ci_check_both_apply(self, tmp_path):
        import sys

        report = run_checks(
            [
                Check(name="hostile-input", command=f'{sys.executable} -c "raise SystemExit(1)"'),
                _ci(),
            ],
            cwd=tmp_path,
            ci_probe=_probe(CheckRun("suite", "completed", "success")),
        )

        assert report.blocking is True  # the local probe still vetoes
        assert [r.name for r in report.results] == ["hostile-input", "ci"]


class TestTheDeclarationIsValidated:
    def test_a_check_with_neither_command_nor_ci_is_rejected(self):
        with pytest.raises(ValueError):
            Check(name="empty", command=None, ci=False)

    def test_a_check_with_both_is_rejected(self):
        """Ambiguity here would silently pick one and skip the other."""
        with pytest.raises(ValueError):
            Check(name="both", command="true", ci=True)
