from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
from typing import Any

import requests

from hivepilot.config import Settings
from hivepilot.models import RunnerDefinition
from hivepilot.runners.base import BaseRunner, RunnerPayload, UsageInfo, set_last_usage
from hivepilot.utils.env import merge_environments
from hivepilot.utils.logging import get_logger
from hivepilot.utils.remote import build_invocation

logger = get_logger(__name__)


@dataclass
class PromptCliRunner(BaseRunner):
    definition: RunnerDefinition
    settings: Settings
    command_name: str = ""
    # Per-CLI non-interactive invocation (overridable via definition.options):
    cli_subcommand: str | None = None  # e.g. codex 'exec', opencode 'run'
    cli_flags: tuple[str, ...] = ()  # e.g. ('--print',) for cursor-agent
    prompt_flag: str | None = None  # if set, prompt passed as [flag, prompt] (gemini '-p')
    model_flag: str = "--model"

    def _load_prompt(self, payload: RunnerPayload) -> str:
        prompt_file = payload.step.prompt_file
        if not prompt_file:
            raise ValueError(f"Step '{payload.step.name}' must set prompt_file for CLI runner.")
        prompt_path = self.settings.resolve_config_path(prompt_file)
        if not prompt_path.exists():
            raise FileNotFoundError(f"Prompt file not found: {prompt_path}")
        from hivepilot.config import settings
        from hivepilot.utils.prompt_vars import render_prompt_vars

        raw = prompt_path.read_text(encoding="utf-8").strip()
        target_repo = str(payload.project.path) if payload.project.path else "."
        obsidian_vault = (
            str(self.settings.obsidian_vault)
            if getattr(self.settings, "obsidian_vault", None)
            else ""
        )
        return render_prompt_vars(
            raw,
            target_repo=target_repo,
            governance_repo=settings.governance_repo or "",
            obsidian_vault=obsidian_vault,
        )

    def _build_cli_args(self, payload: RunnerPayload, prompt_text: str) -> list[str]:
        command_str = self.definition.command or self.command_name
        if not command_str:
            raise ValueError("CLI runner requires a command.")
        opts = self.definition.options
        args = shlex.split(command_str)
        subcommand = opts.get("subcommand", self.cli_subcommand)
        if subcommand:
            args.append(subcommand)
        args.extend(opts.get("cli_flags", list(self.cli_flags)))
        model = payload.step.metadata.get("model") or self.definition.model
        if model:
            args.extend([opts.get("model_flag", self.model_flag), model])
        prompt_flag = opts.get("prompt_flag", self.prompt_flag)
        if prompt_flag:
            args.extend([prompt_flag, prompt_text])
        else:
            args.append(prompt_text)
        return args

    def _build_knowledge_context(self, payload: RunnerPayload) -> str | None:
        """Mirror ClaudeRunner._build_knowledge_context: read knowledge_files into a
        single string that can be pre-injected before the (volatile) prompt body.
        Tolerates no files (returns None) and missing files (knowledge_service skips them).
        """
        from pathlib import Path

        from hivepilot.services.knowledge_service import build_context

        files = payload.step.metadata.get("knowledge_files") or payload.step.knowledge_files
        if not files:
            return None
        return build_context(payload.project.path, [Path(f) for f in files])

    def _augment_prompt(self, payload: RunnerPayload, prompt_text: str) -> str:
        """Prepend stable knowledge context then append volatile sections.

        Order (cache-friendly — stable content first):
          1. Knowledge context (governance docs, injected inline)
          2. Base prompt (role instructions)
          3. Volatile: extra_prompt, prior_context

        Mirrors ClaudeRunner so non-Claude CLIs (opencode/cursor/codex/gemini/vibe)
        also receive the pipeline hand-off context and pre-injected governance docs.
        """
        # Stable: knowledge context goes BEFORE the prompt so prefix caching covers it.
        knowledge = self._build_knowledge_context(payload)

        # Volatile sections (user-specific, per-run context).
        volatile: list[str] = []
        extra = payload.metadata.get("extra_prompt")
        if extra:
            volatile.append(f"Extra instructions from user: {extra}")
        prior = payload.metadata.get("prior_context")
        if prior:
            volatile.append(f"Outputs from previous agents:\n{prior}")

        if not knowledge and not volatile:
            return prompt_text

        parts: list[str] = []
        if knowledge:
            parts.append(f"Knowledge context:\n{knowledge}")
        parts.append(f"Instructions:\n{prompt_text}")
        parts.extend(volatile)
        return "\n\n".join(parts)

    def capture(self, payload: RunnerPayload) -> str:
        """Run the CLI in capture mode and return stdout (used for debate positions).

        In API mode (``mode: api`` — step metadata or runner options, same
        resolution ``run()`` already used) this instead calls the provider's
        HTTP API directly and additionally captures token usage/cost when the
        response reports it (Phase 24 follow-up: non-claude usage capture).
        Previously ``capture()`` ignored ``mode`` entirely and always took the
        CLI-subprocess branch below — since every real step-execution path
        (``RunnerRegistry.capture_definition`` / ``_capture_or_execute``)
        calls ``capture()`` rather than ``run()``, API mode was effectively
        unreachable in production; this closes that gap as a side effect of
        wiring up usage capture. No opt-in flag is needed here — unlike
        ``ClaudeRunner``'s ``claude_capture_usage`` flag (which re-invokes the
        CLI a second time with ``--output-format json``), the provider SDKs
        already return usage in the SAME request/response that produces the
        text, so capturing it changes nothing about the run itself.

        Clears any stale usage stashed by an earlier, unrelated capture()
        call UNCONDITIONALLY, before the mode branch — both the CLI branch
        below and the API branch in ``_capture_api`` rely on this. This
        matters because ``orchestrator._capture_or_execute`` (the path for
        all non-role tasks) calls ``capture()`` directly, bypassing the
        ``RunnerRegistry.capture_definition`` choke-point clear — without an
        unconditional clear here, a stale ``UsageInfo`` left by a prior
        debate/rebuttal capture that never popped could otherwise be
        mis-attributed by this step's ``pop_last_usage()`` to the wrong
        step's tokens/cost/model. Mirrors ``ClaudeRunner.capture()``'s
        clear-at-entry for the same reason.
        """
        set_last_usage(None)
        mode = (
            payload.step.metadata.get("mode") or self.definition.options.get("mode") or "cli"
        ).lower()
        env = merge_environments(payload.project.env, self.definition.env, payload.secrets)
        if mode == "api":
            return self._capture_api(payload, env)
        args = self._build_cli_args(
            payload, self._augment_prompt(payload, self._load_prompt(payload))
        )
        argv, cwd, run_env = build_invocation(
            args,
            payload.project.path,
            env,
            host=self.definition.host,
            ssh_options=self.settings.ssh_options or None,
        )
        timeout = payload.step.timeout_seconds or self.definition.timeout_seconds
        result = subprocess.run(
            argv,
            cwd=cwd,
            env=run_env,
            check=False,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "").strip()[-2000:]
            raise RuntimeError(f"{argv[0]} exited {result.returncode}: {err}")
        return result.stdout

    def _capture_api(self, payload: RunnerPayload, env: dict[str, str]) -> str:
        """API-mode ``capture()``: call the provider's HTTP API, extract the
        reply text and (when reported) usage, and stash usage via
        ``set_last_usage()`` for the orchestrator to pop right after this call
        returns — the same handoff contract ``ClaudeRunner.capture()`` uses.

        The stale-usage clear happens unconditionally at the top of
        ``capture()`` (before the mode branch), not here — no need to repeat
        it in this branch-specific helper.
        """
        prompt_text = self._augment_prompt(payload, self._load_prompt(payload))
        provider = self.definition.options.get("api_provider") or ""
        result = self._run_api(prompt_text, payload, env)
        text = self._extract_api_text(provider, result)
        usage = self._extract_api_usage(provider, result)
        if usage is not None:
            set_last_usage(usage)
        return text

    def _extract_api_text(self, provider: str, result: Any) -> str:
        """Extract the assistant's reply text from a provider's raw JSON
        response. Defensive: never raises — an unexpected shape (missing key,
        wrong type) yields "" rather than crashing the step; the HTTP call
        itself already succeeded (2xx) by the time this runs.
        """
        if not isinstance(result, dict):
            return ""
        try:
            if provider == "anthropic":
                blocks = result.get("content")
                if not isinstance(blocks, list):
                    return ""
                return "".join(
                    b.get("text", "")
                    for b in blocks
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            if provider == "google":
                candidates = result.get("candidates")
                if not isinstance(candidates, list) or not candidates:
                    return ""
                first = candidates[0]
                if not isinstance(first, dict):
                    return ""
                content = first.get("content")
                parts = content.get("parts") if isinstance(content, dict) else None
                if not isinstance(parts, list):
                    return ""
                return "".join(p.get("text", "") for p in parts if isinstance(p, dict))
            # openai / mistral / perplexity / openrouter: OpenAI chat-completions shape
            choices = result.get("choices")
            if not isinstance(choices, list) or not choices:
                return ""
            first_choice = choices[0]
            if not isinstance(first_choice, dict):
                return ""
            message = first_choice.get("message")
            if not isinstance(message, dict):
                return ""
            return message.get("content") or ""
        except Exception:  # noqa: BLE001 - never break a run over a text-shape surprise
            logger.warning("prompt_cli_runner.api_text_extraction_failed", provider=provider)
            return ""

    def _extract_api_usage(self, provider: str, result: Any) -> UsageInfo | None:
        """Build a ``UsageInfo`` from *result* when the provider reported
        usage, else ``None``. Never invents values — only fields the response
        actually carries are set. ``cost_usd`` stays ``None`` for every
        provider wired here today: none of them report cost in their default
        (non-streaming, no special params) response body — a later phase's
        price-map can estimate it downstream from tokens+model.

        Wrapped in a broad ``except`` so a malformed/unexpected shape
        degrades to "no usage captured" rather than failing the step — usage
        capture is purely observability, never load-bearing for the run.
        The warning log carries only the provider name and exception type,
        never the response body or any secret.
        """
        if not isinstance(result, dict):
            return None
        try:
            if provider == "anthropic":
                usage = result.get("usage")
                if not isinstance(usage, dict):
                    return None
                input_tokens = usage.get("input_tokens")
                output_tokens = usage.get("output_tokens")
                if input_tokens is None and output_tokens is None:
                    return None
                model = result.get("model")
                return UsageInfo(
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_usd=None,
                    model=model if isinstance(model, str) else None,
                )
            if provider == "google":
                usage = result.get("usageMetadata")
                if not isinstance(usage, dict):
                    return None
                input_tokens = usage.get("promptTokenCount")
                output_tokens = usage.get("candidatesTokenCount")
                if input_tokens is None and output_tokens is None:
                    return None
                return UsageInfo(
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_usd=None,
                    model=None,
                )
            # openai / mistral / perplexity / openrouter: OpenAI usage shape
            usage = result.get("usage")
            if not isinstance(usage, dict):
                return None
            input_tokens = usage.get("prompt_tokens")
            output_tokens = usage.get("completion_tokens")
            if input_tokens is None and output_tokens is None:
                return None
            model = result.get("model")
            return UsageInfo(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=None,
                model=model if isinstance(model, str) else None,
            )
        except Exception as exc:  # noqa: BLE001 - usage capture must never break a run
            logger.warning(
                "prompt_cli_runner.api_usage_extraction_failed",
                provider=provider,
                error_type=type(exc).__name__,
            )
            return None

    def run(self, payload: RunnerPayload) -> None:
        mode = (
            payload.step.metadata.get("mode") or self.definition.options.get("mode") or "cli"
        ).lower()

        prompt_text = self._augment_prompt(payload, self._load_prompt(payload))

        env = merge_environments(payload.project.env, self.definition.env, payload.secrets)
        if mode == "api":
            self._run_api(prompt_text, payload, env)
        else:
            args = self._build_cli_args(payload, prompt_text)
            command_str = args[0]
            argv, cwd, run_env = build_invocation(
                args,
                payload.project.path,
                env,
                host=self.definition.host,
                ssh_options=self.settings.ssh_options or None,
            )
            timeout = payload.step.timeout_seconds or self.definition.timeout_seconds
            logger.info(
                "cli_runner.start",
                project=payload.project_name,
                step=payload.step.name,
                command=command_str,
                host=self.definition.host,
            )
            subprocess.run(argv, cwd=cwd, env=run_env, check=True, text=True, timeout=timeout)
            logger.info("cli_runner.end", project=payload.project_name, step=payload.step.name)

    def _run_api(self, prompt: str, payload: RunnerPayload, env: dict[str, str]) -> dict[str, Any]:
        provider = self.definition.options.get("api_provider")
        model = payload.step.metadata.get("model") or self.definition.options.get("api_model")
        if not provider or not model:
            raise ValueError("API mode requires api_provider and api_model.")
        timeout = payload.step.timeout_seconds or self.definition.timeout_seconds

        if provider == "openai":
            endpoint = env.get("OPENAI_API_BASE", "https://api.openai.com/v1")
            api_key = env.get("OPENAI_API_KEY")
            if not api_key:
                raise RuntimeError("OPENAI_API_KEY missing.")
            return self._post_json(
                url=f"{endpoint}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                payload={"model": model, "messages": [{"role": "user", "content": prompt}]},
                timeout=timeout,
            )
        elif provider == "anthropic":
            api_key = env.get("ANTHROPIC_API_KEY")
            if not api_key:
                raise RuntimeError("ANTHROPIC_API_KEY missing.")
            headers: dict[str, str] = {
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            }
            # Split into stable system (cacheable) + volatile user trigger.
            # The prompt has already been ordered stable→volatile by _augment_prompt.
            if self.settings.anthropic_prompt_cache:
                headers["anthropic-beta"] = "prompt-caching-2024-07-31"
                api_payload: dict = {
                    "model": model,
                    "max_tokens": 1000,
                    "system": [
                        {
                            "type": "text",
                            "text": prompt,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                    "messages": [{"role": "user", "content": "Execute the instructions above."}],
                }
            else:
                api_payload = {
                    "model": model,
                    "max_tokens": 1000,
                    "messages": [{"role": "user", "content": prompt}],
                }
            return self._post_json(
                url="https://api.anthropic.com/v1/messages",
                headers=headers,
                payload=api_payload,
                timeout=timeout,
            )
        elif provider == "google":
            api_key = env.get("GOOGLE_API_KEY")
            if not api_key:
                raise RuntimeError("GOOGLE_API_KEY missing.")
            return self._post_json(
                url=f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                headers={"x-goog-api-key": api_key},
                payload={"contents": [{"parts": [{"text": prompt}]}]},
                timeout=timeout,
            )
        elif provider == "mistral":
            api_key = env.get("MISTRAL_API_KEY")
            if not api_key:
                raise RuntimeError("MISTRAL_API_KEY missing.")
            return self._post_json(
                url="https://api.mistral.ai/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                payload={"model": model, "messages": [{"role": "user", "content": prompt}]},
                timeout=timeout,
            )
        elif provider == "perplexity":
            api_key = env.get("PERPLEXITY_API_KEY")
            if not api_key:
                raise RuntimeError("PERPLEXITY_API_KEY missing.")
            return self._post_json(
                url="https://api.perplexity.ai/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                payload={"model": model, "messages": [{"role": "user", "content": prompt}]},
                timeout=timeout,
            )
        elif provider == "openrouter":
            api_key = env.get("OPENROUTER_API_KEY")
            if not api_key:
                raise RuntimeError("OPENROUTER_API_KEY missing.")
            return self._post_json(
                url="https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                payload={"model": model, "messages": [{"role": "user", "content": prompt}]},
                timeout=timeout,
            )
        else:
            raise ValueError(f"Unsupported API provider: {provider}")

    def _post_json(
        self, url: str, payload: dict[str, Any], headers: dict[str, str], timeout: int | None = None
    ) -> dict[str, Any]:
        logger.info("api_runner.request", url=url, model=payload.get("model"))
        response = requests.post(url, json=payload, headers=headers, timeout=timeout or 60)
        if not response.ok:
            raise RuntimeError(f"API request failed: {response.status_code} {response.text}")
        result = response.json()
        # Metadata only — never log response content: a provider's reply body
        # can carry the reflected prompt (which may itself carry knowledge
        # context / business content) and this path is now reachable from the
        # primary production flow (capture() dispatches to it for mode: api).
        logger.info(
            "api_runner.response", status_code=response.status_code, bytes=len(response.content)
        )
        return result if isinstance(result, dict) else {}


@dataclass
class CodexRunner(PromptCliRunner):
    command_name: str = "codex"
    cli_subcommand: str | None = "exec"
    cli_flags: tuple[str, ...] = ("-c", "model_reasoning_effort=medium")


@dataclass
class GeminiRunner(PromptCliRunner):
    command_name: str = "gemini"
    prompt_flag: str | None = "-p"


@dataclass
class OpenCodeRunner(PromptCliRunner):
    command_name: str = "opencode"
    cli_subcommand: str | None = "run"


@dataclass
class VibeRunner(PromptCliRunner):
    """Mistral 'vibe' coding CLI (mistral-vibe).

    Non-interactive "programmatic mode" via ``--prompt``; ``--auto-approve`` skips
    tool-call confirmations during automation. ``vibe`` has no ``--model`` flag —
    the model comes from its own config / ``MISTRAL_API_KEY`` — so none is passed
    unless explicitly set on the runner definition.
    """

    command_name: str = "vibe"
    cli_flags: tuple[str, ...] = ("--auto-approve",)
    prompt_flag: str | None = "--prompt"


@dataclass
class OllamaRunner(PromptCliRunner):
    command_name: str = "ollama run codellama"
