"""honcho memory backend — a model of a ROLE over time, not a fact store.

Read from honcho's own docs (honcho.dev and plastic-labs/honcho, 2026-08-17).
It is not mem0 under another name:

- mem0 extracts and returns FACTS;
- honcho ingests MESSAGES and returns **Representations** — conclusions it
  derives about a *Peer*, an entity that persists and changes over time.

The natural Peer here is a **role**. How Victor reviews, how Hugo audits, and
how that drifts across runs is a different question from "what did we decide
about worktrees". That is why enabling honcho alongside mem0 is legitimate
rather than redundant — and why enabling it alongside Hindsight would not be.

**Semantics: ADDITIVE.** `recall` appends to `extra_prompt` and never replaces
it, mirroring `plugins/obsidian.py`. Declared through `RECALL_SEMANTICS` so
`hivepilot.services.memory_kind.resolve_composition` can rule on combinations:
declaring it wrongly would silently permit a pairing that destroys another
backend's recall.

**Dormant unless configured.** No package, no API key, or a raising client all
degrade to doing nothing. A memory backend that can fail a pipeline is worse
than no memory at all, and every other hook here holds the same contract.

⚠️ The PyPI package is ``honcho-ai``. Plain ``honcho`` is an unrelated Procfile
runner — installing that and importing ``honcho`` imports something else
entirely, which is exactly how a plugin ends up silently inert.

**Self-hosting is supported** (AGPL-3.0, ``docker compose``, Postgres+pgvector
and an LLM key): set ``HIVEPILOT_HONCHO_URL`` and the client targets it rather
than the managed service. Worth stating because the marketing page implies a
managed service only.
"""

from __future__ import annotations

import os
from typing import Any

from hivepilot.services.memory_kind import RecallSemantics
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

#: Read by the engine's composition check. See the module docstring.
RECALL_SEMANTICS = RecallSemantics.ADDITIVE

_RECALL_FIELD = "extra_prompt"
#: `Orchestrator._execute_task` builds ONE metadata dict per task and reuses
#: that same object for every step. Without a sentinel the representation is
#: appended on every step — headroom's and mem0's exact problem. `_`-prefixed
#: so it reads as private and is never rendered into a prompt.
_RECALL_SENTINEL = "_honcho_recalled"


def _enabled() -> bool:
    """Read the setting, not the raw env var.

    `Settings` is the single place a deployment configures anything, and the
    repository's gating conformance test asserts every plugin stem has a
    matching `<stem>_enabled` flag there. Reading `os.environ` directly would
    pass by accident and drift the day the setting is renamed.
    """
    from hivepilot.config import settings

    return bool(getattr(settings, "honcho_enabled", False))


def _client() -> Any | None:
    """Build a honcho client, or None when the deployment has not set one up.

    Imported lazily: a plugin that hard-imports an optional dependency makes
    the whole plugin set fail to load on a stock install.
    """
    api_key = os.environ.get("HONCHO_API_KEY", "").strip()
    workspace = os.environ.get("HIVEPILOT_HONCHO_WORKSPACE", "hivepilot").strip()
    base_url = os.environ.get("HIVEPILOT_HONCHO_URL", "").strip()
    if not api_key and not base_url:
        return None
    try:
        from honcho import Honcho  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001 - the package is optional by design
        return None

    kwargs: dict[str, Any] = {"workspace_id": workspace}
    if api_key:
        kwargs["api_key"] = api_key
    if base_url:
        kwargs["base_url"] = base_url
    return Honcho(**kwargs)


def _session_id(payload: Any) -> str:
    project = getattr(payload, "project_name", None) or "unknown"
    task = getattr(payload, "task_name", None) or "unknown"
    return f"{project}:{task}"


def recall(**kwargs: Any) -> None:
    """Ask honcho what it has concluded about this role, and append it.

    Appends rather than replaces, so a block written by mem0 or obsidian
    survives underneath ours. Never raises: a memory backend that can fail a
    pipeline is worse than no memory.
    """
    if not _enabled():
        return
    payload = kwargs.get("payload")
    role = kwargs.get("role")
    metadata = getattr(payload, "metadata", None)
    if metadata is None or not role:
        return
    if metadata.get(_RECALL_SENTINEL):
        return
    metadata[_RECALL_SENTINEL] = True

    try:
        client = _client()
        if client is None:
            return
        peer = client.peer(str(role))
        answer = peer.chat(
            "What should this role keep in mind from how it has worked before? "
            "Answer in at most three short bullets, or say nothing if you have "
            "no grounded conclusion."
        )
    except Exception as exc:  # noqa: BLE001 - best-effort, like every memory hook
        logger.warning("plugin.honcho.recall_failed", error=str(exc))
        return

    text = (answer or "").strip()
    if not text:
        return

    block = f"What honcho has observed about {role}:\n{text}"
    existing = (metadata.get(_RECALL_FIELD) or "").strip()
    metadata[_RECALL_FIELD] = f"{existing}\n\n{block}" if existing else block
    logger.info("plugin.honcho.recalled", role=str(role))


def store(**kwargs: Any) -> None:
    """Feed this step's output into the role's session.

    honcho reasons over messages, so the raw output is the right unit -- it
    does the extraction itself, unlike mem0 where we would pre-digest.
    """
    if not _enabled():
        return
    payload = kwargs.get("payload")
    role = kwargs.get("role")
    output = (kwargs.get("output") or "").strip()
    # A step that produced nothing teaches nothing, and an empty message would
    # still cost a reasoning pass on honcho's side.
    if not output or not role:
        return

    try:
        client = _client()
        if client is None:
            return
        peer = client.peer(str(role))
        session = client.session(_session_id(payload))
        session.add_messages([peer.message(output)])
    except Exception as exc:  # noqa: BLE001 - best-effort
        logger.warning("plugin.honcho.store_failed", error=str(exc))
        return
    logger.info("plugin.honcho.stored", role=str(role))


def health(**kwargs: Any) -> dict[str, Any]:
    """Report configured/idle/absent without ever raising.

    "Disabled" and "enabled but unreachable" are deliberately distinct: a
    health check that collapses them is how a backend stays dead in plain
    sight.
    """
    if not _enabled():
        return {"status": "idle", "detail": "HIVEPILOT_HONCHO_ENABLED is off"}
    try:
        if _client() is None:
            return {
                "status": "error",
                "detail": "enabled, but no client -- set HONCHO_API_KEY or "
                "HIVEPILOT_HONCHO_URL, and install the `honcho-ai` package "
                "(NOT `honcho`, which is an unrelated Procfile runner)",
            }
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "detail": str(exc)}
    return {"status": "ok", "detail": "honcho client reachable"}


def register() -> dict[str, Any]:
    """Register nothing at all when the flag is off.

    Returning hooks that no-op would leave a disabled backend visible in the
    hook chain -- and this codebase's rule is the stricter one: a plugin that
    is off contributes nothing, so it cannot be mistaken for one that is on
    and silent.
    """
    if not _enabled():
        return {}
    return {"before_step": recall, "after_step": store, "health": {"honcho": health}}
