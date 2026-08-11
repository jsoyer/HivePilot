"""token-savior: wire its MCP server onto a step, per project.

token-savior (github.com/mibayy/token-savior, MIT) is an MCP server that
indexes a codebase into symbols, so an agent can fetch `get(symbol)` instead
of reading whole files.

The claude runner already knows how to pass `--mcp-config` and
`--allowed-tools`. What was missing is the wiring, and the wiring is not
boilerplate: the vendor's stanza carries `WORKSPACE_ROOTS`, which means the
config is **per project**, not global. Hand-maintaining one JSON file per
project is exactly the sort of thing that drifts silently -- a stale
`WORKSPACE_ROOTS` indexes the wrong repository and the agent gets confident
answers about someone else's code.

Three positions this module takes.

**Opt-in, default off** (`HIVEPILOT_TOKEN_SAVIOR_ENABLED`). The server
indexes the repository and keeps a memory database. That belongs to whoever
owns the code, so this follows `headroom_enabled`'s precedent rather than
`rtk_enabled`'s opt-out.

**It never takes the wheel.** A step that already declares `mcp_config` was
configured deliberately; the hook fills a gap and then stays out of the way.
It also declines to pre-approve its tools onto someone else's config, where
they would advertise availability that does not exist.

**It reports no savings of its own.** The README claims 97.9% against 78.3%
and -80% active tokens, measured by its author on its author's benchmark.
Surfacing a vendor ratio as though it were our telemetry is how
codebase-memory-mcp's 99.2% turned out to be ~10% here. HivePilot already
records `input_tokens`/`cache_read_tokens`/`cost_usd` per step; the honest
measurement is the same review run twice, and that is an operator's
experiment rather than a number this plugin can assert.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

from hivepilot.config import settings
from hivepilot.plugins import HealthStatus
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

#: The server key the vendor documents. Kept verbatim so an operator's
#: `claude mcp add token-savior ...` and our generated stanza agree, and so
#: the `mcp__<server>__<tool>` names match what the server actually exports.
_SERVER_NAME = "token-savior-recall"

#: Console script the package installs; also what the vendor's stanza names.
_BINARY = "token-savior"

#: Used only when a step carries no project — the per-project name is the
#: normal case, because `WORKSPACE_ROOTS` is what makes the index correct.
_CONFIG_FILENAME = "token-savior.mcp.json"

_SLUG_RE = re.compile(r"[^A-Za-z0-9_.-]+")

#: Claude Code's own tool for bringing MCP servers up. Because
#: `--allowed-tools` is a whitelist that RESTRICTS, naming only the server's
#: tools excludes the very thing that STARTS the server -- run 347 died on
#: `400 Tool reference 'WaitForMcpServers' not found in available tools`.
#: The plugin that wires MCP is the one that has to grant it.
_MCP_BOOTSTRAP_TOOL = "WaitForMcpServers"


def _config_dir() -> Path:
    """Where the generated MCP stanza is written.

    XDG_DATA_HOME, not `base_dir`. `base_dir` is the process cwd and the
    services run with `WorkingDirectory=/`, so this resolved to
    `/token-savior` and every stage of a real run logged
    `Permission denied: '/token-savior/<project>.mcp.json'` -- the plugin was
    inert for the entire pipeline and said so only in a warning nobody reads.

    A generated per-install file belongs beside the other generated
    per-install state (plugin files, the config-repo clone), in a directory
    the service user owns.
    """
    from hivepilot.config import settings as _settings

    return Path(_settings.xdg_data_home) / "token-savior"


def _resolve_binary() -> str | None:
    """Absolute path to the MCP server binary, or None.

    PATH first, then next to `sys.executable`. That second lookup is not
    belt-and-braces: measured on the reference box, the server installs to
    `/opt/hivepilot/venv/bin/token-savior` while the systemd unit's PATH is
    `/root/.local/bin:/usr/local/sbin:...` with no venv in it. The binary was
    present, invisible, and the health check honestly reported `degraded`
    while an operator who had just installed it would swear it was there.

    HivePilot runs from that venv, so its sibling console scripts are the
    likeliest location of all -- `self-update` resolves `sys.executable` for
    exactly this reason.
    """
    found = shutil.which(_BINARY)
    if found:
        return found
    sibling = Path(sys.executable).parent / _BINARY
    return str(sibling) if sibling.is_file() else None


def _server_available() -> bool:
    """Whether the MCP server binary can actually be launched."""
    return _resolve_binary() is not None


def _config_path(project_name: str | None) -> Path:
    if not project_name:
        return _config_dir() / _CONFIG_FILENAME
    return _config_dir() / f"{_SLUG_RE.sub('-', project_name)}.mcp.json"


def _stanza(workspace_root: str | None) -> dict[str, Any]:
    env: dict[str, str] = {
        "TOKEN_SAVIOR_CLIENT": "hivepilot",
        "TOKEN_SAVIOR_PROFILE": settings.token_savior_profile,
        # token-savior ships a memory engine of its own. HivePilot already
        # has one (mem0), and two stores recalling near-identical context
        # into the same prompt is paying twice for one fact -- the duplication
        # that makes a layered context stack expensive instead of cheap.
        #
        # Today it is inert anyway, because the `[memory-vector]` extra we do
        # not install is what provides `fastembed`/`sqlite-vec`. That is an
        # accident of packaging, not a decision, and installing that extra for
        # any reason would silently re-enable it. Stated here so the boundary
        # survives the next `pip install`.
        "TS_MEMORY_DISABLE": "1",
    }
    if workspace_root:
        env["WORKSPACE_ROOTS"] = workspace_root
    return {
        "mcpServers": {
            _SERVER_NAME: {
                # Absolute: the server is launched by the agent CLI, whose
                # PATH is not necessarily the one this process resolved.
                "command": _resolve_binary() or _BINARY,
                "env": env,
            }
        }
    }


def _write_config(path: Path, workspace_root: str | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_stanza(workspace_root), indent=2), encoding="utf-8")


def before_step(**kwargs: Any) -> None:
    """Point the step at a per-project token-savior MCP config.

    No-op when disabled, when the server binary is absent, when the step
    already declares `mcp_config`, or on any error. Never raises: an
    optional index that cannot be wired must degrade to running without it,
    not take the pipeline down with it.
    """
    try:
        if not settings.token_savior_enabled:
            return
        if not _server_available():
            # Wiring a config that names a binary which is not there would
            # leave the agent silently without the tools it was told to use.
            return

        payload = kwargs.get("payload")
        step = getattr(payload, "step", None)
        metadata = getattr(step, "metadata", None)
        if not isinstance(metadata, dict):
            return
        if metadata.get("mcp_config"):
            # Explicitly configured. Adding our tools to someone else's
            # server would advertise availability that does not exist.
            return

        project = getattr(payload, "project", None)
        project_name = getattr(payload, "project_name", None)
        workspace_root = str(getattr(project, "path", "")) or None

        path = _config_path(project_name)
        _write_config(path, workspace_root)

        metadata["mcp_config"] = str(path)
        # Additive. `--allowed-tools` is a whitelist that RESTRICTS, so
        # replacing the list would silently revoke grants the role depends
        # on; and every entry stays scoped to this server so enabling an
        # index never widens what anything else may do.
        existing = metadata.get("allowed_tools") or []
        if isinstance(existing, str):
            existing = [existing]
        metadata["allowed_tools"] = [
            *existing,
            f"mcp__{_SERVER_NAME}",
            _MCP_BOOTSTRAP_TOOL,
        ]

        logger.info(
            "token_savior.wired",
            project=project_name,
            mcp_config=str(path),
            workspace_root=workspace_root,
        )
    except Exception as exc:  # noqa: BLE001 — never break a step
        logger.warning("token_savior.wiring_failed", error=str(exc))


def health() -> HealthStatus:
    """Report whether the server can be launched — not whether it is enabled.

    "Enabled" is a flag; "available" is a fact. A check that returns ok
    because a setting is true is the defect the Health panel was rebuilt to
    remove, and it is worse here than elsewhere: a missing binary costs no
    error, only an agent quietly reading whole files again.
    """
    try:
        if _server_available():
            return HealthStatus("ok", f"token-savior available ({_resolve_binary()})")
        return HealthStatus(
            "degraded",
            f"token-savior enabled but `{_BINARY}` is not on PATH — "
            "steps run without the symbol index "
            '(pip install "token-savior-recall[mcp]")',
        )
    except Exception as exc:  # noqa: BLE001
        return HealthStatus("error", f"token-savior health check failed: {exc}")


def register() -> dict[str, Any]:
    """Declares `filesystem` only — and deliberately NOT `subprocess`.

    It is tempting to declare `subprocess` because a token-savior process
    does get spawned. It is not spawned by this module: the agent CLI
    launches it from the `--mcp-config` stanza, under the authority the
    runner already holds. What this module does is write a small JSON file
    into `base_dir` and read PATH via `shutil.which` (a lookup, not a
    spawn). `filesystem` is the whole of it.

    Over-declaring is not the safe direction here. `plugins_capability_policy`
    defaults to `[]` — deny everything declared — so an inflated manifest
    would make an operator allowlist `subprocess`, the most load-bearing
    token there is, in order to run a plugin that writes one config file.
    That trades a real reduction in what the policy discriminates for no
    security at all.
    """
    if not settings.token_savior_enabled:
        return {}
    return {
        "before_step": before_step,
        "health": {"token_savior": health},
        "capabilities": ["filesystem"],
    }
