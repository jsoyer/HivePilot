"""headroom plugin — compresses each step's prompt/context before execution.

`before_step` (lifecycle hook, see docs/PLUGINS.md) mutates the SAME
`RunnerPayload` object the orchestrator hands straight through to
`registry.capture_definition(...)` / `runner.capture(...)`. In
`Orchestrator._execute_task` (`hivepilot/orchestrator.py`), the `payload`
built at the top of the per-step loop is the exact object passed to
``self.plugins.run_hook("before_step", payload=payload)`` AND, moments
later, to the runner (``self.registry.capture_definition(runner_def,
payload)`` / ``self._capture_or_execute(runner_key, payload)``) — no copy
is made anywhere in between. Because ``RunnerPayload.metadata``
(`hivepilot/runners/base.py`) is a plain, mutable ``dict``, an in-place
edit here IS seen by every runner's prompt builder. e.g.
``ClaudeRunner._build_prompt`` (`hivepilot/runners/claude_runner.py`)
reads ``payload.metadata.get("extra_prompt")`` and
``payload.metadata.get("prior_context")`` directly off this same object
when it assembles the volatile tail of the prompt it sends to the model.

``prior_context`` is the highest-value target: it accumulates every
upstream stage's full output across a multi-stage pipeline
(``hivepilot.orchestrator.build_prior_context``) and is typically the
largest chunk of a step's prompt — this is also why PRD A2 built a keyed
routing alternative for it. ``extra_prompt`` (the user's free-text
instructions for the run) is compressed too when present, since it flows
into the very same prompt.

**Idempotency (shared ``metadata`` dict):** ``Orchestrator._execute_task``
builds ONE ``metadata`` dict per *task* (``hivepilot/orchestrator.py``,
``metadata = {"extra_prompt": ..., "prior_context": ...}``) and reuses
that SAME dict object, by reference, for every step's ``RunnerPayload`` in
``task.steps``. Compressing on every ``before_step`` call would therefore
re-compress already-compressed text on step 2 onward — lossy-on-lossy,
degrading unbounded across a multi-step task. A private sentinel key
(``_headroom_compressed``) is set on the shared ``metadata`` dict after the
first successful pass; subsequent calls for the same dict short-circuit
before touching ``compress`` again. The sentinel is safe to leave on
``metadata``: every runner that reads prompt-relevant fields off it
(``ClaudeRunner._build_prompt`` / the prompt-cli runner) reads specific
keys (``extra_prompt``, ``prior_context``) rather than iterating or
serializing the whole dict, so the sentinel never reaches a rendered
prompt.

**Opt-in (dormant by default):** gated on ``settings.headroom_enabled``
(``hivepilot/config.py``, default ``False``, env
``HIVEPILOT_HEADROOM_ENABLED``) — mirrors PRD A2's
``context_routing_mode`` opt-in pattern. The plugin ships dormant even
when this file is present and ``headroom-ai`` is installed; an operator
must explicitly flip the flag.

Uses `headroom <https://github.com/headroomlabs-ai/headroom>`_
(``pip install "headroom-ai[all]"``) — NOT a hivepilot dependency, and
deliberately not installed by this plugin. Imported lazily so the plugin
loads fine (and no-ops) when the library isn't present; see
``plugins/rtk.py`` for the same "external tool optional, graceful no-op"
pattern this plugin follows for a token-saving companion tool: rtk
compresses *command output* tokens, this plugin compresses *agent
input/context* tokens (see docs/PLUGINS.md for the complementarity
note).

Deliberately NOT a ``@dataclass``: local-file plugins are loaded via
``importlib.util.spec_from_file_location()`` / ``exec_module()``
(``hivepilot.plugins._scan_local_plugins``), which never registers the
module in ``sys.modules``. Combined with ``from __future__ import
annotations``, that trips a real CPython 3.14 ``dataclasses`` bug
(``_is_type`` does ``sys.modules[cls.__module__].__dict__``, which is
``None`` for an unregistered module) — see ``plugins/rtk.py`` for the
full write-up. This plugin sticks to plain functions, sidestepping the
issue entirely.
"""

