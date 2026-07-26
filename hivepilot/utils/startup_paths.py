"""Startup path logging -- the bug-debt item never finished: every long-
running HivePilot entry point (api server, scheduler daemon; see the module
docstring on `hivepilot.services.notification_service` for why the
Telegram/Slack/Discord bot processes are NOT wired to this yet -- a parallel
sprint is actively refactoring streaming in that file) must log ONCE, at
INFO, the RESOLVED, ABSOLUTE paths it is actually reading/writing.

RATIONALE: `hivepilot.config.Settings` defaults several path fields
(`state_db`, `prompts_dir`, `obsidian_vault`, ...) to RELATIVE paths,
resolved against `base_dir` (which itself defaults to `Path.cwd()` at
process-start time). A service started with `cwd=/` (the OpenRC/systemd
`WorkingDirectory` default) therefore resolves EVERY one of those fields to
a completely different absolute location than the SAME command run
interactively from a login shell (`cwd=$HOME` or a project checkout) --
silently. This exact split has cost real debugging hours (two state DBs, a
prompts dir that "goes missing", ...): a process that never states which
files it is actually using leaves an operator with no way to tell, from
logs alone, which of the two contexts they're even looking at. This module
closes that gap for every entry point that calls `log_resolved_startup_
paths()`.
"""

from __future__ import annotations

import weakref
from pathlib import Path

from hivepilot.config import Settings
from hivepilot.config import settings as _default_settings
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

# Keyed by id(settings), valued by a WEAK reference to the settings object
# itself -- idempotent per `Settings` INSTANCE, not globally: a startup hook
# that could theoretically fire more than once in the same process (a
# reload, a test re-entering the same process) never spams the log for the
# SAME settings object, while a genuinely different `Settings` instance (a
# distinct deployment identity, a fresh test fixture) always gets its own
# line. Deliberately NOT a plain `set[int]` of ids: once a `Settings`
# instance is garbage-collected, CPython is free to reuse its `id()` for an
# UNRELATED later object -- a bare int-keyed set would then wrongly treat
# that new, never-before-seen instance as "already logged". A
# `WeakValueDictionary` removes its entry via a GC callback at the moment
# the original object dies, closing that reuse window entirely.
_logged_for: weakref.WeakValueDictionary[int, Settings] = weakref.WeakValueDictionary()


def _resolve_topics_registry_path(s: Settings) -> Path:
    """Mirror (deliberately NOT import) `hivepilot.services.notification_
    service._topics_registry_path`'s resolution: the configured
    `stream_topics_registry_path` override when set, else `xdg_data_home/
    stream_topics.json` -- the same stable, cwd-INDEPENDENT data directory
    notification_service itself uses. Duplicated here rather than imported
    on purpose: `notification_service.py` is a file a parallel sprint is
    actively refactoring (streaming) -- coupling this read-only startup log
    to its internals would risk a confusing import break from unrelated
    work. This is two lines of simple, stable resolution logic; if it ever
    drifts from notification_service's own, that is the one place to look.
    """
    override = s.stream_topics_registry_path
    path = override if override is not None else (s.xdg_data_home / "stream_topics.json")
    return Path(path).expanduser().resolve()


def log_resolved_startup_paths(settings: Settings | None = None) -> None:
    """Log ONCE, at INFO, the RESOLVED ABSOLUTE paths this process is
    actually going to use for its five cwd-sensitive files/dirs: the state
    DB, the Telegram stream-topics registry, the config dir, the prompts
    dir, and the Obsidian vault.

    Call this from every long-running entry point's startup path (the API
    server's FastAPI `startup` event, the scheduler daemon's `run()`) --
    "a process begins serving" is the same seam `init_tracing()` already
    hooks in both of those.
    """
    s = settings or _default_settings
    key = id(s)
    if key in _logged_for:
        return
    _logged_for[key] = s

    logger.info(
        "startup.resolved_paths",
        state_db=str(s.resolve_path(s.state_db)),
        topics_registry=str(_resolve_topics_registry_path(s)),
        config_dir=str(s.xdg_config_home),
        prompts_dir=str(s.resolve_path(s.prompts_dir)),
        vault=str(s.resolve_path(s.obsidian_vault)),
    )
