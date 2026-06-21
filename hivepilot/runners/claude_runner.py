from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from hivepilot.config import Settings
from hivepilot.models import RunnerDefinition
from hivepilot.runners.base import BaseRunner, RunnerPayload
from hivepilot.services.profile_service import load_claude_profiles
from hivepilot.utils.env import merge_environments
from hivepilot.utils.logging import get_logger
from hivepilot.utils.remote import build_invocation

logger = get_logger(__name__)


@dataclass
class ClaudeRunner(BaseRunner):
    definition: RunnerDefinition
    settings: Settings
    profiles: dict[str, dict[str, str]] = field(default_factory=load_claude_profiles)

    def _build_invocation(self, payload: RunnerPayload) -> tuple[list[str], dict[str, str]]:
        command = self.definition.command or self.settings.claude_command
        if not command:
            raise ValueError("Claude command not configured.")
        prompt_file = payload.step.prompt_file
        if not prompt_file:
            raise ValueError(
                f"Step '{payload.step.name}' requires a prompt_file for Claude runner."
            )
        prompt_path = self.settings.resolve_config_path(prompt_file)
        if not prompt_path.exists():
            raise FileNotFoundError(f"Prompt file not found: {prompt_path}")
        prompt_text = prompt_path.read_text(encoding="utf-8").strip()
        knowledge_context = self._build_knowledge_context(payload)
        prompt = self._build_prompt(payload, prompt_text, knowledge_context)
        args = [command, "--print"]
        model = self._resolve_model(payload)
        if model:
            args.extend(["--model", model])
        if self.definition.agent:
            args.extend(["--agent", self.definition.agent])
        # Permission mode (e.g. acceptEdits/bypassPermissions) lets the developer
        # agent actually write code in headless --print mode. Without it claude
        # blocks on an interactive permission prompt it cannot show and the run
        # hangs to timeout. A per-step/runner override wins over the global setting.
        permission_mode = (
            payload.step.metadata.get("permission_mode")
            or self.definition.options.get("permission_mode")
            or self.settings.claude_permission_mode
        )
        if permission_mode:
            args.extend(["--permission-mode", permission_mode])
        args.append(prompt)
        env = merge_environments(payload.project.env, self.definition.env, payload.secrets)
        return args, env

    def run(self, payload: RunnerPayload) -> None:
        args, env = self._build_invocation(payload)
        argv, cwd, run_env = build_invocation(
            args,
            payload.project.path,
            env,
            host=self.definition.host,
            ssh_options=self.settings.ssh_options or None,
        )
        logger.info(
            "claude_runner.start",
            project=payload.project_name,
            step=payload.step.name,
            host=self.definition.host,
        )
        subprocess.run(argv, cwd=cwd, env=run_env, check=True, text=True)
        logger.info("claude_runner.end", project=payload.project_name, step=payload.step.name)

    def capture(self, payload: RunnerPayload) -> str:
        """Run claude and return its stdout (so the agent's output can be surfaced
        in the interaction log / live stream, not just discarded)."""
        args, env = self._build_invocation(payload)
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
            raise RuntimeError(f"claude exited {result.returncode}: {err}")
        return result.stdout

    def _build_prompt(
        self, payload: RunnerPayload, instructions: str, knowledge_context: str | None
    ) -> str:
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
        prior = payload.metadata.get("prior_context")
        if prior:
            sections.append(f"Outputs from previous agents:\n{prior}")
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
