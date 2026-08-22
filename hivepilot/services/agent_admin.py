"""Install and update agent binaries from Pollen — box only, consent first.

`agent_install.py`'s interactive guard is deliberate and stays untouched:
"No auto-install, ever… refuses even when `assume_yes=True` unless stdin AND
stdout are TTYs." A Pollen button is non-interactive by definition, so this
module REPLACES that guarantee rather than bypassing it — the API route that
calls in here requires the admin role and an explicit consent field, and every
action lands in the audit log with the actor and the version before and after:
the `pr_gate_outcomes` shape, a human decision paired with a machine state.

Red lines:

  * only registry CONSTANTS execute. The kind is validated against
    `AGENT_INSTALL_SPECS` before anything runs; no URL or command ever arrives
    from the UI. A button that took a URL would make Pollen remote code
    execution.
  * docs-only entries stay docs-only — where the vendor has no vetted
    one-liner the answer is the link, never an improvised command.
  * nothing here is ever called by a run. Installing is an operator action.

`UPDATE_COMMANDS` holds VERIFIED constants, read from each binary's --help on
2026-08-22 — never guessed. `None` means "no verified non-interactive updater":
vibe's `--check-upgrade` PROMPTS, and gemini shows none. None means no button,
not a greyed-out one that lies.

The box trap this reports rather than repeats: these installers are per-user.
grok's landed in `$HOME/.grok/bin`, the systemd units set no PATH, and
`shutil.which` returned None for the SERVICE while the installing shell saw the
binary fine — so the runner plugin silently never registered. Every result
carries `on_service_path`, probed in THIS process: the service's own view, the
only one that decides whether a runner registers.
"""

from __future__ import annotations

import shutil
import subprocess  # nosec B404 - only registry constants are ever executed; see module docstring
from typing import Any, Callable

from hivepilot.services.agent_install import AGENT_INSTALL_SPECS
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

__all__ = ["AgentAdminError", "UPDATE_COMMANDS", "list_agents_admin", "perform_agent_action"]


class AgentAdminError(RuntimeError):
    """A refused agent-admin action. The message is operator-facing."""


#: kind -> the exact update argv, VERIFIED against the installed binary's
#: --help (2026-08-22), or None when no non-interactive updater exists.
#: argv lists, never shell strings: updates run WITHOUT a shell, so nothing
#: can be smuggled through word-splitting. Install is the one vetted shell
#: pipeline (the spec's official `curl | bash` one-liner), and the only one.
UPDATE_COMMANDS: dict[str, list[str] | None] = {
    "grok": ["grok", "update"],  # `grok update --help`: "Check for updates or install"
    "claude": ["claude", "update"],  # `claude update|upgrade`: "Check for updates and install"
    "codex": ["codex", "update"],  # `codex --help`: "Update Codex to the latest version"
    "cursor": ["cursor-agent", "update"],  # "Update Cursor Agent to the latest version"
    # vibe's `--check-upgrade` PROMPTS ("prompt to install it") — interactive,
    # unusable headless. gemini/opencode/ollama/antigravity/qwen-code/kimi-cli
    # showed no update surface. None -> no button.
    "vibe": None,
    "gemini": None,
}

_ACTION_TIMEOUT_SECONDS = 600.0


def _probe_version(kind: str) -> str | None:
    """The installed version, via the existing probe — None when absent or
    unreadable. Never invented."""
    from hivepilot.services.agent_versions import probe_agent_cli

    try:
        probe = probe_agent_cli(kind)
    except Exception:  # noqa: BLE001 - a probe must never fail the action
        return None
    return getattr(probe, "installed", None)


