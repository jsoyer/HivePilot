"""Execute one agent invocation inside a herdr pane, as if it were a subprocess.

The contract is deliberately narrow: give it the argv the runner was about to
spawn and get back a `subprocess.CompletedProcess`. Everything downstream in
`ClaudeRunner.capture()` -- the usage envelope, the cost, permission-denial
reporting, the failure context and its hints -- is built on `.returncode`,
`.stdout` and `.stderr`, is well tested, and must not learn that a pane exists.

The engine cannot import `plugins/herdr.py` to get this: plugins are not in the
wheel and are loaded dynamically at runtime, so a runner depending on one would
break on every box where the plugin is stale or absent. The overlap in herdr
argv shapes between the two is duplication accepted on purpose -- and the two
hard-won argument orders below are commented where they are used, not left for
the reader to rediscover.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any, Callable, Protocol

import structlog

from hivepilot.runners.pane_capture import (
    CapturePaths,
    build_pane_command,
    capture_paths,
    completion_sentinel,
    write_env_file,
)

logger = structlog.get_logger(__name__)

#: Used when the step declares no timeout. herdr's own help says an omitted
#: `--timeout` waits indefinitely, which would hang the scheduler on a pane
#: nobody is watching -- so a bound is always sent, even a generous one.
_DEFAULT_TIMEOUT_S = 3600

#: Returned when the step's own status could never be read. Outside the range a
#: process can exit with (POSIX allows 0-255), so "we lost the step" is never
#: confused with "the step failed this way".
#:
#: POSITIVE on purpose. A NEGATIVE exit code means "killed by signal N" by
#: convention, and `_runner_failure_context` reads it that way -- with -1024 it
#: reported `signal: SIG1024` and told the operator "an INFRASTRUCTURE failure
#: (the OS terminated it)". Every word of that was invented by the sentinel.
#: The real reason is in `stderr`, stated plainly, where it can be read.
_STATUS_UNKNOWN = 1024

#: Keys herdr has been observed to name a pane with.
_PANE_ID_KEYS = ("pane_id", "paneId", "id")


class PaneExecutionError(RuntimeError):
    """The pane itself failed -- split, dispatch or wait.

    Distinct from the agent failing: an agent that exits non-zero comes back as
    a `CompletedProcess` with that status, so the runner reports it with its
    usual context. This is raised only when we could not run the agent at all,
    which is an operator problem with the herdr server, not with the step.
    """


class _RunCli(Protocol):
    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess: ...


def _default_run_cli(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, check=False, **kwargs)


def run_in_pane(
    argv: list[str],
    *,
    capture_dir: str,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    split_direction: str = "right",
    target_pane: str | None = None,
    run_cli: Callable[..., subprocess.CompletedProcess] | None = None,
) -> subprocess.CompletedProcess:
    """Run `argv` in a fresh pane and return what a subprocess would have.

    `run_cli` is injected so the whole sequence is testable without a herdr
    server; nothing else about the flow changes between the two.
    """
    cli = run_cli or _default_run_cli
    paths = capture_paths(capture_dir)
    sentinel = completion_sentinel()
    env_file: str | None = None

    if env:
        env_file = f"{capture_dir.rstrip('/')}/{sentinel}.env.sh"
        write_env_file(env_file, env)

    try:
        pane_id = _split_pane(cli, split_direction, target_pane)
        command = build_pane_command(
            argv=argv, paths=paths, sentinel=sentinel, cwd=cwd, env_file=env_file
        )
        _pane_run(cli, pane_id, command)
        try:
            _wait_for_sentinel(cli, pane_id, sentinel, timeout)
        finally:
            _close_pane(cli, pane_id)
        return _collect(paths, argv)
    finally:
        # Before the capture files, and in a `finally`: this one holds the
        # step's credentials in cleartext, and a raised split or wait must not
        # leave it behind.
        _unlink(env_file)
        _unlink(paths.stdout)
        _unlink(paths.stderr)
        _unlink(paths.rc)


def _split_pane(
    cli: Callable[..., subprocess.CompletedProcess],
    direction: str,
    target_pane: str | None = None,
) -> str:
    """Open a pane, in a NAMED workspace when one was given.

    `--current` means *whatever workspace the operator is looking at*. With a
    single workspace that is indistinguishable from correct; with one per run
    it puts a step in another run's workspace, and merely focusing a different
    tab moves where agents appear.

    `--pane <ID>` was measured against TWO workspaces -- the only arrangement
    in which the right answer and `--current` differ -- and lands in the target
    workspace while the operator is focused elsewhere.

    `--current` remains the fallback so a run without a workspace of its own
    keeps working, rather than making worktrees all-or-nothing.
    """
    where = ["--pane", target_pane] if target_pane else ["--current"]
    result = cli(["herdr", "pane", "split", *where, "--direction", direction, "--no-focus"])
    if result.returncode != 0:
        raise PaneExecutionError(
            f"herdr pane split failed (exit {result.returncode}): "
            f"{_tail(result.stderr or result.stdout)}"
        )
    pane_id = _extract_pane_id(_loads(result.stdout))
    if not pane_id:
        # Never fall back to a default or an empty id: the command would
        # dispatch the agent somewhere nobody can watch, which is the one thing
        # this whole path exists to prevent.
        raise PaneExecutionError(f"herdr pane split named no pane: {_tail(result.stdout)}")
    return pane_id


def _pane_run(cli: Callable[..., subprocess.CompletedProcess], pane_id: str, command: str) -> None:
    result = cli(["herdr", "pane", "run", pane_id, command])
    if result.returncode != 0:
        raise PaneExecutionError(
            f"herdr pane run failed on {pane_id} (exit {result.returncode}): "
            f"{_tail(result.stderr or result.stdout)}"
        )


def _wait_for_sentinel(
    cli: Callable[..., subprocess.CompletedProcess],
    pane_id: str,
    sentinel: str,
    timeout: float | None,
) -> None:
    timeout_ms = int((timeout or _DEFAULT_TIMEOUT_S) * 1000)
    # PANE ID FIRST, despite the help printing `[OPTIONS] <--match <TEXT>>
    # <PANE_ID>`. Probed on the box against 0.8.0: with the id last, every form
    # answers `unknown option: <the VALUE>` -- the parser consumes the flag and
    # then treats its argument as another option. `--match` is a literal
    # substring; `--regex` would turn a marker into a pattern.
    result = cli(
        [
            "herdr",
            "pane",
            "wait-output",
            pane_id,
            "--match",
            sentinel,
            "--timeout",
            str(timeout_ms),
        ]
    )
    if result.returncode != 0:
        raise PaneExecutionError(
            f"pane {pane_id} never printed its completion marker within "
            f"{timeout_ms}ms (exit {result.returncode}): "
            f"{_tail(result.stderr or result.stdout)}"
        )


def _close_pane(cli: Callable[..., subprocess.CompletedProcess], pane_id: str) -> None:
    """Best effort. A pane left open is a leak; a failure to close one is not
    worth losing a completed step's output over, so this never raises.

    `close`, not `kill`: 0.8.0 has no `kill` verb -- probed. The test that
    covers this once accepted either, and so proved nothing about which one
    herdr actually understands.
    """
    try:
        result = cli(["herdr", "pane", "close", pane_id])
    except OSError as exc:  # pragma: no cover - defensive
        logger.warning("pane_exec.close_failed", pane=pane_id, error=str(exc))
        return
    if result.returncode != 0:
        logger.warning(
            "pane_exec.close_failed",
            pane=pane_id,
            exit_code=result.returncode,
            detail=_tail(result.stderr or result.stdout),
        )


def _collect(paths: CapturePaths, argv: list[str]) -> subprocess.CompletedProcess:
    stdout = _read(paths.stdout)
    stderr = _read(paths.stderr)
    status = _read_status(paths.rc)

    if status is None:
        # The shell never reached the end of its line: the pane was killed, the
        # server dropped it, the box rebooted. Reporting 0 would turn every
        # lost step into a clean success with empty output -- so this says so,
        # in `stderr`, where the runner's failure context already looks.
        #
        # `stdout` is kept: the agent may have finished and spent real money
        # before the pane was lost, and the runner's failure path reads cost
        # out of the envelope.
        stderr = (
            f"hivepilot: the step's exit status could not be read back from "
            f"{paths.rc} -- the pane did not run to completion.\n{stderr}"
        )
        return subprocess.CompletedProcess(argv, _STATUS_UNKNOWN, stdout, stderr)

    return subprocess.CompletedProcess(argv, status, stdout, stderr)


def _read_status(path: str) -> int | None:
    raw = _read(path).strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""


def _unlink(path: str | None) -> None:
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass


def _loads(raw: str) -> Any:
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw.strip()


def _extract_pane_id(data: Any) -> str | None:
    """Find a pane id, searching `result` BEFORE the top level.

    herdr wraps its answer in `result`, and the envelope carries an `id` of its
    own -- the REQUEST's. Reading the top level first returns that request id,
    which is accepted as a pane id and then addresses nothing.
    """
    if isinstance(data, str):
        return data or None
    if not isinstance(data, dict):
        return None

    result = data.get("result")
    if isinstance(result, dict):
        pane = result.get("pane")
        if isinstance(pane, dict):
            found = _extract_pane_id(pane)
            if found:
                return found
        for key in _PANE_ID_KEYS:
            value = result.get(key)
            if isinstance(value, str) and value:
                return value
        return None

    for key in _PANE_ID_KEYS:
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    pane = data.get("pane")
    if isinstance(pane, dict):
        return _extract_pane_id(pane)
    return None


def _tail(text: str | None) -> str:
    return (text or "").strip()[-2000:]
