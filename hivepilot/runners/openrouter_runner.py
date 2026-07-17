"""OpenRouterRunner — new, API-only built-in runner (Sprint 2,
runner-defaults-plugins-mode PRD).

A thin subclass of `PromptCliRunner` that reuses the EXISTING OpenRouter
branch of `PromptCliRunner._run_api` (`elif provider == "openrouter":` —
unchanged, untouched invocation logic; see prompt_cli_runner.py) and adds
three things on top:

1. `supported_modes = frozenset({"api"})` — openrouter has no CLI binary of
   its own, so a `mode: cli` step must never reach a subprocess call. The
   orchestrator only calls `validate_runner_mode` when the resolved mode is
   non-"cli" (`_resolve_effective_mode` short-circuits on the "cli" default
   — see `hivepilot/orchestrator.py`), so this runner ALSO validates its own
   resolved mode at the top of `run()`/`capture()` — closing the gap for the
   "no mode configured at all" case (which resolves to "cli") too, not just
   an explicit `mode: cli`.

2. `api_provider` is force-set to `"openrouter"` in `__post_init__`,
   regardless of what a stray `options["api_provider"]` might already say —
   a step wired to `kind: openrouter` always routes through the openrouter
   branch of `_run_api`. Immutable: builds a NEW `RunnerDefinition` via
   `model_copy`, never mutates the instance the caller passed in.

3. Fail-closed + mask-at-the-runner, mirroring the Sprint-1 pattern
   established by `ClaudeRunner._run_api`: a missing `OPENROUTER_API_KEY`
   raises a clear `RuntimeError` naming `${secret:OPENROUTER_API_KEY}` before
   any HTTP call, and the resolved key is registered via
   `register_secret_value` + the returned/raised text is passed through
   `redact_text` AT the runner — so `RunResult.detail` is masked even if the
   provider (or an HTTP error body) reflects the key back, never relying on
   any downstream sink alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from hivepilot.runners.base import RunnerPayload, validate_runner_mode
from hivepilot.runners.prompt_cli_runner import PromptCliRunner
from hivepilot.services.config_provenance import redact_text, register_secret_value
from hivepilot.utils.env import merge_environments

_KIND = "openrouter"


@dataclass
class OpenRouterRunner(PromptCliRunner):
    """API-only runner routed through OpenRouter's provider-agnostic
    chat-completions endpoint (https://openrouter.ai/api/v1/chat/completions).
    """

    command_name: str = "openrouter"
    # ClassVar so @dataclass does not treat it as an instance field (mirrors
    # PromptCliRunner.supported_modes) — strictly api-only, unlike the
    # cli+api default every other prompt-cli subclass inherits.
    supported_modes: ClassVar[frozenset[str]] = frozenset({"api"})

    def __post_init__(self) -> None:
        # Force api_provider — immutable update via model_copy; never mutate
        # the RunnerDefinition instance the caller passed in.
        forced_options = {**self.definition.options, "api_provider": "openrouter"}
        self.definition = self.definition.model_copy(update={"options": forced_options})

    def _resolve_mode(self, payload: RunnerPayload) -> str:
        """Same resolution channel PromptCliRunner.run/capture already use:
        step metadata wins over the runner definition's options, falling
        back to "cli"."""
        return (
            payload.step.metadata.get("mode") or self.definition.options.get("mode") or "cli"
        ).lower()

    def _resolved_api_key(self, payload: RunnerPayload) -> str:
        env = merge_environments(payload.project.env, self.definition.env, payload.secrets)
        api_key = env.get("OPENROUTER_API_KEY")
        if not api_key:
            raise RuntimeError(
                "OPENROUTER_API_KEY is not set — refusing to run the openrouter "
                "runner without a key (fail closed). Provide it via "
                "${secret:OPENROUTER_API_KEY} on the step/runner definition, or "
                "the OPENROUTER_API_KEY environment variable."
            )
        # Register the resolved key so it is redacted everywhere downstream,
        # and so the redact_text calls below actually mask it.
        register_secret_value(api_key)
        return api_key

    def run(self, payload: RunnerPayload) -> None:
        mode = self._resolve_mode(payload)
        validate_runner_mode(_KIND, self.supported_modes, mode)
        self._resolved_api_key(payload)
        try:
            super().run(payload)
        except Exception as exc:  # noqa: BLE001 - mask AT the runner, then re-raise
            raise RuntimeError(redact_text(str(exc))) from None

    def capture(self, payload: RunnerPayload) -> str:
        mode = self._resolve_mode(payload)
        validate_runner_mode(_KIND, self.supported_modes, mode)
        self._resolved_api_key(payload)
        try:
            text = super().capture(payload)
        except Exception as exc:  # noqa: BLE001 - mask AT the runner, then re-raise
            raise RuntimeError(redact_text(str(exc))) from None
        # Mask AT the runner — RunResult.detail is known-unredacted at the
        # choke point, so never depend on a downstream sink to catch the key.
        return redact_text(text)
