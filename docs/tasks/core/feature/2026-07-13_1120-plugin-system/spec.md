# Plugin System (v1: Runners + Notifiers + Step Hooks): Product Requirements Document

## 1. What & Why

**Problem:** HivePilot's extension points are closed. Runner *kinds* are hardcoded as a `Literal[...]` type (`hivepilot/models.py:8-21`, 12 values) backed by a static `RUNNER_MAP` dict (`hivepilot/registry.py:22-34`, 11 entries — see Context Loaded for the `"api"` kind quirk). Pydantic rejects any `RunnerDefinition.kind` outside those 12 strings, so a new runner *kind* (a new class of executor — e.g. a Bedrock runner, a local llama.cpp runner) cannot be added without editing HivePilot core. Notifiers are similarly closed: `notification_service.send_notification()` is a static `if/elif` over `slack`/`discord`/`telegram` (`hivepilot/services/notification_service.py`). Step hooks exist (`hivepilot/plugins.py`: `before_step`/`after_step`) but there is no way to hook pipeline-level lifecycle events (start/end/error), and no visibility into what is loaded.

**Desired Outcome:** A third party (or a config-repo maintainer) can add a new runner kind, a new notifier channel, or a new lifecycle hook — in a plugin file or a pip package — without touching `hivepilot/` core, with zero regression of the 12 built-in runner kinds and 3 built-in notifiers, and with a fail-closed, no-silent-failure trust model.

