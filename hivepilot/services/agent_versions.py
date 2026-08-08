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

import subprocess

__all__ = ["probe_version"]

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
