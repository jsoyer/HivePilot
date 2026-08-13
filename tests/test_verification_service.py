"""A check that did not run is not a check that passed.

Measured on the A/B, 2026-08-12: ten stages, six review outputs totalling
~62 000 characters, $7.20 per arm, and none of it found that the produced tool
emitted a raw `\\x1b[31m` byte to stdout. A five-line hostile-input probe found
it instantly.

The review layer is generative, not verificatory: it produces prose *about* the
code rather than executing it. Catching an injection by reading requires
noticing; catching it by running is deterministic. That is why the reviewer
caught this class once (run 485) and missed it twice.

So the release gate needs an input that is not an opinion. Today it has exactly
one -- `_agent_verdict_blocked`, which parses a `status:` line out of an LLM's
prose -- plus optionally a judge verdict, which is another LLM.

The polarity is deliberately OPPOSITE to that path. The agent gate fails OPEN
on purpose: an unparseable verdict must not let a broken parser freeze every
pipeline. A deterministic check has no such excuse. If it timed out, if the
binary was missing, if it raised -- nobody verified anything, and reporting
that as a pass is the exact failure this exists to remove.
"""

from __future__ import annotations

import sys

import pytest

from hivepilot.services.verification_service import Check, run_checks


def _py(code: str) -> str:
    """A check command that needs no shell builtins and no PATH luck."""
    return f'{sys.executable} -c "{code}"'


class TestTheExitCodeIsTheVerdict:
    def test_exit_zero_passes(self, tmp_path):
        report = run_checks([Check(name="ok", command=_py("pass"))], cwd=tmp_path)

        assert report.blocking is False
        assert report.results[0].outcome == "passed"
        assert report.results[0].exit_code == 0

    def test_a_non_zero_exit_blocks(self, tmp_path):
        report = run_checks(
            [Check(name="probe", command=_py("import sys; sys.exit(3)"))], cwd=tmp_path
        )

        assert report.blocking is True
        assert report.results[0].outcome == "failed"
        assert report.results[0].exit_code == 3

    def test_no_opinion_is_parsed_out_of_the_output(self, tmp_path):
        """A check that prints the word PASS while exiting 1 still blocks.

        The whole point is that the verdict comes from the process, not from
        text a model -- or a test fixture -- can write.
        """
        report = run_checks(
            [Check(name="liar", command=_py("print('status: PASS'); raise SystemExit(1)"))],
            cwd=tmp_path,
        )

        assert report.blocking is True


class TestFailClosed:
    def test_a_timeout_blocks(self, tmp_path):
        report = run_checks(
            [Check(name="slow", command=_py("import time; time.sleep(30)"), timeout_seconds=1)],
            cwd=tmp_path,
        )

        assert report.blocking is True
        assert report.results[0].outcome == "errored"
        assert "timed out" in report.results[0].output.lower()

    def test_a_missing_binary_blocks(self, tmp_path):
        report = run_checks(
            [Check(name="absent", command="hivepilot-no-such-binary-9f3a --run")], cwd=tmp_path
        )

        assert report.blocking is True

    def test_a_check_that_cannot_start_blocks(self, tmp_path):
        """cwd that does not exist: the check never ran, so it never passed."""
        report = run_checks(
            [Check(name="nowhere", command=_py("pass"))], cwd=tmp_path / "does-not-exist"
        )

        assert report.blocking is True
        assert report.results[0].outcome == "errored"

    def test_one_failure_among_passes_still_blocks(self, tmp_path):
        report = run_checks(
            [
                Check(name="a", command=_py("pass")),
                Check(name="b", command=_py("raise SystemExit(1)")),
                Check(name="c", command=_py("pass")),
            ],
            cwd=tmp_path,
        )

        assert report.blocking is True
        assert [r.outcome for r in report.results] == ["passed", "failed", "passed"]

    def test_every_check_runs_even_after_one_fails(self, tmp_path):
        """Stopping at the first failure would hide the others, and the operator
        would fix one, re-run, and discover the next -- one expensive run at a
        time."""
        report = run_checks(
            [
                Check(name="a", command=_py("raise SystemExit(1)")),
                Check(name="b", command=_py("raise SystemExit(1)")),
            ],
            cwd=tmp_path,
        )

        assert len(report.results) == 2


class TestNoChecksIsNotAPass:
    def test_an_empty_list_does_not_block(self, tmp_path):
        """Backward compatible: a pipeline that declares nothing behaves as it
        always has."""
        report = run_checks([], cwd=tmp_path)

        assert report.blocking is False

    def test_but_it_reports_that_nothing_was_verified(self, tmp_path):
        """The distinction the whole session turned on: 'verified and clean' and
        'never checked' must not render the same."""
        report = run_checks([], cwd=tmp_path)

        assert report.ran is False
        assert "no deterministic check" in report.summary.lower()

    def test_a_passing_run_says_what_it_verified(self, tmp_path):
        report = run_checks([Check(name="hostile-input", command=_py("pass"))], cwd=tmp_path)

        assert report.ran is True
        assert "hostile-input" in report.summary


class TestTheOutputIsUsable:
    def test_the_failing_output_is_captured(self, tmp_path):
        """A gate that says BLOCKED without saying why moves the work of finding
        out onto the person least able to do it."""
        report = run_checks(
            [
                Check(
                    name="probe",
                    command=_py(
                        "import sys; sys.stderr.write('ESC byte reached stdout'); sys.exit(1)"
                    ),
                )
            ],
            cwd=tmp_path,
        )

        assert "ESC byte reached stdout" in report.results[0].output

    def test_the_output_is_bounded(self, tmp_path):
        report = run_checks(
            [Check(name="loud", command=_py("print('x' * 200000); raise SystemExit(1)"))],
            cwd=tmp_path,
        )

        assert len(report.results[0].output) < 20_000
        assert "truncated" in report.results[0].output.lower()

    def test_a_registered_secret_is_redacted(self, tmp_path):
        from hivepilot.services.config_provenance import register_secret_value

        register_secret_value("sk-verification-must-not-leak-4471")
        report = run_checks(
            [
                Check(
                    name="leaky",
                    command=_py(
                        "print('token sk-verification-must-not-leak-4471'); raise SystemExit(1)"
                    ),
                )
            ],
            cwd=tmp_path,
        )

        assert "sk-verification-must-not-leak-4471" not in report.results[0].output


class TestItNeverTakesDownTheRun:
    def test_a_broken_check_definition_is_reported_not_raised(self, tmp_path):
        """Reporting must not become a new way for the pipeline to die --
        but it must also not become a way for it to pass."""
        report = run_checks([Check(name="empty", command="   ")], cwd=tmp_path)

        assert report.blocking is True
        assert report.results[0].outcome == "errored"


class TestTheSummaryNamesWhatFailed:
    def test_it_names_the_failing_check_and_its_code(self, tmp_path):
        report = run_checks(
            [
                Check(name="tests", command=_py("pass")),
                Check(name="hostile-input", command=_py("raise SystemExit(2)")),
            ],
            cwd=tmp_path,
        )

        assert "hostile-input" in report.summary
        assert "2" in report.summary

    @pytest.mark.parametrize("code", [1, 2, 127])
    def test_the_exit_code_survives_into_the_report(self, tmp_path, code):
        report = run_checks(
            [Check(name="c", command=_py(f"raise SystemExit({code})"))], cwd=tmp_path
        )

        assert report.results[0].exit_code == code