from __future__ import annotations

from typing import Any

from hivepilot.plugins import HealthStatus
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

try:
    from headroom import compress
except ImportError:  # headroom-ai is optional — never installed by this plugin
    compress = None  # type: ignore[assignment]

# RunnerPayload.metadata keys that end up verbatim in a runner's rendered
# prompt (see ClaudeRunner._build_prompt) and are therefore worth
# compressing. prior_context is checked first — it's usually the largest —
# then extra_prompt.
_COMPRESSIBLE_METADATA_KEYS = ("prior_context", "extra_prompt")

# Private marker set on a task's shared `metadata` dict once `before_step`
# has run for it — see the "Idempotency" note in the module docstring.
# `_`-prefixed so it reads as private; never consumed by any runner (they
# read specific keys off `metadata`, never iterate/dump the whole dict —
# see TestSentinelKeyNeverRenderedIntoPrompt in tests/test_headroom.py).
_SENTINEL_KEY = "_headroom_compressed"


def before_step(**kwargs: Any) -> None:
    """Compress the reachable prompt/context field(s) on ``payload`` in place.

    No-op when ``settings.headroom_enabled`` is False (the default —
    dormant/opt-in), when ``headroom`` isn't installed (``compress is
    None``), when no ``payload`` kwarg is supplied, when there's nothing
    compressible on it, or when this ``metadata`` dict was already
    compressed (idempotency guard — see the module docstring). Never
    raises — a broken/misbehaving compression call must not crash a
    pipeline step, the same guarantee every other hook in this repo has.
    """
    try:
        from hivepilot.config import settings

        if not settings.headroom_enabled:
            return
        if compress is None:
            return
        payload = kwargs.get("payload")
        if payload is None:
            return
        metadata = getattr(payload, "metadata", None)
        if not isinstance(metadata, dict):
            return
        if metadata.get(_SENTINEL_KEY):
            # Already compressed for this shared metadata dict (same task,
            # a later step) — skip to avoid lossy-on-lossy re-compression.
            return

        model_hint = None
        step = getattr(payload, "step", None)
        if step is not None:
            model_hint = (getattr(step, "metadata", None) or {}).get("model")

        attempted_any = False
        for key in _COMPRESSIBLE_METADATA_KEYS:
            original = metadata.get(key)
            if not original or not isinstance(original, str):
                continue
            compressed = compress(original, model=model_hint)
            if not compressed or not isinstance(compressed, str):
                continue
            attempted_any = True
            chars_before = len(original)
            chars_after = len(compressed)
            if chars_after >= chars_before:
                logger.info(
                    "plugin.headroom.skipped_non_shrinking",
                    field=key,
                    chars_before=chars_before,
                    chars_after=chars_after,
                )
                continue
            metadata[key] = compressed
            ratio = round(chars_after / chars_before, 3)
            logger.info(
                "plugin.headroom.compressed",
                field=key,
                chars_before=chars_before,
                chars_after=chars_after,
                ratio=ratio,
            )

        # Only mark this metadata dict as "done" when we actually attempted a
        # compression pass — an empty/no-compressible-fields payload leaves
        # no sentinel behind (nothing to guard against re-running).
        if attempted_any:
            metadata[_SENTINEL_KEY] = True
    except Exception as exc:  # noqa: BLE001 — a hook must never crash a run
        logger.warning("plugin.headroom.before_step_failed", error=str(exc))


def health(**kwargs: Any) -> HealthStatus:
    """`error` when `headroom-ai` isn't importable (`compress is None`);
    otherwise `ok`/`degraded` reflecting `settings.headroom_enabled` — ships
    dormant by default, so "installed but disabled" is the common, expected
    steady state, not a failure. No values in the detail — presence/config
    booleans only.
    """
    if compress is None:
        return HealthStatus("error", "headroom-ai not installed")

    from hivepilot.config import settings

    if not settings.headroom_enabled:
        return HealthStatus("degraded", "installed but disabled (headroom_enabled=False)")
    return HealthStatus("ok", "installed and enabled")


def register() -> dict[str, Any]:
    return {"before_step": before_step, "health": {"headroom": health}}
