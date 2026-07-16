from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from hivepilot.config import Settings, settings
from hivepilot.models import RunnerDefinition
from hivepilot.plugins import SkillSpec
from hivepilot.runners.base import BaseRunner, RunnerPayload, UsageInfo, set_last_usage
from hivepilot.services.profile_service import load_claude_profiles
from hivepilot.utils.env import gather_overrides, merge_environments
from hivepilot.utils.logging import get_logger
from hivepilot.utils.prompt_vars import render_prompt_vars
from hivepilot.utils.remote import build_invocation
from hivepilot.utils.sandbox import DEFAULT_ALLOWLIST, scrub_env, wrap_bwrap

logger = get_logger(__name__)

_ELEVATED_PERMISSION_MODES = frozenset({"bypassPermissions", "acceptEdits"})

# Metadata keys `apply_skill` stashes on the (copied) payload for
# `_build_invocation` to consume, and `run()`/`capture()` to clean up after
# the subprocess call completes (success or exception). Private to this
# module — not part of the public RunnerPayload/SkillSpec contract.
_SKILL_SCRATCH_DIR_KEY = "skill_scratch_dir"
_SKILL_SYSTEM_PROMPT_KEY = "skill_system_prompt"


def _resolve_skill_text(text: str, catalog: dict[str, dict[str, Any]]) -> str:
    """Resolve ``${secret:NAME}`` references in *text* via the EXISTING
    masking/resolution choke point (``hivepilot.services.secret_refs
    .resolve_secret_refs`` — the same one ``Orchestrator._resolve_secrets``
    uses for ``project.env``). Every resolved value is registered for
    redaction (``config_provenance.register_secret_value``) as a side effect
    of that call, so it is masked from every later log/sink automatically.

    Fail-closed (``fail_mode="closed"``): an unresolvable reference raises
    rather than silently leaving the raw ``${secret:...}`` token in a
    materialised skill file or an appended system prompt.

    Text with no reference is returned unchanged (nothing to resolve/mask).

    Imported lazily (module-level, not top-of-file) to avoid a circular
    import: `hivepilot.services.secret_refs` imports `secrets_service`,
    which `hivepilot.registry` imports `claude_runner` FOR — a top-level
    import here would deadlock that chain at process start (mirrors the
    existing lazy-import pattern used by `_build_knowledge_context` below
    for `knowledge_service`).
    """
    from hivepilot.services.secret_refs import resolve_secret_refs

    resolved = resolve_secret_refs({"_": text}, catalog=catalog, fail_mode="closed")
    return resolved.get("_", text)


def _insert_output_format_json(argv: list[str]) -> list[str]:
    """Return a copy of *argv* with ``--output-format json`` inserted right
    after the command + ``--print`` (index 2), before any other flags and
    before the trailing positional prompt argument. Never mutates *argv*.
    """
    return [*argv[:2], "--output-format", "json", *argv[2:]]


