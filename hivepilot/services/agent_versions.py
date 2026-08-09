"""Which version of an agent CLI is actually installed.

Nothing recorded this. `doctor` reported whether `claude` was on PATH and
stopped there, so the box could move from one version to another unnoticed —
and the CLI *is* the runtime:

- `WaitForMcpServers`, which `token_savior` bootstraps against, is a Claude
  Code internal whose name is version-dependent.
- `--mcp-config` / `--strict-mcp-config` are recent flags.
- `Read(./**)`-style scoped permission specifiers are a permission-syntax
  feature the noxys roles now depend on to *refuse* reads outside the
  workspace. If a later CLI parsed that string differently, a grant relied on
  for secret containment could widen silently.

Reporting only. Updating the agent CLI would change every role's behaviour
with no PR, no review and no verdict — and the deployment already sets
`CLAUDE_CODE_DISABLE_LEGACY_MODEL_REMAP` because an update can remap a model
underneath us. Being a version behind is the lesser harm; not knowing which
version is the one worth fixing.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from hivepilot.registry import active_agent_runner_kinds

if TYPE_CHECKING:  # pragma: no cover - typing only
    from hivepilot.services.config_doctor import DoctorFinding

logger = structlog.get_logger(__name__)


__all__ = [
    "AgentCliProbe",
    "check_agent_cli_versions",
    "fetch_latest_npm_version",
    "probe_agent_cli",
    "probe_version",
    "update_npm_package",
]

#: A diagnostic must not hang the command it diagnoses.
_TIMEOUT_SECONDS = 5.0

#: Kept short enough to sit in a `doctor` table column. Some CLIs print a
#: paragraph; the version is at the front of it.
_MAX_CHARS = 80


def probe_version(binary: str, *, timeout: float = _TIMEOUT_SECONDS) -> str | None:
    """The first line of ``<binary> --version``, or None.

    Returns None rather than a placeholder for every failure — missing
    binary, non-zero exit, timeout, unreadable output. A CLI that does not
    understand ``--version`` has told us nothing, and echoing its usage text
    into a version column would be worse than silence.

    Never raises: this runs inside `doctor`, and a diagnostic that breaks the
    diagnosis is worse than one that abstains.
    """
    try:
        # `--version` and nothing else. It is the one invocation safe to run
        # against a binary we have not identified yet: no subcommand, no
        # flags that could do work.
        result = subprocess.run(
            [binary, "--version"],
            check=False,
            text=True,
            capture_output=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except Exception:  # noqa: BLE001 — see docstring: this must never raise
        return None
    if result.returncode != 0:
        return None
    raw = (result.stdout or "") or (result.stderr or "")
    first = raw.strip().splitlines()[0].strip() if raw.strip() else ""
    if not first:
        return None
    return first[:_MAX_CHARS]


# ---------------------------------------------------------------------------
# Is it CURRENT? -- added on top of the presence/version probe above.
#
# `probe_version` answers "which version". This half answers "and is that the
# latest", plus "how would you even update it" -- which turns out to depend on
# how the CLI was installed, not on which CLI it is.
#
# Generic on purpose: `claude` is one of twelve `AGENT_RUNNER_KINDS`. Nothing
# below knows a CLI by name. The version is parsed from whatever `--version`
# printed, and the install method is inferred from the RESOLVED BINARY PATH.
#
# The module's stance above is unchanged and load-bearing: reporting only.
# `update_npm_package` exists for `hivepilot agents versions --update`, which
# an operator types. Nothing in the engine calls it, and nothing should -- an
# agent CLI that updated itself underneath a running fleet would change every
# role's behaviour at once with no PR, no review and no verdict, and the
# regression would look like the agents simply getting worse.
# ---------------------------------------------------------------------------

_CHECK = "agent_cli_versions"

# Every agent CLI wraps its version in something different -- "2.1.220 (Claude
# Code)", "v0.9.13", "codex 0.12.1" -- so take the first dotted-numeric token,
# the one thing they agree on.
#
# The lookbehind is load-bearing. A plain `\b` does NOT fire between `v` and
# `0` (both are word characters), so `v0.9.13` matched at the `9` and parsed
# as `9.13`: a wrong version reported with full confidence, which is worse
# than none. Caught by a test, not by reading it.
_VERSION_RE = re.compile(r"(?<![\w.])v?(\d+(?:\.\d+)+)")

_NODE_MODULES = "node_modules"


@dataclass(frozen=True)
class AgentCliProbe:
    """One agent CLI, as found on this host."""

    kind: str
    binary: str | None
    on_path: bool
    version: str | None
    npm_package: str | None
    error: str | None


def _parse_version(raw: str | None) -> str | None:
    """First dotted-numeric token in *raw*, or None.

    None means "ran, could not tell" -- a different fact from "old", and it
    must never render as one.
    """
    match = _VERSION_RE.search(raw or "")
    return match.group(1) if match else None


def _version_tuple(version: str) -> tuple[int, ...] | None:
    try:
        return tuple(int(part) for part in version.split("."))
    except ValueError:
        return None


def _is_outdated(installed: str | None, latest: str | None) -> bool | None:
    """True/False, or None when undecidable.

    None rather than False: False reads as "up to date", a claim this has no
    basis for.

    Segments compare as NUMBERS. As strings "2.1.220" < "2.1.99", which would
    report a current CLI as outdated forever -- and the box runs 2.1.220, so
    that is the live case, not a hypothetical.
    """
    if not installed or not latest:
        return None
    a, b = _version_tuple(installed), _version_tuple(latest)
    if a is None or b is None:
        return None
    return a < b


def _npm_package_for(binary: Path | None) -> str | None:
    """The npm package owning *binary*, or None if it is not npm-installed.

    A natively-installed CLI must not be handed an `npm install -g`
    instruction: it would report success and change nothing. Claude Code's
    own installer lives under `~/.local/share/claude/versions/`, so this
    returning None is the common case, not an error.
    """
    if binary is None:
        return None
    parts = binary.resolve().parts if binary.is_absolute() else binary.parts
    if _NODE_MODULES not in parts:
        return None
    rest = parts[parts.index(_NODE_MODULES) + 1 :]
    if not rest:
        return None
    if rest[0].startswith("@") and len(rest) >= 2:
        return f"{rest[0]}/{rest[1]}"
    return rest[0]


def _command_for(kind: str) -> str:
    """The binary name for an agent kind, honouring a settings override."""
    from hivepilot.config import settings

    attr = f"{kind.replace('-', '_')}_command"
    return str(getattr(settings, attr, None) or kind)


def probe_agent_cli(kind: str) -> AgentCliProbe:
    """Resolve *kind*'s CLI and report path, version and install origin.

    Delegates the actual `--version` call to `probe_version` above rather
    than repeating it -- one invocation of an unidentified binary in this
    module, not two.
    """
    command = _command_for(kind)
    resolved = shutil.which(command)
    if resolved is None:
        return AgentCliProbe(kind, None, False, None, None, None)

    npm_package = _npm_package_for(Path(resolved))
    raw = probe_version(resolved)
    version = _parse_version(raw)
    error = None if version else "`--version` printed no recognisable version"
    return AgentCliProbe(kind, resolved, True, version, npm_package, error)


def fetch_latest_npm_version(package: str) -> str | None:
    """Latest published version of *package*, or None. Network. Never raises.

    Reached ONLY from `--check-latest`. `config doctor` must stay fast and
    work offline: a health command that needs the internet stops being run on
    the box that has no outbound access.
    """
    try:
        completed = subprocess.run(  # noqa: S603
            ["npm", "view", package, "version"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("agent_versions.npm_view_failed", package=package, error=str(exc))
        return None
    return _parse_version(completed.stdout)


def update_npm_package(package: str) -> tuple[bool, str]:
    """`npm install -g <package>@latest`. Returns (ok, output tail).

    Only ever reached from an explicit, confirmed operator command -- see the
    stance in this module's header.
    """
    try:
        completed = subprocess.run(  # noqa: S603
            ["npm", "install", "-g", f"{package}@latest"],
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    tail = (completed.stdout + completed.stderr).strip().splitlines()
    return completed.returncode == 0, "\n".join(tail[-8:])


def check_agent_cli_versions() -> list[DoctorFinding]:
    """Doctor check: each ACTIVE agent CLI and its version.

    Active kinds only. Twelve exist and a deployment runs one or two;
    probing all of them would print ten "not installed" lines that are not
    problems -- the noise that made the first draft of
    `plugins_written_vs_installed` useless.

    Offline. The registry lookup lives behind `--check-latest`.
    """
    from hivepilot.services.config_doctor import _finding

    findings: list[DoctorFinding] = []
    for kind in sorted(active_agent_runner_kinds()):
        probe = probe_agent_cli(kind)

        if not probe.on_path:
            findings.append(
                _finding(
                    "warning",
                    _CHECK,
                    f"{kind}: registered as a runner but its CLI is not on PATH",
                    "The kind is dispatchable, so a pipeline can route a step to it "
                    "and every call fails at exec time rather than at config time.",
                    f"Install the {kind} CLI, or disable the runner "
                    f"(HIVEPILOT_{kind.replace('-', '_').upper()}_ENABLED=false).",
                )
            )
            continue

        if probe.version is None:
            findings.append(
                _finding(
                    "info",
                    _CHECK,
                    f"{kind}: {probe.binary} (version unknown)",
                    f"Present, but {probe.error}. Not a fault -- some CLIs report "
                    "none -- but this deployment cannot tell whether it is current.",
                    "No action needed.",
                )
            )
            continue

        origin = f" · npm {probe.npm_package}" if probe.npm_package else ""
        findings.append(
            _finding(
                "info",
                _CHECK,
                f"{kind}: {probe.version} ({probe.binary}{origin})",
                "Printed even when healthy: the agent CLI IS the runtime, so the "
                "version at the time of a run is part of what explains that run.",
                "`hivepilot agents versions --check-latest` compares against the "
                "registry. Updating is never automatic.",
            )
        )

    return findings
