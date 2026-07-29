"""pinokio DETECTION plugin — answers "is Pinokio installed on this host, and
what has it installed?", gated on the per-plugin enable flag AND Pinokio
actually being present.

WHY THIS IS NOT `plugins/ollama.py`
-----------------------------------
`plugins/ollama.py` is the canonical gated-agent-plugin skeleton: flag +
`shutil.which("ollama")` -> register a `RunnerPlugin`. Pinokio does not fit
it, and this module deliberately does not pretend otherwise.

Ollama ships a CLI you hand a prompt to. Pinokio is a LAUNCHER/MANAGER for
local AI applications: it downloads app repositories and runs them behind
local web UIs. It is distributed on Linux as a `.deb` / `.rpm` / `.AppImage`
Electron app (upstream `pinokiocomputer/pinokio`, `build.linux.target`), and
there is NO `pinokio` binary on `PATH` to gate on — the `.deb`/`.rpm` install
an Electron bundle under `/opt/Pinokio` and the AppImage is wherever the user
dropped it. Its only CLI, `pterm`, is an OPTIONAL on-demand install that
`pinokiod` places inside the Pinokio home itself
(`PINOKIO_HOME/bin/npm/bin/pterm`, upstream `kernel/bin/cli.js`), not on
`PATH`, and it is a terminal client rather than a prompt-in/text-out
interface.

So there is nothing here to route a HivePilot task to, and this plugin
contributes NO runner — only a `health` check reporting presence, the
resolved install path, and how many apps are installed. Shipping a
`RunnerPlugin` that shells out to a binary we cannot show exists would be
worse than shipping less.

HOW THE HOME DIRECTORY IS RESOLVED (upstream-faithful, no guessing)
-------------------------------------------------------------------
`pinokiod`'s `kernel/index.js` resolves its own home as:

    let home = this.store.get("home") || process.env.PINOKIO_HOME

This module mirrors that precedence exactly, then adds the documented
default location as a last candidate:

1. `<userData>/config.json` -> key `home`. `<userData>` is Electron's
   per-app data dir, which on Linux is `$XDG_CONFIG_HOME/<app name>` (or
   `$HOME/.config/<app name>` when `XDG_CONFIG_HOME` is unset). Pinokio's
   Electron app name is `Pinokio` (its `package.json` sets `name: "Pinokio"`
   and no `productName` override) and its `config.js` constructs a plain
   `new Store()` from `electron-store@8`, whose default store file name is
   `config.json`.
2. `$PINOKIO_HOME`.
3. `$HOME/pinokio` — the conventional default the official docs use for
   `/PINOKIO_HOME`.

`$XDG_DATA_HOME` is deliberately NOT consulted. Pinokio's own
`kernel/environment.js` REWRITES `XDG_DATA_HOME` (and `XDG_CONFIG_HOME`) to
`PINOKIO_HOME/cache/...` for every app it launches, so inside any
Pinokio-spawned process that variable points at a cache directory, not at an
install. Reading it would produce a confidently wrong path.

FAIL-CLOSED VALIDATION
----------------------
A candidate only counts as a Pinokio home when it is a readable directory
containing BOTH files `pinokiod` writes unconditionally on every successful
init (`kernel/index.js`): `ENVIRONMENT` and `key.json`. `api/` is NOT
required — it is created lazily on the first app download, so a fresh home
legitimately has none, and demanding it would be a false negative.

Anything else — a missing marker, a path that is not a directory, an
unreadable directory, malformed `config.json`, a `home` key present but
blank — is reported as `degraded` naming exactly what was checked and what
failed. An empty or absent value is never read as "no constraint" and never
as success.

SIDE-EFFECT FREEDOM
-------------------
`register()` runs on every process start, so detection is filesystem
`stat`/`listdir` only: no network, no port probing, no process spawning, and
nothing that is discovered is ever executed. Enumerating installed apps is a
directory listing of `PINOKIO_HOME/api` and nothing more.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, NamedTuple

from hivepilot.plugins import HealthStatus

_NAME = "pinokio"

# Written unconditionally by `pinokiod`'s `kernel/index.js` on every init of a
# configured home — the cheapest reliable "this really is a Pinokio home"
# signal that does not depend on any app having been installed yet.
_MARKER_FILES: tuple[str, ...] = ("ENVIRONMENT", "key.json")

# Installed app repositories live one directory deep under `<home>/api`.
_APPS_DIRNAME = "api"

# Electron `app.name` for the Pinokio desktop app -> its `userData` dir name.
_ELECTRON_APP_DIRNAME = "Pinokio"
# `electron-store@8`'s default store file name (`new Store()`, no options).
_STORE_FILENAME = "config.json"
_STORE_HOME_KEY = "home"

# Conventional default home when nothing is configured (official docs).
_DEFAULT_HOME_DIRNAME = "pinokio"

# Health details are rendered into terminals and notification sinks; both the
# home path (read out of a user-writable JSON file) and app names (read out of
# a directory listing) are untrusted text as far as that rendering goes.
_MAX_DETAIL_CHARS = 200
_MAX_PROBLEMS_IN_DETAIL = 3


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


class Detection(NamedTuple):
    """Outcome of one detection pass.

    `home`/`source` are set only when a candidate FULLY validated; `problems`
    lists every precise reason a candidate was rejected (or, when `home` is
    set, why the app inventory could not be completed). `checked` is the
    ordered list of locations that were actually stat'ed, so a `degraded`
    report can name them.

    A `NamedTuple` rather than a `@dataclass`, for the reason `plugins/gh.py`
    documents at length: local-file plugins are exec'd into a module that is
    never registered in `sys.modules`, which trips a real CPython 3.14
    `dataclasses` bug (`_is_type` does `sys.modules[cls.__module__].__dict__`
    -> `AttributeError` on `None`). `NamedTuple` sidesteps it entirely and is
    what `HealthStatus` itself uses.
    """

    home: str | None
    source: str | None
    apps: tuple[str, ...]
    problems: tuple[str, ...]
    checked: tuple[str, ...]

    @property
    def present(self) -> bool:
        return self.home is not None

    @property
    def status(self) -> str:
        """`ok` requires BOTH a validated home AND zero problems — partial
        evidence is always `degraded`, never `ok`."""
        return "ok" if self.home is not None and not self.problems else "degraded"


# ---------------------------------------------------------------------------
# Environment helpers (env-only: never `Path.home()` / `expanduser`, which
# would resolve through the password database and silently work for exactly
# one user)
# ---------------------------------------------------------------------------


def _clean(value: str | None) -> str | None:
    """A set-but-blank variable means NOTHING CONFIGURED, not "no constraint"."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _user_home(environ: Mapping[str, str]) -> Path | None:
    raw = _clean(environ.get("HOME"))
    return Path(raw) if raw else None


