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

logger = get_logger(__name__)


@dataclass
class PromptCliRunner(BaseRunner):
    definition: RunnerDefinition
    settings: Settings
    command_name: str = ""

    def run(self, payload: RunnerPayload) -> None:
        mode = (payload.step.metadata.get("mode") or self.definition.options.get("mode") or "cli").lower()

        prompt_file = payload.step.prompt_file
        if not prompt_file:
            raise ValueError(f"Step '{payload.step.name}' must set prompt_file for CLI runner.")
        prompt_path = self.settings.resolve_config_path(prompt_file)
        if not prompt_path.exists():
            raise FileNotFoundError(f"Prompt file not found: {prompt_path}")
        prompt_text = prompt_path.read_text(encoding="utf-8").strip()

        env = merge_environments(payload.project.env, self.definition.env, payload.secrets)
        if mode == "api":
            self._run_api(prompt_text, payload, env)
        else:
            command_str = self.definition.command or self.command_name
            if not command_str:
                raise ValueError("CLI runner requires a command.")
            args = shlex.split(command_str)
            model = payload.step.metadata.get("model") or self.definition.model
            if model:
                args.extend(["--model", model])
            args.append(prompt_text)
            timeout = payload.step.timeout_seconds or self.definition.timeout_seconds
            logger.info(
                "cli_runner.start",
                project=payload.project_name,
                step=payload.step.name,
                command=command_str,
            )
            subprocess.run(args, cwd=str(payload.project.path), env=env, check=True, text=True, timeout=timeout)
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
            self._post_json(
                url="https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                },
                payload={"model": model, "max_tokens": 1000, "messages": [{"role": "user", "content": prompt}]},
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

    def _post_json(self, url: str, payload: dict[str, Any], headers: dict[str, str], timeout: int | None = None) -> None:
        logger.info("api_runner.request", url=url, model=payload.get("model"))
        response = requests.post(url, json=payload, headers=headers, timeout=timeout or 60)
        if not response.ok:
            raise RuntimeError(f"API request failed: {response.status_code} {response.text}")
        result = response.json()
        logger.info("api_runner.response", response=json.dumps(result)[:200])


@dataclass
class CodexRunner(PromptCliRunner):
    command_name: str = "codex"


@dataclass
class GeminiRunner(PromptCliRunner):
    command_name: str = "gemini-cli"


@dataclass
class OpenCodeRunner(PromptCliRunner):
    command_name: str = "opencode"


@dataclass
class OllamaRunner(PromptCliRunner):
    command_name: str = "ollama run codellama"
