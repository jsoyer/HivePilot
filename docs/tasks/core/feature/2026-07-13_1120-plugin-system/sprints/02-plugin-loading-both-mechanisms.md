# Sprint 2: Plugin loading — both mechanisms

## Meta

- **PRD:** `../spec.md` (do not need to read it — this spec is self-contained)
- **Sprint:** 2 of 4
- **Depends on:** Sprint 1 (needs `RunnerRegistry.register()`, `RunnerKindCollisionError`, `KNOWN_RUNNER_KINDS` from `hivepilot/registry.py` / `hivepilot/models.py`)
- **Batch:** 2 (sequential — runs after Sprint 1 merges, before Sprint 3/4)
- **Model:** sonnet
- **Estimated effort:** L

## Objective

Extend `hivepilot/plugins.py`'s `register()` contract so a plugin can contribute new runner kinds and (in preparation for Sprint 3) notifier channels, wire discovered runners into `RunnerRegistry` (from Sprint 1), add a second discovery mechanism — Python entry points, group `hivepilot.plugins` — alongside the existing local-file scan, track where each loaded plugin came from, and make every load step fail closed and loud (never silent, never CLI-crashing) on a broken plugin or a kind collision.

## Why this exists (context, no PRD lookup needed)

Today, `hivepilot/plugins.py` (57 lines) has:

```python
def load_plugins(entry: str | None = None) -> list[Callable[..., Any]]:
    """Load plugin callables from a module path or from `plugins/` directory."""
    plugins: list[Callable[..., Any]] = []
    if entry:
        module_name, attr = entry.split(":") if ":" in entry else (entry, "register")
        module = import_module(module_name)
        plugin_callable = getattr(module, attr)
        plugins.append(plugin_callable)
    else:
        plugin_dir = settings.base_dir / "plugins"
        if plugin_dir.exists():
            import importlib.util
            for file in sorted(plugin_dir.glob("*.py")):
                if file.stem.startswith("_"):
                    continue
                try:
                    spec = importlib.util.spec_from_file_location(f"hivepilot_plugin_{file.stem}", file)
                    if spec and spec.loader:
                        module = importlib.util.module_from_spec(spec)
                        spec.loader.exec_module(module)
                        if hasattr(module, "register"):
                            plugins.append(module.register)
                except Exception as exc:  # noqa: BLE001 — a broken plugin must not kill a run
                    logger.warning("plugins.load_failed", file=str(file), error=str(exc))
    logger.info("plugins.loaded", count=len(plugins))
    return plugins


class PluginManager:
    def __init__(self) -> None:
        self.plugins = load_plugins(settings.__dict__.get("plugins_entry"))
        self.hooks: dict[str, list[Any]] = {"before_step": [], "after_step": []}
        for plugin in self.plugins:
            hooks = plugin()
            for hook_name, hook_callable in hooks.items():
                self.hooks.setdefault(hook_name, []).append(hook_callable)

    def run_hook(self, hook_name: str, **kwargs: Any) -> None:
        for hook in self.hooks.get(hook_name, []):
            hook(**kwargs)
```

Two important existing facts, verified by reading `tests/test_plugins.py`:

1. `load_plugins()` is called by existing tests and asserted to return `list[Callable]` — e.g. `assert callable(loaded[0])`. **This return contract must not change** — do not make `load_plugins()` return tuples or richer objects; those existing tests would break.
2. `PluginManager.__init__`'s hook-accumulation loop (`for hook_name, hook_callable in hooks.items(): self.hooks.setdefault(hook_name, []).append(hook_callable)`) is **already agnostic to the hook-name key** — any key a plugin's `register()` returns gets appended as a list entry under that key. This matters for this sprint: you do not need to special-case `"before_step"`/`"after_step"` vs. new keys in that loop — but `"runners"` and `"notifiers"` are NOT meant to accumulate as hook-callable lists (a runner is a `dict[str, type[BaseRunner]]`, not a callable to invoke per-step) — they must be popped out and routed to their registries *before* the generic accumulation loop runs, or excluded from it.

## File Boundaries

### Creates (new files)

- `tests/test_plugin_loading_mechanisms.py` — new tests covering local-file plugin runner registration, entry-point plugin runner registration (via a mocked `importlib.metadata.entry_points`), collision handling across both mechanisms, and broken-plugin isolation for both mechanisms.
- `tests/fixtures/entry_point_plugin.py` — a plain (non-test-collected — no `test_` prefix, so pytest ignores it) fixture module with a `register()` function returning `{"runners": {"fixture-kind": <a minimal fake BaseRunner class defined in the same file>}}`. Used by `tests/test_plugin_loading_mechanisms.py` via a monkeypatched `importlib.metadata.EntryPoint`/`entry_points()` that points at this module — this avoids needing to actually `pip install` a second package for the test while still exercising the real entry-point-loading code path (`ep.load()` against a real importable module).

