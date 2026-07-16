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
            wrapped_cmd = (
                f"cd {project_path}; set -a; source {shlex.quote(env_file)}; set +a; {command_str}"
            )
            self._pane_run(payload, pane_id, wrapped_cmd)
            self._wait_idle(payload, pane_id)
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

    def _wait_idle(self, payload: RunnerPayload, pane_id: str) -> None:
        timeout_ms = payload.step.metadata.get("herdr_wait_timeout_ms") or (
            self.settings.herdr_wait_timeout_ms
        )
        argv = [
            "herdr",
            "wait",
            "agent-status",
            pane_id,
            "--status",
            "idle",
            "--timeout",
            str(timeout_ms),
        ]
        result = subprocess.run(argv, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            # Fail-closed: blocked / unknown / timed-out are ALL treated as a
            # step failure — there is no path here that silently succeeds.
            err = (result.stderr or result.stdout or "").strip()[-2000:]
            raise RuntimeError(
                f"herdr runner step '{payload.step.name}': pane {pane_id} did not "
                f"reach 'idle' within {timeout_ms}ms (exit {result.returncode}): {err}"
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
        if isinstance(data, str):
            return data or None
        if isinstance(data, dict):
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
