"""mem0 plugin — persistent cross-run agent memory via a `recall`/`store` pair.

Mirrors `plugins/headroom.py`'s proven shape (opt-in gate, lazy import,
never-raise, sentinel-guarded idempotency on the shared `metadata` dict) but
wires TWO lifecycle hooks instead of one:

- `before_step` (``recall``): search `mem0 <https://github.com/mem0ai/mem0>`_
  for memories relevant to this project/task and inject them into
  ``payload.metadata["extra_prompt"]``.
- `after_step` (``store``): persist the available salient content back to
  mem0.

**Step 0 findings (investigated before writing this plugin — see
``hivepilot/orchestrator.py`` / ``hivepilot/runners/claude_runner.py``):**

1. **Recall injection field.** ``Orchestrator._execute_task`` builds the
   per-step ``payload = RunnerPayload(..., metadata=metadata, ...)`` and
   passes that SAME object straight through to
   ``self.plugins.run_hook("before_step", payload=payload)`` and, moments
   later, to the runner (``self.registry.capture_definition(...)`` /
   ``self._capture_or_execute(...)``) — no copy is made anywhere in
   between, exactly headroom's mechanism. ``ClaudeRunner._build_prompt``
   reads ``payload.metadata.get("extra_prompt")`` verbatim into the
   rendered prompt ("Extra instructions from user: ..."), so an in-place
   edit to that field here IS seen by the runner. ``extra_prompt`` (not
   ``prior_context``) is the injection target: it's the free-text
   "instructions" channel a runner already treats as directives to the
   agent, which is the right semantic slot for "here's what you
   remembered", whereas ``prior_context`` is reserved for actual upstream
   stage output.

2. **Store data availability — RESOLVED (hook-context-enrichment).**
   Originally, the runner's return value (the step's real output) was
   appended to a local ``outputs`` list INSIDE ``_execute_task`` and never
   attached to ``payload`` or threaded into the ``after_step`` call's
   kwargs, so ``store()`` had no access to what the agent actually
   produced. ``Orchestrator._execute_task`` now threads it through:
   ``self.plugins.run_hook("after_step", payload=payload, dry_run=dry_run,
   role=task.role, output=outputs[-1] if outputs else None)`` — the SAME
   value just appended to ``outputs`` for this step, passed straight into
   the hook call that follows it. ``store()`` reads ``kwargs.get("output")``
   and persists it (labeled ``output: ...``) IN ADDITION to task/step
   identity and the step's INPUT context (``extra_prompt`` /
   ``prior_context``) — output captures what happened, extra_prompt/
   prior_context capture what was asked for; both are kept as
   complementary, not mutually exclusive.

   A related wrinkle: because ``recall`` mutates ``extra_prompt`` in place
   (appending a "Relevant memories:" block), a naive ``store()`` reading
   the CURRENT ``extra_prompt`` would re-persist mem0's own recalled
   memories back into mem0 — a feedback loop. ``recall`` snapshots the
   pre-mutation value under a private key
   (``_mem0_original_extra_prompt``) the first time it runs for a shared
   ``metadata`` dict; ``store`` prefers that snapshot when present, falling
   back to the current ``extra_prompt`` only when ``recall`` never ran
   (disabled, no client, etc.).

3. **Sentinel safety.** Both private keys this plugin writes onto the
   shared ``metadata`` dict (``_mem0_recalled``, the idempotency sentinel;
   ``_mem0_original_extra_prompt``, the snapshot) are ``_``-prefixed and
   never rendered into a prompt — ``ClaudeRunner._build_prompt`` reads only
   the specific ``extra_prompt`` / ``prior_context`` keys off
   ``payload.metadata``, never iterating or serializing the whole dict
   (verified by ``TestSentinelKeyNeverRenderedIntoPrompt`` in
   ``tests/test_mem0.py``, mirroring headroom's own test of the same
   shape).

**Idempotency (shared ``metadata`` dict):** exactly headroom's problem —
``Orchestrator._execute_task`` builds ONE ``metadata`` dict per *task* and
reuses that SAME dict object, by reference, for every step's
``RunnerPayload`` in ``task.steps``. Searching mem0 on every ``before_step``
call would re-query (and, without the snapshot key, re-append memories)
on every step of a multi-step task. The ``_mem0_recalled`` sentinel is set
after the first search call for a given ``metadata`` dict (regardless of
whether any memories were found), and subsequent calls short-circuit.

**Recall key — RESOLVED (hook-context-enrichment).** ``RunnerPayload``
(``hivepilot/runners/base.py``) still doesn't carry the task's ``role`` —
``role`` lives on ``TaskConfig`` (``hivepilot/models.py``), one level up in
``Orchestrator._execute_task``. Rather than widen the shared
``RunnerPayload`` dataclass (a contract every runner depends on) just for
this plugin, ``role`` is threaded straight into the hook call instead:
``run_hook("before_step"/"after_step", ..., role=task.role)``. ``recall``/
``store`` both read ``kwargs.get("role")`` and pass it into ``_memory_key``,
which appends it to the key (``f"{project}:{task}:{role}"``) when present,
falling back to the original ``f"{project}:{task}"`` shape when absent (a
non-role task, or a direct call that doesn't supply ``role``) — so existing
memories keyed the old way keep matching for non-role tasks.

**Opt-in (dormant by default):** gated on ``settings.mem0_enabled``
(``hivepilot/config.py``, default ``False``, env ``HIVEPILOT_MEM0_ENABLED``)
— mirrors ``headroom_enabled``'s opt-in pattern exactly. Two backends are
supported via Settings, mirroring how other provider credentials
(``linear_api_key``, etc.) are read: a hosted API-key path
(``settings.mem0_api_key`` -> ``mem0.MemoryClient(api_key=...)``) and a
self-host/local path (no key set -> ``mem0.Memory()``, optionally
customized via ``settings.mem0_config``, a raw dict passed to
``Memory.from_config()``). mem0's exact constructor/``search()``/``add()``
signatures are NOT pinned by this optional integration (``mem0ai`` is
never installed by this plugin) — if the real API differs from what's
coded here, the outer ``try/except`` in every function degrades to a
logged no-op, the same graceful-degradation contract every hook in this
repo has.

Uses `mem0 <https://github.com/mem0ai/mem0>`_ (``pip install mem0ai``) —
NOT a hivepilot dependency, and deliberately not installed by this plugin.
Imported lazily so the plugin loads fine (and no-ops) when the library
isn't present; see ``plugins/rtk.py`` / ``plugins/headroom.py`` for the same
"external tool optional, graceful no-op" pattern.

**PROVENANCE metadata (Sprint 1 of the mem0-typed-and-plugin-health spec).**
``store()`` now passes a structured ``metadata`` dict to ``client.add(...)``
(mem0's ``add()`` accepts per-memory ``metadata`` on both the hosted
``MemoryClient`` and self-host ``Memory`` clients) so persisted memories are
typed/filterable — inspired by a memory-dashboard view. Built by
``_provenance_metadata()``: **real values only, no fabrication** — a key is
included ONLY when a real value is reachable, never as a ``None``/placeholder
stand-in.

- ``source``: always ``"hivepilot"``.
- ``project`` / ``task``: always present (``RunnerPayload.project_name`` /
  ``task_name`` are required fields).
- ``role``: included when the caller supplies it (``run_hook(..., role=
  task.role)``, threaded by ``Orchestrator._execute_task`` since #139);
  omitted otherwise.
- ``step``: included when ``payload.step.name`` is set (``RunnerPayload``
  always carries a ``step``, so this is effectively always present); reads
  straight off the payload already on hand — no orchestrator change needed.
- ``run_id``: **omitted.** ``Orchestrator._execute_task`` does NOT thread
  ``run_id`` into the ``after_step`` ``run_hook(...)`` call today (only
  ``payload``/``dry_run``/``role``/``output`` are — see the "Store data
  availability" note above); rather than force an orchestrator signature
  change for this sprint, ``run_id`` is left out and noted here as a
  follow-up for whenever it IS threaded through.
- ``category``: optional, read from ``payload.step.metadata.get(
  "memory_category")`` when a caller sets it on the step config; defaults to
  ``"run"`` otherwise. Never invented beyond that one explicit config knob.
- ``ts``: a UTC ISO-8601 timestamp (``datetime.now(timezone.utc).isoformat()``),
  generated at store time — genuinely available, not fabricated.
- ``confidence``: **deliberately NOT included.** A memory-dashboard view
  might show a confidence score, but HivePilot has no real signal to back
  one — inventing a number here would be exactly the kind of fabricated
  metadata this feature is trying to avoid. Add it later only if/when a
  genuine source for it exists (e.g. a role's self-assessment, a reviewer
  score).

This metadata is attached to the SAME ``client.add(...)`` call ``store()``
already makes (still skipped when there's no salient content beyond bare
task identity — see below) — no new mem0 calls, no new egress beyond what
was already being sent (the metadata dict is small and is sent alongside the
existing content string). In hosted mode this metadata ALSO leaves the
machine — see the data-egress warning below, extended to cover it.

**Complementarity with headroom:** headroom *compresses* context already on
the payload; mem0 *enriches* it with recalled memory. If both are enabled,
mem0's ``recall`` should run before headroom's compression pass so the
injected memories are subject to the same compression as the rest of the
prompt rather than bypassing it. Local-file plugins are discovered by
``sorted(plugin_dir.glob("*.py"))`` (``hivepilot.plugins._scan_local_plugins``)
and hooks run in that discovery order, and ``"headroom.py"`` sorts BEFORE
``"mem0.py"`` alphabetically — meaning, as shipped, headroom compresses
FIRST and mem0 recalls SECOND, injecting fresh (uncompressed) memories into
an already-compressed ``extra_prompt``. This is the opposite of the
recommended ordering; it's documented here rather than silently relied
upon. Operators running both plugins together and wanting recall-then-
compress should rename files to control ``sorted()`` order (e.g.
``a_mem0.py`` / ``b_headroom.py``) — see ``docs/v4/PLUGINS.md``.

Deliberately NOT a ``@dataclass``: local-file plugins are loaded via
``importlib.util.spec_from_file_location()`` / ``exec_module()``
(``hivepilot.plugins._scan_local_plugins``), which never registers the
module in ``sys.modules``. Combined with ``from __future__ import
annotations``, that trips a real CPython 3.14 ``dataclasses`` bug
(``_is_type`` does ``sys.modules[cls.__module__].__dict__``, which is
``None`` for an unregistered module) — see ``plugins/rtk.py`` for the full
write-up. This plugin sticks to plain functions, sidestepping the issue
entirely.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from hivepilot.plugins import HealthStatus
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

try:
    from mem0 import Memory, MemoryClient
except ImportError:  # mem0ai is optional — never installed by this plugin
    Memory = None  # type: ignore[assignment,misc]
    MemoryClient = None  # type: ignore[assignment,misc]

# RunnerPayload.metadata field that ends up verbatim in a runner's rendered
# prompt (see ClaudeRunner._build_prompt) and is therefore where recalled
# memories are injected. Deliberately extra_prompt, not prior_context — see
# the "Recall injection field" note in the module docstring.
_RECALL_FIELD = "extra_prompt"

# Private marker set on a task's shared `metadata` dict once `recall` has run
# for it — see the "Idempotency" note in the module docstring. `_`-prefixed
# so it reads as private; never consumed by any runner (see
# TestSentinelKeyNeverRenderedIntoPrompt in tests/test_mem0.py).
_SENTINEL_KEY = "_mem0_recalled"

# Private snapshot of `extra_prompt` taken BEFORE `recall` mutates it, so
# `store` can persist the user's original ask instead of re-persisting
# mem0's own recalled-memories block (a feedback loop) — see the "Store data
# availability" note in the module docstring.
_ORIGINAL_EXTRA_PROMPT_KEY = "_mem0_original_extra_prompt"

_MAX_MEMORIES = 5

# Optional per-step config knob (`TaskStep.metadata["memory_category"]`) a
# task author can set to categorize the memories `store` persists for that
# step — see `_provenance_metadata`'s docstring. Falls back to
# `_DEFAULT_MEMORY_CATEGORY` when unset; never invented beyond this one
# explicit config field.
_MEMORY_CATEGORY_KEY = "memory_category"
_DEFAULT_MEMORY_CATEGORY = "run"


def _get_client() -> Any | None:
    """Build a mem0 client from Settings, or ``None`` if unavailable.

    Hosted path: ``settings.mem0_api_key`` set -> ``MemoryClient(api_key=...)``.
    Self-host path: no key -> ``Memory()`` (optionally customized via
    ``settings.mem0_config``, passed to ``Memory.from_config()``).
    Never raises — any construction failure (library absent, bad config,
    network error on hosted init, etc.) degrades to ``None``.
    """
    try:
        from hivepilot.config import settings

        if settings.mem0_api_key:
            if MemoryClient is None:
                return None
            return MemoryClient(api_key=settings.mem0_api_key)
        if Memory is None:
            return None
        config = settings.mem0_config
        return Memory.from_config(config) if config else Memory()
    except Exception as exc:  # noqa: BLE001 — a hook must never crash a run
        logger.warning("plugin.mem0.client_init_failed", error=str(exc))
        return None


def _memory_key(payload: Any, role: str | None = None) -> str:
    """Best-effort identity key for this task, used as mem0's `user_id`.

    Keyed by project + task, plus `role` when reachable (threaded in via
    `run_hook(..., role=task.role)` by `Orchestrator._execute_task` —
    `hivepilot/orchestrator.py`; `RunnerPayload` itself still doesn't carry
    `role` — see the "Recall key" note in the module docstring). Falls back
    to the project:task key when `role` is `None`/absent, exactly as before
    — a caller that doesn't pass `role` (e.g. a direct test invocation, or a
    non-role task) sees unchanged keying.
    """
    project = getattr(payload, "project_name", None) or "unknown"
    task = getattr(payload, "task_name", None) or "unknown"
    if role:
        return f"{project}:{task}:{role}"
    return f"{project}:{task}"


def _provenance_metadata(payload: Any, role: str | None = None) -> dict[str, Any]:
    """Build the structured PROVENANCE `metadata` dict passed to `client.add(
    ..., metadata=...)` — see the "PROVENANCE metadata" note in the module
    docstring for the full rationale.

    Real values only, no fabrication: a key is included ONLY when a real
    value is reachable off `payload`/`role` — never as a `None`/placeholder
    stand-in. `confidence` is deliberately never included (no genuine signal
    exists for it yet).
    """
    metadata: dict[str, Any] = {
        "source": "hivepilot",
        "project": payload.project_name,
        "task": payload.task_name,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    if role:
        metadata["role"] = role

    step = getattr(payload, "step", None)
    step_name = getattr(step, "name", None)
    if step_name:
        metadata["step"] = step_name

    step_metadata = getattr(step, "metadata", None)
    category = None
    if isinstance(step_metadata, dict):
        category = step_metadata.get(_MEMORY_CATEGORY_KEY)
    metadata["category"] = (
        category if isinstance(category, str) and category else _DEFAULT_MEMORY_CATEGORY
    )

    # `run_id` is NOT threaded into the `after_step` `run_hook(...)` call by
    # `Orchestrator._execute_task` today (only `payload`/`dry_run`/`role`/
    # `output` are) — deliberately omitted rather than forcing an
    # orchestrator signature change for this sprint. Follow-up: thread
    # `run_id` through once it's cheaply available there.

    return metadata


def _extract_memory_texts(results: Any) -> list[str]:
    """Best-effort extraction of memory text from a mem0 `search()` result.

    mem0's response shape has changed across versions/API surfaces (a bare
    list of dicts historically; `{"results": [...]}` in newer hosted API
    responses) and isn't pinned by this optional integration (`mem0ai` is
    never installed by this plugin — see the module docstring). Tolerant of
    unrecognized shapes — degrades to an empty list rather than raising,
    consistent with the "never raise" contract callers wrap this in.
    """
    if results is None:
        return []
    items: Any = results
    if isinstance(results, dict):
        items = results.get("results", results.get("memories", []))
    if not isinstance(items, list):
        return []
    texts: list[str] = []
    for item in items:
        if isinstance(item, str) and item:
            texts.append(item)
        elif isinstance(item, dict):
            text = item.get("memory") or item.get("text") or item.get("content")
            if isinstance(text, str) and text:
                texts.append(text)
    return texts


def recall(**kwargs: Any) -> None:
    """Search mem0 for relevant memories and inject them into `extra_prompt`.

    Keyed by `project:task[:role]` via `_memory_key` — `role` is included
    when the caller supplies it (`run_hook("before_step", ..., role=...)`),
    the SAME keying `store` uses, so recall/store stay matched. No-op when
    `settings.mem0_enabled` is False (the default — dormant/opt-in), when no
    client can be built (library absent / unconfigured), when no `payload`
    kwarg is supplied, or when this `metadata` dict was already recalled for
    (idempotency guard — see module docstring). Never raises.
    """
    try:
        from hivepilot.config import settings

        if not settings.mem0_enabled:
            return
        payload = kwargs.get("payload")
        if payload is None:
            return
        metadata = getattr(payload, "metadata", None)
        if not isinstance(metadata, dict):
            return
        if metadata.get(_SENTINEL_KEY):
            # Already recalled for this shared metadata dict (same task, a
            # later step) — skip to avoid re-querying / re-appending.
            return

        client = _get_client()
        if client is None:
            return

        # Snapshot the pre-mutation value so `store` can persist the
        # original ask instead of mem0's own recalled-memories block.
        original_extra_prompt = metadata.get(_RECALL_FIELD)
        metadata[_ORIGINAL_EXTRA_PROMPT_KEY] = original_extra_prompt

        role = kwargs.get("role")
        key = _memory_key(payload, role)
        step = getattr(payload, "step", None)
        step_name = getattr(step, "name", None) or ""
        query = f"{payload.task_name} {step_name}".strip() or key

        results = client.search(query, user_id=key)
        memories = _extract_memory_texts(results)[:_MAX_MEMORIES]

        if memories:
            block = "Relevant memories:\n" + "\n".join(f"- {m}" for m in memories)
            if isinstance(original_extra_prompt, str) and original_extra_prompt:
                metadata[_RECALL_FIELD] = f"{original_extra_prompt}\n\n{block}"
            else:
                metadata[_RECALL_FIELD] = block
            logger.info("plugin.mem0.recalled", count=len(memories), key=key)

        # Mark this metadata dict as recalled-for regardless of whether any
        # memories were found — search() already ran; a later step's
        # before_step call must not re-query.
        metadata[_SENTINEL_KEY] = True
    except Exception as exc:  # noqa: BLE001 — a hook must never crash a run
        logger.warning("plugin.mem0.recall_failed", error=str(exc))


def store(**kwargs: Any) -> None:
    """Persist the available salient content for this step to mem0.

    Persists task/step identity, the step's real `output` when the caller
    supplies it (`Orchestrator._execute_task` threads the runner's captured
    return value into `run_hook("after_step", ..., output=...)` — see the
    "Store data availability" note in the module docstring, now resolved),
    plus the reachable input context (`extra_prompt` / `prior_context`) as
    complementary "what this task was about" data. Keyed by
    `project:task[:role]` — `role` is included when the caller supplies it
    (see `_memory_key`'s docstring / the "Recall key" note). Every persisted
    memory also carries a structured PROVENANCE `metadata` dict (see
    `_provenance_metadata` / the "PROVENANCE metadata" note in the module
    docstring) — real values only, no fabricated `confidence`. A no-op when
    there's no client, no `payload`, or no salient content beyond bare task
    identity. Never raises.
    """
    try:
        from hivepilot.config import settings

        if not settings.mem0_enabled:
            return
        payload = kwargs.get("payload")
        if payload is None:
            return
        metadata = getattr(payload, "metadata", None)
        if not isinstance(metadata, dict):
            return

        client = _get_client()
        if client is None:
            return

        step = getattr(payload, "step", None)
        step_name = getattr(step, "name", None) or "step"
        role = kwargs.get("role")
        key = _memory_key(payload, role)

        content_parts = [f"Task: {payload.task_name} / Step: {step_name}"]

        # The step's real output (threaded in via `run_hook("after_step",
        # ..., output=outputs[-1])` by `Orchestrator._execute_task` —
        # `hivepilot/orchestrator.py`) is the actual outcome, and takes
        # priority when present. Kept in ADDITION to (not instead of) the
        # input-context fields below: `extra_prompt`/`prior_context` capture
        # what the task was ASKED to do, `output` captures what actually
        # happened — both are salient, complementary content to persist.
        output_val = kwargs.get("output")
        if isinstance(output_val, str) and output_val:
            content_parts.append(f"output: {output_val}")

        # Prefer the pre-recall snapshot (avoids re-persisting mem0's own
        # recalled-memories block); fall back to the live field when recall
        # never ran for this metadata dict.
        if _ORIGINAL_EXTRA_PROMPT_KEY in metadata:
            extra_val = metadata.get(_ORIGINAL_EXTRA_PROMPT_KEY)
        else:
            extra_val = metadata.get(_RECALL_FIELD)
        if isinstance(extra_val, str) and extra_val:
            content_parts.append(f"extra_prompt: {extra_val}")

        prior_val = metadata.get("prior_context")
        if isinstance(prior_val, str) and prior_val:
            content_parts.append(f"prior_context: {prior_val}")

        if len(content_parts) <= 1:
            # Nothing salient beyond bare task identity — not worth storing.
            return

        content = "\n".join(content_parts)
        provenance = _provenance_metadata(payload, role)
        client.add(content, user_id=key, metadata=provenance)
        logger.info("plugin.mem0.stored", key=key, step=step_name, category=provenance["category"])
    except Exception as exc:  # noqa: BLE001 — a hook must never crash a run
        logger.warning("plugin.mem0.store_failed", error=str(exc))


def health(**kwargs: Any) -> HealthStatus:
    """`error` when `mem0ai` isn't importable; `degraded` when installed but
    `mem0_enabled` is False (the default — dormant); otherwise `ok`/`error`
    depending on whether `_get_client()` can actually build a client.

    **No secret/token value in any branch's detail** (Phase 19 discipline):
    only presence/mode booleans — "hosted mode configured" / "self-host" /
    "disabled" / "lib missing" — never `settings.mem0_api_key` itself.
    """
    if Memory is None and MemoryClient is None:
        return HealthStatus("error", "mem0ai not installed")

    from hivepilot.config import settings

    if not settings.mem0_enabled:
        return HealthStatus("degraded", "installed but disabled (mem0_enabled=False)")

    client = _get_client()
    if client is None:
        return HealthStatus("error", "mem0_enabled but client could not be built")

    mode = "hosted mode configured" if settings.mem0_api_key else "self-host"
    return HealthStatus("ok", mode)


def register() -> dict[str, Any]:
    return {"before_step": recall, "after_step": store, "health": {"mem0": health}}
