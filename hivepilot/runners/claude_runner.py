from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from hivepilot.config import Settings, settings
from hivepilot.models import RunnerDefinition
from hivepilot.runners.base import BaseRunner, RunnerPayload
from hivepilot.services.profile_service import load_claude_profiles
from hivepilot.utils.env import gather_overrides, merge_environments
from hivepilot.utils.logging import get_logger
from hivepilot.utils.prompt_vars import render_prompt_vars
from hivepilot.utils.remote import build_invocation
from hivepilot.utils.sandbox import DEFAULT_ALLOWLIST, scrub_env, wrap_bwrap

logger = get_logger(__name__)

_ELEVATED_PERMISSION_MODES = frozenset({"bypassPermissions", "acceptEdits"})


def _apply_sandbox(
    argv: list[str],
    run_env: dict[str, str] | None,
    cwd: str | None,
    *,
    permission_mode: str | None,
    definition_host: str | None,
    settings_obj: Settings,
    intentional_env: dict[str, str],
) -> tuple[list[str], dict[str, str] | None]:
    """Return (argv, env) with sandbox applied when appropriate.

    Sandbox is applied when ALL of the following hold:
    - ``definition_host`` is None (local run — SSH runs must not be double-wrapped)
    - ``permission_mode`` is an elevated mode (bypassPermissions or acceptEdits)
    - ``settings_obj.dev_sandbox == "bwrap"``

    ``intentional_env`` must be the project/definition/secrets overlay ONLY —
    do NOT pass the full ``merge_environments`` output (which includes os.environ)
    or the scrub step will be undone.

    On any error the original argv/env are returned unchanged and a warning is
    logged so the developer run is never broken by sandboxing code.
    """
    if definition_host:
        # Remote SSH run — bwrap cannot wrap an ssh process meaningfully.
        return argv, run_env

    if permission_mode not in _ELEVATED_PERMISSION_MODES:
        return argv, run_env

    sandbox_mode = getattr(settings_obj, "dev_sandbox", "none")
    if sandbox_mode != "bwrap":
        return argv, run_env

    try:
        # --- env scrub ---
        # Start from a clean scrub of the host environment, then layer only the
        # intentional project/role/secrets overrides on top.  intentional_env
        # must NOT include os.environ (use gather_overrides, not merge_environments).
        allowlist = getattr(settings_obj, "sandbox_env_allowlist", None) or DEFAULT_ALLOWLIST
        base_env = scrub_env(os.environ.copy(), allowlist)
        base_env.update(intentional_env)

        # --- bwrap wrap ---
        workdir = cwd or str(Path.cwd())
        wrapped_argv = wrap_bwrap(argv, workdir=workdir)

        logger.info(
            "sandbox.applied",
            permission_mode=permission_mode,
            workdir=workdir,
        )
        return wrapped_argv, base_env

    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "sandbox.error_fallback: sandboxing failed — running UNSANDBOXED. error=%s",
            exc,
        )
        return argv, run_env


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

    def _permission_mode(self, payload: RunnerPayload) -> str | None:
        """Resolve the effective permission mode for *payload* (same logic as _build_invocation)."""
        return (
            payload.step.metadata.get("permission_mode")
            or self.definition.options.get("permission_mode")
            or self.settings.claude_permission_mode
        )

    def run(self, payload: RunnerPayload) -> None:
        args, env = self._build_invocation(payload)
        argv, cwd, run_env = build_invocation(
            args,
            payload.project.path,
            env,
            host=self.definition.host,
            ssh_options=self.settings.ssh_options or None,
        )
        # gather_overrides produces the project/definition/secrets overlay WITHOUT
        # inheriting os.environ — safe to layer on top of the scrubbed base env.
        env_overlay = gather_overrides(payload.project.env, self.definition.env, payload.secrets)
        argv, run_env = _apply_sandbox(
            argv,
            run_env,
            cwd,
            permission_mode=self._permission_mode(payload),
            definition_host=self.definition.host,
            settings_obj=self.settings,
            intentional_env=env_overlay,
        )
        logger.info(
            "claude_runner.start",
            project=payload.project_name,
            step=payload.step.name,
            host=self.definition.host,
        )
        subprocess.run(argv, cwd=cwd, env=run_env, check=True, text=True, stdin=subprocess.DEVNULL)
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
        # gather_overrides produces the project/definition/secrets overlay WITHOUT
        # inheriting os.environ — safe to layer on top of the scrubbed base env.
        env_overlay = gather_overrides(payload.project.env, self.definition.env, payload.secrets)
        argv, run_env = _apply_sandbox(
            argv,
            run_env,
            cwd,
            permission_mode=self._permission_mode(payload),
            definition_host=self.definition.host,
            settings_obj=self.settings,
            intentional_env=env_overlay,
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
            stdin=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "").strip()[-2000:]
            raise RuntimeError(f"claude exited {result.returncode}: {err}")
        return result.stdout

    def _build_prompt(
        self, payload: RunnerPayload, instructions: str, knowledge_context: str | None
    ) -> str:
        # Stable sections first so Anthropic/OpenAI prefix caching covers the static prefix.
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
        if knowledge_context:
            sections.append(f"Knowledge context:\n{knowledge_context}")
        # Volatile sections last (user-specific, per-run context).
        extra = payload.metadata.get("extra_prompt")
        if extra:
            sections.append(f"Extra instructions from user: {extra}")
        append = payload.step.append_prompt or self.definition.append_prompt
        if append:
            sections.append(f"Step-specific instructions: {append}")
        prior = payload.metadata.get("prior_context")
        if prior:
            sections.append(f"Outputs from previous agents:\n{prior}")
        target_repo = str(payload.project.path) if payload.project.path else "."
        obsidian_vault = (
            str(self.settings.obsidian_vault)
            if getattr(self.settings, "obsidian_vault", None)
            else ""
        )
        instructions = render_prompt_vars(
            instructions,
            target_repo=target_repo,
            governance_repo=settings.governance_repo or "",
            obsidian_vault=obsidian_vault,
        )
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
