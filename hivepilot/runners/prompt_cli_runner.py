from __future__ import annotations

import json
import shlex
import subprocess
from dataclasses import dataclass
from typing import Any

import requests

from hivepilot.config import Settings
from hivepilot.models import RunnerDefinition
from hivepilot.runners.base import BaseRunner, RunnerPayload
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
        from hivepilot.agent_rules import _NOXYS_ROOT
        from hivepilot.utils.prompt_vars import render_prompt_vars

        raw = prompt_path.read_text(encoding="utf-8").strip()
        target_repo = str(payload.project.path) if payload.project.path else "."
        obsidian_vault = str(self.settings.obsidian_vault) if getattr(self.settings, "obsidian_vault", None) else ""
        return render_prompt_vars(
            raw,
            target_repo=target_repo,
            governance_repo=_NOXYS_ROOT,
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

    def _augment_prompt(self, payload: RunnerPayload, prompt_text: str) -> str:
        """Append volatile context to the stable base prompt for cache-friendliness.

        Stable role/system instructions go FIRST so prefix caching covers them.
        Volatile sections (extra_prompt, prior_context) go LAST.
        Mirrors ClaudeRunner so non-Claude CLIs (opencode/cursor/codex/gemini/vibe)
        also receive the pipeline hand-off context.
        """
        # Volatile sections (user-specific, per-run context).
        volatile: list[str] = []
        extra = payload.metadata.get("extra_prompt")
        if extra:
            volatile.append(f"Extra instructions from user: {extra}")
        prior = payload.metadata.get("prior_context")
        if prior:
            volatile.append(f"Outputs from previous agents:\n{prior}")
        if not volatile:
            return prompt_text
        return f"Instructions:\n{prompt_text}\n\n" + "\n\n".join(volatile)

    def capture(self, payload: RunnerPayload) -> str:
        """Run the CLI in capture mode and return stdout (used for debate positions)."""
        env = merge_environments(payload.project.env, self.definition.env, payload.secrets)
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

    def _run_api(self, prompt: str, payload: RunnerPayload, env: dict[str, str]) -> None:
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
            self._post_json(
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
            self._post_json(
                url="https://api.anthropic.com/v1/messages",
                headers=headers,
                payload=api_payload,
                timeout=timeout,
            )
        elif provider == "google":
            api_key = env.get("GOOGLE_API_KEY")
            if not api_key:
                raise RuntimeError("GOOGLE_API_KEY missing.")
            self._post_json(
                url=f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                headers={"x-goog-api-key": api_key},
                payload={"contents": [{"parts": [{"text": prompt}]}]},
                timeout=timeout,
            )
        elif provider == "mistral":
            api_key = env.get("MISTRAL_API_KEY")
            if not api_key:
                raise RuntimeError("MISTRAL_API_KEY missing.")
            self._post_json(
                url="https://api.mistral.ai/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                payload={"model": model, "messages": [{"role": "user", "content": prompt}]},
                timeout=timeout,
            )
        elif provider == "perplexity":
            api_key = env.get("PERPLEXITY_API_KEY")
            if not api_key:
                raise RuntimeError("PERPLEXITY_API_KEY missing.")
            self._post_json(
                url="https://api.perplexity.ai/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                payload={"model": model, "messages": [{"role": "user", "content": prompt}]},
                timeout=timeout,
            )
        elif provider == "openrouter":
            api_key = env.get("OPENROUTER_API_KEY")
            if not api_key:
                raise RuntimeError("OPENROUTER_API_KEY missing.")
            self._post_json(
                url="https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                payload={"model": model, "messages": [{"role": "user", "content": prompt}]},
                timeout=timeout,
            )
        else:
            raise ValueError(f"Unsupported API provider: {provider}")

    def _post_json(
        self, url: str, payload: dict[str, Any], headers: dict[str, str], timeout: int | None = None
    ) -> None:
        logger.info("api_runner.request", url=url, model=payload.get("model"))
        response = requests.post(url, json=payload, headers=headers, timeout=timeout or 60)
        if not response.ok:
            raise RuntimeError(f"API request failed: {response.status_code} {response.text}")
        result = response.json()
        logger.info("api_runner.response", response=json.dumps(result)[:200])


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
