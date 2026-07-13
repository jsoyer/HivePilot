# Sprint 1: Open the runner registry (foundation)

## Meta

- **PRD:** `../spec.md` (do not need to read it — this spec is self-contained)
- **Sprint:** 1 of 4
- **Depends on:** None
- **Batch:** 1 (sequential — no other sprint runs alongside this one)
- **Model:** sonnet
- **Estimated effort:** M

## Objective

Widen `RunnerKind` from a closed `Literal[...]` to `str`, replace the static `RUNNER_MAP` dict literal with a registry (`RunnerRegistry.register()`) that the 11 built-in runner classes populate at import time, and replace the one runtime consumer of `get_args(RunnerKind)` (`orchestrator.py`'s `_parse_brain`) with a live-registry check — with zero behavior change for the 12 currently-declared kinds (including the pre-existing `"api"` gap).

## Why this exists (context, no PRD lookup needed)

HivePilot's runner kinds are hardcoded: `hivepilot/models.py` declares `RunnerKind = Literal["claude","shell","langchain","internal","codex","gemini","opencode","ollama","api","container","cursor","vibe"]` (12 values), and pydantic rejects any `RunnerDefinition(kind=...)` outside that set at construction time. `hivepilot/registry.py` has a static `RUNNER_MAP: Dict[str, Type[BaseRunner]]` dict literal mapping 11 of those 12 kinds to runner classes (`"api"` has no entry — this is a pre-existing gap, not something this sprint introduces or fixes; resolving kind=`"api"` already raises `KeyError` today via `RUNNER_MAP.get(definition.kind)` returning `None`).

This sprint opens both the type and the map so a later sprint (Sprint 2) can let a plugin register a brand-new kind. This sprint does **not** implement plugin loading — it only makes the registry pluggable and self-consistent.

**The one subtle risk:** `hivepilot/orchestrator.py` (~line 146-152, function `_parse_brain`) does:

```python
def _parse_brain(entry: str, default_runner: str) -> tuple[str, str]:
    from typing import get_args
    from hivepilot.models import RunnerKind
    if ":" in entry:
        prefix, rest = entry.split(":", 1)
        if prefix in set(get_args(RunnerKind)):
            return prefix, rest
    return default_runner, entry
```

Once `RunnerKind` becomes a plain `str`, `get_args(str)` returns `()` (empty tuple) — this check would **silently stop matching anything**, silently disabling every `"runner:model"` debate-brain pin (e.g. `"claude:claude-sonnet-4-6"`), with no error, no test failure unless you know to look. This is the load-bearing reason this sprint exists and must fix this call site in the same commit as the `Literal → str` change — never ship the type change without also fixing this consumer.

## File Boundaries

### Creates (new files)

- `tests/test_runner_registry_open.py` — new focused test file for the registry-opening behavior (registration, collision, `known_kinds()`, and the `_parse_brain` fix). Keep separate from `tests/test_registry.py` (existing file, minimal edits only) per the project's "many small files" convention.

### Modifies (can touch)

- `hivepilot/models.py` — change `RunnerKind` from `Literal[...]` to a `str` type alias; add `KNOWN_RUNNER_KINDS: tuple[str, ...]` containing the same 12 original literal strings, in the same order, for docs/help/typing convenience only (never used for runtime rejection).
- `hivepilot/registry.py` — replace the `RUNNER_MAP` dict *literal* with an empty `RUNNER_MAP: Dict[str, Type[BaseRunner]] = {}` populated via new `RunnerRegistry.register()` calls for the same 11 classes, in the same order, at module import time (bottom of the file, after the class is defined, so `RunnerRegistry.register` exists). Add `RunnerKindCollisionError(RuntimeError)`. Add `RunnerRegistry.register(kind: str, cls: type[BaseRunner], *, override: bool = False) -> None` (staticmethod) and `RunnerRegistry.known_kinds() -> frozenset[str]` (staticmethod). `RUNNER_MAP` must remain the *exact same object* other code imports (`from hivepilot.registry import RUNNER_MAP`) — do not rename it or replace it with a differently-named private dict; the existing `tests/test_registry.py` imports and `monkeypatch.setitem`s this name directly and must keep working unmodified.
- `hivepilot/orchestrator.py` — in `_parse_brain` (~line 146-152), replace `if prefix in set(get_args(RunnerKind)):` with a check against the union of the live registry and the known-kinds tuple: `if prefix in (frozenset(RUNNER_MAP) | frozenset(KNOWN_RUNNER_KINDS)):` (import `RUNNER_MAP` from `hivepilot.registry` and `KNOWN_RUNNER_KINDS` from `hivepilot.models` at the top of the function, matching the existing local-import style in that function). The union is deliberate: it preserves today's exact behavior (including the `"api"` kind counting as a recognized prefix even though it has no `RUNNER_MAP` entry) while also recognizing any kind a plugin registers later (Sprint 2+). Remove the now-unused `from typing import get_args` import in that function if nothing else in the function needs it.
- `tests/test_models.py` — update `test_runner_definition_rejects_unknown_kind` (currently lines 25-27): pydantic no longer rejects an unknown `kind` string at construction time (that was the whole point of widening the type), so `RunnerDefinition(kind="does-not-exist")` must now succeed instead of raising `ValidationError`. Replace the test with two assertions: (1) `RunnerDefinition(kind="does-not-exist").kind == "does-not-exist"` (construction succeeds — this is the new, intentional contract), and (2) resolving that unknown kind through the registry still fails closed: `pytest.raises(KeyError)` when calling `RunnerRegistry({}).get_runner("does-not-exist")` (or equivalent, using the existing `_definition_for`/`get_runner` path) for a name not present in `runner_defs` and not in `RUNNER_MAP`. Keep the test function name (or rename to `test_runner_definition_accepts_unknown_kind_but_registry_rejects_it` — either is fine, prefer the rename since the old name is now factually wrong) and add a one-line comment stating this is an intentional, documented contract change from the Plugin System PRD (Sprint 1), not an accidental test weakening.
- `tests/test_registry.py` — no functional change required (it already imports `RUNNER_MAP` by name and uses `monkeypatch.setitem`, both of which keep working); add one new test asserting `RunnerRegistry.known_kinds()` returns a `frozenset` containing at least the 11 built-in kinds, to close the loop with the new file's collision tests.

### Read-Only (reference but do NOT modify)

- `hivepilot/runners/base.py` — `BaseRunner` Protocol, unchanged in this sprint.
- `hivepilot/config.py` — `Settings` class, unchanged in this sprint.
- `hivepilot/runners/claude_runner.py`, `container_runner.py`, `cursor_runner.py`, `internal_runner.py`, `langchain_runner.py`, `prompt_cli_runner.py`, `shell_runner.py` — the runner classes themselves are unchanged; only how they're registered in `hivepilot/registry.py` changes.

### Shared Contracts (consume from prior sprints or PRD)

- None consumed (this is the foundation sprint). This sprint **produces** the contracts Sprint 2, 3, 4 consume: `RunnerKind = str`, `KNOWN_RUNNER_KINDS`, `RunnerRegistry.register()`, `RunnerRegistry.known_kinds()`, `RunnerKindCollisionError`, and the still-live `RUNNER_MAP` name.

### Consumed Invariants (from INVARIANTS.md)

- None consumed yet — this sprint establishes "Runner registry is the single source of truth" and "Built-in non-regression (runners)" and "No silent kind collision" (runner half). Verify both after your changes:
  - `python -c "from hivepilot.registry import RUNNER_MAP, RunnerRegistry; assert set(RUNNER_MAP) == set(RunnerRegistry.known_kinds())"`
  - `python -c "from hivepilot.registry import RUNNER_MAP; ks={'claude','shell','langchain','internal','codex','gemini','opencode','ollama','container','cursor','vibe'}; assert ks <= set(RUNNER_MAP), sorted(ks - set(RUNNER_MAP))"`

## Tasks

- [ ] In `hivepilot/models.py`: change `RunnerKind = Literal[...]` to `RunnerKind = str` (keep the name — it is imported widely as a type hint, e.g. `cast(RunnerKind, ...)` call sites, which remain valid no-op casts). Add `KNOWN_RUNNER_KINDS: tuple[str, ...] = ("claude", "shell", "langchain", "internal", "codex", "gemini", "opencode", "ollama", "api", "container", "cursor", "vibe")` directly below it, same order as the original `Literal` args, with a one-line comment: `# Built-in kinds, for docs/help/typing only — NOT enforced at runtime; see RunnerRegistry.`
- [ ] In `hivepilot/registry.py`: keep the existing imports of all 11 runner classes. Replace the `RUNNER_MAP = {...}` dict *literal* with `RUNNER_MAP: Dict[str, Type[BaseRunner]] = {}`. Define `class RunnerKindCollisionError(RuntimeError): pass` near the top (after imports). Add to `RunnerRegistry`: `@staticmethod def register(kind: str, cls: type[BaseRunner], *, override: bool = False) -> None:` — if `kind in RUNNER_MAP and RUNNER_MAP[kind] is not cls and not override: raise RunnerKindCollisionError(f"Runner kind '{kind}' is already registered to {RUNNER_MAP[kind].__name__}; refusing to silently replace it with {cls.__name__}")`; else `RUNNER_MAP[kind] = cls`. Add `@staticmethod def known_kinds() -> frozenset[str]: return frozenset(RUNNER_MAP)`. After the `RunnerRegistry` class body, add the self-registration block (module level, runs at import): `for _kind, _cls in {"claude": ClaudeRunner, "shell": ShellRunner, "langchain": LangChainRunner, "internal": InternalRunner, "codex": CodexRunner, "gemini": GeminiRunner, "opencode": OpenCodeRunner, "ollama": OllamaRunner, "container": ContainerRunner, "cursor": CursorRunner, "vibe": VibeRunner}.items(): RunnerRegistry.register(_kind, _cls)` — same 11 kind→class pairs as the original dict literal, same order, do not add or remove any.
- [ ] In `hivepilot/orchestrator.py`, `_parse_brain`: replace the `get_args(RunnerKind)` check with the `RUNNER_MAP | KNOWN_RUNNER_KINDS` union check described above. Add the two imports (`from hivepilot.registry import RUNNER_MAP`, `from hivepilot.models import RunnerKind, KNOWN_RUNNER_KINDS` — note `RunnerKind` may no longer be needed in this function if only used for `get_args`; keep it only if still referenced elsewhere in the function, otherwise drop it) at the top of the function body, matching the existing local-import style. Do not touch any other function in `orchestrator.py`.
- [ ] Update `tests/test_models.py::test_runner_definition_rejects_unknown_kind` per the Modifies section above — this is a deliberate, PRD-documented change to an established test expectation (Plugin System PRD, Sprint 1): pydantic construction now succeeds for unknown kinds; rejection moves to the registry.
- [ ] Add `tests/test_runner_registry_open.py` covering: (a) a fresh dummy kind registers and resolves via `RunnerRegistry.register` + `RUNNER_MAP`; (b) registering the same kind twice with the *same* class is a no-op (does not raise); (c) registering an already-registered kind with a *different* class and no `override` raises `RunnerKindCollisionError`; (d) passing `override=True` succeeds and replaces the class; (e) `RunnerRegistry.known_kinds()` is a `frozenset` containing all 11 built-ins; (f) `_parse_brain`-equivalent behavior — a debate-brain string like `"claude:claude-sonnet-4-6"` still resolves to `("claude", "claude-sonnet-4-6")` after the change (call `orchestrator._parse_brain` directly, or the smallest public wrapper that exercises it — inspect `hivepilot/orchestrator.py` for how the function is currently unit-tested, if at all, and follow that pattern; if untested today, add a direct unit test importing `_parse_brain` from `hivepilot.orchestrator`).
- [ ] Add one test to `tests/test_registry.py` asserting `RunnerRegistry.known_kinds()` returns the 11 built-ins as a `frozenset`.
- [ ] Run the full existing test suite locally (not just the new/edited files) to catch any other place that assumed `RunnerKind` was a `Literal` (e.g. a schema-generation script, an OpenAPI/JSON-schema export, or a `pydantic`-driven CLI `--help` that lists valid choices from the type). If found, note it in Agent Notes below — do not silently leave it broken.

## Acceptance Criteria

- [ ] `RunnerDefinition(kind="anything-at-all")` constructs successfully (pydantic no longer rejects unknown kind strings).
- [ ] `RunnerRegistry.known_kinds()` returns exactly the 11 built-in kinds immediately after `import hivepilot.registry` with no plugins loaded.
- [ ] Registering a new kind via `RunnerRegistry.register()` makes it resolvable via `RunnerRegistry({}).get_runner(...)` / `RUNNER_MAP`.
- [ ] Registering an existing kind with a different class and no `override=True` raises `RunnerKindCollisionError`.
- [ ] `_parse_brain("claude:claude-sonnet-4-6", "shell")` returns `("claude", "claude-sonnet-4-6")` (unchanged from before this sprint).
- [ ] `_parse_brain("api:some-model", "shell")` returns `("api", "some-model")` (unchanged pre-existing quirk — `"api"` is still treated as a recognized prefix even though it has no runner implementation).
- [ ] All 10 `cast(RunnerKind, ...)` call sites still type-check (they are static casts to a type alias that is now `str` — trivially valid) and the code still runs unchanged.
- [ ] `tests/test_models.py::test_runner_definition_rejects_unknown_kind` (or its renamed replacement) documents, in a comment, that this is an intentional PRD-driven contract change.

## Verification

- [ ] Build passes (no build step for this Python project beyond import — verify `python -c "import hivepilot.registry, hivepilot.models, hivepilot.orchestrator"` succeeds)
- [ ] Lint passes: `ruff check hivepilot/models.py hivepilot/registry.py hivepilot/orchestrator.py tests/test_runner_registry_open.py tests/test_models.py tests/test_registry.py`
- [ ] Type-check passes: `mypy hivepilot/models.py hivepilot/registry.py` (project already runs mypy per `pyproject.toml` `[tool.mypy]` `python_version = "3.12"`)
- [ ] Sprint-specific tests pass: `pytest tests/test_runner_registry_open.py tests/test_registry.py tests/test_models.py -q`
- [ ] No regression: `pytest -q` (full suite) shows zero new failures compared to the pre-sprint baseline

> **Note:** Dev server smoke test and content verification are handled by the orchestrator after merge — do not run in the sprint-executor. Sprint-executors do static verification only.

## Context

- The 11 built-in kind→class pairs, verbatim from the current `RUNNER_MAP` literal (`hivepilot/registry.py:22-34`): `claude→ClaudeRunner, shell→ShellRunner, langchain→LangChainRunner, internal→InternalRunner, codex→CodexRunner, gemini→GeminiRunner, opencode→OpenCodeRunner, ollama→OllamaRunner, container→ContainerRunner, cursor→CursorRunner, vibe→VibeRunner`. `"api"` is NOT in this list — do not add it; that gap is intentionally out of scope (see Non-Goals in the PRD).
- `RunnerRegistry` also has `_definition_for`, `execute`, `execute_definition`, `capture_definition`, `_is_worker_host` — none of these need to change in this sprint; they already call `RUNNER_MAP.get(definition.kind)`, which continues to work identically once `RUNNER_MAP` is populated via `register()` instead of a literal.
- `hivepilot/orchestrator.py`'s `_parse_brain` docstring currently says: *"Only a recognised `RunnerKind` prefix is treated as a runner, so `"opencode-go/kimi"` and other slash-style ids stay plain models."* — keep this docstring accurate; update its wording if it explicitly mentions `get_args`/`Literal`.
- Do not attempt to fix the `"api"` kind gap, add a 12th `RUNNER_MAP` entry, or otherwise "complete" the Literal → RUNNER_MAP parity. That is explicitly out of scope for this PRD (see Non-Goals).

## Agent Notes (filled during execution)

- Assigned to: sprint-executor a552f92 + main-agent verification (session 0ed22355)
- Completed: 2026-07-13
- Decisions made:
  - Kept `RUNNER_MAP` as the same importable dict object (mutated via `register`, never reassigned) so existing `monkeypatch.setitem` keeps working.
  - Self-registration uses a named `_BUILTIN_RUNNERS: Dict[str, Type[BaseRunner]]` (not an anonymous `{...}.items()`) — the explicit annotation is required or mypy infers the value type as `ABCMeta` and errors.
  - `_parse_brain` union check `frozenset(RUNNER_MAP) | frozenset(KNOWN_RUNNER_KINDS)` landed in the same change as the `Literal→str` widening (load-bearing; preserves the `"api"` prefix quirk + recognizes future plugin kinds).
- Assumptions: none needed — spec was fully prescriptive (HIGH confidence).
- Issues found & resolved:
  - **Transitive regression outside Sprint 1 boundary:** `hivepilot/cli.py` `role set-field --field runner` validation also used `get_args(RunnerKind)`, which returns `()` after the widening → rejected every runner value. No test covered it (pytest would not catch). Fixed here (small, clear, foundation-caused) with the same registry-union approach; documented in session-learnings. Grep `get_args(RunnerKind)` when widening a Literal.
- Verification: ruff PASS, ruff format PASS, mypy PASS (0 err), pytest 971 passed / 7 pre-existing (test_agent_rules noxys paths) / 2 skipped. All 3 INVARIANTS pass.
