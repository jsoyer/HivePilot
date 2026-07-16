"""Mirador web UI — FastAPI serving glue.

Serves the pre-built static assets committed under `hivepilot/webui/static/`
(built by `web/`, a separate Vite + React + TypeScript + Tailwind + shadcn/ui
app — see `docs/v4/WEBUI.md` and `web/README.md`). The build output is
committed into the Python package so `pip install hivepilot[webui]` ships it
and needs **zero Node at runtime**.

Gated in `hivepilot/services/api_service.py` by `settings.enable_webui`
(env `HIVEPILOT_ENABLE_WEBUI`) AND a real build being present
(`static_available()`) — mirrors how `settings.enable_textual_ui`
(`HIVEPILOT_ENABLE_TEXTUAL_UI`) gates the Textual dashboard in
`hivepilot/cli.py`. Both checks are read fresh on every request (the same
"settings read at call time" pattern already used elsewhere in
`api_service.py`, e.g. `body_size_limit`'s `settings.api_max_body_size`)
rather than baked in once at import time — so toggling the flag, or a
missing static/ directory, is reflected immediately and can never leave the
route registered-but-broken.

This module does **no filesystem I/O at import time** — only `Path` object
construction — so a package installed without a built `static/` directory
(e.g. the `webui` extra without ever running `web/`'s build) can never break
core API startup by merely importing this module.
"""

from __future__ import annotations

from pathlib import Path

STATIC_DIR = Path(__file__).parent / "static"
INDEX_HTML = STATIC_DIR / "index.html"


def static_available() -> bool:
    """True if a real Mirador web UI build is present. Never raises — a
    missing/partial `static/` directory (e.g. no build was ever run) just
    means "not available", not an error."""
    try:
        return INDEX_HTML.is_file()
    except OSError:
        return False


def resolve_static_path(sub_path: str) -> Path | None:
    """Resolve `sub_path` (the part of the URL after `/ui/`) to a real file
    inside `STATIC_DIR`.

    Returns `None` if `sub_path` is empty, doesn't correspond to an existing
    file, or would resolve outside `STATIC_DIR` (path-traversal guard, e.g.
    `../../etc/passwd`) — callers should fall back to serving `INDEX_HTML`
    in that case (SPA client-side routing fallback), exactly as they would
    for the bare `/ui` route.
    """
    if not sub_path:
        return None
    try:
        candidate = (STATIC_DIR / sub_path).resolve()
        candidate.relative_to(STATIC_DIR.resolve())
    except (OSError, ValueError):
        return None
    return candidate if candidate.is_file() else None