def _parse_usage_envelope(stdout: str) -> tuple[str, UsageInfo] | None:
    """Parse a ``claude --output-format json`` stdout envelope.

    Returns ``(agent_text, usage)`` on success, or ``None`` when the output
    isn't valid JSON or lacks the one field that actually matters for
    correctness — ``result`` (the agent's own text, which becomes the step
    output). ``usage``/``cost``/``model`` sub-fields are independently
    None-safe: a CLI that reports the text but not, say, cost still yields a
    usable result with ``cost_usd=None`` rather than discarding everything.

    Assumption (🟡 MEDIUM — not verified against live CLI output, mocked in
    tests): the envelope shape is
    ``{"result": str, "usage": {"input_tokens": int, "output_tokens": int},
    "total_cost_usd": float, "model": str}``. Any deviation degrades
    gracefully via the None-safe field extraction below plus the caller's
    fallback-to-raw-stdout path when this function returns None.
    """
    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    text = data.get("result")
    if not isinstance(text, str):
        return None

    usage_field = data.get("usage")
    raw_input = usage_field.get("input_tokens") if isinstance(usage_field, dict) else None
    raw_output = usage_field.get("output_tokens") if isinstance(usage_field, dict) else None
    raw_cost = data.get("total_cost_usd")
    raw_model = data.get("model")

    input_tokens = int(raw_input) if isinstance(raw_input, (int, float)) else None
    output_tokens = int(raw_output) if isinstance(raw_output, (int, float)) else None
    cost_usd = float(raw_cost) if isinstance(raw_cost, (int, float)) else None
    model = raw_model if isinstance(raw_model, str) else None

    return text, UsageInfo(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
        model=model,
    )


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
        # Skill materialisation (Sprint 2, skill-plugin-type PRD): apply_skill()
        # stashes the ephemeral scratch dir + concatenated system prompt on
        # payload.metadata; absent when no skills were applied (no-op).
        # `--add-dir` grants Claude tool access to the scratch (the skill's
        # `.claude/skills/<name>/...` files live there — the REAL repo's own
        # `.claude/skills/` is never written to, see `apply_skill`).
        skill_scratch_dir = payload.metadata.get(_SKILL_SCRATCH_DIR_KEY)
        if skill_scratch_dir:
            args.extend(["--add-dir", str(skill_scratch_dir)])
        # `--append-system-prompt` (verified via `claude --help`) appends to —
        # rather than replaces — the default system prompt, and is documented
        # as the explicit way to inject context when a session is non-interactive.
        # Preferred over prepending skill content into the positional prompt so
        # it can never be confused with the user/task's own instructions to the
        # agent (see Agent Notes for the full open-question-(b) rationale).
        skill_system_prompt = payload.metadata.get(_SKILL_SYSTEM_PROMPT_KEY)
        if skill_system_prompt:
            args.extend(["--append-system-prompt", str(skill_system_prompt)])
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

    def apply_skill(self, payload: RunnerPayload, skills: list[SkillSpec]) -> RunnerPayload:
        """Materialise the *skills* applicable to this runner into an
        EPHEMERAL scratch directory and stash a concatenated system prompt —
        the Claude-runner implementation of the optional/structural
        `apply_skill` contract documented on `hivepilot.runners.base.BaseRunner`.

        * `applies_to` mismatch (present and doesn't include this runner's
          `definition.kind`) skips that skill — non-fatal, logged at info.
        * Each applicable skill's `files` are written to
          `<scratch>/.claude/skills/<name>/<relpath>` where `<scratch>` is a
          FRESH `tempfile.mkdtemp()` directory created by this call — never
          the project's own working directory, so a pre-existing REAL
          `.claude/skills/<name>/` in the target repo is never touched.
        * `${secret:NAME}` references in `files` values / `system_prompt` are
          resolved + masked via `_resolve_skill_text` (the existing
          `secret_refs.resolve_secret_refs` choke point) before they are
          written to disk or stashed on the payload — never logged raw.
        * Returns a NEW `RunnerPayload` (copied `metadata`); *payload* itself
          is never mutated.
        * The scratch directory is intentionally NOT cleaned up here — it
          must survive until `_build_invocation` consumes it inside
          `run()`/`capture()`, which remove it in a `finally` block after the
          subprocess call completes (success or exception) so it never
          outlives the step.
        """
        kind = self.definition.kind
        applicable: list[SkillSpec] = []
        for skill in skills:
            applies_to = skill.get("applies_to")
            if applies_to and kind not in applies_to:
                logger.info(
                    "claude_runner.apply_skill.skipped",
                    skill=skill.get("name"),
                    reason="applies_to_mismatch",
                    runner_kind=kind,
                )
                continue
            applicable.append(skill)

        metadata = dict(payload.metadata)
        if not applicable:
            return replace(payload, metadata=metadata)

        catalog = payload.project.secrets
        scratch_dir = Path(tempfile.mkdtemp(prefix="hivepilot-skill-"))
        prompts: list[str] = []
        for skill in applicable:
            skill_dir = scratch_dir / ".claude" / "skills" / skill["name"]
            for rel_path, content in skill["files"].items():
                target = skill_dir / rel_path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(_resolve_skill_text(content, catalog), encoding="utf-8")
            system_prompt = skill.get("system_prompt")
            if system_prompt:
                prompts.append(_resolve_skill_text(system_prompt, catalog))

        metadata[_SKILL_SCRATCH_DIR_KEY] = str(scratch_dir)
        if prompts:
            metadata[_SKILL_SYSTEM_PROMPT_KEY] = "\n\n".join(prompts)
        return replace(payload, metadata=metadata)

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
        scratch_dir = payload.metadata.get(_SKILL_SCRATCH_DIR_KEY)
        try:
            subprocess.run(
                argv, cwd=cwd, env=run_env, check=True, text=True, stdin=subprocess.DEVNULL
            )
        finally:
            # Ephemeral skill scratch (see apply_skill) must not outlive the
            # step — removed here on BOTH success and exception.
            if scratch_dir:
                shutil.rmtree(scratch_dir, ignore_errors=True)
        logger.info("claude_runner.end", project=payload.project_name, step=payload.step.name)

    def capture(self, payload: RunnerPayload) -> str:
        """Run claude and return its stdout (so the agent's output can be surfaced
        in the interaction log / live stream, not just discarded)."""
        # Clear any stale usage from a prior capture() call up front — every
        # code path below either overwrites this with fresh usage (well-formed
        # JSON) or must leave it None (flag off / any degradation path).
        set_last_usage(None)
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
        capture_usage = bool(getattr(self.settings, "claude_capture_usage", False))
        scratch_dir = payload.metadata.get(_SKILL_SCRATCH_DIR_KEY)

        try:
            if capture_usage:
                json_argv = _insert_output_format_json(argv)
                json_result = subprocess.run(
                    json_argv,
                    cwd=cwd,
                    env=run_env,
                    check=False,
                    text=True,
                    capture_output=True,
                    timeout=timeout,
                    stdin=subprocess.DEVNULL,
                )
                if json_result.returncode != 0:
                    # Do NOT retry without the flag: a non-zero exit here can
                    # happen AFTER the agent already did real work (mid-run
                    # crash, OOM/SIGKILL, network drop post-push, rate-limit
                    # after partial work) — for the developer role
                    # (bypassPermissions) that means files were edited/committed/
                    # pushed already. Re-invoking the same prompt would DUPLICATE
                    # that work. Instead raise exactly what the flag-off path
                    # raises below, so this is "no worse than flag off" (which
                    # never retries either). If a claude build genuinely doesn't
                    # support --output-format json, enabling this flag surfaces
                    # as a run failure and the operator turns the flag back off —
                    # we never silently double-run the agent to route around it.
                    err = (json_result.stderr or json_result.stdout or "").strip()[-2000:]
                    raise RuntimeError(f"claude exited {json_result.returncode}: {err}")
                parsed = _parse_usage_envelope(json_result.stdout)
                if parsed is not None:
                    text, usage = parsed
                    set_last_usage(usage)
                    return text
                # Valid exit, but the JSON was unparseable or lacked the
                # `result` field — no need to re-invoke (the agent already
                # ran to completion once); treat this attempt's own stdout
                # as raw text, exactly like flag-off behaviour would have
                # produced, and record null usage.
                logger.warning(
                    "claude_runner.usage_capture.malformed_envelope_fallback",
                    project=payload.project_name,
                    step=payload.step.name,
                )
                return json_result.stdout

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
        finally:
            # Ephemeral skill scratch (see apply_skill) must not outlive the
            # step — removed here on BOTH success and exception.
            if scratch_dir:
                shutil.rmtree(scratch_dir, ignore_errors=True)

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
