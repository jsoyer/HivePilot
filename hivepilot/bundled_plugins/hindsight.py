"""Hindsight memory backend — world-facts via retain/recall (HP-51).

Hindsight (vectorize-io/hindsight) is a persistent memory engine: temporal +
semantic + entity memory on Postgres/pgvector. HivePilot does **not** embed
that stack. This plugin is an HTTP client (`hindsight-client`) pointed at a
Hindsight server the operator deploys (Docker / `hindsight-api` / Cloud).

The three operations Hindsight names:

- **retain** — store (our ``after_step`` / ``store``)
- **recall** — retrieve (our ``before_step``)
- **reflect** — reasoning over the bank (HP-54: mission ``narrative`` via
  ``hivepilot.services.hindsight_reflect``)

**Semantics: ADDITIVE.** ``recall`` appends to ``extra_prompt`` and never
replaces it, so Hindsight composes with honcho (role model) and obsidian
(local vault). mem0 is also a world-fact store — enabling both is a
deployment cost choice, not a composition bug.

**Dormant unless configured.** No package, no reachable client, or a raising
call all degrade to doing nothing. A memory backend that can fail a pipeline
is worse than no memory.

⚠️ The PyPI package is ``hindsight-client`` (import ``hindsight_client``).
``hindsight-api`` is the *server*. Embedding ``MemoryEngine`` in-process would
pull Hindsight's LLM + Postgres into the HivePilot worker — that is the
server's job, not this hook.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from hivepilot.plugins import hook_phase
from hivepilot.services.memory_kind import RecallSemantics
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

RECALL_SEMANTICS = RecallSemantics.ADDITIVE

_RECALL_FIELD = "extra_prompt"
_RECALL_SENTINEL = "_hindsight_recalled"
_DEFAULT_BASE_URL = "http://127.0.0.1:8888"
_MAX_MEMORIES = 8


def _enabled() -> bool:
    from hivepilot.config import settings

    return bool(getattr(settings, "hindsight_enabled", False))


def _base_url() -> str:
    from hivepilot.config import settings

    return (getattr(settings, "hindsight_base_url", None) or _DEFAULT_BASE_URL).strip()


def leaves_host(base_url: str | None = None) -> bool:
    """True when retain/recall leave this machine (Hindsight Cloud / remote API).

    Loopback is the Docker/pip default and stays on the host. Anything else
    — including ``api.hindsight.vectorize.io`` — is egress.
    """
    parsed = urlparse(base_url or _base_url())
    host = (parsed.hostname or "").lower()
    return host not in {"", "localhost", "127.0.0.1", "::1"}


def _client() -> Any | None:
    """Build a Hindsight HTTP client, or None when the extra is missing."""
    try:
        from hindsight_client import Hindsight  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001 — optional extra
        return None

    from hivepilot.config import settings

    kwargs: dict[str, Any] = {"base_url": _base_url()}
    api_key = (getattr(settings, "hindsight_api_key", None) or "").strip()
    if api_key:
        kwargs["api_key"] = api_key
    return Hindsight(**kwargs)


def _bank_id(payload: Any, role: Any) -> str:
    # Episodic run memory: ``{project}:{task}:{role}``. Role identity
    # (mission / directives) lives in ``role:{name}`` — see
    # hivepilot.services.hindsight_role_sync (HP-52). Do not collapse the two.
    from hivepilot.config import settings

    override = (getattr(settings, "hindsight_bank_id", None) or "").strip()
    if override:
        return override
    project = getattr(payload, "project_name", None) or "unknown"
    task = getattr(payload, "task_name", None) or "unknown"
    if role:
        return f"{project}:{task}:{role}"
    return f"{project}:{task}"


def _extract_texts(results: Any) -> list[str]:
    """Tolerate the shapes Hindsight has shipped (list / object / dict)."""
    if results is None:
        return []
    if isinstance(results, str):
        text = results.strip()
        return [text] if text else []
    payload = results
    for attr in ("results", "memories", "items", "data"):
        if hasattr(results, attr):
            payload = getattr(results, attr)
            break
        if isinstance(results, dict) and attr in results:
            payload = results[attr]
            break
    if isinstance(payload, str):
        text = payload.strip()
        return [text] if text else []
    if not isinstance(payload, list):
        return []
    texts: list[str] = []
    for item in payload:
        if isinstance(item, str):
            text = item.strip()
        elif isinstance(item, dict):
            text = str(item.get("text") or item.get("content") or item.get("memory") or "").strip()
        else:
            text = str(
                getattr(item, "text", None)
                or getattr(item, "content", None)
                or getattr(item, "memory", None)
                or ""
            ).strip()
        if text:
            texts.append(text)
    return texts


def _record_search(namespace: str, query: str, result_count: int, actor: Any, run_id: Any) -> None:
    try:
        from hivepilot.services import memory_service

        memory_service.record_search(
            namespace=namespace,
            query=query,
            result_count=result_count,
            actor=actor or "system",
            backend="hindsight",
            run_id=run_id,
        )
    except Exception as exc:  # noqa: BLE001 — instrumentation must never break recall
        logger.warning("plugin.hindsight.instrumentation_failed", op="search", error=str(exc))


def _record_store(namespace: str, actor: Any) -> None:
    try:
        from hivepilot.services import memory_service

        memory_service.record_store(
            namespace=namespace, key=namespace, actor=actor or "system", backend="hindsight"
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("plugin.hindsight.instrumentation_failed", op="store", error=str(exc))


@hook_phase("retrieve")
def recall(**kwargs: Any) -> None:
    """Ask Hindsight what it already knows, and append it.

    Never raises. Skips untrusted review payloads so a hostile diff cannot
    be recalled later as if it were ours (same rule as mem0).
    """
    if not _enabled():
        return
    payload = kwargs.get("payload")
    metadata = getattr(payload, "metadata", None)
    if metadata is None:
        return
    if metadata.get(_RECALL_SENTINEL):
        return
    metadata[_RECALL_SENTINEL] = True

    try:
        from hivepilot.services.review_context import is_untrusted

        if is_untrusted(payload):
            logger.info("plugin.hindsight.recall_skipped_untrusted_input")
            return
        client = _client()
        if client is None:
            return
        role = kwargs.get("role")
        bank = _bank_id(payload, role)
        step = getattr(payload, "step", None)
        step_name = getattr(step, "name", None) or ""
        query = f"{getattr(payload, 'task_name', '')} {step_name}".strip() or bank
        texts = _extract_texts(client.recall(bank_id=bank, query=query))[:_MAX_MEMORIES]
        _record_search(bank, query, len(texts), role, kwargs.get("run_id"))
        if not texts:
            return
        block = "Relevant memories (hindsight):\n" + "\n".join(f"- {m}" for m in texts)
        existing = (metadata.get(_RECALL_FIELD) or "").strip()
        metadata[_RECALL_FIELD] = f"{existing}\n\n{block}" if existing else block
        logger.info("plugin.hindsight.recalled", count=len(texts), bank=bank)
    except Exception as exc:  # noqa: BLE001 — a hook must never crash a run
        logger.warning("plugin.hindsight.recall_failed", error=str(exc))


def store(**kwargs: Any) -> None:
    """Retain this step's redacted output in the Hindsight bank."""
    if not _enabled():
        return
    payload = kwargs.get("payload")
    output = (kwargs.get("output") or "").strip()
    if not output:
        return

    try:
        from hivepilot.services.config_provenance import redact_text

        client = _client()
        if client is None:
            return
        role = kwargs.get("role")
        bank = _bank_id(payload, role)
        project = getattr(payload, "project_name", None) or "unknown"
        task = getattr(payload, "task_name", None) or "unknown"
        content = redact_text(f"[{project}:{task}:{role or '-'}]\n{output}")
        client.retain(bank_id=bank, content=content)
        _record_store(bank, role)
        logger.info("plugin.hindsight.retained", bank=bank)
    except Exception as exc:  # noqa: BLE001
        logger.warning("plugin.hindsight.store_failed", error=str(exc))


def health(**kwargs: Any) -> dict[str, Any]:
    """Report idle / missing extra / configured without ever raising."""
    if not _enabled():
        return {"status": "idle", "detail": "HIVEPILOT_HINDSIGHT_ENABLED is off"}
    try:
        if _client() is None:
            return {
                "status": "error",
                "detail": (
                    "enabled, but no client — install `hindsight-client` "
                    "(the HTTP SDK, not `hindsight-api` which is the server) "
                    "and point HIVEPILOT_HINDSIGHT_BASE_URL at a running Hindsight"
                ),
            }
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "detail": str(exc)}
    where = "remote" if leaves_host() else "loopback"
    return {"status": "ok", "detail": f"hindsight client configured ({where})"}


def register() -> dict[str, Any]:
    """Register nothing when the flag is off — same stricter rule as honcho."""
    if not _enabled():
        return {}
    return {"before_step": recall, "after_step": store, "health": {"hindsight": health}}
