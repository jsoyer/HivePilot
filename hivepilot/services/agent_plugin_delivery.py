"""Which agent CLIs owe a plugin file, and what it is called.

`plugins/*.py` does not ship in the wheel, and `hivepilot plugins install` only
knows the curated fetch registry. Agent CLIs are excluded from that registry on
purpose -- `plugin_installer` says they are "already covered by the guided
`hivepilot agents install` flow". They are not: that flow installs the BINARY
and has never placed a plugin file. The comment describes a coverage that does
not exist.

Measured on the box before this module was written:

    vibe.py     2026-08-14 16:44   <- copied by hand after #520
    codex.py    ABSENT
    cursor.py   ABSENT
    gemini.py   ABSENT
    opencode.py ABSENT
    ollama.py   ABSENT

Nobody noticed for codex/cursor because their binaries were absent anyway. The
day a previously-builtin runner becomes a gated plugin -- which is what
happened to `vibe` -- the deploy drops it silently, and the only symptom is a
runner that no longer exists.

The other route, shipping `plugins/` in the wheel, is deliberately NOT taken:
it risks squatting a top-level `plugins` name in every installation, which is
an operator decision about packaging rather than an engine one.

The set is DERIVED from `plugin_installer.AGENT_CLI_PLUGINS`, never retyped.
Two hand-maintained lists of one fact eventually disagree, and this
disagreement would be invisible -- a kind added there and missed here goes
straight back to shipping nothing.
"""

from __future__ import annotations

from typing import Any, Callable

import structlog

logger = structlog.get_logger(__name__)


def agents_owing_a_plugin() -> set[str]:
    """Every agent-CLI kind whose plugin file must travel with its binary.

    Read from `AGENT_CLI_PLUGINS` at call time rather than copied, so the two
    cannot drift apart.
    """
    from hivepilot.services.plugin_installer import AGENT_CLI_PLUGINS

    return set(AGENT_CLI_PLUGINS)


def plugin_file_for_agent(kind: str) -> str | None:
    """`<kind>.py` when this agent owes a plugin file, else None.

    None for `claude` (a builtin runner -- fetching a file for it would 404 and
    report a failure for something that works), for tools already covered by
    the curated registry like `gh` and `herdr`, and for anything unrecognised:
    a typo must not become a fetch for `plugins/<typo>.py`.

    The kind is used VERBATIM. `kimi_cli`, not `kimi-cli`: the file is a Python
    module and the fetch path is `plugins/<name>.py`, so a hyphen would 404 --
    which this path would then report as "no plugin for this agent", the exact
    silence it exists to remove.
    """
    if not kind or kind not in agents_owing_a_plugin():
        return None
    return f"{kind}.py"


def deliver_plugin_for(
    kind: str,
    *,
    fetch: Callable[..., Any] | None = None,
) -> dict[str, str]:
    """Place this agent's plugin file, and say plainly what happened.

    Runs after the BINARY install has already succeeded, which is why it never
    raises: turning a working install into a failed command would be a worse
    outcome than the one being fixed.

    But it is never silent either, and that is the entire point. A binary
    installed without its plugin is invisible from outside -- the CLI is on
    PATH and the runner simply does not exist -- which is how `vibe` reached a
    box only because somebody copied the file by hand.

    Three outcomes, and the caller must surface all three:

        not_needed -- a builtin runner, or a tool the curated plugin registry
                      already covers. Nothing was owed.
        placed     -- the file is on disk.
        failed     -- it is NOT, and the message says how to finish by hand.
    """
    filename = plugin_file_for_agent(kind)
    if filename is None:
        return {"state": "not_needed", "message": f"{kind} ships no plugin file"}

    fetcher = fetch
    if fetcher is None:
        from hivepilot.services.plugin_installer import fetch_plugin

        fetcher = fetch_plugin

    try:
        path = fetcher(kind)
    except Exception as exc:  # noqa: BLE001
        # Named with the manual fix. An operator reading this should not have
        # to work out that the file can be fetched by hand.
        # NOT `hivepilot plugins install <kind>`: agent CLIs are excluded from
        # the curated registry, so that command answers "unknown plugin" --
        # which is the whole reason this delivery path exists. Advice that does
        # not work is worse than no advice, and my first draft gave it because
        # the test asserted a string I had written rather than a command that
        # runs.
        message = (
            f"{kind}: the binary installed but plugins/{filename} did NOT. "
            f"The runner will not exist until it does -- re-run "
            f"`hivepilot agents install {kind}`, or copy plugins/{filename} "
            f"from the source repo into the plugin directory. Cause: {exc}"
        )
        logger.warning("agent_install.plugin_missing", kind=kind, error=str(exc))
        return {"state": "failed", "message": message}

    logger.info("agent_install.plugin_placed", kind=kind, path=str(path))
    return {"state": "placed", "message": f"{kind}: plugins/{filename} -> {path}"}