def _electron_user_data(environ: Mapping[str, str]) -> Path | None:
    """Electron's `app.getPath('userData')` on Linux:
    `$XDG_CONFIG_HOME/<app name>`, falling back to `$HOME/.config/<app name>`."""
    xdg = _clean(environ.get("XDG_CONFIG_HOME"))
    if xdg:
        return Path(xdg) / _ELECTRON_APP_DIRNAME
    home = _user_home(environ)
    if home is None:
        return None
    return home / ".config" / _ELECTRON_APP_DIRNAME


def _one_line(value: str) -> str:
    """Collapse a path/name into one bounded, control-character-free line."""
    cleaned = "".join(" " if (ord(ch) < 32 or ord(ch) == 127) else ch for ch in value)
    if len(cleaned) > _MAX_DETAIL_CHARS:
        cleaned = cleaned[:_MAX_DETAIL_CHARS] + "..."
    return cleaned


# ---------------------------------------------------------------------------
# Candidate discovery
# ---------------------------------------------------------------------------


def _store_candidate(environ: Mapping[str, str]) -> tuple[Path | None, str | None]:
    """Read `home` out of the Electron-store `config.json`.

    Returns `(candidate, problem)`. A missing store file is not a problem (it
    just means the desktop app never wrote settings here); an unreadable,
    malformed, or `home`-less store IS, because it is positive evidence that
    Pinokio exists while leaving us unable to say where.
    """
    user_data = _electron_user_data(environ)
    if user_data is None:
        return None, None
    store = user_data / _STORE_FILENAME
    try:
        raw = store.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, f"{store} unreadable ({type(exc).__name__})"
    except ValueError as exc:  # e.g. undecodable bytes
        return None, f"{store} undecodable ({type(exc).__name__})"

    try:
        data: Any = json.loads(raw)
    except ValueError:
        return None, f"{store} is not valid JSON"
    if not isinstance(data, dict):
        return None, f"{store} is not a JSON object"

    value = data.get(_STORE_HOME_KEY)
    if not isinstance(value, str) or not value.strip():
        return None, f"{store} has no non-empty {_STORE_HOME_KEY!r} — no Pinokio home configured"
    return Path(value.strip()), None


