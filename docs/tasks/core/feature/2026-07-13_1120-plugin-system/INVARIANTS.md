# INVARIANTS — Plugin System (v1: Runners + Notifiers + Step Hooks)

Machine-verifiable contracts for this PRD. Cascades from (and does not yet exist at) the project root — see `~/.claude/skills/plan/sprint-extraction-protocol.md` Step 4 for the cascading rule; this file is scoped to the plugin-system PRD directory for now.

## Runner registry is the single source of truth

- **Owner:** `hivepilot/registry.py` (`RUNNER_MAP` dict + `RunnerRegistry.register()`)
- **Preconditions:** Any code that wants a `kind` resolvable at runtime — built-in or plugin — MUST call `RunnerRegistry.register(kind, cls)` before that kind is looked up (built-ins at module import time; plugins at `PluginManager.__init__` time, which happens during `Orchestrator._load()`, before any pipeline runs).
- **Postconditions:** After `RunnerRegistry.register(kind, cls)` returns without raising, `RUNNER_MAP[kind] is cls` and `kind in RunnerRegistry.known_kinds()`.
- **Invariants:** Every `RunnerKind` string resolvable at runtime (via `RunnerRegistry.get_runner`/`execute_definition`/`capture_definition`) is a key in `RUNNER_MAP`. There is no second, parallel runner-kind lookup table anywhere in the codebase.
- **Verify:** `python -c "from hivepilot.registry import RUNNER_MAP, RunnerRegistry; assert set(RUNNER_MAP) == set(RunnerRegistry.known_kinds())"`
- **Fix:** If a new lookup table appears (e.g. a second dict in `orchestrator.py` or `cli.py`), replace it with a call into `RunnerRegistry`/`RUNNER_MAP` — do not duplicate the mapping.

## Built-in non-regression (runners)

- **Owner:** `hivepilot/registry.py`
- **Preconditions:** None (built-ins self-register unconditionally at import).
- **Postconditions:** All 11 pre-existing runner kinds resolve to their original classes after any plugin loading.
- **Invariants:** `{claude, shell, langchain, internal, codex, gemini, opencode, ollama, container, cursor, vibe} ⊆ RUNNER_MAP.keys()`, always, in every process — plugin loading may only *add* keys, never remove or silently replace these 11.
- **Verify:** `python -c "from hivepilot.registry import RUNNER_MAP; ks={'claude','shell','langchain','internal','codex','gemini','opencode','ollama','container','cursor','vibe'}; assert ks <= set(RUNNER_MAP), sorted(ks - set(RUNNER_MAP))"`
- **Fix:** If a built-in kind is missing, check that its runner module still calls `RunnerRegistry.register(kind, cls)` at import time in `hivepilot/registry.py` and that nothing (a plugin or a code change) removed the entry.

## Built-in non-regression (notifiers)

- **Owner:** `hivepilot/services/notification_service.py`
- **Preconditions:** None (built-ins self-register unconditionally at import).
- **Postconditions:** All 3 pre-existing notifier channels resolve to their original functions after any plugin loading.
- **Invariants:** `{slack, discord, telegram} ⊆ NOTIFIER_MAP.keys()`, always, in every process.
- **Verify:** `python -c "from hivepilot.services.notification_service import NOTIFIER_MAP; ks={'slack','discord','telegram'}; assert ks <= set(NOTIFIER_MAP), sorted(ks - set(NOTIFIER_MAP))"`
- **Fix:** Check `_send_slack`/`_send_discord`/`_send_telegram` still call `NotifierRegistry.register(name, fn)` at module import time.

## No silent kind collision

- **Owner:** `hivepilot/registry.py` (runners) and `hivepilot/services/notification_service.py` (notifiers)
- **Preconditions:** A caller registering a kind/name that already maps to a *different* callable/class must not pass `override=True` unless it intends to replace it deliberately.
- **Postconditions:** `RunnerRegistry.register(kind, cls)` / `NotifierRegistry.register(name, fn)` raises `RunnerKindCollisionError` / `NotifierKindCollisionError` (both subclasses of `RuntimeError`) when `kind`/`name` is already registered to a different class/callable and `override` is not `True`. The exception message names the kind/name and both contending sources where determinable.
- **Invariants:** Two plugins (or a plugin and a built-in) can never end up silently sharing one kind/name where only one's implementation actually runs — the load step aborts with a raised, named error instead.
- **Verify:** `pytest tests/test_runner_registry_open.py -k collision -q && pytest tests/test_notifier_registry.py -k collision -q`
- **Fix:** If collisions stop raising, check that `register()` still compares `existing is not cls`/`existing is not fn` before writing, and that the comparison isn't accidentally short-circuited by a truthy-but-wrong check (e.g. `if kind not in MAP` alone, which would allow same-kind-different-class overwrites).

## Plugin trust boundary

- **Owner:** `hivepilot/plugins.py`
- **Preconditions:** None — this must hold unconditionally, for every code path in the plugin loader.
- **Postconditions:** Plugin discovery only ever reads from (a) the local filesystem (`plugins/*.py` under `settings.base_dir` or `config_repo`, or an explicit `module:attr` string already present in local config) or (b) `importlib.metadata.entry_points(group="hivepilot.plugins")`, which resolves against packages already installed in the current Python environment. No plugin code is ever fetched from a URL, git remote, or artifact registry at runtime.
- **Invariants:** `hivepilot/plugins.py` contains no import of `urllib`, `requests`, `httpx`, or any other HTTP client.
- **Verify:** `! grep -nE "urllib|requests\.(get|post)|httpx" hivepilot/plugins.py`
- **Fix:** If a network-fetch import appears in the loader, remove it — network-sourced plugin code is an explicit Non-Goal (see spec.md Section 7) and a named Danger (spec.md Section 2).

## Plugin load errors never crash the CLI

- **Owner:** `hivepilot/plugins.py`
- **Preconditions:** A plugin's module import, entry-point `.load()`, or `register()` invocation may raise any exception.
- **Postconditions:** Every per-plugin load/invoke step is wrapped in `try/except Exception`; a failure is logged (`logger.warning` or higher, including plugin name/location and the exception) and that one plugin is skipped — `PluginManager()` construction itself never raises because one plugin was broken, and neither does `hivepilot` CLI startup.
- **Invariants:** The number of `try/except Exception` guards around plugin load/invoke steps in `hivepilot/plugins.py` is >= the number of distinct load paths (local-file scan, explicit `module:attr` entry, entry-point discovery, entry-point `.load()`, `register()` call).
- **Verify:** `pytest tests/test_plugins.py tests/test_plugin_loading_mechanisms.py -k broken -q`
- **Fix:** Wrap the failing step in `try/except Exception as exc: logger.warning(...); continue` (or equivalent), matching the existing pattern already used for the local-file scan (`hivepilot/plugins.py`, current `load_plugins()`).
