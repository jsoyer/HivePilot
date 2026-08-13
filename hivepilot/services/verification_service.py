"""Give the release gate an input that is not an opinion.

Why
---
Measured on the memory A/B, 2026-08-12: ten stages, six review outputs
totalling ~62 000 characters, $7.20 per arm -- and none of it found that the
produced tool emitted a raw ``\\x1b[31m`` byte to stdout. A five-line
hostile-input probe found it instantly, and the same class had been caught by
the reviewer once before (run 485) and missed twice.

The review layer is generative, not verificatory: it produces prose *about* the
code rather than executing it. Catching an injection by reading requires
noticing. Catching it by running is deterministic.

Today the gate has exactly one input -- ``_agent_verdict_blocked``, which
parses a ``status:`` line out of an LLM's prose -- plus, optionally, a judge
verdict, which is another LLM. This module adds the other kind.

The polarity is deliberately the OPPOSITE of the agent path
-------------------------------------------------------------
``_agent_verdict_blocked`` fails **open** on purpose: an unparseable verdict
must not let a broken parser freeze every pipeline, so anything it cannot read
means "proceed".

A deterministic check has no such excuse. Timed out, binary missing, could not
start, raised -- in every one of those cases nobody verified anything, and
reporting it as a pass is precisely the failure this exists to remove. So here,
anything that is not a clean exit 0 blocks.

Trust boundary
--------------
A check's command comes from the operator's configuration, and is run the same
way ``shell_runner`` runs a declared step -- same trust level, deliberately.
Agent output is never interpolated into a command: the whole value of this
module is that a model cannot influence its verdict, and a model that could
write part of the command would have influenced it completely.
"""

from __future__ import annotations

import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

#: Per-check output cap. A failing test suite can print megabytes; a gate
#: report nobody scrolls to the end of is a gate report nobody reads. The cut
#: is declared in the text, because a silent one reads as "that was all it
#: said".
_MAX_OUTPUT_CHARS = 8_000

#: Default per-check wall clock. Generous enough for a real test suite, finite
#: because a check that never returns must become a BLOCK rather than a hung
#: pipeline.
DEFAULT_TIMEOUT_SECONDS = 300


@dataclass(frozen=True)
class Check:
    """One deterministic check: a name for the report, a command to run."""

    name: str
    command: str
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS


@dataclass(frozen=True)
class CheckResult:
    """What one check did. ``outcome`` is never inferred from its output."""

    name: str
    outcome: str  # "passed" | "failed" | "errored"
    exit_code: int | None
    output: str
    duration_seconds: float

    @property
    def blocking(self) -> bool:
        return self.outcome != "passed"


@dataclass(frozen=True)
class VerificationReport:
    """Every check's result, and whether the gate should refuse."""

    results: tuple[CheckResult, ...]

    @property
    def ran(self) -> bool:
        """Whether any check ran at all.

        Kept separate from ``blocking`` because "verified and clean" and "never
        checked" are opposite findings that must not render the same -- the
        distinction this whole module exists to restore.
        """
        return bool(self.results)

    @property
    def blocking(self) -> bool:
        return any(result.blocking for result in self.results)

    @property
    def summary(self) -> str:
        if not self.results:
            return (
                "no deterministic check ran: this pipeline declares none, so nothing "
                "here attests that the produced code was executed at all"
            )
        failures = [r for r in self.results if r.blocking]
        if not failures:
            names = ", ".join(r.name for r in self.results)
            return f"{len(self.results)} deterministic check(s) passed: {names}"
        parts = []
        for result in failures:
            code = "no exit code" if result.exit_code is None else f"exit {result.exit_code}"
            parts.append(f"{result.name} ({result.outcome}, {code})")
        return (
            f"{len(failures)} of {len(self.results)} deterministic check(s) blocked: "
            + ", ".join(parts)
        )


def _bounded(text: str) -> str:
    """Redacted, capped, and honest about the cut."""
    from hivepilot.services.config_provenance import redact_text

    body = redact_text(text.strip())
    if len(body) <= _MAX_OUTPUT_CHARS:
        return body
    return (
        body[:_MAX_OUTPUT_CHARS]
        + f"\n\n[truncated: {len(body):,} characters, {_MAX_OUTPUT_CHARS:,} shown]"
    )


def _run_one(check: Check, *, cwd: Path, env: dict[str, str] | None) -> CheckResult:
    started = time.monotonic()

    def errored(detail: str, exit_code: int | None = None) -> CheckResult:
        # Every path that is not a clean exit 0 lands here or in "failed".
        # There is deliberately no branch that turns an unknown state into a
        # pass.
        logger.warning("verification.errored", check=check.name, detail=detail)
        return CheckResult(
            name=check.name,
            outcome="errored",
            exit_code=exit_code,
            output=_bounded(detail),
            duration_seconds=time.monotonic() - started,
        )

    if not check.command.strip():
        return errored("the check declares an empty command, so nothing was verified")

    try:
        completed = subprocess.run(  # noqa: S602 - operator-declared command, see module docstring
            ["bash", "-lc", check.command],
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            timeout=check.timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return errored(
            f"the check timed out after {check.timeout_seconds}s and was killed; "
            "nothing was verified"
        )
    except OSError as exc:
        return errored(f"the check could not be started: {exc}")
    except Exception as exc:  # noqa: BLE001 - reporting must not kill the run...
        # ...but it must not let the run pass either, which is why this is
        # "errored" and errored blocks.
        return errored(f"the check raised before producing a verdict: {exc}")

    output = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
    outcome = "passed" if completed.returncode == 0 else "failed"
    logger.info(
        "verification.checked",
        check=check.name,
        outcome=outcome,
        exit_code=completed.returncode,
    )
    return CheckResult(
        name=check.name,
        outcome=outcome,
        exit_code=completed.returncode,
        output=_bounded(output),
        duration_seconds=time.monotonic() - started,
    )


def run_checks(
    checks: list[Check],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> VerificationReport:
    """Run every check and report. Never raises.

    All of them run, even after one fails: stopping early would hide the rest,
    and the operator would fix one, pay for another whole pipeline, and
    discover the next.
    """
    if not checks:
        return VerificationReport(results=())
    results = [_run_one(check, cwd=cwd, env=env) for check in checks]
    report = VerificationReport(results=tuple(results))
    logger.info(
        "verification.report",
        blocking=report.blocking,
        checks=[shlex.quote(c.name) for c in checks],
    )
    return report
