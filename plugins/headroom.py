"""headroom plugin — compresses each step's prompt/context before execution.

`before_step` (lifecycle hook, see docs/v4/PLUGINS.md) mutates the SAME
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

Uses `headroom <https://github.com/headroomlabs-ai/headroom>`_
(``pip install "headroom-ai[all]"``) — NOT a hivepilot dependency, and
deliberately not installed by this plugin. Imported lazily so the plugin
loads fine (and no-ops) when the library isn't present; see
``plugins/rtk.py`` for the same "external tool optional, graceful no-op"
pattern this plugin follows for a token-saving companion tool: rtk
compresses *command output* tokens, this plugin compresses *agent
input/context* tokens (see docs/v4/PLUGINS.md for the complementarity
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


def before_step(**kwargs: Any) -> None:
    """Compress the reachable prompt/context field(s) on ``payload`` in place.

    No-op when ``headroom`` isn't installed (``compress is None``), when no
    ``payload`` kwarg is supplied, or when there's nothing compressible on
    it. Never raises — a broken/misbehaving compression call must not crash
    a pipeline step, the same guarantee every other hook in this repo has.
    """
    try:
        if compress is None:
            return
        payload = kwargs.get("payload")
        if payload is None:
            return
        metadata = getattr(payload, "metadata", None)
        if not isinstance(metadata, dict):
            return

        model_hint = None
        step = getattr(payload, "step", None)
        if step is not None:
            model_hint = (getattr(step, "metadata", None) or {}).get("model")

        for key in _COMPRESSIBLE_METADATA_KEYS:
            original = metadata.get(key)
            if not original or not isinstance(original, str):
                continue
            compressed = compress(original, model=model_hint)
            if not compressed or not isinstance(compressed, str):
                continue
            chars_before = len(original)
            chars_after = len(compressed)
            metadata[key] = compressed
            ratio = round(chars_after / chars_before, 3) if chars_before else 1.0
            logger.info(
                "plugin.headroom.compressed",
                field=key,
                chars_before=chars_before,
                chars_after=chars_after,
                ratio=ratio,
            )
    except Exception as exc:  # noqa: BLE001 — a hook must never crash a run
        logger.warning("plugin.headroom.before_step_failed", error=str(exc))


def register() -> dict[str, Any]:
    return {"before_step": before_step}
