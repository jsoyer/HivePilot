"""OpenAiCompatRunner — API-only built-in runner for any OpenAI-compatible
chat-completions endpoint (OpenAI itself, OpenCode Zen, Ollama Cloud,
LM Studio, vLLM, …).

A thin subclass of `PromptCliRunner` that reuses the EXISTING `openai` branch
of `PromptCliRunner._run_api` (`if provider == "openai":` — unchanged) and,
exactly like `OpenRouterRunner`, adds three things on top:

1. `supported_modes = frozenset({"api"})` — there is no CLI binary, so a
   `mode: cli` step must never reach a subprocess call.

2. `api_provider` is force-set to `"openai"` in `__post_init__`, so a step
   wired to `kind: openai` always routes through the openai branch of
   `_run_api` (which reads `OPENAI_BASE_URL`/`OPENAI_API_BASE` for the endpoint
   and `OPENAI_API_KEY` for auth — point the base URL at any OpenAI-compatible
   gateway). Immutable: builds a NEW `RunnerDefinition` via `model_copy`.

3. Fail-closed + mask-at-the-runner, mirroring `OpenRouterRunner`: a missing
   `OPENAI_API_KEY` raises a clear `RuntimeError` naming
   `${secret:OPENAI_API_KEY}` before any HTTP call, and the resolved key is
   registered via `register_secret_value` + returned/raised text is passed
   through `redact_text` AT the runner.

This is what lets the natural-language concierge (see
`hivepilot/services/concierge_service.py`) run its classifier on a hosted OSS
model (e.g. OpenCode Zen) instead of the Anthropic-only `claude` runner.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from hivepilot.runners.base import RunnerPayload, validate_runner_mode
from hivepilot.runners.prompt_cli_runner import PromptCliRunner
from hivepilot.services.config_provenance import redact_text, register_secret_value
from hivepilot.utils.env import merge_environments

_KIND = "openai"


@dataclass
class OpenAiCompatRunner(PromptCliRunner):
    """API-only runner for an OpenAI-compatible `/chat/completions` endpoint.

    Endpoint is `OPENAI_BASE_URL` (or `OPENAI_API_BASE`), defaulting to
    `https://api.openai.com/v1`; auth is `OPENAI_API_KEY`.
    """

    command_name: str = "openai"
    supported_modes: ClassVar[frozenset[str]] = frozenset({"api"})

    def __post_init__(self) -> None:
        forced_options = {**self.definition.options, "api_provider": "openai"}
        self.definition = self.definition.model_copy(update={"options": forced_options})

    def _resolve_mode(self, payload: RunnerPayload) -> str:
        return (
            payload.step.metadata.get("mode") or self.definition.options.get("mode") or "cli"
        ).lower()

    def _resolved_api_key(self, payload: RunnerPayload) -> str:
        env = merge_environments(payload.project.env, self.definition.env, payload.secrets)
        api_key = env.get("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "OPENAI_API_KEY is not set — refusing to run the openai runner "
                "without a key (fail closed). Provide it via "
                "${secret:OPENAI_API_KEY} on the step/runner definition, or the "
                "OPENAI_API_KEY environment variable."
            )
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
        return redact_text(text)
