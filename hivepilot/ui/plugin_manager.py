"""TUI for browsing/inspecting loaded HivePilot plugins, with an
enable/disable toggle.

v1 (see docs/v4/PLUGINS.md, roadmap Phase 26a) was browse + inspect only.
Sprint 5 (Phase 26b) adds a `space` binding that flips the highlighted
plugin's presence in `settings.plugins_disabled` and persists that change —
see `persist_plugins_disabled` below. Toggling does NOT live-unregister a
plugin's runners/notifiers/hooks: `hivepilot.plugins.PluginManager` only
scans/registers once, at construction, so a toggle takes effect the next
time the process starts (live hot-reload is out of scope for this sprint).

Modeled directly on `hivepilot.ui.dashboard.RunDashboard` (same `App` shape:
`compose()`/`BINDINGS`/`Header`+`Footer`/`DataTable`, same unconditional
top-of-module `textual` import — the optional-dependency guard lives at the
CLI-command boundary, exactly like the `dashboard` command in
`hivepilot/cli.py`, not inside this module).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from textual.app import App, ComposeResult
from textual.coordinate import Coordinate
from textual.widgets import DataTable, Footer, Header, Static

from hivepilot.orchestrator import Orchestrator
from hivepilot.plugins import PluginRecord

# Re-exported for backwards compatibility: `_ENV_KEY` isn't referenced
# directly in this module, `persist_plugins_disabled` is (toggle_selected,
# below) -- both are kept importable from here so existing
# `monkeypatch.setattr(plugin_manager, "persist_plugins_disabled", ...)`
# call sites (tests/test_plugin_manager_tui.py) keep working.
from hivepilot.ui.plugin_persist import _ENV_KEY, persist_plugins_disabled  # noqa: F401

PLUGIN_COLUMNS = ("Name", "Source", "Status", "Type(s)", "Detail")

# Index of the "Status" column within PLUGIN_COLUMNS / each plugin_rows() row.
_STATUS_COLUMN_INDEX = 2

# Kept in the display order the Type/Detail columns render capabilities in.
# `secrets` / `panels` were added when the secrets-backend and Mirador-panel
# plugin types landed — this Textual screen originally only knew runners/
# notifiers/hooks, so a secrets-backend plugin (infisical/onepassword) or a
# panel-contributing plugin (sample) rendered as "unknown (see aggregate)".
_CAPABILITY_KINDS = ("runners", "notifiers", "hooks", "secrets", "panels")
_CAPABILITY_LABELS = {
    "runners": "runner",
    "notifiers": "notifier",
    "hooks": "hook",
    "secrets": "secrets",
    "panels": "panel",
}


def _plugin_module_hint(record: PluginRecord) -> str | None:
    """Best-effort module name a plugin's contributed objects would carry.

    Only used as a FALLBACK when `record.contributions` is empty — see
    `plugin_capabilities` below, which now prefers the real per-plugin
    attribution `PluginManager` records at registration time.

    Mirrors how `hivepilot/plugins.py` loads each source:
    - local-file: `_scan_local_plugins` loads via
      `importlib.util.spec_from_file_location(f"hivepilot_plugin_{file.stem}", file)`
      — so any class/function defined in that file reports that module name.
    - entry-point / explicit-entry: `record.location` is
      `"<module>:<attr> (<dist>==<version>)"` (entry-point, see
      `load_entry_point_plugins`) or plain `"<module>:<attr>"`
      (explicit-entry, see `PluginManager.__init__` — the `settings.
      plugins_entry` pin) — the module before `:` is the hint either way.

    Returns None when no reliable hint can be derived.
    """
    if record.source == "local-file":
        stem = Path(record.location).stem
        if not stem:
            return None
        return f"hivepilot_plugin_{stem}"
    if record.source in ("entry-point", "explicit-entry"):
        value = record.location.split(" (", 1)[0]
        module = value.split(":", 1)[0].strip()
        return module or None
    return None


def _module_matches(obj: Any, hint: str) -> bool:
    module = getattr(obj, "__module__", "") or ""
    return module == hint or module.startswith(hint + ".")


def plugin_capabilities(
    record: PluginRecord,
    runner_map: dict[str, Any],
    notifier_map: dict[str, Any],
    hooks: dict[str, list[Any]],
    secrets_map: dict[str, Any] | None = None,
    panels: dict[str, Any] | None = None,
) -> dict[str, list[str]]:
    """Per-plugin attribution for the TUI's Type/Detail columns.

    Phase 26a: `PluginManager` now records real per-plugin attribution on
    `PluginRecord.contributions` (`hivepilot/plugins.py`) — exactly which
    runner/notifier/secrets-backend/panel/skill names and lifecycle-hook
    names THIS plugin contributed, collision-rollback-aware. When
    `record.contributions` is non-empty, it is used directly — no guessing.

    Only when it's empty (e.g. a `PluginRecord` constructed by hand in a
    test/fixture, predating this attribution, or from a source this function
    can't otherwise resolve) does this fall back to the original best-effort
    cross-reference against the process-global RUNNER_MAP / NOTIFIER_MAP /
    SECRETS_MAP / PluginManager.hooks / .panels, by matching `__module__`
    against the hint derived from the plugin's own source/location.

    Secrets backends are registered as INSTANCES, so their module is read off
    the instance's class (`instance.__module__` resolves to the class attr) —
    the same cross-reference the CLI `plugins list` performs against SECRETS_MAP.
    Panels are matched by their `fetch` callable's module.

    When neither the real attribution nor the fallback hint can be derived,
    or nothing matches, every list comes back empty and the caller should
    show the "unknown (see aggregate)" fallback.
    """
    if record.contributions:
        return {kind: sorted(record.contributions.get(kind, [])) for kind in _CAPABILITY_KINDS}

    caps: dict[str, list[str]] = {kind: [] for kind in _CAPABILITY_KINDS}
    hint = _plugin_module_hint(record)
    if not hint:
        return caps

    for kind, cls in runner_map.items():
        if _module_matches(cls, hint):
            caps["runners"].append(kind)
    for name, fn in notifier_map.items():
        if _module_matches(fn, hint):
            caps["notifiers"].append(name)
    for hook_name, callables in hooks.items():
        if any(_module_matches(fn, hint) for fn in callables):
            caps["hooks"].append(hook_name)
    for secret_name, backend in (secrets_map or {}).items():
        if _module_matches(backend, hint):
            caps["secrets"].append(secret_name)
    for panel_name, spec in (panels or {}).items():
        fetch = spec.get("fetch") if isinstance(spec, dict) else None
        if fetch is not None and _module_matches(fetch, hint):
            caps["panels"].append(panel_name)

    for kind in _CAPABILITY_KINDS:
        caps[kind].sort()
    return caps


def _format_type_label(caps: dict[str, list[str]]) -> str:
    labels = [_CAPABILITY_LABELS[key] for key in _CAPABILITY_KINDS if caps.get(key)]
    return ", ".join(labels) if labels else "unknown (see aggregate)"


def _format_detail(caps: dict[str, list[str]]) -> str:
    parts = [f"{kind}=" + ",".join(caps[kind]) for kind in _CAPABILITY_KINDS if caps.get(kind)]
    return "; ".join(parts) if parts else "no attributable capabilities (see aggregate detail)"


def plugin_rows(
    loaded: list[PluginRecord],
    runner_map: dict[str, Any],
    notifier_map: dict[str, Any],
    hooks: dict[str, list[Any]],
    disabled: set[str] | None = None,
    secrets_map: dict[str, Any] | None = None,
    panels: dict[str, Any] | None = None,
) -> list[tuple[str, str, str, str, str]]:
    """Build (name, source, status, type(s), detail) rows for the Loaded
    Plugins table.

    `disabled` is an optional set of plugin names to show as "disabled" —
    used by the TUI to reflect an in-session `space` toggle immediately
    (see `PluginManagerApp.action_toggle_plugin`). `loaded` itself only ever
    contains plugins `PluginManager` actually loaded (i.e. NOT already in
    `settings.plugins_disabled` at construction time), so every row defaults
    to "enabled" unless explicitly marked otherwise via `disabled`.

    `secrets_map` / `panels` are cross-referenced the same way runners/
    notifiers/hooks are (see `plugin_capabilities`) so secrets-backend and
    panel-contributing plugins render a real Type/Detail instead of the
    "unknown (see aggregate)" fallback.
    """
    disabled = disabled or set()
    rows: list[tuple[str, str, str, str, str]] = []
    for record in loaded:
        caps = plugin_capabilities(
            record, runner_map, notifier_map, hooks, secrets_map=secrets_map, panels=panels
        )
        status = "disabled" if record.name in disabled else "enabled"
        rows.append(
            (record.name, record.source, status, _format_type_label(caps), _format_detail(caps))
        )
    return rows


class PluginManagerApp(App):
    """Interactive browser/inspector for loaded plugins, with an
    enable/disable toggle (`space`).

    Pass `loaded`/`runner_map`/`notifier_map`/`hooks` to inject plugin data
    for testing; when omitted, data is read from a fresh `Orchestrator()`
    (`orchestrator.plugins.loaded`, `hivepilot.registry.RUNNER_MAP`,
    `hivepilot.services.notification_service.NOTIFIER_MAP`,
    `orchestrator.plugins.hooks`).
    """

    CSS = """
    #plugins {
        height: 80%;
    }
    #details {
        height: 20%;
    }
    """

    BINDINGS = [
        ("r", "refresh", "Refresh"),
        ("q", "quit", "Quit"),
        ("enter", "details", "Details"),
        # Deliberately "toggle_plugin", not "toggle" — see
        # action_toggle_plugin's docstring: "toggle" would resolve to
        # textual's own built-in DOMNode.action_toggle(attribute_name),
        # a different, incompatible action.
        ("space", "toggle_plugin", "Enable/Disable"),
    ]

    def __init__(
        self,
        *,
        loaded: list[PluginRecord] | None = None,
        runner_map: dict[str, Any] | None = None,
        notifier_map: dict[str, Any] | None = None,
        hooks: dict[str, list[Any]] | None = None,
        health: dict[str, Any] | None = None,
        secrets_map: dict[str, Any] | None = None,
        panels: dict[str, Any] | None = None,
    ) -> None:
        super().__init__()
        self._loaded = loaded
        self._runner_map = runner_map
        self._notifier_map = notifier_map
        self._hooks = hooks
        # Injectable for testing, same shape as the other maps. When omitted
        # (real usage), resolved from a fresh `Orchestrator()` in `_load_data`
        # (SECRETS_MAP / orchestrator.plugins.panels), same as the rest — so
        # secrets-backend and panel plugins get a real Type/Detail instead of
        # "unknown (see aggregate)".
        self._secrets_map = secrets_map
        self._panels = panels
        # Injectable for testing, same shape as the other four — a mapping of
        # health-check name -> HealthStatus(-like), as returned by
        # `PluginManager.check_all()`. When omitted (real usage), resolved
        # from a fresh `Orchestrator()` in `_load_data`, same as the rest.
        self._health_override = health
        self._rows: list[tuple[str, str, str, str, str]] = []
        # name -> HealthStatus(-like), resolved by the most recent
        # `refresh_plugins()` call — surfaced in the details pane (see
        # `show_details`) by matching a row's plugin name against this dict.
        # Example plugins register their health check under the SAME name as
        # the plugin file stem (== PluginRecord.name for local-file plugins),
        # so a direct name lookup is the right join key here — same
        # attribution limitation `plugin_capabilities` documents elsewhere in
        # this module (best-effort, not a guaranteed join).
        self._health: dict[str, Any] = {}

    def compose(self) -> ComposeResult:
        yield Header()
        self.plugins_table: DataTable = DataTable(id="plugins")
        self.plugins_table.add_columns(*PLUGIN_COLUMNS)
        self.details: Static = Static("Select a plugin and press Enter for details.", id="details")
        yield self.plugins_table
        yield self.details
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_plugins()
        self.plugins_table.cursor_type = "row"
        self.plugins_table.focus()

    def action_refresh(self) -> None:
        self.refresh_plugins()

    def action_details(self) -> None:
        self.show_details()

    def action_toggle_plugin(self) -> None:
        # NOT named `action_toggle` — textual's own `DOMNode.action_toggle
        # (attribute_name: str)` is a built-in generic action (toggles a
        # reactive attribute by name) with an incompatible signature;
        # shadowing it with a zero-arg override is a Liskov violation mypy
        # flags once textual's stubs are resolvable, and needlessly hides a
        # framework primitive future bindings elsewhere in this app might
        # want to use.
        self.toggle_selected()

    def _load_data(
        self,
    ) -> tuple[
        list[PluginRecord],
        dict[str, Any],
        dict[str, Any],
        dict[str, list[Any]],
        dict[str, Any],
        dict[str, Any],
        dict[str, Any],
    ]:
        if self._loaded is not None:
            return (
                self._loaded,
                self._runner_map or {},
                self._notifier_map or {},
                self._hooks or {},
                self._health_override or {},
                self._secrets_map or {},
                self._panels or {},
            )
        from hivepilot.registry import RUNNER_MAP, SECRETS_MAP
        from hivepilot.services.notification_service import NOTIFIER_MAP

        orchestrator = Orchestrator()
        return (
            orchestrator.plugins.loaded,
            RUNNER_MAP,
            NOTIFIER_MAP,
            orchestrator.plugins.hooks,
            orchestrator.plugins.check_all(),
            SECRETS_MAP,
            orchestrator.plugins.panels,
        )

    def refresh_plugins(self) -> None:
        loaded, runner_map, notifier_map, hooks, health, secrets_map, panels = self._load_data()
        self._health = health
        self._rows = plugin_rows(
            loaded, runner_map, notifier_map, hooks, secrets_map=secrets_map, panels=panels
        )
        self.plugins_table.clear()
        for name, source, status, type_label, detail in self._rows:
            self.plugins_table.add_row(name, source, status, type_label, detail)
        if self._rows:
            self.plugins_table.move_cursor(row=0, column=0)
        self.show_details()

    def _selected_row_index(self) -> int | None:
        if not self._rows:
            return None
        row_index = self.plugins_table.cursor_row
        if row_index is None or not (0 <= row_index < len(self._rows)):
            row_index = 0
        return row_index

    def show_details(self) -> None:
        row_index = self._selected_row_index()
        if row_index is None:
            self.details.update("No plugins loaded.")
            return
        name, source, status, type_label, detail = self._rows[row_index]
        text = f"{name} ({source}, {status}) — {type_label}\n{detail}"
        health = self._health.get(name)
        if health is not None:
            text += f"\nHealth: {health.status} — {health.detail}"
        self.details.update(text)

    def toggle_selected(self) -> None:
        """Flip the highlighted plugin's presence in `settings.plugins_disabled`,
        persist it via `persist_plugins_disabled`, and update its row's status
        cell in place. Does NOT live-unregister the plugin — see module
        docstring: this only takes effect on the next start."""
        row_index = self._selected_row_index()
        if row_index is None:
            return

        from hivepilot.config import settings

        name = self._rows[row_index][0]
        current = set(settings.plugins_disabled)
        if name in current:
            current.discard(name)
            now_disabled = False
        else:
            current.add(name)
            now_disabled = True

        updated = sorted(current)
        # Persist to .env first; only mutate in-memory settings once the write
        # succeeds, so a failing persist can't diverge settings.plugins_disabled
        # from .env (mirrors toggle_plugin_endpoint in api_service.py).
        persist_plugins_disabled(updated)
        settings.plugins_disabled = updated

        old_row = self._rows[row_index]
        new_status = "disabled" if now_disabled else "enabled"
        self._rows[row_index] = (
            old_row[0],
            old_row[1],
            new_status,
            old_row[3],
            old_row[4],
        )
        self.plugins_table.update_cell_at(Coordinate(row_index, _STATUS_COLUMN_INDEX), new_status)

        self.details.update(
            f"{name} {new_status}. Persisted to plugins_disabled — "
            "applies on next start (plugins load once at startup; "
            "no live reload)."
        )

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:  # type: ignore[override]
        # `event.data_table` (not `.table`) is the attribute on textual's
        # DataTable.RowHighlighted message — see hivepilot/ui/dashboard.py
        # for the identical (pre-existing, out-of-boundary-for-this-sprint)
        # `event.table` usage that hits the same AttributeError on
        # textual>=0.6x; logged, not fixed here (outside this sprint's file
        # boundaries).
        if event.data_table.id != "plugins":
            return
        self.show_details()
