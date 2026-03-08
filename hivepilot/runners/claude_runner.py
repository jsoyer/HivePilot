from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from hivepilot.config import Settings
from hivepilot.models import RunnerDefinition
from hivepilot.runners.base import BaseRunner, RunnerPayload
from hivepilot.services.profile_service import load_claude_profiles
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class ClaudeRunner(BaseRunner):
    definition: RunnerDefinition
    settings: Settings
    profiles: dict[str, dict[str, str]] = load_claude_profiles()

    def run(self, payload: RunnerPayload) -> None:
        command = self.definition.command or self.settings.claude_command
        if not command:
            raise ValueError("Claude command not configured.")
        prompt_file = payload.step.prompt_file
        if not prompt_file:
            raise ValueError(f"Step '{payload.step.name}' requires a prompt_file for Claude runner.")
        prompt_path = self.settings.resolve_path(Path(prompt_file))
        if not prompt_path.exists():
            raise FileNotFoundError(f"Prompt file not found: {prompt_path}")
        prompt_text = prompt_path.read_text(encoding="utf-8").strip()
        knowledge_context = self._build_knowledge_context(payload)
        prompt = self._build_prompt(payload, prompt_text, knowledge_context)
        args = [command]
        model = self._resolve_model(payload)
        if model:
            args.extend(["--model", model])
        if self.definition.agent:
            args.extend(["--agent", self.definition.agent])
        args.append(prompt)
        env = payload.project.env.copy()
        env.update(self.definition.env)
        logger.info("claude_runner.start", project=payload.project_name, step=payload.step.name)
        subprocess.run(args, cwd=str(payload.project.path), env=env, check=True, text=True)
        logger.info("claude_runner.end", project=payload.project_name, step=payload.step.name)

    def _build_prompt(self, payload: RunnerPayload, instructions: str, knowledge_context: str | None) -> str:
        sections = [
            f"Project: {payload.project_name}",
            f"Task: {payload.task_name}",
            f"Step: {payload.step.name}",
            f"Repository path: {payload.project.path}",
        ]
        if payload.project.description:
            sections.append(f"Project description: {payload.project.description}")
        if payload.project.claude_md:
            sections.append(f"Repository instructions file: {payload.project.claude_md}")
        extra = payload.metadata.get("extra_prompt")
        if extra:
            sections.append(f"Extra instructions from user: {extra}")
        append = payload.step.append_prompt or self.definition.append_prompt
        if append:
            sections.append(f"Step-specific instructions: {append}")
        if knowledge_context:
            sections.append(f"Knowledge context:\n{knowledge_context}")
        return "\n".join(sections) + f"\n\nInstructions:\n{instructions}"

    def _resolve_model(self, payload: RunnerPayload) -> str | None:
        profile = (
            payload.step.metadata.get("claude_profile")
            or self.definition.options.get("profile")
            or self.definition.agent  # fallback if using agent field to encode
        )
        if profile and profile in self.profiles:
            return self.profiles[profile].get("model")
        return (
            payload.step.metadata.get("model")
            or self.definition.model
            or self.settings.default_model
        )

    def _build_knowledge_context(self, payload: RunnerPayload) -> str | None:
        from hivepilot.services.knowledge_service import build_context

        files = payload.step.metadata.get("knowledge_files") or payload.step.knowledge_files
        if not files:
            return None
        return build_context(payload.project.path, [Path(file) for file in files])
