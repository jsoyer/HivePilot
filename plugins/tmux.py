"""tmux runner plugin — executes each pipeline step *inside a dedicated,
deterministically-named tmux session*, enabling live attach/observe
(`tmux attach -t <session>`) and full scrollback capture, with a raw-shell
fallback when the `tmux` binary is absent.

Modeled directly on `plugins/rtk.py` (subprocess + `shutil.which` PATH check
+ graceful raw `bash -lc` fallback) and on `plugins/herdr.py` (execution-
wrapper multiplexer runner + the optional `capture()` method + the private
0600 temp-file env-overlay trick — see "Env/secrets -> session" below).

Execution model when `tmux` is on PATH (`run()`/`capture()`):
    1. `tmux new-session -d -s <name> -c <project_path>` — a detached
       session is created running an ordinary IDLE login shell (no command
       attached yet). Deliberately NOT "run the command directly as the
       session's process": a pane whose sole process exits gets torn down
       by tmux immediately, which raced with (and sometimes preceded) the
       `capture-pane` step below during real-tmux testing. Keeping an idle
       shell as the pane's process means the session only ever goes away
       when THIS runner explicitly kills it (step 5).
    2. `tmux send-keys -t <name> -l <wrapped-command>` (`-l` = literal, so
       the text is typed verbatim rather than tmux trying to interpret any
       of it as a key name like "Enter"/"C-c") followed by a separate
       `tmux send-keys -t <name> Enter` to submit it — mirrors typing a
       command into an interactive shell. The wrapped command always `cd`s
       into `payload.project.path` FIRST (matching the `cwd=` the raw
       fallback path passes to `subprocess.run` — a step must run in the
       same directory whether or not `tmux` happens to be on PATH), then,
       on completion, writes its exit code to a private temp file and
       signals a `tmux wait-for` channel so step 3 can block
       deterministically instead of polling.
    3. `tmux wait-for <channel>` — blocks (with a timeout) until the wrapped
       command's `tmux wait-for -S <channel>` signal fires. The pane's shell
       is still alive and waiting for its next command at this point.
    4. `tmux capture-pane -p -S - -t <name>` — captures the pane's FULL
       scrollback (`-S -` = from the start of history) as the step's
       result, while the session is guaranteed to still be alive (step 1).
    5. `tmux kill-session -t <name>` — always attempted in a `finally`, so a
       failed step never leaks a lingering tmux session.

Determinism (spec requirement): the session name derives ONLY from stable
identifiers already on `RunnerPayload` (`project_name`, `task_name`,
`step.name`) — no timestamp, UUID, or other randomness — so the SAME step
always maps to the SAME session name, which is what makes "attach to the
step's session while it's running" a predictable operation for an operator.
Each component is sanitized (`_sanitize`) because tmux session-name/target
syntax treats characters like `:` and `.` specially (`session:window.pane`)
and disallows some others outright.

Graceful degradation: if `tmux` is not installed, this runner logs a clear
INFO message (once per call — no spam) and falls back to executing the raw
command directly (no `tmux` involved) — it never crashes a run just because
the multiplexer isn't on the host.

Step-result contract (mirrors `plugins/herdr.py`): `hivepilot.orchestrator`'s
`_capture_or_execute()` does `capture = getattr(runner, "capture", None)`;
when a runner defines the OPTIONAL `capture(payload) -> str` method, the
orchestrator calls it and uses the returned string as the step's captured
output, which is later passed through `hivepilot.pipelines`'s
`redact_text()` — the SAME masking path every other runner's output flows
through. `TmuxRunner` implements BOTH: `run()` is required to satisfy
`BaseRunner` structurally and delegates to `capture()`; `capture()` is what
the orchestrator actually prefers, and is what surfaces the session's real
scrollback instead of discarding it. This runner introduces no new/unmasked
output sink — the captured pane text is returned exactly like `HerdrRunner`
returns pane text, so it hits `redact_text()` at the same call site.

Env/secrets -> session (SECURITY-SENSITIVE, mirrors `plugins/herdr.py`
exactly): a fresh tmux SERVER only inherits our subprocess's environment
when it's the one starting that server; if a tmux server is already running
(e.g. another concurrent session), a brand-new session's shell instead
inherits whatever environment the server captured when IT started — which
may be stale or absent for secrets resolved just for this step. Passing
`env=` on the `new-session` subprocess call would therefore be unreliable
AND would still leave secret VALUES sitting in the env of a subprocess this
runner itself spawns. Instead: the env/secrets overlay (`project.env` +
`definition.env` + `payload.secrets`) is written to a private (mode 0600)
temp file as `export KEY=<shlex.quote(value)>` lines, and the wrapped
command sourced inside the session is prefixed with
`set -a; source <path>; set +a; ` — only the file *path* (never a value)
ever appears on any argv, and it works identically regardless of whether
tmux's server was already running. The file is removed immediately after
the session finishes (in a `finally`), and every KEY (not just each value)
is validated against `_ENV_KEY_RE` before anything is written — keys come
from operator config, not untrusted input, but an unvalidated key could
still inject directly into the `export <key>=...` line regardless of how
well the value is quoted.

Status mapping (fail-closed, mirrors `plugins/herdr.py`'s `_wait_idle`):
the wrapped command's own exit code — read back from the private exit-marker
file after `wait-for` unblocks — is the ONLY thing that decides success. A
non-zero exit raises a clear `RuntimeError` naming the step and session; a
`wait-for` timeout (the command never signaled completion) ALSO raises,
distinctly, rather than silently treating "we gave up waiting" as success.
`kill-session` runs in a `finally` in both cases, so a failed/timed-out step
never leaves a lingering session behind.

Deliberately NOT a `@dataclass`: local-file plugins are loaded via
`importlib.util.spec_from_file_location()` / `exec_module()`
(`hivepilot.plugins._scan_local_plugins`), which never registers the module
in `sys.modules`. Combined with `from __future__ import annotations`, that
trips a real CPython 3.14 `dataclasses` bug (`_is_type` does
`sys.modules[cls.__module__].__dict__`, which is `None` for an unregistered
module) — a plain class with an explicit `__init__` sidesteps it entirely
(see `plugins/rtk.py`).
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from hivepilot.config import Settings
from hivepilot.models import RunnerDefinition
from hivepilot.plugins import HealthStatus
from hivepilot.runners.base import RunnerPayload
from hivepilot.templates import render_template
from hivepilot.utils.env import gather_overrides, merge_environments
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

# tmux session-name/target syntax treats `:`/`.` specially (`session:window.pane`)
# and rejects some other characters outright — collapse anything outside this
# safe set to `-` rather than trying to enumerate every disallowed character.
_SESSION_NAME_RE = re.compile(r"[^A-Za-z0-9_-]+")
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# No corresponding Settings field per this sprint's file boundaries (only
# `tmux_enabled` was added to config.py) — a per-step override is still
# available via `step.metadata["tmux_wait_timeout_seconds"]`.
_DEFAULT_WAIT_TIMEOUT_SECONDS = 1800


def _sanitize(component: str) -> str:
    cleaned = _SESSION_NAME_RE.sub("-", component).strip("-")
    return cleaned or "x"


class TmuxRunner:
    """Runner that drives a tmux session (new-session -> wait-for ->
    capture-pane -> kill-session) to execute a step's command with live
    attach/observe visibility."""

    def __init__(self, definition: RunnerDefinition, settings: Settings) -> None:
        self.definition = definition
        self.settings = settings

    def run(self, payload: RunnerPayload) -> None:
        self.capture(payload)

    def capture(self, payload: RunnerPayload) -> str:
        """Execute the step and return its captured output — see the
        "Step-result contract" note in the module docstring for why this is
        the method the orchestrator actually prefers over `run()`."""
        command_str = self._render_command(payload)
        tmux_path = shutil.which("tmux")

        if not tmux_path:
            return self._run_fallback(payload, command_str)
        return self._run_in_session(payload, command_str)

    # -- fallback (tmux not on PATH) ---------------------------------------

    def _run_fallback(self, payload: RunnerPayload, command_str: str) -> str:
        env = merge_environments(payload.project.env, self.definition.env, payload.secrets)
        logger.info(
            "tmux_runner.tmux_not_found",
            project=payload.project_name,
            step=payload.step.name,
            detail="tmux not found on PATH — falling back to raw command execution",
        )
        result = subprocess.run(
            ["bash", "-lc", command_str],
            cwd=str(payload.project.path),
            env=env,
            check=False,
            text=True,
            capture_output=True,
        )
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "").strip()[-2000:]
            raise RuntimeError(
                f"tmux runner '{self.definition.name}' step '{payload.step.name}' "
                f"(raw fallback, tmux not on PATH) exited {result.returncode}: {err}"
            )
        return result.stdout

    # -- session naming -----------------------------------------------------

    def _session_name(self, payload: RunnerPayload) -> str:
        """Deterministic session name derived ONLY from stable identifiers —
        no timestamp/UUID/random — so the same step always maps to the same
        session name. See the "Determinism" note in the module docstring."""
        parts = [
            _sanitize(payload.project_name),
            _sanitize(payload.task_name),
            _sanitize(payload.step.name),
        ]
        return ("hivepilot-" + "-".join(parts))[:200]

    # -- session-driven execution --------------------------------------------

    def _run_in_session(self, payload: RunnerPayload, command_str: str) -> str:
        session_name = self._session_name(payload)
        logger.info(
            "tmux_runner.start",
            project=payload.project_name,
            step=payload.step.name,
            session=session_name,
        )
        env_file = self._write_env_file(payload)
        fd, exit_file = tempfile.mkstemp(prefix="hivepilot-tmux-exit-", suffix=".txt")
        os.close(fd)
        wait_channel = f"{session_name}-done"
        try:
            project_path = shlex.quote(str(payload.project.path))
            wrapped_cmd = (
                f"cd {project_path}; set -a; source {shlex.quote(env_file)}; set +a; "
                f"{command_str}; echo $? > {shlex.quote(exit_file)}; "
                f"tmux wait-for -S {shlex.quote(wait_channel)}"
            )
            self._new_session(payload, session_name)
            self._send_command(payload, session_name, wrapped_cmd)
            self._wait_for_completion(payload, session_name, wait_channel)
            output = self._capture_pane(payload, session_name)
            exit_code = self._read_exit_code(payload, exit_file)
        finally:
            self._kill_session(session_name)
            self._cleanup_env_file(env_file)
            self._cleanup_env_file(exit_file)
        logger.info(
            "tmux_runner.end",
            project=payload.project_name,
            step=payload.step.name,
            session=session_name,
        )
        if exit_code != 0:
            err = output.strip()[-2000:]
            raise RuntimeError(
                f"tmux runner step '{payload.step.name}' (session {session_name}) "
                f"exited {exit_code}: {err}"
            )
        return output

    def _new_session(self, payload: RunnerPayload, session_name: str) -> None:
        """Create a detached session running an IDLE login shell (no
        command attached) — see the "Execution model" note in the module
        docstring for why the command is `send-keys`'d afterward instead of
        being attached directly to `new-session`."""
        argv = [
            "tmux",
            "new-session",
            "-d",
            "-s",
            session_name,
            "-c",
            str(payload.project.path),
        ]
        result = subprocess.run(argv, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "").strip()[-2000:]
            raise RuntimeError(
                f"tmux runner step '{payload.step.name}': failed to create session "
                f"'{session_name}' (exit {result.returncode}): {err}"
            )

    def _send_command(self, payload: RunnerPayload, session_name: str, wrapped_cmd: str) -> None:
        """Type the wrapped command into the idle pane (`-l` = literal, so
        tmux never tries to interpret the text as a key name) and submit it
        with a separate `Enter` keypress — mirrors typing into an
        interactive shell rather than attaching the command as the pane's
        own process (see the "Execution model" docstring note)."""
        literal_argv = ["tmux", "send-keys", "-t", session_name, "-l", wrapped_cmd]
        result = subprocess.run(literal_argv, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "").strip()[-2000:]
            raise RuntimeError(
                f"tmux runner step '{payload.step.name}': failed to send command to "
                f"session '{session_name}' (exit {result.returncode}): {err}"
            )
        enter_argv = ["tmux", "send-keys", "-t", session_name, "Enter"]
        enter_result = subprocess.run(enter_argv, capture_output=True, text=True, check=False)
        if enter_result.returncode != 0:
            err = (enter_result.stderr or enter_result.stdout or "").strip()[-2000:]
            raise RuntimeError(
                f"tmux runner step '{payload.step.name}': failed to submit command to "
                f"session '{session_name}' (exit {enter_result.returncode}): {err}"
            )

    def _wait_for_completion(
        self, payload: RunnerPayload, session_name: str, wait_channel: str
    ) -> None:
        timeout = payload.step.metadata.get("tmux_wait_timeout_seconds") or (
            _DEFAULT_WAIT_TIMEOUT_SECONDS
        )
        argv = ["tmux", "wait-for", wait_channel]
        try:
            result = subprocess.run(
                argv, capture_output=True, text=True, check=False, timeout=timeout
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"tmux runner step '{payload.step.name}': session '{session_name}' timed "
                f"out after {timeout}s waiting for completion"
            ) from exc
        if result.returncode != 0:
            # Fail-closed: a non-zero wait-for is treated as a step failure,
            # not silently ignored.
            err = (result.stderr or result.stdout or "").strip()[-2000:]
            raise RuntimeError(
                f"tmux runner step '{payload.step.name}': wait-for '{wait_channel}' failed "
                f"on session '{session_name}' (exit {result.returncode}): {err}"
            )

    def _capture_pane(self, payload: RunnerPayload, session_name: str) -> str:
        argv = ["tmux", "capture-pane", "-p", "-S", "-", "-t", session_name]
        result = subprocess.run(argv, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "").strip()[-2000:]
            raise RuntimeError(
                f"tmux runner step '{payload.step.name}': capture-pane failed on session "
                f"'{session_name}' (exit {result.returncode}): {err}"
            )
        return result.stdout

    def _read_exit_code(self, payload: RunnerPayload, exit_file: str) -> int:
        try:
            raw = Path(exit_file).read_text().strip()
            return int(raw)
        except (OSError, ValueError) as exc:
            raise RuntimeError(
                f"tmux runner step '{payload.step.name}': could not determine the "
                f"command's exit status (missing/invalid exit marker file)"
            ) from exc

    @staticmethod
    def _kill_session(session_name: str) -> None:
        # Best-effort, always attempted (called from `finally`) — a step that
        # already failed must not ALSO leak a lingering tmux session.
        subprocess.run(
            ["tmux", "kill-session", "-t", session_name],
            capture_output=True,
            text=True,
            check=False,
        )

    # -- env -> session (argv-safe, mirrors plugins/herdr.py) ----------------

    def _write_env_file(self, payload: RunnerPayload) -> str:
        """Write the step's env/secrets overlay to a private (0600) temp file
        as `export KEY=value` lines. Only the *path* to this file is ever
        embedded in the command sent to `tmux new-session` — never a value —
        see the "Env/secrets -> session" note in the module docstring.

        Keys come from operator config (project.env / definition.env /
        payload.secrets), not untrusted input, but are validated against
        `_ENV_KEY_RE` anyway before anything is written: an unvalidated key
        (e.g. `X;evil`) would inject directly into the `export <key>=...`
        line regardless of how well the VALUE is `shlex.quote`d. Fail
        closed — refuse to write ANY of the overlay rather than silently
        drop the offending var and risk a step running with a missing
        secret it doesn't notice."""
        overlay = gather_overrides(payload.project.env, self.definition.env, payload.secrets)
        for key in overlay:
            if not _ENV_KEY_RE.match(key):
                raise RuntimeError(
                    f"tmux runner step '{payload.step.name}': refusing to write env "
                    f"var with invalid name {key!r} to the session env file (must "
                    f"match {_ENV_KEY_RE.pattern!r}) — fail-closed against injection "
                    f"via the sourced env file"
                )
        fd, path = tempfile.mkstemp(prefix="hivepilot-tmux-env-", suffix=".sh")
        os.chmod(path, 0o600)
        try:
            with os.fdopen(fd, "w") as fh:
                for key, value in overlay.items():
                    fh.write(f"export {key}={shlex.quote(str(value))}\n")
        except Exception:
            os.remove(path)
            raise
        return path

    @staticmethod
    def _cleanup_env_file(path: str) -> None:
        """Best-effort removal of a private temp file (env-overlay OR
        exit-marker — both share the same cleanup semantics)."""
        try:
            os.remove(path)
        except OSError:
            pass

    # -- command rendering ----------------------------------------------------

    def _render_command(self, payload: RunnerPayload) -> str:
        template = payload.step.command or self.definition.command
        if not template:
            raise ValueError(f"tmux runner '{self.definition.name}' missing command")

        context: dict[str, Any] = {
            "project_name": payload.project_name,
            "project_path": str(payload.project.path),
            "project_description": payload.project.description or "",
            "project_default_branch": payload.project.default_branch,
            "project_owner_repo": payload.project.owner_repo or "",
            "task_name": payload.task_name,
            "step_name": payload.step.name,
            "extra_prompt": payload.metadata.get("extra_prompt", ""),
        }
        return render_template(template, context)


def health(**kwargs: Any) -> HealthStatus:
    """`ok` when `tmux` is on PATH; `degraded` when it isn't — `TmuxRunner`
    already falls back to raw `bash -lc` execution in that case, so a missing
    `tmux` binary degrades attach/observe visibility rather than breaking
    runs. Never raises: any internal error is reported as the exception TYPE
    name only (never a message/value), matching `PluginManager.run_health_check`.
    Never leaks command contents or secrets — only presence/absence of the
    binary.
    """
    try:
        if shutil.which("tmux"):
            return HealthStatus("ok", "tmux on PATH")
        return HealthStatus("degraded", "tmux not found; using shell fallback")
    except Exception as exc:  # noqa: BLE001 — a health check must never crash
        return HealthStatus("error", type(exc).__name__)


def register() -> dict[str, Any]:
    from hivepilot.config import settings

    if not settings.tmux_enabled:
        return {}
    return {"runners": {"tmux": TmuxRunner}, "health": {"tmux": health}}
