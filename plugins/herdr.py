"""herdr runner plugin — executes each pipeline step *inside a dedicated
herdr pane* by driving the `herdr` CLI (github.com/ogulcancelik/herdr), a
terminal multiplexer built for coding agents (tmux-like but agent-aware:
workspaces -> tabs -> panes, agent-status detection, opaque hierarchy ids).

Modeled directly on `plugins/rtk.py` (subprocess + `shutil.which` PATH check
+ graceful raw `bash -lc` fallback) and on `hivepilot.runners.claude_runner`
/ `hivepilot.runners.prompt_cli_runner` for the optional `capture()` method
(see "Step-result contract" below).

Flow when `herdr` is on PATH (`run()`/`capture()`):
    1. `herdr pane split --current --direction <dir> --no-focus` -> parse
       the pane id **from the JSON stdout** (`json.loads`, never hand-built
       — hierarchy ids are opaque per the herdr SKILL.md).
    2. `herdr pane run <pane-id> <wrapped-command>` — a single argv element,
       no `shell=True` (same injection posture as `ShellRunner`'s
       `["bash", "-lc", command_str]`). The wrapped command always `cd`s
       into `payload.project.path` FIRST, matching the `cwd=` the raw
       fallback path passes to `subprocess.run` — a step must run in the
       same directory whether or not `herdr` happens to be on PATH.
    3. `herdr wait agent-status <pane-id> --status idle --timeout <ms>`.
    4. `herdr pane read <pane-id> --source recent-unwrapped --lines <n>` —
       the pane's captured output IS the step's result.

Graceful degradation: if `herdr` is not installed, this runner logs an INFO
message and falls back to executing the raw command directly (no `herdr`
involved) — it never crashes a run just because herdr isn't on the host.

Step-result contract (resolves spec §4.4): `hivepilot.orchestrator`'s
`_capture_or_execute()` does `capture = getattr(runner, "capture", None)`;
when a runner defines the OPTIONAL `capture(payload) -> str` method, the
orchestrator calls it and uses the returned string as the step's captured
output (interaction log / live stream / `task_result`, later passed through
`hivepilot.pipelines`'s `redact_text()`). Only when `capture` is absent does
it fall back to `run(payload) -> None` (success = no exception raised) with
an empty output. `HerdrRunner` implements BOTH: `run()` is required to
satisfy `BaseRunner` structurally, and delegates to `capture()` so a step
still executes correctly even if some future caller only calls `run()`;
`capture()` is what the orchestrator actually prefers, and is what surfaces
the pane's real output instead of discarding it.

Env/secrets -> pane (resolves spec §4.5, SECURITY-SENSITIVE): `herdr pane
run <id> <cmd>` executes in the PANE'S OWN shell (spawned earlier by `pane
split`), which does NOT automatically inherit the env overlay this runner
computes (`project.env` + `definition.env` + `payload.secrets`) — that
overlay only applies to a subprocess WE spawn directly (the `herdr` CLI
invocations themselves), not to the shell living inside the pane. Piping
the overlay onto the command line as `env VAR=secret ...` would put the
secret VALUE on the argv of our own `herdr pane run` subprocess for the
lifetime of that process — `ps`/`/proc/<pid>/cmdline` visible to any other
local user/process. Instead: the overlay is written to a private (mode
0600) temp file as `export KEY=<shlex.quote(value)>` lines, and the command
sent to the pane is prefixed with `set -a; source <path>; set +a; `. Only
the file *path* (never a value) ever appears on any argv. The file is
removed immediately after the pane finishes the step (in a `finally`), and
is scoped to ONLY the intentional overlay (`gather_overrides`, NOT the full
`os.environ`) so nothing beyond what the step actually needs is written to
disk. Every KEY (not just each value) is validated against `_ENV_KEY_RE`
before anything is written — keys come from operator config, not untrusted
input, but an unvalidated key could still inject directly into the
`export <key>=...` line regardless of how well the value is quoted. This
does not defeat the Phase 19 masking layer
(`config_provenance.redact_text`): every resolved secret value is already
registered for masking by the orchestrator before this runner ever sees it
(`orchestrator._resolve_secrets` -> `register_secret_value`), and the pane's
captured output still flows through the same `redact_text()` call site in
`hivepilot.pipelines` as any other runner's output — this runner adds no
new unmasked path.

Status mapping (resolves spec §6, fail-closed): `herdr wait agent-status
--status idle --timeout <ms>` is the ONLY state this runner treats as
success. Any non-zero exit from that command — whether the pane went
`blocked`, stayed `unknown`, or the wait simply timed out — raises a clear
`RuntimeError` naming the step and pane id. There is no code path that
treats a non-idle/timed-out wait as success.

HERDR_ENV reuse (resolves spec's 4th open question): per spec §4.2 default,
this runner ALWAYS splits a fresh pane per step — it does not special-case
`HERDR_ENV=1` (already-inside-herdr) to reuse the current pane. Reusing the
invoking pane would mix multiple steps' output in one pane's scrollback and
complicate `pane read`'s per-step capture; always-split keeps each step's
output cleanly isolated, which is what `capture()` needs to return an
accurate per-step result. Deferred as a possible future enhancement.

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

import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from typing import Any

from hivepilot.config import Settings
from hivepilot.models import RunnerDefinition
from hivepilot.plugins import HealthStatus
from hivepilot.runners.base import RunnerPayload
from hivepilot.templates import render_template
from hivepilot.utils.env import gather_overrides, merge_environments
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

_PANE_ID_KEYS = ("id", "pane_id", "paneId")
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class HerdrRunner:
    """Runner that drives a herdr pane (split -> run -> wait idle -> read)
    to execute a step's command with live parallel-pane visibility."""

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
        herdr_path = shutil.which("herdr")

        if not herdr_path:
            return self._run_fallback(payload, command_str)
        return self._run_in_pane(payload, command_str)

    # -- fallback (herdr not on PATH) ------------------------------------

    def _run_fallback(self, payload: RunnerPayload, command_str: str) -> str:
        env = merge_environments(payload.project.env, self.definition.env, payload.secrets)
        logger.info(
            "herdr_runner.herdr_not_found",
            project=payload.project_name,
            step=payload.step.name,
            detail="herdr not found on PATH — falling back to raw command execution",
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
                f"herdr runner '{self.definition.name}' step '{payload.step.name}' "
                f"(raw fallback, herdr not on PATH) exited {result.returncode}: {err}"
            )
        return result.stdout

    # -- pane-driven execution --------------------------------------------

    def _run_in_pane(self, payload: RunnerPayload, command_str: str) -> str:
        logger.info(
            "herdr_runner.start",
            project=payload.project_name,
            step=payload.step.name,
        )
        pane_id = self._split_pane(payload)
        env_file = self._write_env_file(payload)
        try:
            project_path = shlex.quote(str(payload.project.path))
            sentinel = self._completion_sentinel()
            inner = self._wrap_with_sentinel(command_str, sentinel)
            wrapped_cmd = (
                f"cd {project_path}; set -a; source {shlex.quote(env_file)}; set +a; {inner}"
            )
            self._pane_run(payload, pane_id, wrapped_cmd)
            self._wait_idle(payload, pane_id, sentinel)
            output = self._pane_read(payload, pane_id)
        finally:
            self._cleanup_env_file(env_file)
        logger.info(
            "herdr_runner.end",
            project=payload.project_name,
            step=payload.step.name,
            pane_id=pane_id,
        )
        return output

    def _split_pane(self, payload: RunnerPayload) -> str:
        argv = [
            "herdr",
            "pane",
            "split",
            "--current",
            "--direction",
            self.settings.herdr_split_direction,
            "--no-focus",
        ]
        result = subprocess.run(argv, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "").strip()[-2000:]
            raise RuntimeError(
                f"herdr runner step '{payload.step.name}': pane split failed "
                f"(exit {result.returncode}): {err}"
            )
        return self._parse_pane_id(payload, result.stdout, context="pane split")

    def _pane_run(self, payload: RunnerPayload, pane_id: str, wrapped_cmd: str) -> None:
        argv = ["herdr", "pane", "run", pane_id, wrapped_cmd]
        result = subprocess.run(argv, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "").strip()[-2000:]
            raise RuntimeError(
                f"herdr runner step '{payload.step.name}': pane run failed on "
                f"pane {pane_id} (exit {result.returncode}): {err}"
            )

    @staticmethod
    def _completion_sentinel() -> str:
        """A unique end-of-command marker.

        Unique per call on purpose: a second step in the same pane would
        otherwise match the FIRST one's marker and read output that is not its
        own. No shell metacharacters -- it is interpolated into a command and
        passed to `--match`.
        """
        import uuid

        return f"HIVEPILOT_DONE_{uuid.uuid4().hex}"

    @staticmethod
    def _wrap_with_sentinel(command_str: str, sentinel: str) -> str:
        """Run the command, then announce completion UNCONDITIONALLY.

        `;` and not `&&`: a failing command must still print the marker.
        Otherwise it hangs to the timeout and the operator is told "timed out"
        about something that finished in a second -- a wrong diagnosis, not
        merely a slow one.

        `$?` is captured before the echo and re-raised after it, so the step's
        own exit status is not replaced by the echo's.

        And the sentinel is never written WHOLE into the command. `pane run`
        TYPES it into the shell, which echoes it, so a literal marker is on
        screen the instant the step is dispatched -- `wait-output --match` then
        matched that echo and returned immediately, and every step here has
        been read back before it finished. Measured on the box against 0.8.0,
        with a command sleeping 6s before printing its marker:

            marker written literally into the command : wait returned in 0.0s
            marker assembled by the shell at runtime  : wait returned in 6.0s

        The halves sit in one echoed line but with `; __hp_s2=` between them,
        so the concatenation exists nowhere until the shell performs it.
        """
        cut = max(1, len(sentinel) - 8)
        head, tail = sentinel[:cut], sentinel[cut:]
        return (
            f"__hp_s1={head}; __hp_s2={tail}; {command_str}; __hp_rc=$?; "
            f'echo "$__hp_s1$__hp_s2"; (exit $__hp_rc)'
        )

    @staticmethod
    def _wait_argv(pane_id: str, sentinel: str, timeout_ms: int) -> list[str]:
        """`pane wait-output`, which is what 0.8.0 actually has.

        NOT `herdr wait agent-status`: that command exists in neither 0.7.5 nor
        0.8.0 -- probed on both -- and the plugin had been calling it since
        #140, so this runner had never completed a step against a real server.

        NOT `agent wait` either: it needs an AGENT target, and a pane running a
        shell command is not one (`agent_not_found`, probed). We are waiting
        for a COMMAND to finish, not for an agent to stop thinking.

        `--match` is a literal substring; `--regex` would turn a marker into a
        pattern. `--timeout` is never omitted -- herdr's own help says it
        otherwise waits indefinitely.
        """
        # PANE_ID FIRST, despite the help printing
        # `[OPTIONS] <--match <TEXT>> <PANE_ID>`. Probed on the box against
        # 0.8.0: with the id last, every form answers `unknown option: <the
        # VALUE>` -- the parser consumes the flag and then treats its argument
        # as another option. With the id first it answers a proper
        # `{"code":"timeout"}`. The documented order does not work; this one
        # does.
        return [
            "herdr",
            "pane",
            "wait-output",
            pane_id,
            "--match",
            sentinel,
            "--timeout",
            str(timeout_ms),
        ]

    def _wait_idle(self, payload: RunnerPayload, pane_id: str, sentinel: str) -> None:
        timeout_ms = payload.step.metadata.get("herdr_wait_timeout_ms") or (
            self.settings.herdr_wait_timeout_ms
        )
        argv = self._wait_argv(pane_id, sentinel, timeout_ms)
        result = subprocess.run(argv, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            # Fail-closed: blocked / unknown / timed-out are ALL treated as a
            # step failure — there is no path here that silently succeeds.
            err = (result.stderr or result.stdout or "").strip()[-2000:]
            raise RuntimeError(
                f"herdr runner step '{payload.step.name}': pane {pane_id} never printed "
                f"its completion marker within {timeout_ms}ms "
                f"(exit {result.returncode}): {err}"
            )

    def _pane_read(self, payload: RunnerPayload, pane_id: str) -> str:
        read_lines = payload.step.metadata.get("herdr_read_lines") or self.settings.herdr_read_lines
        argv = [
            "herdr",
            "pane",
            "read",
            pane_id,
            "--source",
            "recent-unwrapped",
            "--lines",
            str(read_lines),
        ]
        result = subprocess.run(argv, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "").strip()[-2000:]
            raise RuntimeError(
                f"herdr runner step '{payload.step.name}': pane read failed on "
                f"pane {pane_id} (exit {result.returncode}): {err}"
            )
        return result.stdout

    def _parse_pane_id(self, payload: RunnerPayload, raw_stdout: str, *, context: str) -> str:
        try:
            data = json.loads(raw_stdout)
        except (json.JSONDecodeError, TypeError) as exc:
            raise RuntimeError(
                f"herdr runner step '{payload.step.name}': {context} returned "
                f"malformed JSON — cannot parse a pane id (fail-closed, refusing "
                f"to construct one manually): {raw_stdout.strip()[:500]!r}"
            ) from exc
        pane_id = self._extract_pane_id(data)
        if not pane_id:
            raise RuntimeError(
                f"herdr runner step '{payload.step.name}': {context} JSON did not "
                f"contain a recognizable pane id field {_PANE_ID_KEYS}: "
                f"{raw_stdout.strip()[:500]!r}"
            )
        return pane_id

    @staticmethod
    def _extract_pane_id(data: Any) -> str | None:
        """Find a pane id, searching `result` BEFORE the top level.

        herdr answers in a JSON-RPC envelope whose top-level `id` is the
        REQUEST id -- measured against 0.7.5 on 2026-08-18::

            {"id": "cli:pane:split",
             "result": {"type": "pane_split", "pane": {"pane_id": ...}}}

        `_PANE_ID_KEYS` starts with `"id"`, so searching the top level first
        returned `"cli:pane:split"` and the next call failed with
        `pane_not_found` -- reading as though herdr had lost the pane it had
        just created. Only a live server shows this.

        The top-level fallback stays: an older or future protocol may answer
        with a bare id, and this is also called on nested `pane` objects. It is
        simply no longer consulted first. When `result` exists but holds
        nothing recognisable, the envelope id is NOT used as a consolation
        prize -- returning None makes the caller raise with the raw JSON, and a
        parse failure must not become a confident call against a pane that does
        not exist.
        """
        if isinstance(data, str):
            return data or None
        if not isinstance(data, dict):
            return None

        result = data.get("result")
        if isinstance(result, dict):
            pane = result.get("pane")
            if isinstance(pane, dict):
                found = HerdrRunner._extract_pane_id(pane)
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
            return HerdrRunner._extract_pane_id(pane)
        return None

    # -- env -> pane (argv-safe) -------------------------------------------

    def _write_env_file(self, payload: RunnerPayload) -> str:
        """Write the step's env/secrets overlay to a private (0600) temp file
        as `export KEY=value` lines. Only the *path* to this file is ever
        embedded in a command sent to `herdr pane run` — never a value —
        see the "Env/secrets -> pane" note in the module docstring.

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
                    f"herdr runner step '{payload.step.name}': refusing to write "
                    f"env var with invalid name {key!r} to the pane env file "
                    f"(must match {_ENV_KEY_RE.pattern!r}) — fail-closed against "
                    f"injection via the sourced env file"
                )
        fd, path = tempfile.mkstemp(prefix="hivepilot-herdr-env-", suffix=".sh")
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
        try:
            os.remove(path)
        except OSError:
            pass

    # -- command rendering --------------------------------------------------

    def _render_command(self, payload: RunnerPayload) -> str:
        template = payload.step.command or self.definition.command
        if not template:
            raise ValueError(f"herdr runner '{self.definition.name}' missing command")

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
    """`ok` when `herdr` is on PATH; `degraded` when it isn't — `HerdrRunner`
    already falls back to raw `bash -lc` execution in that case, so a missing
    `herdr` binary degrades pane visibility rather than breaking runs. Never
    raises: any internal error is reported as the exception TYPE name only
    (never a message/value), matching `PluginManager.run_health_check`.
    """
    try:
        if shutil.which("herdr"):
            return HealthStatus("ok", "herdr on PATH")
        return HealthStatus("degraded", "herdr not on PATH — falls back to raw shell")
    except Exception as exc:  # noqa: BLE001 — a health check must never crash
        return HealthStatus("error", type(exc).__name__)


def register() -> dict[str, Any]:
    from hivepilot.config import settings

    if not settings.herdr_enabled:
        return {}
    return {"runners": {"herdr": HerdrRunner}, "health": {"herdr": health}}