def _candidates(
    environ: Mapping[str, str],
) -> Iterator[tuple[Path | None, str, str | None]]:
    """Yield `(candidate, source, problem)` in `pinokiod`'s own precedence."""
    store_path, store_problem = _store_candidate(environ)
    yield store_path, "electron-store", store_problem

    env_home = _clean(environ.get("PINOKIO_HOME"))
    yield (Path(env_home) if env_home else None), "PINOKIO_HOME", None

    home = _user_home(environ)
    yield (home / _DEFAULT_HOME_DIRNAME if home else None), "default", None


# ---------------------------------------------------------------------------
# Validation + app inventory (filesystem reads only)
# ---------------------------------------------------------------------------


def _validate(candidate: Path) -> str | None:
    """`None` when `candidate` is a complete Pinokio home, else a precise,
    single-line reason it is not."""
    try:
        if not candidate.is_dir():
            return f"{candidate} is not a directory"
    except OSError as exc:
        return f"{candidate} unreadable ({type(exc).__name__})"

    missing: list[str] = []
    for marker in _MARKER_FILES:
        try:
            if not (candidate / marker).is_file():
                missing.append(marker)
        except OSError as exc:
            return f"{candidate} unreadable ({type(exc).__name__})"
    if missing:
        return f"{candidate} is missing Pinokio marker file(s): {', '.join(missing)}"
    return None


def _list_apps(home: Path) -> tuple[tuple[str, ...], str | None]:
    """Directory listing of `<home>/api` — never launched, never probed.

    A missing `api/` is NOT a problem: it is created lazily on the first app
    download, so a freshly-initialised home genuinely has zero apps.
    """
    api = home / _APPS_DIRNAME
    try:
        entries = sorted(child.name for child in api.iterdir() if child.is_dir())
    except FileNotFoundError:
        return (), None
    except NotADirectoryError:
        return (), f"{api} exists but is not a directory — cannot list installed apps"
    except OSError as exc:
        return (), f"{api} unreadable ({type(exc).__name__}) — cannot list installed apps"
    return tuple(entries), None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def detect(env: Mapping[str, str] | None = None) -> Detection:
    """Cheap, side-effect-free presence check. Filesystem reads only."""
    environ: Mapping[str, str] = os.environ if env is None else env
    problems: list[str] = []
    checked: list[str] = []

    for candidate, source, problem in _candidates(environ):
        if problem:
            problems.append(problem)
        if candidate is None:
            continue
        checked.append(f"{candidate} ({source})")
        reason = _validate(candidate)
        if reason is not None:
            problems.append(reason)
            continue
        apps, app_problem = _list_apps(candidate)
        return Detection(
            home=str(candidate),
            source=source,
            apps=apps,
            problems=(app_problem,) if app_problem else (),
            checked=tuple(checked),
        )

    return Detection(
        home=None,
        source=None,
        apps=(),
        problems=tuple(problems),
        checked=tuple(checked),
    )


def health(**kwargs: Any) -> HealthStatus:
    """`ok` only for a fully validated home whose app inventory was readable;
    `degraded` in every other case, naming exactly what was checked.

    A pure detection probe: it reports what the HOST looks like and never
    consults `settings.pinokio_enabled`, which governs only whether the
    engine wires this check in at all (see `register()`).
    """
    detection = detect()

    if detection.home is None:
        if detection.problems:
            why = "; ".join(_one_line(p) for p in detection.problems[:_MAX_PROBLEMS_IN_DETAIL])
        elif detection.checked:
            why = "checked " + ", ".join(_one_line(c) for c in detection.checked)
        else:
            why = (
                "no location to check — none of HOME, XDG_CONFIG_HOME or "
                "PINOKIO_HOME is set to a non-empty value"
            )
        return HealthStatus("degraded", f"{_NAME} not detected ({why})")

    where = f"{_NAME} home {_one_line(detection.home)} (via {detection.source})"
    if detection.problems:
        why = "; ".join(_one_line(p) for p in detection.problems[:_MAX_PROBLEMS_IN_DETAIL])
        return HealthStatus("degraded", f"{where} — {why}")
    return HealthStatus("ok", f"{where}, {len(detection.apps)} app(s) installed")


def register() -> dict[str, Any]:
    """Contribute a `health` check ONLY, and only when the operator's
    permission gate is on AND Pinokio is actually present.

    `pinokio_enabled` defaults True as a PERMISSION gate ("activate if you
    find it"), exactly like `ollama_enabled` — it is never an assertion that
    the operator opted into Pinokio. A partially-present or unreadable
    install does not count as present, so it contributes nothing; a present
    install whose app inventory could not be read DOES register, because the
    wired-in health check is how the operator learns it is degraded.
    """
    from hivepilot.config import settings

    if not settings.pinokio_enabled:
        return {}
    if not detect().present:
        return {}
    return {"health": {_NAME: health}}