def _record_audit(**kw: Any) -> None:
    """One audit row per action. Failure to record is a WARNING, never a
    reason the action's outcome is hidden from the caller — but it is also
    never silent."""
    from hivepilot.services import state_service

    try:
        state_service.record_audit(
            token_hash=kw.get("token_hash", ""),
            role=f"agent-admin:{kw['actor']}",
            endpoint=f"/v1/agents/{kw['kind']}/{kw['action']}",
            method="POST",
            result=(
                f"{kw['result']} version_before={kw['version_before']} "
                f"version_after={kw['version_after']}"
            ),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("agent_admin.audit_record_failed", error=str(exc))


def list_agents_admin() -> list[dict[str, Any]]:
    """Every registry kind with its capabilities and the SERVICE's view.

    `updatable` reflects the verified constant, not hope; `installable`
    reflects a vetted one-liner existing. `on_service_path` is this process's
    `shutil.which` — the view that decides whether a runner registers.
    """
    rows: list[dict[str, Any]] = []
    for kind, spec in sorted(AGENT_INSTALL_SPECS.items()):
        rows.append(
            {
                "kind": kind,
                "name": spec.name,
                "vendor": spec.vendor,
                "binary": spec.binary,
                "docs_url": spec.docs_url,
                "installable": spec.command is not None,
                "updatable": UPDATE_COMMANDS.get(kind) is not None,
                "on_service_path": shutil.which(spec.binary) is not None,
                "installed_version": _probe_version(kind),
            }
        )
    return rows


def perform_agent_action(
    kind: str,
    action: str,
    *,
    actor: str,
    token_hash: str = "",
    run_cli: Callable[..., subprocess.CompletedProcess] | None = None,
) -> dict[str, Any]:
    """Run one install or update for *kind*, refusing everything else.

    *actor* is who consented — it travels into the audit row. *run_cli* is
    injected for tests; nothing else about the flow differs.
    """
    runner = run_cli or subprocess.run

    spec = AGENT_INSTALL_SPECS.get(kind)
    if spec is None:
        raise AgentAdminError(
            f"unknown agent kind {kind!r} — only the curated registry can be "
            f"acted on. Available: {', '.join(sorted(AGENT_INSTALL_SPECS))}"
        )
    if action not in ("install", "update"):
        raise AgentAdminError(f"unknown action {action!r} — only install and update exist")

    if action == "install":
        if spec.command is None:
            raise AgentAdminError(
                f"{kind!r} is docs-only: no vetted install one-liner exists. "
                f"Install it by hand from {spec.docs_url} — this path never "
                f"improvises a command."
            )
        # The one shell line this module ever runs: the vetted official
        # pipeline, verbatim from the registry constant.
        argv: list[str] = ["bash", "-lc", spec.command]
    else:
        update_argv = UPDATE_COMMANDS.get(kind)
        if update_argv is None:
            raise AgentAdminError(
                f"{kind!r} has no verified update command — vibe's is "
                f"interactive and some CLIs have none. None means no button, "
                f"not a guessed command."
            )
        argv = list(update_argv)

    version_before = _probe_version(kind)
    logger.info("agent_admin.action_start", kind=kind, action=action, actor=actor)
    try:
        completed = runner(
            argv,
            check=False,
            text=True,
            capture_output=True,
            timeout=_ACTION_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
        )
    except Exception as exc:  # noqa: BLE001 - the failure is the result, not a crash
        _record_audit(
            kind=kind,
            action=action,
            actor=actor,
            token_hash=token_hash,
            result=f"failed: {type(exc).__name__}",
            version_before=version_before,
            version_after=version_before,
        )
        raise AgentAdminError(f"{action} of {kind!r} did not run: {exc}") from exc

    version_after = _probe_version(kind)
    ok = completed.returncode == 0
    _record_audit(
        kind=kind,
        action=action,
        actor=actor,
        token_hash=token_hash,
        result="ok" if ok else f"failed: exit {completed.returncode}",
        version_before=version_before,
        version_after=version_after,
    )
    return {
        "kind": kind,
        "action": action,
        "ok": ok,
        "exit_code": completed.returncode,
        "version_before": version_before,
        "version_after": version_after,
        # The service's own view — the only one that decides whether a runner
        # registers. False here after a "successful" install is the grok trap.
        "on_service_path": shutil.which(spec.binary) is not None,
        "detail": (completed.stdout or "")[-2000:]
        if ok
        else (completed.stderr or completed.stdout or "")[-2000:],
    }
