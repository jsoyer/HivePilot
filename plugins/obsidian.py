"""obsidian plugin — logs pipeline runs into the Obsidian vault, and (Sprint
02 of the plugin-arch-overhaul PRD) recalls relevant vault context back into
the step prompt.

Ships as a notifier, four lifecycle hooks, and a health check:

- Notifier ``obsidian``: each `send_notification(...)` string becomes a
  timestamped line in today's journal.
- Hooks ``on_pipeline_end`` / ``on_error``: append a structured run-report
  block (run_id, pipeline, status/stage, timestamp) to the same journal.
- Hook ``before_step`` (`recall`, Sprint 02): search the vault for notes
  relevant to the current task/role/step and inject bounded excerpts into
  `RunnerPayload.metadata["extra_prompt"]` — mirrors `plugins/mem0.py`'s
  `recall`/`store` injection contract (same field, same
  append-not-overwrite discipline), the vault standing in for mem0's memory
  store as the context source. See `recall()`'s docstring for the full
  contract.
- Hook ``after_step`` (`store`, Sprint 02): append a structured step-outcome
  entry (task/role/step/status/summary) to the SAME daily journal note
  `notify`/`on_pipeline_end`/`on_error` already write to. See `store()`'s
  docstring.

`notify`/`on_pipeline_end`/`on_error`/`health` and the daily-journal write
behavior are all UNCHANGED by Sprint 02 — `recall`/`store` are purely
additive hooks.

Reuses `hivepilot.services.obsidian_service.ObsidianService` for ALL vault
WRITES (path guard + frontmatter) — never a raw `open().write()`. `recall`
reads notes directly (`Path.read_text`) since `ObsidianService` exposes no
generic read API and read access isn't subject to the write-path safety
guard. The vault comes from `settings.obsidian_vault`, resolved lazily
inside each function so a config change picked up between calls is honored
and importing this module has no side effects.

Contract:
- Notifier: raises `NotConfigured` (the standard "skip silently" signal, see
  `hivepilot.services.notification_service`) when the vault isn't configured
  or doesn't exist on disk. Does NOT honor `dry_run` — see the "Known
  limitation" note in `notify()`'s docstring.
- Hooks: never raise. A broken/misconfigured vault is a silent no-op — a
  hook must never crash a pipeline run. `on_pipeline_end` / `on_error` /
  `store` honor the run's `dry_run` flag (threaded in via
  `run_hook(..., dry_run=...)` by `Orchestrator.run_pipeline` /
  `Orchestrator._execute_task` — `hivepilot/orchestrator.py`): a dry-run
  pipeline builds `ObsidianService(vault, dry_run=True)`, which plans the
  write but never touches the vault. `recall` never writes to the vault at
  all (read-only), so `dry_run` doesn't apply to it.

Deliberately NOT a `@dataclass`: local-file plugins are loaded via
`importlib.util.spec_from_file_location()` / `exec_module()`
(`hivepilot.plugins._scan_local_plugins`), which never registers the module
in `sys.modules`. Combined with `from __future__ import annotations`, that
trips a real CPython 3.14 `dataclasses` bug (`_is_type` does
`sys.modules[cls.__module__].__dict__`, which is `None` for an unregistered
module) — see `plugins/rtk.py` for the full write-up. This plugin sticks to
plain functions, sidestepping the issue entirely.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hivepilot.plugins import HealthStatus
from hivepilot.services.notification_service import NotConfigured
from hivepilot.services.obsidian_service import ObsidianService
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

# `settings.obsidian_vault` (hivepilot/config.py) is a non-Optional `Path`
# field defaulting to `Path("obsidian-vault")` rather than `None` — there is
# no clean "unset" sentinel on the field itself. `health()` below treats a
# vault still equal to this field default (and absent on disk) as "unset /
# not configured" (degraded), distinct from an operator-set path that simply
# doesn't exist on disk (error). Read once, lazily, from the `Settings`
# class's own field metadata rather than hardcoded here, so it can never
# drift from the real default.
from hivepilot.config import Settings  # noqa: E402

_DEFAULT_OBSIDIAN_VAULT = Settings.model_fields["obsidian_vault"].default


def _resolve_vault() -> Path | None:
    """Return the configured vault path if set and present on disk, else None."""
    from hivepilot.config import settings

    vault = settings.obsidian_vault
    if not vault:
        return None
    path = Path(vault).expanduser()
    if not path.exists():
        return None
    return path.resolve()


def _timestamp() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def notify(message: str) -> None:
    """Append a timestamped line for *message* to today's daily journal.

    Raises `NotConfigured` (the notifier "skip silently" contract) when the
    vault isn't configured or doesn't exist on disk.

    **Known limitation — does NOT honor `dry_run`.** Unlike `on_pipeline_end`
    / `on_error`, this notifier always writes for real
    (`ObsidianService(vault, dry_run=False)`). The notifier contract
    (`NOTIFIER_MAP: dict[str, Callable[[str], None]]`,
    `hivepilot/services/notification_service.py`) is a bare
    `Callable[[str], None]` shared by every notifier (built-in
    slack/discord/telegram + any plugin's) — there is no per-call `dry_run`
    parameter to thread through without changing that shared contract for
    all of them, which is out of scope for this kwarg-threading change (see
    the hook-context-enrichment investigation). In practice this is low-risk
    today: no call site in this repo passes `channels=["obsidian", ...]` to
    `notification_service.send_notification()` (the default channel list is
    `["slack", "discord", "telegram"]`), so `notify()` is only reachable via
    a caller that explicitly opts in to the "obsidian" channel — a case this
    codebase doesn't exercise. Left undone, not silently — a future revision
    could widen the notifier `Callable` to `Callable[[str], None] |
    Callable[[str, bool], None]` (or add an optional `dry_run` kwarg) if a
    real dry-run-aware notifier caller emerges.
    """
    vault = _resolve_vault()
    if vault is None:
        raise NotConfigured("obsidian_vault not configured or does not exist")

    entry = f"- {_timestamp()} — {message}"
    svc = ObsidianService(vault, dry_run=False)
    svc.append_daily(entry)


def on_pipeline_end(**kwargs: Any) -> None:
    """Append a structured run-report block for a finished pipeline run.

    Honors the run's ``dry_run`` flag (``Orchestrator.run_pipeline`` passes
    it through ``run_hook("on_pipeline_end", ..., dry_run=...)``): a dry-run
    pipeline builds an ``ObsidianService(vault, dry_run=True)``, which plans
    the write but never touches the vault (see
    ``hivepilot/services/obsidian_service.py::_write_or_plan``). Absent the
    kwarg (older caller / direct test invocation), defaults to ``False`` —
    a real write, preserving prior behavior.

    Never raises — a broken hook must never crash a run. Silent no-op when
    the vault isn't configured or doesn't exist.
    """
    try:
        vault = _resolve_vault()
        if vault is None:
            return
        entry = (
            f"### Run report — {_timestamp()}\n"
            f"- run_id: {kwargs.get('run_id')}\n"
            f"- pipeline: {kwargs.get('pipeline')}\n"
            f"- status: {kwargs.get('status')}\n"
        )
        svc = ObsidianService(vault, dry_run=bool(kwargs.get("dry_run", False)))
        svc.append_daily(entry)
    except Exception as exc:  # noqa: BLE001 — a hook must never crash a run
        logger.warning("plugin.obsidian.on_pipeline_end_failed", error=str(exc))


def on_error(**kwargs: Any) -> None:
    """Append a structured failure-report block for a failed pipeline stage.

    Honors the run's ``dry_run`` flag the same way ``on_pipeline_end`` does
    — see that function's docstring.

    Never raises — a broken hook must never crash a run. Silent no-op when
    the vault isn't configured or doesn't exist.
    """
    try:
        vault = _resolve_vault()
        if vault is None:
            return
        entry = (
            f"### Run error — {_timestamp()}\n"
            f"- run_id: {kwargs.get('run_id')}\n"
            f"- pipeline: {kwargs.get('pipeline')}\n"
            f"- stage: {kwargs.get('stage')}\n"
        )
        svc = ObsidianService(vault, dry_run=bool(kwargs.get("dry_run", False)))
        svc.append_daily(entry)
    except Exception as exc:  # noqa: BLE001 — a hook must never crash a run
        logger.warning("plugin.obsidian.on_error_failed", error=str(exc))


def health(**kwargs: Any) -> HealthStatus:
    """`ok` when `settings.obsidian_vault` is set (differs from the field
    default) AND exists on disk; `error` when it's set but the path is
    missing; `degraded` ("not configured") when it's still the field
    default — see the `_DEFAULT_OBSIDIAN_VAULT` note above. Only the
    boolean/existence is reported, never the path's contents.
    """
    from hivepilot.config import settings

    vault = settings.obsidian_vault
    path = Path(vault).expanduser()
    if path.exists():
        return HealthStatus("ok", "vault configured and present")
    if path == _DEFAULT_OBSIDIAN_VAULT:
        return HealthStatus("degraded", "not configured")
    return HealthStatus("error", "obsidian_vault configured but path does not exist")


# `RunnerPayload.metadata` field that ends up verbatim in a runner's rendered
# prompt (see `ClaudeRunner._build_prompt`) — same injection target
# `plugins/mem0.py` uses (see that module's "Recall injection field" note).
_RECALL_FIELD = "extra_prompt"

# Private marker set on a task's shared `metadata` dict once `recall` has run
# for it, so a later step of the same multi-step task doesn't re-scan the
# vault / re-append excerpts — same idempotency shape as mem0's
# `_mem0_recalled` sentinel (`plugins/mem0.py`). `_`-prefixed so it reads as
# private and is never rendered into a prompt (`ClaudeRunner._build_prompt`
# reads only the specific `extra_prompt`/`prior_context` keys, never the
# whole `metadata` dict).
_RECALL_SENTINEL_KEY = "_obsidian_recalled"

# ${secret:NAME} — NAME is a catalog key (letters, digits, _ . -). Same
# pattern as `hivepilot.services.secret_refs._SECRET_REF_RE`, duplicated
# here (rather than imported) so `recall` never pulls in that module's
# resolution machinery — reading a vault note must only ever DETECT/STRIP a
# secret reference, never resolve one.
_SECRET_REF_RE = re.compile(r"\$\{secret:([A-Za-z0-9_.\-]+)\}")

# Read-only vault scan bounds for `recall` — keeps a large vault's grep pass
# cheap and the injected block small, both enforced regardless of how many
# notes actually match.
_MAX_NOTES_SCANNED = 500
_MAX_RESULTS = 5
_EXCERPT_CHARS = 400


def _strip_secret_refs(text: str) -> str:
    """Strip literal ``${secret:NAME}`` tokens from *text*.

    `recall` reads vault note TEXT ONLY and must never resolve or forward a
    secret reference into a rendered prompt — this replaces each token with
    a neutral placeholder (never the literal `${` / `}` characters, so the
    result can't be mistaken for a still-live reference downstream).
    """
    return _SECRET_REF_RE.sub("[secret-ref-omitted]", text)


def _first_matching_excerpt(text: str, terms: list[str]) -> str:
    """Return the first line of *text* containing any (lowercased) *terms*,
    else the note's first non-empty line. Capped to `_EXCERPT_CHARS`."""
    for line in text.splitlines():
        lower = line.lower()
        if any(term in lower for term in terms):
            return line.strip()[:_EXCERPT_CHARS]
    for line in text.splitlines():
        if line.strip():
            return line.strip()[:_EXCERPT_CHARS]
    return ""


# Per-note read cap: bound how much of any single note is pulled into memory
# for scoring/excerpting, so one pathological note (e.g. a multi-MB pasted log)
# can't blow up recall. The `obsidian_recall_max_bytes` cap in `recall` only
# bounds the INJECTED block, never the read — this bounds the read itself.
# Matches beyond this cap are intentionally ignored (v1 relevance heuristic).
_MAX_NOTE_READ_BYTES = 64 * 1024


def _search_vault(vault: Path, query_terms: list[str]) -> list[tuple[Path, str]]:
    """Simple ranked grep over the vault's `.md` notes for *query_terms*.

    v1 relevance heuristic: filename matches are weighted higher (x3) than
    body matches; body score is the raw occurrence count of each
    (lowercased) term. Notes that fail to read (permission error, non-UTF8
    binary blob, etc.) are skipped, never raised. Scans at most
    `_MAX_NOTES_SCANNED` notes and returns at most `_MAX_RESULTS`
    `(path, excerpt)` pairs, highest score first.
    """
    terms = [t.lower() for t in query_terms if t]
    if not terms:
        return []

    try:
        candidates = sorted(vault.rglob("*.md"))[:_MAX_NOTES_SCANNED]
    except OSError:
        return []

    scored: list[tuple[int, Path, str]] = []
    for note in candidates:
        try:
            # Bounded read (see _MAX_NOTE_READ_BYTES): only pull enough of the
            # note into memory to score it and lift a one-line excerpt.
            with note.open("r", encoding="utf-8", errors="ignore") as fh:
                text = fh.read(_MAX_NOTE_READ_BYTES)
        except OSError:
            continue
        name_lower = note.stem.lower()
        text_lower = text.lower()
        score = sum(3 for term in terms if term in name_lower)
        score += sum(text_lower.count(term) for term in terms)
        if score <= 0:
            continue
        scored.append((score, note, _first_matching_excerpt(text, terms)))

    scored.sort(key=lambda item: item[0], reverse=True)
    return [(path, excerpt) for _, path, excerpt in scored[:_MAX_RESULTS]]


def recall(**kwargs: Any) -> None:
    """Search the vault for notes relevant to this step and append bounded
    excerpts to ``extra_prompt`` — mirrors `plugins/mem0.py`'s
    `recall`/`store` injection contract (same field, same
    append-not-overwrite discipline), using the Obsidian vault as the
    context source instead of a mem0 memory store.

    No-op (silently) when `settings.obsidian_enabled` or
    `settings.obsidian_recall_enabled` is False, when the vault isn't
    configured/present, when no `payload` kwarg is supplied, or when this
    task's shared `metadata` dict was already recalled-for (idempotency
    guard — see `_RECALL_SENTINEL_KEY`).

    Reads note TEXT ONLY: any ``${secret:NAME}`` token found in a matched
    excerpt is stripped (never resolved/forwarded) via `_strip_secret_refs`
    before injection. The injected block is APPENDED to any existing
    `extra_prompt` content (e.g. from `plugins/mem0.py`), never overwritten,
    and the injected portion alone is hard-capped to
    `settings.obsidian_recall_max_bytes` (pre-existing `extra_prompt`
    content is never truncated). Never raises.
    """
    try:
        from hivepilot.config import settings

        if not settings.obsidian_enabled or not settings.obsidian_recall_enabled:
            return
        payload = kwargs.get("payload")
        if payload is None:
            return
        metadata = getattr(payload, "metadata", None)
        if not isinstance(metadata, dict):
            return
        if metadata.get(_RECALL_SENTINEL_KEY):
            # Already recalled for this shared metadata dict (same task, a
            # later step) — skip to avoid re-scanning / re-appending.
            return

        vault = _resolve_vault()
        if vault is None:
            return

        role = kwargs.get("role")
        step = getattr(payload, "step", None)
        step_name = getattr(step, "name", None) or ""
        task_name = getattr(payload, "task_name", None) or ""
        terms = [term for term in (task_name, role, step_name) if term]

        results = _search_vault(vault, terms)
        # Mark this metadata dict as recalled-for regardless of outcome —
        # the scan already ran; a later step's before_step call must not
        # re-scan.
        metadata[_RECALL_SENTINEL_KEY] = True
        if not results:
            return

        lines = ["Relevant vault notes:"]
        for note_path, excerpt in results:
            try:
                rel = note_path.relative_to(vault)
            except ValueError:
                rel = note_path
            lines.append(f"- {rel}: {_strip_secret_refs(excerpt)}")
        block = "\n".join(lines)

        max_bytes = max(0, int(settings.obsidian_recall_max_bytes))
        block_bytes = block.encode("utf-8")
        if len(block_bytes) > max_bytes:
            block = block_bytes[:max_bytes].decode("utf-8", errors="ignore")
        if not block:
            return

        existing = metadata.get(_RECALL_FIELD)
        if isinstance(existing, str) and existing:
            metadata[_RECALL_FIELD] = f"{existing}\n\n{block}"
        else:
            metadata[_RECALL_FIELD] = block

        logger.info("plugin.obsidian.recalled", count=len(results))
    except Exception as exc:  # noqa: BLE001 — a hook must never crash a run
        logger.warning("plugin.obsidian.recall_failed", error=str(exc))


def store(**kwargs: Any) -> None:
    """Append a structured step-outcome entry to the SAME daily journal note
    `notify` / `on_pipeline_end` / `on_error` already write to
    (`ObsidianService.append_daily`) — reuses the existing, already-tested
    safe-append path rather than inventing a new note-path scheme.

    Honors the run's ``dry_run`` flag the same way `on_pipeline_end` /
    `on_error` do — see those functions' docstrings. No-op (silently) when
    `settings.obsidian_enabled` is False, the vault isn't
    configured/present, or no `payload` kwarg is supplied. Never raises —
    a hook must never crash a run.
    """
    try:
        from hivepilot.config import settings

        if not settings.obsidian_enabled:
            return
        payload = kwargs.get("payload")
        if payload is None:
            return

        vault = _resolve_vault()
        if vault is None:
            return

        task_name = getattr(payload, "task_name", None) or "unknown"
        role = kwargs.get("role") or "unknown"
        step = getattr(payload, "step", None)
        step_name = getattr(step, "name", None) or "unknown"
        # `after_step` (`Orchestrator._execute_task`) is only ever invoked
        # on the success path today — no `status`/`error` kwarg is threaded
        # through — so this defaults to "success" rather than fabricating a
        # status this hook can't actually observe; a future caller
        # supplying `status=` explicitly overrides it.
        status = kwargs.get("status") or "success"

        output_val = kwargs.get("output")
        summary = ""
        if isinstance(output_val, str) and output_val.strip():
            summary = output_val.strip().splitlines()[0][:200]

        entry_lines = [
            f"### Step outcome — {_timestamp()}",
            f"- task: {task_name}",
            f"- role: {role}",
            f"- step: {step_name}",
            f"- status: {status}",
        ]
        if summary:
            entry_lines.append(f"- summary: {summary}")
        entry = "\n".join(entry_lines)

        svc = ObsidianService(vault, dry_run=bool(kwargs.get("dry_run", False)))
        svc.append_daily(entry)
    except Exception as exc:  # noqa: BLE001 — a hook must never crash a run
        logger.warning("plugin.obsidian.store_failed", error=str(exc))


def register() -> dict[str, Any]:
    from hivepilot.config import settings

    if not settings.obsidian_enabled:
        return {}
    return {
        "notifiers": {"obsidian": notify},
        "before_step": recall,
        "after_step": store,
        "on_pipeline_end": on_pipeline_end,
        "on_error": on_error,
        "health": {"obsidian": health},
    }