**Justification:** The config-edit CLI (PR #118) closed the gap for *tuning* existing runner instances (new name/model/host in `tasks.yaml`). The remaining gap is adding a new runner *kind* or notifier *channel* in code — that requires the registry to be open. This is the natural next increment, sequenced after PR #118.

## 2. Correctness Contract

**Audience:** The HivePilot maintainer, and the coding agent that builds this PRD next via `/plan-build-test`. Output = build-ready specs (this document + sprint files), not running code.
**Failure Definition:** A plugin cannot register a new runner kind or notifier; a built-in runner/notifier regresses; a plugin load error crashes the whole CLI instead of being isolated and reported.
**Danger Definition:** Arbitrary code execution from an untrusted plugin source; a plugin silently overriding/shadowing a built-in kind (collision) with no error.
**Risk Tolerance:** Security is non-negotiable (explicit trust model; no auto-remote-fetch of plugin code). Backward compatibility is non-negotiable (zero regression of the 12 runner kinds + 3 notifiers).

## 3. Context Loaded

- `hivepilot/models.py:8-21` — `RunnerKind = Literal["claude","shell","langchain","internal","codex","gemini","opencode","ollama","api","container","cursor","vibe"]` (12 values). Pydantic enforces this as a closed set at `RunnerDefinition` construction time (`tests/test_models.py:25-27`, `test_runner_definition_rejects_unknown_kind`, currently asserts `ValidationError` for an unknown kind — this test's *expectation* must change under this PRD; see Sprint 1, this is a deliberate, documented change, not a silent weakening).
- `hivepilot/registry.py:22-34` — `RUNNER_MAP` static dict, **11 entries** (not 12 — `"api"` is declared in the `Literal` but has no `RUNNER_MAP` entry; resolving kind=`"api"` already raises `KeyError` today via `RunnerRegistry.get_runner`/`execute_definition`, verified by reading `hivepilot/runners/prompt_cli_runner.py:159` which only uses `"api"` as an internal `mode` option, not a registry kind). This is a pre-existing inconsistency, not introduced by this PRD — Sprint 1 preserves it exactly (see Non-Goals).
- `hivepilot/orchestrator.py:146-152` (`_parse_brain`) — the **only runtime consumer** of `get_args(RunnerKind)`: it treats a debate-brain string's `"prefix:"` as a runner override only if `prefix in set(get_args(RunnerKind))`. Under a `Literal → str` widening, `get_args(str)` returns `()` — this call would **silently stop matching anything**, silently disabling all `runner:model` brain pins. This is the one call site that must be rewritten, not left alone.
- 10 call sites of `cast(RunnerKind, ...)` across `hivepilot/cli.py` and `hivepilot/orchestrator.py` (verified via `grep -rn "cast(RunnerKind" hivepilot/`) — all static casts for type-checking only, no runtime behavior, unaffected by `Literal → str`.
- `hivepilot/runners/base.py` — `BaseRunner` is a `Protocol`: `__init__(definition: RunnerDefinition, settings: Settings)`, `run(payload: RunnerPayload) -> None`, optional `capture(payload) -> str`. This is the contract any plugin runner class must satisfy. `RunnerPayload` is a `@dataclass(slots=True)`: `project_name, project, task_name, step, metadata, secrets`.
- `hivepilot/plugins.py` (current, 57 lines) — `load_plugins(entry=None)` scans `plugins/*.py` by file path (loader independent of `sys.path` — regression-tested by `tests/test_plugins.py::TestLoadPluginsByPath`) or imports an explicit `module:attr` entry, collecting each module's `register()` callable. `PluginManager.__init__` calls each `register()` and merges the returned `dict[str, Callable]` into `self.hooks` (keyed by hook name) — **this accumulation loop is already hook-key-agnostic**: any key in the returned dict becomes a `self.hooks[key]` list. Wired into `orchestrator.py` at `self.plugins.run_hook("before_step", payload=payload)` (~line 1763) and `after_step` (~line 1866).
- `hivepilot/services/notification_service.py:51-65` — `send_notification()` is an `if/elif channel == "slack"/"discord"/"telegram"` dispatch calling `_send_slack`/`_send_discord`/`_send_telegram`. **Not currently unit-tested directly** — the existing `tests/test_notification_service.py` tests target `stream_agent_turn`'s direct `_send_telegram` calls, a separate rich-card streaming code path that does **not** go through `send_notification()`. The Sprint 3 refactor of `send_notification()` therefore cannot break those existing tests (verified: none of them call `send_notification(` directly).
- `pyproject.toml` — `requires-python = ">=3.10"`; `importlib.metadata.entry_points(group=...)` keyword form is native from 3.10, no backport dependency needed. `[project.scripts] hivepilot = "hivepilot.cli:app"` is the only existing entry-point declaration; no `[project.entry-points.*]` group exists yet.
- `hivepilot/cli.py` — Typer app with sub-apps (`gh_app`, `config_app`, `project_app`, `task_app`, `role_app`, `telegram_app`, `caddy_app`, `slack_app`, …) each added via `app.add_typer(sub_app, name=...)`. No `plugins` sub-app exists yet — this is the pattern Sprint 4's `hivepilot plugins list` follows.

## 4. Success Metrics

| Metric | Current | Target | How to Measure |
| --- | --- | --- | --- |
| Runner kinds addable without editing `hivepilot/` core | 0 | 1+ (demonstrated) | A fixture plugin registers a new kind, loaded via local file AND entry-point, executed in a test pipeline |
| Built-in runner kinds resolving | 11/11 (of 12 declared; `api` is a pre-existing gap) | 11/11 (no regression) | `tests/test_registry.py` + Sprint 1 non-regression test |
| Built-in notifiers resolving | 3/3 (untested directly today) | 3/3 (regression-tested) | Sprint 3 test asserts `NOTIFIER_MAP` contains `slack`, `discord`, `telegram` |
| Plugin load errors that crash the CLI | Unknown for entry-points (untested); local-file path already isolates | 0 | Sprint 2/4 tests: a broken plugin (either mechanism) is logged and skipped, CLI continues |
| Kind collisions silently accepted | Unguarded today | 0 (always raises) | Sprint 1 (runners) + Sprint 3 (notifiers) collision tests |

## 5. User Stories

GIVEN a `plugins/my_runner.py` file with a `register()` returning `{"runners": {"bedrock": BedrockRunner}}`
WHEN HivePilot starts and a `tasks.yaml` runner definition uses `kind: bedrock`
THEN the pipeline executes `BedrockRunner` for that step, with no changes to `hivepilot/` core.

GIVEN a pip-installed package declaring `[project.entry-points."hivepilot.plugins"] my_plugin = "my_pkg:register"`
WHEN HivePilot starts
THEN the entry point's `register()` is discovered, loaded, and its runners/notifiers/hooks are wired exactly like a local-file plugin.

GIVEN two plugins (or a plugin and a built-in) that both try to register the runner kind `"claude"`
WHEN HivePilot loads plugins
THEN loading raises a `RunnerKindCollisionError` naming both sources — it never silently shadows.

GIVEN a plugin's `register()` raises an exception during import or invocation
WHEN HivePilot starts
THEN the error is logged loudly (plugin name/location + exception), that one plugin is skipped, and the CLI continues running normally with all other plugins and built-ins intact.

GIVEN a maintainer runs `hivepilot plugins list`
WHEN plugins are loaded from both a local file and an entry-point package
THEN the output lists each plugin's name, source (`local-file` / `entry-point`), location, and what it contributes (runner kinds / notifier names / hook names).

## 6. Acceptance Criteria

- [ ] `RunnerDefinition.kind` accepts any `str`; `KNOWN_RUNNER_KINDS: tuple[str, ...]` in `hivepilot/models.py` documents the 12 built-in values for help/typing only (not enforced by pydantic).
- [ ] `RunnerRegistry.register(kind: str, cls: type[BaseRunner], *, override: bool = False)` is the single write path into `RUNNER_MAP`; the 11 built-in runner classes self-register at import time (`hivepilot/registry.py` module load).
- [ ] `orchestrator.py`'s `_parse_brain` no longer calls `get_args(RunnerKind)`; it checks against the live registry (union of `RUNNER_MAP` keys and `KNOWN_RUNNER_KINDS`, so current `"api"`-prefix behavior is unchanged and plugin kinds are recognized).
- [ ] A fixture plugin registering a new runner kind, loaded via (a) a local `plugins/*.py` file and (b) a mocked `hivepilot.plugins` entry-point, is resolvable by `RunnerRegistry` and executes in a pipeline test.
- [ ] Registering an already-registered kind (built-in or plugin) without `override=True` raises `RunnerKindCollisionError` / `NotifierKindCollisionError` — never silently replaces it.
- [ ] `notification_service.send_notification()` dispatches via `NOTIFIER_MAP` (a registry, mirroring `RUNNER_MAP`); `slack`/`discord`/`telegram` self-register at import; a plugin notifier is invoked for the same event.
- [ ] `PluginManager` exposes `on_pipeline_start`, `on_pipeline_end`, `on_error` hook keys (in addition to existing `before_step`/`after_step`), fired from `orchestrator.run_pipeline` at the appropriate points, best-effort (a hook exception is logged, never crashes the run).
- [ ] `hivepilot plugins list` prints every loaded plugin's name, source, location, and contributed runners/notifiers/hooks (built-ins included, marked `source: built-in`).
- [ ] `docs/v4/PLUGINS.md` documents: the trust model, how to author a runner/notifier/hook plugin, local-file vs entry-point packaging, and the collision/error-handling behavior.
- [ ] All 11 built-in runner kinds + 3 built-in notifiers still resolve and pass their existing tests, unmodified in behavior.
- [ ] No `urllib`/`requests`/`httpx` (or any HTTP client) import exists in the plugin *loading* code path (`hivepilot/plugins.py`) — plugins are never fetched over the network.

## 7. Non-Goals (at least as detailed as goals)

- **Fixing the pre-existing `"api"` `RunnerKind` orphan** (declared in the `Literal`/`KNOWN_RUNNER_KINDS`, absent from `RUNNER_MAP`) — out of scope. Sprint 1 must preserve today's behavior exactly (kind=`"api"` still raises `KeyError` on resolution, and its literal string still counts as a "known" prefix in `_parse_brain`).
- **A plugin marketplace, registry service, or remote plugin discovery** — v1 trust model is local filesystem (project `plugins/` dir or `config_repo`) and installed pip packages only. No network fetch of plugin code, ever, in this PRD.
- **Sandboxing / capability-restricting plugin execution** (e.g. subprocess isolation, seccomp) — a plugin is trusted, arbitrary Python code, exactly like any other installed dependency. Documented, not mitigated by a sandbox, in v1.
- **New runner *instances*** (same kind, different model/host/command via `tasks.yaml` `runners:`) — already works today with zero code; not part of this PRD (see justification above).
- **Hot-reload of plugins without restart** — plugins load once at `PluginManager()` construction (process start / `Orchestrator._load()`); no file-watching or live-reload in v1.
- **A plugin dependency-version resolver or plugin-to-plugin dependency graph** — plugins are independent; load order is local-file-then-entry-point, deterministic only at that granularity (whatever `glob()`/`entry_points()` returns *within* each mechanism is not made deterministic beyond that).
- **UI-based plugin management** (enable/disable via dashboard/Telegram) — `plugins list` (read-only CLI) is the only v1 introspection surface; `settings.plugins_enabled` (a boolean, env/config file) is the only enable/disable control, not a live toggle.

## 8. Technical Constraints

- Stack: Python 3.10+, Pydantic v2, Typer, `importlib.metadata` (stdlib, no new dependency), `structlog` logging (existing `get_logger` pattern).
- Architecture: Extend, do not rewrite, `hivepilot/plugins.py`'s existing `register()` contract and file-scan loader. Mirror the existing `RUNNER_MAP`/`RunnerRegistry` pattern for the new `NOTIFIER_MAP`/`NotifierRegistry` (structural consistency across the two registries).
- Performance: Plugin discovery happens once per process at `PluginManager()` construction — not a hot path, no measured budget needed; must not meaningfully slow CLI startup (entry-point scan is a single `importlib.metadata.entry_points(group=...)` call).

## 9. Architecture Decisions

| Decision | Reversal Cost | Alternatives Considered | Rationale |
| --- | --- | --- | --- |
| `RunnerKind`: `Literal[...]` → `str` type alias, with `KNOWN_RUNNER_KINDS` tuple kept for docs/help only | Medium (touches a widely-imported type) | Keep `Literal` and add a plugin-specific parallel type (`PluginRunnerKind = str`) | A single, widened type is simpler than two parallel runner-kind concepts; runtime safety moves to the registry (where it belongs — pydantic can't know about plugin kinds at class-definition time anyway) |
| Runtime kind validation lives in `RunnerRegistry` (`RUNNER_MAP` membership), not in `RunnerDefinition` (pydantic) | Low | A custom pydantic validator calling into the registry at parse time | Avoids an import cycle (`models.py` would need to import `registry.py`, which imports runner classes, which import `models.py`) and keeps `RunnerDefinition` a pure data schema; validation happens naturally at execution (`get_runner`/`execute_definition` already raise `KeyError` for unknown kinds) |
| One entry-point group `hivepilot.plugins` (not separate `.runners`/`.notifiers` groups) | Medium (a public packaging contract) | Three separate entry-point groups matching the three surfaces | A single group reusing the exact `register() -> dict` contract that local-file plugins already use means one mental model, one doc section, one loader function per mechanism — not three |
| Local-file plugins load before entry-point plugins; any kind/name collision (across or within mechanisms) raises immediately (fail closed) | Low | Silently let the later-loaded plugin win (last-write-wins) | Matches the Danger Definition explicitly: "a plugin silently overriding/shadowing a built-in kind... with no error" is a named danger, so last-write-wins is rejected outright |
| `settings.plugins_enabled: bool = True` master switch (`config.py`) | Low | No global switch; only per-mechanism switches | Cheap defense-in-depth: an operator can disable all plugin loading (e.g. investigating a suspected bad plugin) without editing `plugins.py` |
| Provenance tracking (`PluginRecord`: name/source/location) lives in `PluginManager` (Sprint 2), not bolted on later in Sprint 4 | Low | Add provenance as a Sprint 4 afterthought, wrapping the loaders again | The two loading mechanisms are introduced in Sprint 2 — that is the only place that naturally knows which mechanism a given plugin came from; Sprint 4 only *reads* `PluginManager.loaded` |
| Sprint 4 touches only `orchestrator.py` + `cli.py` (not `plugins.py`) because `PluginManager`'s hook accumulation is already key-agnostic | Low | Have Sprint 4 also modify `plugins.py` to add explicit `on_pipeline_*`/`on_error` keys | The existing `self.hooks.setdefault(hook_name, []).append(hook_callable)` loop already accepts any key a plugin's `register()` returns — no `plugins.py` change is needed to *accept* new hook names, only `orchestrator.py` needs to *call* `run_hook` with them. This also removes a same-batch file conflict with Sprint 3 (which does modify `plugins.py`) |

## 10. Security Boundaries

- **Auth model:** N/A directly (no new HTTP endpoints) — `hivepilot plugins list` is a local CLI command, subject to the same OS-user trust as running `hivepilot` at all.
- **Trust boundaries:** A plugin is arbitrary Python code. Trusted sources (v1, exhaustive): (1) `plugins/*.py` under the project `base_dir` or the synced `config_repo` (both already local-filesystem trust, same as `tasks.yaml`/`projects.yaml`); (2) any Python package installed in the current environment that declares a `hivepilot.plugins` entry point (trust = "you, or your package manager, chose to `pip install` it" — identical trust boundary to any other dependency). No other source is ever consulted; no URL, git-remote, or artifact-registry fetch of plugin code exists anywhere in `hivepilot/plugins.py`.
- **Data sensitivity:** Plugin runners/notifiers execute with the same process environment and `settings.secrets_allowed_dirs`/env-merge access as built-in runners (`RunnerPayload.secrets`) — no new secret surface is introduced; a plugin sees what a built-in runner would see for the same `RunnerDefinition`/`payload`.
- **Tenant isolation:** N/A (HivePilot is single-tenant per process/config).

## 11. Data Model

N/A — no persistent schema. `PluginManager.loaded: list[PluginRecord]` and `RUNNER_MAP`/`NOTIFIER_MAP` are in-memory, rebuilt every process start; nothing new is written to disk beyond `plugins/*.py` files a maintainer already authors today.

## 12. Shared Contracts

- **`RunnerKind`** (`hivepilot/models.py`): type alias `str` (was `Literal[...]`). `KNOWN_RUNNER_KINDS: tuple[str, ...]` — the original 12 literal strings, for CLI help/typing hints only, never for runtime rejection.
- **`RunnerRegistry.register(kind: str, cls: type[BaseRunner], *, override: bool = False) -> None`** (`hivepilot/registry.py`, static method): the single write path into `RUNNER_MAP`. Raises `RunnerKindCollisionError` if `kind` is already registered to a *different* class and `override` is not `True`.
- **`RunnerRegistry.known_kinds() -> frozenset[str]`**: current `RUNNER_MAP` keys, live (reflects plugin registrations made before the call).
- **`BaseRunner` Protocol** (`hivepilot/runners/base.py`, unchanged): `__init__(definition: RunnerDefinition, settings: Settings)`, `run(payload: RunnerPayload) -> None`, optional `capture(payload: RunnerPayload) -> str`. Every plugin runner class must satisfy this.
- **Plugin `register()` contract** (extended, `hivepilot/plugins.py`): a zero-arg callable returning a `dict` with any of these optional keys — `"runners": dict[str, type[BaseRunner]]`, `"notifiers": dict[str, Callable[[str], None]]`, `"before_step"`, `"after_step"`, `"on_pipeline_start"`, `"on_pipeline_end"`, `"on_error"`: each a `Callable[..., None]`. Unrecognized keys are stored under `self.hooks[key]` (forward-compatible, never an error) — only `runners`/`notifiers` keys are eagerly popped out and routed to their respective registries; the rest accumulate as hook-callable lists exactly as `before_step`/`after_step` do today.
- **`PLUGIN_ENTRY_POINT_GROUP = "hivepilot.plugins"`** (`hivepilot/plugins.py`): the `importlib.metadata` entry-point group name third-party packages declare under `[project.entry-points."hivepilot.plugins"]`. Each entry point resolves to the same zero-arg `register() -> dict` callable as a local-file plugin.
- **`PluginRecord`** (`hivepilot/plugins.py`, new `@dataclass(slots=True)`): `name: str`, `source: str` (`"local-file"` | `"entry-point"` | `"built-in"`), `location: str` (file path, or `"<entry-point-value> (<dist-name>==<version>)"`, or `"built-in"`).
- **`NotifierRegistry.register(name: str, fn: Callable[[str], None], *, override: bool = False) -> None`** (`hivepilot/services/notification_service.py`, new, mirrors `RunnerRegistry.register`): single write path into `NOTIFIER_MAP`. Raises `NotifierKindCollisionError` on an unflagged collision.
- **`NotConfigured`** (`hivepilot/services/notification_service.py`): public alias of the existing `_NotConfigured` exception — a plugin notifier raises this to signal "not configured, skip silently" exactly like a built-in channel does.
- **`settings.plugins_enabled: bool = True`** (`hivepilot/config.py`): master switch; when `False`, neither local-file nor entry-point plugin discovery runs (built-ins are unaffected).

## 13. Architecture Invariant Registry

| Concept | Owner | Format/Values | Verify Command |
| --- | --- | --- | --- |
| Runner kind registry | `hivepilot/registry.py` | `RUNNER_MAP: dict[str, type[BaseRunner]]`, keys = any `str` | `python -c "from hivepilot.registry import RUNNER_MAP; assert {'claude','shell','langchain','internal','codex','gemini','opencode','ollama','container','cursor','vibe'} <= set(RUNNER_MAP)"` |
| Notifier registry | `hivepilot/services/notification_service.py` | `NOTIFIER_MAP: dict[str, Callable[[str], None]]` | `python -c "from hivepilot.services.notification_service import NOTIFIER_MAP; assert {'slack','discord','telegram'} <= set(NOTIFIER_MAP)"` |
| Plugin `register()` return contract | `hivepilot/plugins.py` | keys subset of `{runners, notifiers, before_step, after_step, on_pipeline_start, on_pipeline_end, on_error}` | `pytest tests/test_plugin_loading_mechanisms.py -q` |
| Plugin trust boundary | `hivepilot/plugins.py` | no network client import in the loader | `! grep -nE "urllib|requests\.(get|post)|httpx" hivepilot/plugins.py` |

**Dependency direction:** `registry.py` owns runner-kind resolution; `plugins.py` (consumer) calls `RunnerRegistry.register`. `notification_service.py` owns notifier-name resolution; `plugins.py` (consumer) calls `NotifierRegistry.register`.

## 14. Open Questions

- [x] Should `settings.plugins_enabled=False` also suppress the *local-file* `plugins/*.py` scan, or only entry-points? **Resolved (Sprint 2 + Phase 4 review):** it suppresses ALL three loading paths — the local-file scan, the entry-point scan, AND the explicit `plugins_entry` pin — so the switch is a true master kill-switch for investigating a suspect plugin regardless of how it was wired.
- [x] Should `hivepilot plugins list` also show *built-in* runners/notifiers (11+3), or only plugin-contributed ones? **Resolved (Sprint 4):** it shows both, as a v1 **inventory** — a "Loaded Plugins" table (name/source/location per `PluginRecord`) plus separate "Runner Kinds" and "Notifiers" tables where each row is labelled `built-in` vs `plugin` by membership in `KNOWN_RUNNER_KINDS` / `KNOWN_NOTIFIER_NAMES`. Note: `PluginRecord.source` only ever takes `"local-file"` / `"entry-point"` (never `"built-in"` — built-ins are not plugins and get no `PluginRecord`); the `"built-in"` value listed for `source` in §12 describes the CLI's row label, not a `PluginRecord.source` value. Full per-plugin attribution (which plugin contributed which kind/name) is a deliberate v1 non-goal.

## 15. Uncertainty Policy

When uncertain: fail closed — reject unknown/colliding kinds, refuse to load from untrusted/unexpected locations, surface plugin load errors loudly (never silent).
When "isolate a broken plugin" conflicts with "never silently swallow errors": prefer isolate-and-log-loudly over crash — a broken plugin is *reported* (logged at `warning` level with plugin name/location + exception) but does not abort the whole CLI/pipeline; a *collision* (ambiguous — there is no safe default to prefer) always raises and aborts loading.

## 16. Verification

- Deterministic: `pytest tests/test_runner_registry_open.py tests/test_registry.py tests/test_models.py tests/test_plugin_loading_mechanisms.py tests/test_notifier_registry.py tests/test_plugin_hooks_lifecycle.py tests/test_cli_plugins_list.py -q`; full existing suite (`pytest -q`) must show zero new failures.
- Manual: maintainer runs `hivepilot plugins list` against a real `plugins/` dir with one fixture plugin, confirms output shows the fixture plugin plus all built-ins.

## 17. Sprint Decomposition

Maximum 5 sprints (4 used). Each sprint is extracted into its own file under `sprints/`. Progress tracked in `progress.json`.

### Sprint Overview

| Sprint | Title | Depends On | Batch | Model | Parallel With |
| --- | --- | --- | --- | --- | --- |
| 1 | Open the runner registry (foundation) | None | 1 | sonnet | — |
| 2 | Plugin loading — both mechanisms | Sprint 1 | 2 | sonnet | — |
| 3 | Pluggable notifiers | Sprint 2 | 3 | sonnet | Sprint 4 |
| 4 | Hooks, trust model, CLI & docs | Sprint 2 | 3 | sonnet | Sprint 3 |

Sprint 3 and Sprint 4 touch disjoint file sets (`notification_service.py` + `plugins.py` vs. `orchestrator.py` + `cli.py`) — safe to run in the same batch (parallel worktrees). See the file-boundary analysis in each sprint spec.

### Sprint 1: Open the runner registry (foundation) → `sprints/01-open-runner-registry.md`

**Objective:** Widen `RunnerKind` to `str`, make `RUNNER_MAP` populated via `RunnerRegistry.register()` (built-ins self-registering), and replace the `get_args(RunnerKind)` runtime check with a live-registry check — zero regression of the 12 declared kinds.

### Sprint 2: Plugin loading — both mechanisms → `sprints/02-plugin-loading-both-mechanisms.md`

**Objective:** Extend `hivepilot/plugins.py`'s `register()` contract to carry `runners`/`notifiers`, wire discovered runners into `RunnerRegistry`, add `importlib.metadata` entry-point discovery (`hivepilot.plugins` group) alongside the existing local-file scan, track provenance, and fail closed on any kind collision or load error.

### Sprint 3: Pluggable notifiers → `sprints/03-pluggable-notifiers.md`

**Objective:** Replace `notification_service.send_notification`'s `if/elif` with a `NotifierRegistry`/`NOTIFIER_MAP`, built-ins self-register, and wire `PluginManager`'s declared notifiers into it.

### Sprint 4: Hooks, trust model, CLI & docs → `sprints/04-hooks-trust-model-cli-docs.md`

**Objective:** Fire `on_pipeline_start`/`on_pipeline_end`/`on_error` hooks from `orchestrator.run_pipeline`, add `hivepilot plugins list`, and write `docs/v4/PLUGINS.md` documenting the trust model and authoring guide.

## 18. Execution Log

[Filled during execution — tracked in progress.json]

## 19. Learnings (filled after all sprints complete)

[Compound step output]