### Modifies (can touch)

- `hivepilot/plugins.py` — the main change. See Tasks below for the exact shape.
- `hivepilot/config.py` — add `plugins_enabled: bool = True` to the `Settings` class (near the other simple boolean settings, e.g. next to `auditor_auto` or similar — follow the existing style/grouping in the file). This is the master on/off switch for **both** loading mechanisms.
- `pyproject.toml` — add a commented documentation stanza near `[project.scripts]` showing third-party packages the entry-point contract they should declare, e.g.:
  ```toml
  # Third-party plugin packages declare, in their OWN pyproject.toml:
  # [project.entry-points."hivepilot.plugins"]
  # my_plugin = "my_package:register"
  ```
  This is documentation only — HivePilot's own `pyproject.toml` does not need to declare anything under `hivepilot.plugins` itself (it is not a plugin of itself). Do not add a real dependency; only the comment.

### Read-Only (reference but do NOT modify)

- `hivepilot/registry.py` — `RunnerRegistry.register()`, `RunnerKindCollisionError`, `RUNNER_MAP` (from Sprint 1). Import and call, do not modify.
- `hivepilot/models.py` — `KNOWN_RUNNER_KINDS`, `RunnerKind` (from Sprint 1). Reference only.
- `hivepilot/runners/base.py` — `BaseRunner` Protocol, for the fixture plugin's fake runner class shape.
- `tests/test_plugins.py` — existing tests; must continue to pass unmodified (they assert `load_plugins()`'s current return contract). Do not edit this file.

### Shared Contracts (consume from prior sprints or PRD)

- **Consumes (Sprint 1):** `RunnerRegistry.register(kind: str, cls: type[BaseRunner], *, override: bool = False) -> None`, `RunnerKindCollisionError`, `RUNNER_MAP`, `KNOWN_RUNNER_KINDS`.
- **Produces (for Sprint 3 & 4):** the extended `register()` contract (`runners`/`notifiers`/hook keys), `PLUGIN_ENTRY_POINT_GROUP = "hivepilot.plugins"`, `PluginRecord` dataclass, `PluginManager.loaded: list[PluginRecord]`, `PluginManager.declared_notifiers: dict[str, Callable[[str], None]]` (collected but not yet wired anywhere — Sprint 3 wires it into `NotifierRegistry`), `settings.plugins_enabled`.

### Consumed Invariants (from INVARIANTS.md)

- **Runner registry is the single source of truth** — this sprint's runner wiring must go through `RunnerRegistry.register()`, never write `RUNNER_MAP` directly.
- **No silent kind collision** — a plugin runner kind colliding with a built-in or another plugin must raise `RunnerKindCollisionError` (propagate the one from `RunnerRegistry.register`, do not swallow it).
- **Plugin trust boundary** — verify after your changes: `! grep -nE "urllib|requests\.(get|post)|httpx" hivepilot/plugins.py`
- **Plugin load errors never crash the CLI** — verify after your changes: `pytest tests/test_plugins.py tests/test_plugin_loading_mechanisms.py -k broken -q`

## Tasks

- [ ] In `hivepilot/config.py`, add `plugins_enabled: bool = True` to `Settings`.
- [ ] In `hivepilot/plugins.py`, add near the top: `PLUGIN_ENTRY_POINT_GROUP = "hivepilot.plugins"` and a `@dataclass(slots=True) class PluginRecord: name: str; source: str; location: str` (import `dataclass` from `dataclasses`).
- [ ] Refactor the local-file scanning logic out of `load_plugins()` into a new internal function `_scan_local_plugins() -> list[tuple[Callable[..., Any], PluginRecord]]` that does exactly what the current `else` branch of `load_plugins()` does (same `plugin_dir = settings.base_dir / "plugins"`, same `sorted(plugin_dir.glob("*.py"))`, same `spec_from_file_location`/`exec_module`/`hasattr(module, "register")` logic, same broad `try/except Exception` per file with `logger.warning("plugins.load_failed", ...)`), but each successfully-loaded plugin is appended as `(module.register, PluginRecord(name=file.stem, source="local-file", location=str(file)))` instead of just the callable. Guard the whole scan with `if not settings.plugins_enabled: return []`.
- [ ] Rewrite `load_plugins(entry: str | None = None) -> list[Callable[..., Any]]` to preserve its **exact existing external contract** (a flat `list[Callable]`, explicit-`entry` behavior unchanged) by delegating to `_scan_local_plugins()` for the no-`entry` branch: `return [fn for fn, _ in _scan_local_plugins()]`. The explicit-`entry` branch (`import_module` + `getattr(module, attr)`) is unchanged — do not wrap it in `_scan_local_plugins`; it is a different mechanism (explicit single-module load, e.g. for testing or a pinned plugin), keep it as-is including its current lack of a `plugins_enabled` guard (it's an explicit opt-in, different trust posture — do not add the guard there unless you also update the one call site in `PluginManager.__init__` consistently; simplest and safest: leave the explicit-entry branch exactly as it is today).
- [ ] Add `load_entry_point_plugins() -> list[tuple[Callable[..., Any], PluginRecord]]`: if `not settings.plugins_enabled`, return `[]`. Otherwise, `import importlib.metadata as metadata`, call `metadata.entry_points(group=PLUGIN_ENTRY_POINT_GROUP)` inside a `try/except Exception` (log `logger.warning("plugins.entry_points_scan_failed", error=str(exc))` and return `[]` on failure — a broken environment must not kill startup). For each entry point `ep`, wrap `ep.load()` in its own `try/except Exception` (log `logger.warning("plugins.entry_point_load_failed", entry_point=ep.name, error=str(exc))` and `continue` — one broken plugin must not skip the rest). On success, build a `location` string from `ep.dist` if available (`f"{ep.value} ({ep.dist.name}=={ep.dist.version})"`, fall back to just `ep.value` if `ep.dist` is `None`), and append `(fn, PluginRecord(name=ep.name, source="entry-point", location=location))`.
- [ ] Rewrite `PluginManager.__init__` to: (1) collect `local = _scan_local_plugins()`; if `settings.__dict__.get("plugins_entry")` is set, also call the existing explicit-entry `load_plugins(entry=...)` path and treat each result as `(fn, PluginRecord(name=entry_str, source="local-file", location=entry_str))` — merge into `local`. (2) collect `entry_point = load_entry_point_plugins()`. (3) `self.loaded: list[PluginRecord] = []`; `self.hooks: dict[str, list[Any]] = {"before_step": [], "after_step": []}` (unchanged initial keys); `self.declared_notifiers: dict[str, Callable[[str], None]] = {}` (new — collected here, wired to a real registry in Sprint 3). (4) Iterate `local` then `entry_point` (local-file precedence first, per the PRD's fixed decision) — for each `(register_fn, record)`: call `register_fn()` inside a `try/except Exception` (log `logger.warning("plugins.register_failed", plugin=record.name, source=record.source, error=str(exc))`, `continue` on failure — do not append to `self.loaded`, do not let it raise past this point); on success, pop `"runners"` (a `dict[str, type[BaseRunner]]`) if present and call `RunnerRegistry.register(kind, cls)` for each entry — **let `RunnerKindCollisionError` propagate** (do not catch it here — a collision is a hard stop per the PRD's Uncertainty Policy, unlike an isolated broken plugin); pop `"notifiers"` (a `dict[str, Callable[[str], None]]`) if present and merge into `self.declared_notifiers` (for now, just `dict.update` — Sprint 3 adds the real collision-checked registration); for every remaining key in the returned dict, keep the existing generic behavior: `self.hooks.setdefault(hook_name, []).append(hook_callable)`. Append `record` to `self.loaded`. (5) Keep `self.plugins` for back-compat if anything external references it (check via `grep -rn "\.plugins\b" hivepilot/ tests/` for read access to `PluginManager().plugins` outside `plugins.py` itself before deciding whether to keep it — if nothing reads it, you may still keep it for the existing `tests/test_plugins.py::test_plugin_manager_has_hooks_attribute`-style tests, which only check `.hooks`, not `.plugins`, so removing it is likely safe, but keeping `self.plugins = [fn for fn, _ in local + entry_point]` costs nothing and preserves maximum compatibility — prefer keeping it).
- [ ] Keep `run_hook(self, hook_name: str, **kwargs: Any) -> None` unchanged.
- [ ] Add `tests/fixtures/entry_point_plugin.py` with a minimal fake runner class (satisfying `BaseRunner`'s `__init__(definition, settings)` / `run(payload)` shape — a trivial no-op `run` is fine) and a `register()` returning `{"runners": {"fixture-kind": FixtureRunner}}`.
- [ ] Add `tests/test_plugin_loading_mechanisms.py` covering: (a) a local-file plugin (write a temp `plugins/*.py` via `tmp_path`, monkeypatch `settings.base_dir`) whose `register()` returns `{"runners": {"local-fixture": SomeCls}}` ends up in `RUNNER_MAP["local-fixture"]` after `PluginManager()` construction; (b) an entry-point plugin — monkeypatch `importlib.metadata.entry_points` (or `hivepilot.plugins.metadata.entry_points` depending on how you imported it) to return a list/tuple containing a fake `EntryPoint`-like object whose `.load()` returns `tests.fixtures.entry_point_plugin.register`, `.name` is e.g. `"fixture-ep"`, `.value` is a dotted path string, `.dist` is `None` or a fake with `.name`/`.version` — assert `RUNNER_MAP["fixture-kind"]` resolves after `PluginManager()` construction; (c) both mechanisms loaded together with no collision — both kinds resolve, `PluginManager().loaded` contains two `PluginRecord`s with `source="local-file"` and `source="entry-point"` respectively; (d) a kind collision between a plugin and a built-in (e.g. a fixture plugin registers `"claude"`) raises `RunnerKindCollisionError` out of `PluginManager()` construction — do not swallow it silently; (e) a broken plugin (register() raises, or the module itself raises on import/exec) is logged and skipped, `PluginManager()` construction still succeeds, and the plugin does not appear in `.loaded`; (f) `settings.plugins_enabled = False` (monkeypatched) means `PluginManager().loaded == []` and no entry-point/local-file scan is attempted (spy/monkeypatch `_scan_local_plugins`/`load_entry_point_plugins` to assert they short-circuit, or simply assert the map is unaffected).

## Acceptance Criteria

- [ ] A local-file plugin registering a new runner kind is resolvable via `RunnerRegistry`/`RUNNER_MAP` after `PluginManager()` construction.
- [ ] An entry-point plugin (loaded via a monkeypatched `importlib.metadata.entry_points`, exercising the real `ep.load()` + `register()` call path against a real importable fixture module) registering a new runner kind is resolvable the same way.
- [ ] Both mechanisms can be loaded together with no collision when their kinds differ.
- [ ] A kind collision (plugin vs. built-in, or plugin vs. plugin, either mechanism) raises `RunnerKindCollisionError` and is not silently absorbed.
- [ ] A broken plugin (raises during import, `.load()`, or `register()` invocation) is logged via `logger.warning` (or higher) and skipped — `PluginManager()` construction does not raise, and the CLI does not crash.
- [ ] `settings.plugins_enabled = False` disables both loading mechanisms.
- [ ] `load_plugins()`'s existing external contract (`list[Callable]`, explicit-`entry` behavior) is unchanged — `tests/test_plugins.py` passes unmodified.
- [ ] `PluginManager.loaded` contains one `PluginRecord` per successfully-loaded plugin with correct `name`/`source`/`location`.
- [ ] `hivepilot/plugins.py` contains no `urllib`/`requests`/`httpx` import.

## Verification

- [ ] Build passes: `python -c "import hivepilot.plugins"`
- [ ] Lint passes: `ruff check hivepilot/plugins.py hivepilot/config.py tests/test_plugin_loading_mechanisms.py tests/fixtures/entry_point_plugin.py`
- [ ] Type-check passes: `mypy hivepilot/plugins.py hivepilot/config.py`
- [ ] Sprint-specific tests pass: `pytest tests/test_plugin_loading_mechanisms.py tests/test_plugins.py -q`
- [ ] No regression: `pytest -q` (full suite) shows zero new failures

> **Note:** Dev server smoke test and content verification are handled by the orchestrator after merge — do not run in the sprint-executor. Sprint-executors do static verification only.

## Context

- `requires-python = ">=3.10"` in `pyproject.toml` — `importlib.metadata.entry_points(group=...)` keyword form works natively, no backport package needed.
- Do not build or install a real second pip package for the entry-point test. Monkeypatch `importlib.metadata.entry_points` (or the name it's imported under in `hivepilot/plugins.py` — e.g. if you do `import importlib.metadata as metadata` inside `load_entry_point_plugins`, patch `hivepilot.plugins.metadata.entry_points`) to return fake `EntryPoint`-like objects whose `.load()` resolves to a real, importable function (`tests.fixtures.entry_point_plugin.register`). This exercises the real "load and call register()" code path without any packaging machinery.
- Precedence is local-file-then-entry-point (PRD fixed decision) — implement the merge in that order so a collision message can meaningfully say which came "first."
- Do not implement `NotifierRegistry` in this sprint — `self.declared_notifiers` is just a plain dict merge for now; Sprint 3 introduces the registry and does the actual wiring.
- Do not touch `hivepilot/orchestrator.py` in this sprint — `Orchestrator.__init__`'s existing `self.plugins = PluginManager()` call requires no change; all new behavior lives inside `PluginManager.__init__`.

## Agent Notes (filled during execution)

- Assigned to: [Agent ID / session]
- Started: [timestamp]
- Completed: [timestamp]
- Decisions made: [list with reasoning]
- Assumptions: [list with confidence level]
- Issues found: [list]
