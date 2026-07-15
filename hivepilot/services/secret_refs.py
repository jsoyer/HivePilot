"""``${secret:NAME}`` reference parsing + lazy resolution.

A config string value may reference a named entry in a project's ``secrets:``
catalog via the ``${secret:NAME}`` token. References are resolved LAZILY at
step-assembly time (see ``Orchestrator._resolve_secrets``) — never at
config-load time — by looking NAME up in the catalog and calling the EXISTING
``secret_resolver`` (provider dispatch is reused, never reimplemented here).

Only the ``${secret:...}`` form is handled; other ``${...}`` tokens (e.g.
``${PWD}`` in a container volume spec) are left completely untouched.

Fail mode (per-project policy, default ``"closed"``):
  * ``"closed"``   — any unresolved/errored reference aborts the step/run.
  * ``"fallback"`` — on provider error (or a missing catalog entry), try the
                     ``env``/``file`` providers keyed by the reference NAME;
                     if nothing resolves, still abort.

Error and warning messages name the reference NAME and provider NAME ONLY —
never the resolved content, and never the underlying store key/path — mirroring
the discipline the codebase already applies to tokens.
"""

from __future__ import annotations

import re
from typing import Any

from hivepilot.services.config_provenance import register_secret_value
from hivepilot.services.secrets_service import secret_resolver
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

# ${secret:NAME} — NAME is a catalog key (letters, digits, _ . -). Deliberately
# narrow so ${PWD}, ${env:FOO}, ${OTHER} and bare ${...} are never matched.
_SECRET_REF_RE = re.compile(r"\$\{secret:([A-Za-z0-9_.\-]+)\}")

# Providers tried, in order, when falling back after a provider error / missing
# catalog entry. Each is keyed by the reference NAME itself.
_FALLBACK_CHAIN: tuple[tuple[str, str], ...] = (("env", "key"), ("file", "path"))


class SecretReferenceError(RuntimeError):
    """A ``${secret:NAME}`` reference could not be resolved.

    The message names the reference and provider ONLY — never resolved content.
    """


def find_secret_refs(text: str) -> list[str]:
    """Return the NAMEs of every ``${secret:NAME}`` token in *text* (in order)."""
    if not isinstance(text, str) or not text:
        return []
    return _SECRET_REF_RE.findall(text)


def has_secret_ref(text: str) -> bool:
    """True if *text* contains at least one ``${secret:NAME}`` token."""
    return bool(isinstance(text, str) and text and _SECRET_REF_RE.search(text))


def resolve_secret_refs(
    values: dict[str, str],
    *,
    catalog: dict[str, dict[str, Any]],
    fail_mode: str = "closed",
) -> dict[str, str]:
    """Resolve ``${secret:NAME}`` tokens found in *values*.

    *values* is an env-like ``key -> string`` mapping (e.g. ``project.env``).
    *catalog* maps ``NAME -> spec`` (the project ``secrets:`` section), where
    each spec is the same ``{source, ...}`` shape ``secret_resolver`` consumes.

    Returns a NEW mapping containing ONLY the entries whose value held at least
    one reference, with every token substituted for its resolved value. Entries
    with no reference are omitted (the caller keeps their originals).

    Every resolved value is registered for masking via
    ``config_provenance.register_secret_value``.
    """
    resolved: dict[str, str] = {}
    for key, raw in values.items():
        if not isinstance(raw, str) or not has_secret_ref(raw):
            continue
        rendered = raw
        for name in find_secret_refs(raw):
            secret_value = _resolve_one(name, catalog=catalog, fail_mode=fail_mode)
            register_secret_value(secret_value)
            rendered = rendered.replace(f"${{secret:{name}}}", secret_value)
        resolved[key] = rendered
    return resolved


def _resolve_one(name: str, *, catalog: dict[str, dict[str, Any]], fail_mode: str) -> str:
    """Resolve a single reference NAME to its secret string, honouring fail_mode."""
    spec = catalog.get(name)
    if spec is None:
        if fail_mode == "fallback":
            fb = _fallback(name)
            if fb is not None:
                return fb
        raise SecretReferenceError(
            f"secret reference '{name}' is not defined in the project 'secrets:' catalog"
        )

    provider = str(spec.get("source", "env"))
    try:
        # Reuse the existing provider dispatch — never reimplement it here.
        return secret_resolver.resolve({name: spec})[name]
    except Exception:
        if fail_mode == "fallback":
            fb = _fallback(name)
            if fb is not None:
                return fb
        # `from None`: the underlying provider exception may embed a store key
        # or path; suppress it so the surfaced error names ref + provider only.
        raise SecretReferenceError(
            f"failed to resolve secret reference '{name}' via provider '{provider}'"
        ) from None


def _fallback(name: str) -> str | None:
    """Best-effort fallback: try env/file providers keyed by *name*.

    On success, logs a WARNING naming the provider only and registers the value
    for masking. Returns ``None`` when nothing resolves.
    """
    for source, spec_key in _FALLBACK_CHAIN:
        try:
            value = secret_resolver.resolve({name: {"source": source, spec_key: name}})[name]
        except Exception:
            continue
        logger.warning("secret_ref.fallback", ref=name, provider=source)
        register_secret_value(value)
        return value
    return None
