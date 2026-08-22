from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, ClassVar

import requests

from hivepilot.config import Settings, settings
from hivepilot.models import RunnerDefinition
from hivepilot.plugins import SkillSpec
from hivepilot.runners.base import (
    BaseRunner,
    RunnerExecutionError,
    RunnerPayload,
    UsageInfo,
    classify_signal_exit,
    prompt_file_not_found_message,
    resolve_runner_effort,
    set_last_resolved_model,
    set_last_usage,
)
from hivepilot.runners.pane_exec import PaneExecutionError, run_in_pane
from hivepilot.services.config_provenance import redact_text, register_secret_value
from hivepilot.services.corrections_service import load_corrections
from hivepilot.services.herdr_workspace import current_pane
from hivepilot.services.obsidian_vault_resolver import resolve_prompt_vault
from hivepilot.services.profile_service import load_claude_profiles
from hivepilot.services.repo_instructions import build_repo_instructions_section
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


# Default response cap for the Anthropic Messages API path (mode: api). Mirrors
# the conservative default the prompt-cli runner uses; API mode is primarily
# used for short capture/debate turns, not long headless coding sessions.
_ANTHROPIC_API_MAX_TOKENS = 4096

#: Linux's `MAX_ARG_STRLEN` — the ceiling on a SINGLE argv element (32 pages
#: of 4 096 bytes), independent of the much larger total `ARG_MAX`. The prompt
#: is passed as one element, so this is the wall it hits, and crossing it
#: raises a bare `[Errno 7] Argument list too long: 'claude'` that names the
#: binary rather than the prompt.
_MAX_ARG_STRLEN = 131_072

# Effort -> MAX_THINKING_TOKENS map (reasoning-effort knob). `effort` is the
# only depth lever HivePilot exposes for Claude besides model choice. A
# role/step with no `effort` declared performs NO injection at all (see
# `_resolve_effort`/`_effort_env_overlay`) -- this map only fires when effort
# is explicitly set, so every existing zero-effort config stays byte-
# identical to pre-effort behaviour. Must cover every `EffortLevel`
# (hivepilot.models); `"xhigh"` is the HivePilot superset level between `high`
# and `max`, mapped to 40000 tokens (in the 24000..63999 gap).
EFFORT_TOKEN_MAP: dict[str, int] = {
    "low": 4000,
    "medium": 12000,
    "high": 24000,
    "xhigh": 40000,
    "max": 63999,
}


# Known Claude CLI failure signatures -> an actionable `hint` a human can act
# on WITHOUT reading `_build_invocation`'s source or reproducing the failure
# (explicit-failure-logs sprint, Part A.1 -- these are REAL failures hit in
# production that were hard to diagnose from the log alone). Checked in
# order; the first match wins.
_STDIN_OR_PROMPT_MARKER = "Input must be provided either through stdin or as a prompt argument"
_ROOT_SUDO_MARKER = "--dangerously-skip-permissions"
_PERMISSION_GRANT_MARKER = "needs a permission grant"


def _detect_known_hint(text: str) -> str | None:
    """Match *text* (raw stderr, or any exception message derived from it)
    against known Claude CLI failure signatures and return an actionable
    ``hint``, or ``None`` when nothing matches.

    Deliberately conservative (substring, not regex) -- a false negative
    (no hint) just means the operator falls back to reading the raw
    stderr excerpt already logged alongside it; a false positive would be
    worse (a misleading hint), so each pattern below is unique enough to the
    real Claude CLI failure text it targets.
    """
    if _STDIN_OR_PROMPT_MARKER in text:
        return (
            "a variadic flag (--tools/--add-dir/--append-system-prompt/...) ate the "
            "positional prompt argument — ensure `--` precedes the prompt "
            "(see claude_runner._build_invocation's unconditional `--` insertion)."
        )
    if _ROOT_SUDO_MARKER in text and "root" in text.lower():
        return (
            "claude refuses --dangerously-skip-permissions under root/sudo. Options: "
            "(a) run the services as a non-root user (cleanest), or (b) on a "
            "dedicated single-purpose container/VM, set IS_SANDBOX=1 in the service "
            "environment so claude accepts bypassPermissions. NOTE: "
            "permission_mode=acceptEdits is NOT a substitute — it auto-accepts file "
            "edits but the agent still needs approval to run Bash (git/tests), which "
            "headless cannot grant, so the step will silently do nothing."
        )
    if _PERMISSION_GRANT_MARKER in text or (
        "cannot be used" in text and "non-interactive" in text.lower()
    ):
        return (
            "the agent tried a tool/action that needs interactive approval while "
            "running headless (--print) — HivePilot persists deliverables itself; "
            "no manual approval is available mid-run."
        )
    return None


def _insert_output_format_json(argv: list[str]) -> list[str]:
    """Return a copy of *argv* with ``--output-format json`` inserted right
    after the command + ``--print`` (index 2), before any other flags and
    before the trailing positional prompt argument. Never mutates *argv*.
    """
    return [*argv[:2], "--output-format", "json", *argv[2:]]


def _num(value: Any) -> float | int | None:
    """Return *value* unchanged when it's a real (non-bool) number, else
    ``None``. ``bool`` is explicitly excluded even though it's an ``int``
    subclass in Python — a stray ``True``/``False`` in a token/cost field
    must never be silently coerced to ``1``/``0``.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    return None


def _extract_model_usage(data: dict[str, Any]) -> UsageInfo | None:
    """Parse the ``modelUsage`` block of a ``claude --output-format json``
    envelope — the PRIMARY usage source (usage-capture-modelusage fix).

    Real envelopes were found to report an empty/zero top-level ``usage``
    block once prompt caching is active (almost all of a turn's real input
    comes from the cache, which ``usage.input_tokens`` does not count), while
    the actual per-model numbers — including the CANONICAL model id, which is
    exactly the price-map's key shape — live in ``modelUsage``:

        "modelUsage": {"claude-haiku-4-5-20251001": {
            "inputTokens": 1304, "outputTokens": 20,
            "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0,
            "costUSD": 0.001404, "canonicalModel": "claude-haiku-4-5"}}

    Attribution rule (a response may report SEVERAL models — e.g. an agent
    that internally dispatched sub-turns on a cheaper model): tokens (base
    input/output AND both cache categories) and cost are SUMMED across every
    entry — that is the step's real total resource usage and real total
    bill, and the only representation that can never understate it. The
    step's single ``model`` field is set to the DOMINANT entry (the one with
    the most combined input+output+cache tokens) — the model that did most
    of the work — since a single ``steps.model`` column cannot hold a set;
    the per-model detail lives only in this function's summation, not as a
    persisted breakdown (documented trade-off — the existing schema is
    one-row-per-step, not one-row-per-model-per-step).

    Cost precedence per entry: a model's own self-reported ``costUSD`` is
    authoritative (it is the CLI's own, real-time computed bill — always
    preferred over a stale local estimate). Only when ``costUSD`` is absent
    for an entry does this fall back to ``pricing.estimate_cost`` for that
    entry's tokens. If EITHER path fails to produce a cost for ANY entry
    (missing costUSD AND the model/cache rate isn't in the price map), the
    step's total ``cost_usd`` is ``None`` — never a partial sum that LOOKS
    like the full total while silently missing one model's contribution.
    Token totals are still returned in that case (tokens are unambiguous
    even when cost isn't).

    Returns ``None`` when ``modelUsage`` is absent, not a dict, empty, or
    contains no entry that is itself a dict — signals the caller to fall
    back to the legacy top-level ``usage`` block.
    """
    from hivepilot.services import pricing

    model_usage = data.get("modelUsage")
    if not isinstance(model_usage, dict) or not model_usage:
        return None

    total_input = 0
    total_output = 0
    total_cache_read = 0
    total_cache_creation = 0
    total_cost: float | None = 0.0
    any_entry = False
    dominant_model: str | None = None
    dominant_weight: int = -1

    for key, entry in model_usage.items():
        if not isinstance(entry, dict):
            continue
        any_entry = True

        entry_input = int(_num(entry.get("inputTokens")) or 0)
        entry_output = int(_num(entry.get("outputTokens")) or 0)
        entry_cache_read = int(_num(entry.get("cacheReadInputTokens")) or 0)
        entry_cache_creation = int(_num(entry.get("cacheCreationInputTokens")) or 0)

        total_input += entry_input
        total_output += entry_output
        total_cache_read += entry_cache_read
        total_cache_creation += entry_cache_creation

        canonical = entry.get("canonicalModel")
        model_id = canonical if isinstance(canonical, str) and canonical else key

        weight = entry_input + entry_output + entry_cache_read + entry_cache_creation
        if weight >= dominant_weight:
            dominant_weight = weight
            dominant_model = model_id

        if total_cost is not None:
            raw_cost = _num(entry.get("costUSD"))
            if raw_cost is not None:
                total_cost += float(raw_cost)
            else:
                estimated = pricing.estimate_cost(
                    model_id,
                    entry_input,
                    entry_output,
                    cache_read_tokens=entry_cache_read,
                    cache_creation_tokens=entry_cache_creation,
                )
                if estimated is None:
                    # Fail closed on the TOTAL, not this one entry: a partial
                    # sum would look like a complete total while silently
                    # missing this model's real, billed contribution.
                    total_cost = None
                else:
                    total_cost += estimated

    if not any_entry:
        return None

    return UsageInfo(
        input_tokens=total_input,
        output_tokens=total_output,
        cache_read_tokens=total_cache_read,
        cache_creation_tokens=total_cache_creation,
        cost_usd=total_cost,
        model=dominant_model,
    )


def extract_permission_denials(stdout: str) -> list[str]:
    """Tools the agent asked for and was refused, from the result envelope.

    Claude Code reports these on every dispatch and HivePilot threw the
    field away -- `permission_denials` appeared nowhere in the codebase.

    Run 356's CTO stage is what that cost. The agent asked for a cheap
    directory listing (`rtk fd . surfaces/agent -t d -d 3`), was refused,
    explored the monorepo file by file instead, reached 84 969 input tokens
    and died on "Prompt is too long". $1.17 spent, the stage failed, eleven
    stages skipped -- over one missing line in a role's `allowed_tools`,
    findable only by reading a 4 000-character failure blob by hand.

    The FAILING case is the lucky one. An agent refused a tool that still
    finishes looks like a success: it did the expensive thing instead, and
    will keep doing it on every run until somebody happens to look. So the
    caller reports these whether the dispatch succeeded or not.

    Never raises: a dispatch must not fail because its diagnostics could not
    be read. This is reporting, not control flow.
    """
    try:
        data = json.loads(stdout)
    except Exception:  # noqa: BLE001 — malformed output is not this function's problem
        return []
    if not isinstance(data, dict):
        return []
    entries = data.get("permission_denials")
    if not isinstance(entries, list):
        return []

    refused: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        tool_input = entry.get("tool_input")
        command = tool_input.get("command") if isinstance(tool_input, dict) else None
        # A command names the missing grant precisely; for a tool that takes
        # none, its own name is still enough to say which grant is absent.
        label = command or entry.get("tool_name")
        if isinstance(label, str) and label.strip():
            refused.append(label)
    return refused


def _parse_usage_envelope(stdout: str) -> tuple[str, UsageInfo] | None:
    """Parse a ``claude --output-format json`` stdout envelope.

    Returns ``(agent_text, usage)`` on success, or ``None`` when the output
    isn't valid JSON or lacks the one field that actually matters for
    correctness — ``result`` (the agent's own text, which becomes the step
    output). ``usage``/``cost``/``model`` sub-fields are independently
    None-safe: a CLI that reports the text but not, say, cost still yields a
    usable result with ``cost_usd=None`` rather than discarding everything.

    Primary source: ``modelUsage`` (see ``_extract_model_usage`` for the
    parsing/attribution/cost-precedence rules) — real operator envelopes
    were found to leave the top-level ``usage`` block at all-zero once
    prompt caching is active, with the actual token/cost/canonical-model
    data living only in ``modelUsage``. Fallback (🟢 verified against a real
    envelope for the ``modelUsage`` shape; legacy ``usage``-only shape kept
    exactly as it was, still mocked-only): a legacy/older envelope shape
    ``{"result": str, "usage": {"input_tokens": int, "output_tokens": int},
    "total_cost_usd": float, "model": str}`` is used whenever ``modelUsage``
    is absent, malformed, or empty — this is the ONLY path older CLI
    versions / other conforming envelopes ever hit, and its behaviour is
    byte-identical to before this fix.
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

    # Top-level, so it has to be read outside `modelUsage` and attached to
    # whichever path produced the usage. `bool` is excluded deliberately:
    # `isinstance(True, int)` is True in Python, and `num_turns: true` must
    # not become one turn.
    raw_turns = data.get("num_turns")
    turns = (
        int(raw_turns) if isinstance(raw_turns, int) and not isinstance(raw_turns, bool) else None
    )

    model_usage_result = _extract_model_usage(data)
    if model_usage_result is not None:
        return text, replace(model_usage_result, turns=turns)

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
        turns=turns,
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

    try:
        # --- env scrub ---
        # UNCONDITIONAL. This used to be gated on dev_sandbox == "bwrap", so
        # on a host without bwrap it never ran: measured in production, the
        # agent subprocess carried eight secrets (Slack/Telegram/Discord bot
        # tokens, the 1Password service-account token, the mem0 key and the
        # HivePilot admin API token). That made the non-root migration's
        # central claim false -- it stopped the agent reading shared.env off
        # disk, and the contents arrived through the environment anyway.
        #
        # Filtering the environment and confining the filesystem are separate
        # properties. There is no reason to hand an agent every credential
        # because a filesystem sandbox happens to be unavailable.
        # Start from a clean scrub of the host environment, then layer only the
        # intentional project/role/secrets overrides on top.  intentional_env
        # must NOT include os.environ (use gather_overrides, not merge_environments).
        allowlist = getattr(settings_obj, "sandbox_env_allowlist", None) or DEFAULT_ALLOWLIST
        base_env = scrub_env(os.environ.copy(), allowlist)
        base_env.update(intentional_env)

        # --- bwrap wrap ---
        # Still conditional: filesystem confinement needs the binary, and its
        # absence must not cost us the env scrub above.
        if sandbox_mode != "bwrap":
            logger.info("sandbox.env_scrubbed", permission_mode=permission_mode, bwrap=False)
            return argv, base_env

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

    # Agent runner: honours both the CLI binary (default) and the Anthropic
    # Messages API (mode: api → `_run_api`). See BaseRunner.supported_modes.
    supported_modes: ClassVar[frozenset[str]] = frozenset({"cli", "api"})

    # Both, and wired: `--allowed-tools` at _build_invocation and
    # `--permission-mode` beside it, plus the elevated-mode sandbox and env
    # scrub. This is the only runner that has ever applied either.
    honoured_controls: ClassVar[frozenset[str]] = frozenset({"permission_mode", "allowed_tools"})

    # ── the flags that DIFFER between claude-shaped CLIs ───────────────────
    #
    # `claude`, `cursor-agent` and `grok` share one headless shape — a
    # print/single flag, a model flag, a permission mode and a tool allow-list
    # — and disagree only on the spelling. Naming them here is what lets grok
    # (and, next, cursor) INHERIT this runner rather than subclass
    # `PromptCliRunner`, which applies neither `permission_mode` nor
    # `allowed_tools` and would silently drop both.
    #
    # `"claude"` appears nowhere as a literal in this module — the command
    # already comes from `definition.command or settings.claude_command` — so
    # these three are the whole of what a sibling needs to override.
    #
    # Verified 2026-08-21 against the installed binaries:
    #   claude       --print          --allowed-tools   --permission-mode
    #   cursor-agent --print          (sandbox/deny)    --sandbox
    #   grok         -p / --single    --allow           --permission-mode
    #                                 (alias --allowedTools)
    #   grok also has --prompt-file, which claude lacks and which removes the
    #   MAX_ARG_STRLEN failure class entirely.
    print_flag: ClassVar[str] = "--print"
    allowed_tools_flag: ClassVar[str] = "--allowed-tools"
    permission_mode_flag: ClassVar[str] = "--permission-mode"

    def _assemble_prompt(self, payload: RunnerPayload) -> str:
        """Load the step's prompt file and build the full agent prompt.

        Extracted from ``_build_invocation`` so the API path (``_run_api``)
        sends the SAME assembled prompt the CLI path would — one source of
        truth for prompt construction across both execution modes.
        """
        prompt_file = payload.step.prompt_file
        if not prompt_file:
            # The engine's own synthesized calls have no role and no file to
            # point at: they carry their whole prompt in `extra_prompt`.
            # Three do this deliberately -- `{role}-judge` and the arbiter in
            # `orchestrator.py`, and `lessons-distiller` in
            # `lessons_service.py` -- and all three were unreachable through
            # this runner because the check below only knew about role steps.
            #
            # The distiller is the one that showed: every pipeline run logged
            # `Step 'lessons-distiller' requires a prompt_file`, and `lessons`
            # stayed 0 across 136 interactions and 15 verdicts.
            #
            # The distinction is not who is calling, it is whether there is a
            # prompt at all. A step with neither a file nor an `extra_prompt`
            # is broken however it was built -- dispatching an agent with no
            # instructions is the failure this error exists to prevent -- so
            # that case still raises.
            supplied = payload.metadata.get("extra_prompt")
            if not (isinstance(supplied, str) and supplied.strip()):
                raise ValueError(
                    f"Step '{payload.step.name}' requires a prompt_file for Claude runner "
                    "(or a non-empty `extra_prompt` for an internally synthesized call)."
                )
            prompt_text = ""
        else:
            prompt_path = self.settings.resolve_config_path(prompt_file)
            if not prompt_path.exists():
                raise FileNotFoundError(
                    prompt_file_not_found_message(payload, self.settings, prompt_file, prompt_path)
                )
            prompt_text = prompt_path.read_text(encoding="utf-8").strip()
        knowledge_context = self._build_knowledge_context(payload)
        return self._build_prompt(payload, prompt_text, knowledge_context)

    def _mode(self, payload: RunnerPayload) -> str:
        """Resolve the effective execution mode for *payload*.

        Same resolution channel the orchestrator writes into and the
        prompt-cli runner already consults: step metadata wins over the runner
        definition's options, falling back to ``"cli"``.
        """
        return (
            payload.step.metadata.get("mode") or self.definition.options.get("mode") or "cli"
        ).lower()

    def _build_invocation(self, payload: RunnerPayload) -> tuple[list[str], dict[str, str]]:
        command = self.definition.command or self.settings.claude_command
        if not command:
            raise ValueError("Claude command not configured.")
        prompt = self._assemble_prompt(payload)
        args = [command, self.print_flag]
        model = self._resolve_model(payload)
        # Stated BEFORE dispatch, so a step that fails still records what it
        # was about to run on. The orchestrator prefers the CLI's own
        # self-report when there is one; this is the floor beneath it, and it
        # is what stops a config ALIAS ("sonnet", or worse "latest") from being
        # stamped as though it were the model.
        set_last_resolved_model(model)
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
        # Tool availability restriction (additive — absent by default, so
        # existing invocations are byte-identical). `--tools` (verified via
        # `claude --help` on the installed CLI): "Specify the list of
        # available tools from the built-in set. Use \"\" to disable all
        # tools, \"default\" to use all tools, or specify tool names."
        # Deliberately preferred over `--allowedTools`/`--disallowedTools`:
        # those only gate the PERMISSION PROMPT for named tools (and a
        # disallow-list fails OPEN for any tool name it doesn't enumerate),
        # whereas `--tools ""` makes the tool set structurally EMPTY — an
        # allowlist that grants nothing, closed by default. A per-step
        # override wins over the runner definition's option, same
        # resolution order as permission_mode below.
        tools = self._resolve_tools(payload)
        if tools is not None:
            args.extend(["--tools", tools])
        # MCP servers (additive — absent by default, so existing invocations
        # stay byte-identical). Until this existed, HivePilot never passed
        # `--mcp-config`, so NO agent could reach an MCP server: a CISO asked
        # for a code-graph review had to answer from plain file reads and said
        # so in its own report. Both flags verified against `claude --help`:
        # `--mcp-config <configs...>` and `--strict-mcp-config` ("Only use MCP
        # servers from --mcp-config, ignoring all other MCP configurations").
        mcp_configs = self._resolve_mcp_config(payload)
        if mcp_configs:
            args.append("--mcp-config")
            args.extend(mcp_configs)
            if self._resolve_bool_option(payload, "strict_mcp_config"):
                args.append("--strict-mcp-config")
        # Named-tool pre-approval. This is the piece that makes MCP usable in
        # headless mode WITHOUT `permission_mode: bypassPermissions`: a tool
        # that is available but not pre-approved still hits a permission
        # prompt that `--print` cannot show, so the call is declined and the
        # agent silently works without it. `--allowed-tools` grants exactly
        # the tools named (e.g. `mcp__<server>__query`) and nothing
        # else — read-only graph access for a reviewer that must NOT get Bash
        # or Edit. Deliberately an ALLOW-list: `--disallowedTools` fails OPEN
        # for every tool name it does not enumerate.
        allowed_tools = self._resolve_allowed_tools(payload)
        if allowed_tools:
            args.append(self.allowed_tools_flag)
            args.extend(allowed_tools)
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
            args.extend([self.permission_mode_flag, permission_mode])
        # `--` is the standard end-of-options separator: it tells claude's
        # arg parser to stop treating subsequent tokens as option values, so
        # the prompt is unambiguously positional. UNCONDITIONALLY emitted
        # immediately before the prompt (not just when `--tools` is set):
        # ANY variadic flag that might precede it here — `--tools <tools...>`,
        # `--add-dir <dirs...>` (skill scratch dir, see `apply_skill`),
        # `--append-system-prompt`, or a future flag we haven't added yet —
        # would otherwise greedily consume the positional prompt as one more
        # value of its own, leaving NO prompt for `--print` and making claude
        # exit 1 with "Input must be provided either through stdin or as a
        # prompt argument when using --print" (empirically reproduced against
        # the real `claude` binary for both `--tools` and `--add-dir`).
        # `claude --print --model X -- <prompt>` is byte-behaviourally
        # identical to `claude --print --model X <prompt>` — `--` is inert
        # when nothing variadic precedes it — so making this unconditional
        # costs nothing on the plain/no-flags path while making every
        # variadic-flag path permanently safe against this class of bug.
        args.append("--")
        # The prompt travels as ONE argv element, and Linux caps a single
        # element at MAX_ARG_STRLEN (131 072 bytes) no matter how large the
        # total ARG_MAX is. Exceeding it raises a bare
        # `[Errno 7] Argument list too long: 'claude'` from `subprocess.run`
        # -- an error that names the binary and says nothing about the
        # prompt. Run 390's lesson distillation died that way, and tracing it
        # back to an unbounded prompt took three rounds of investigation.
        #
        # Checked here rather than caught below so the message names the
        # cause and carries the size. Still a hard failure: silently trimming
        # an agent's prompt would change what it was asked to do.
        _prompt_bytes = len(prompt.encode())
        if _prompt_bytes > _MAX_ARG_STRLEN:
            context = {
                "prompt_bytes": _prompt_bytes,
                "limit_bytes": _MAX_ARG_STRLEN,
                "task": payload.task_name,
                "stage": payload.step.name,
                "project": payload.project_name,
            }
            logger.error("claude_runner.prompt_too_large_for_argv", **context)
            raise RunnerExecutionError(
                f"prompt is {_prompt_bytes} bytes, over the {_MAX_ARG_STRLEN}-byte "
                "single-argument limit the OS imposes on a command line "
                "(MAX_ARG_STRLEN) -- the caller must bound what it assembles",
                context=context,
            )
        args.append(prompt)
        env = merge_environments(payload.project.env, self.definition.env, payload.secrets)
        env = {**env, **self._effort_env_overlay(payload)}
        # Drop Claude Code's git instructions for roles that never touch git.
        # They are roughly 1 800 tokens of system prompt teaching the agent to
        # stage, commit and push -- paid on every call. HivePilot does its own
        # git (`perform_git_actions` promotes and merges), and no role in the
        # reference config is granted a `git` command in `allowed_tools`, so a
        # reviewer handed a diff and told not to explore buys nothing with it.
        #
        # Opt-in and per-role rather than one global setting, which is what it
        # first looked like. Two prompts in the reference config
        # (`refactor.md`, `docs_rewrite.md`) DO instruct their agent to
        # commit; a global switch would have stripped the instructions out
        # from under them silently. Resolution matches `permission_mode` and
        # `allowed_tools` -- step metadata over runner definition.
        #
        # An explicit value already in `env` wins, same rule as
        # ANTHROPIC_BASE_URL below: an operator naming the variable outranks
        # our optimisation.
        if self._resolve_bool_option(payload, "disable_git_instructions") and (
            "CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS" not in env
        ):
            env = {**env, "CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS": "1"}
        # Route through the local compression proxy only when it is actually
        # answering. `ANTHROPIC_BASE_URL` is a hard redirect — pinning it in
        # the service environment means a dead proxy fails every dispatch —
        # so `proxy_route` probes it here and returns None to fall back to a
        # direct call, which is the pre-proxy behaviour and always works.
        # A project/definition/secret that sets the variable explicitly wins:
        # an operator naming a base URL outranks our optimisation.
        if "ANTHROPIC_BASE_URL" not in env:
            from hivepilot.services.proxy_route import resolve_base_url

            proxy_url = resolve_base_url(settings.compression_proxy_url)
            if proxy_url:
                env = {**env, "ANTHROPIC_BASE_URL": proxy_url}
        return args, env

    def _resolve_effort(self, payload: RunnerPayload) -> str | None:
        """Resolve the effective reasoning-effort level for *payload* — the
        value fed to `MAX_THINKING_TOKENS` via `_effort_env_overlay`.

        Delegates to the shared `hivepilot.runners.base.resolve_runner_effort`
        so Claude and Codex resolve effort identically: the orchestrator-
        resolved `RunnerDefinition.effort` (authoritative `policy > stage >
        role` result) wins; a per-step `TaskStep.effort` applies only as a
        fallback when nothing was resolved upstream. `None` means no effort
        declared anywhere -- MAX_THINKING_TOKENS is left unset (unchanged CLI
        default), never invented.
        """
        return resolve_runner_effort(self.definition, payload.step)

    def _effort_env_overlay(self, payload: RunnerPayload) -> dict[str, str]:
        """Return `{"MAX_THINKING_TOKENS": "<tokens>"}` when *payload* resolves
        to a known effort level, else `{}` (no-op — nothing injected). Uses
        `.get()` defensively rather than indexing: an effort string that
        somehow isn't a recognized `EFFORT_TOKEN_MAP` key (shouldn't happen
        given upstream pydantic validation in `hivepilot.models.validate_effort`,
        but defended anyway per this module's existing defensive style, e.g.
        `_apply_sandbox`'s try/except fallback) is logged and skipped rather
        than raising or injecting a garbage value.
        """
        effort = self._resolve_effort(payload)
        if effort is None:
            return {}
        tokens = EFFORT_TOKEN_MAP.get(effort)
        if tokens is None:
            logger.warning(
                "claude_runner.effort.unknown_level_skipped",
                effort=effort,
            )
            return {}
        return {"MAX_THINKING_TOKENS": str(tokens)}

    def _resolve_tools(self, payload: RunnerPayload) -> str | None:
        """Resolve the `--tools` value for *payload*, or `None` if unset
        (meaning: don't emit the flag at all — byte-identical to before this
        option existed).

        `None`-checked explicitly (not `payload.step.metadata.get(...) or
        self.definition.options.get(...)`) because the whole point of this
        option is to support the empty string `""` ("disable all tools"),
        which is falsy and would be silently skipped by an `or` chain. A
        `list[str]` is comma-joined into the single CLI argument `--tools`
        expects; a `str` (including `""`) is passed through verbatim.
        """
        value = payload.step.metadata.get("tools")
        if value is None:
            value = self.definition.options.get("tools")
        if value is None:
            return None
        if isinstance(value, (list, tuple)):
            return ",".join(str(v) for v in value)
        return str(value)

    def _resolve_str_list_option(self, payload: RunnerPayload, name: str) -> list[str]:
        """Resolve a variadic option to a list of non-empty strings.

        Step metadata wins over the runner definition, same precedence as
        `tools`/`permission_mode`. Accepts a single string or a list. Blank
        entries are dropped: an empty value must never reach the CLI as a
        bare flag with no operand, which would make the NEXT argument look
        like its value.
        """
        value = payload.step.metadata.get(name)
        if value is None:
            value = self.definition.options.get(name)
        if value is None:
            return []
        items = value if isinstance(value, (list, tuple)) else [value]
        return [str(v).strip() for v in items if str(v).strip()]

    def _resolve_mcp_config(self, payload: RunnerPayload) -> list[str]:
        """Resolve `--mcp-config` paths, each made absolute against the project.

        A relative path would otherwise resolve against the runner's working
        directory, which is not where an operator writes the file.
        """
        raw = self._resolve_str_list_option(payload, "mcp_config")
        resolved: list[str] = []
        for item in raw:
            path = Path(item)
            resolved.append(str(path if path.is_absolute() else payload.project.path / path))
        return resolved

    def _resolve_allowed_tools(self, payload: RunnerPayload) -> list[str]:
        """Combine `--allowed-tools` across layers -- union, not override.

        Every other option here resolves "step metadata beats the runner
        definition", which is right for a scalar: one model, one permission
        mode, last writer wins. It is wrong for a whitelist. Overriding a set
        of grants does not refine it, it REVOKES everything the other layer
        granted.

        Run 347 is what that cost. `plugins/token_savior.py` appended one MCP
        entry to the step's `allowed_tools`, which replaced the reviewer
        role's entire grant list. The reviewer then had its own tools denied
        (`rtk git status` among them) and the dispatch died on
        `400 Tool reference 'WaitForMcpServers' not found in available tools`
        -- a `--mcp-config` invocation also needs Claude Code's MCP bootstrap
        tool present, and the list had been narrowed to a single entry.
        $0.56 spent, the stage failed, five stages skipped.

        Definition first, then the step's additions, deduplicated: a config
        file's grants read back in the order the operator wrote them.
        """
        combined: list[str] = []
        for source in (
            self.definition.options.get("allowed_tools"),
            payload.step.metadata.get("allowed_tools"),
        ):
            if source is None:
                continue
            items = source if isinstance(source, (list, tuple)) else [source]
            for item in items:
                text = str(item).strip()
                if text and text not in combined:
                    combined.append(text)
        return combined

    def _resolve_bool_option(self, payload: RunnerPayload, name: str) -> bool:
        """Resolve a boolean flag option; step metadata wins over the definition."""
        value = payload.step.metadata.get(name)
        if value is None:
            value = self.definition.options.get(name)
        return bool(value)

    def _permission_mode(self, payload: RunnerPayload) -> str | None:
        """Resolve the effective permission mode for *payload* (same logic as _build_invocation)."""
        return (
            payload.step.metadata.get("permission_mode")
            or self.definition.options.get("permission_mode")
            or self.settings.claude_permission_mode
        )

    def _runner_failure_context(
        self,
        payload: RunnerPayload,
        *,
        exit_code: int | None,
        stderr_text: str,
        skill_applied: bool,
    ) -> dict[str, Any]:
        """Build the structured diagnostic context for a failed `run()`/
        `capture()` invocation (explicit-failure-logs sprint, Part A.1).

        Every field here is chosen to answer "why did this fail?" WITHOUT
        reading code or reproducing the run: the exit code, which runner/
        model/role/task/stage/project/permission_mode were actually in
        effect, whether a skill was materialised, a bounded (~300 char)
        stderr excerpt, and -- when the text matches a KNOWN failure
        signature (`_detect_known_hint`) -- a `hint` explaining the likely
        cause and fix. `stderr_text` is expected to already be the
        combined stderr/stdout tail the caller extracted; it is redacted
        here (via `redact_text`) before being excerpted so a resolved
        `${secret:NAME}` value can never leak into this log line either.

        Bug 1 (run 243, live incident): a negative `exit_code` means the
        process was killed by a POSIX signal (`-N` == signal N) -- an
        INFRASTRUCTURE failure that never ran to completion, so `stderr_text`
        is typically empty (the process never got to write anything) and
        `_detect_known_hint`'s stderr-signature matching can never fire for
        it. `classify_signal_exit` covers this case independently of
        stderr: when the exit code is a signal death, its `signal` name is
        added to the context and its actionable `message` is used as the
        `hint` whenever no stderr-based hint already matched (a stderr-based
        signature, if one somehow matched anyway, is more specific and wins).
        """
        redacted = redact_text(stderr_text) if stderr_text else stderr_text
        context: dict[str, Any] = {
            "exit_code": exit_code,
            "runner_kind": self.definition.kind,
            "model": self._resolve_model(payload),
            "role": payload.metadata.get("role"),
            "task": payload.task_name,
            "stage": payload.step.name,
            "project": payload.project_name,
            "permission_mode": self._permission_mode(payload),
            "skill_applied": skill_applied,
            "stderr_excerpt": redacted[:300] if redacted else "",
        }
        hint = _detect_known_hint(stderr_text) if stderr_text else None
        signal_death = classify_signal_exit(exit_code)
        if signal_death is not None:
            context["signal"] = signal_death.signal_name
            context["deliberate_stop"] = signal_death.deliberate
            if hint is None:
                hint = signal_death.message
        if hint:
            context["hint"] = hint
        return context

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
        * On success, the scratch directory is intentionally NOT cleaned up
          here — it must survive until `_build_invocation` consumes it inside
          `run()`/`capture()`, which remove it in a `finally` block after the
          subprocess call completes (success or exception) so it never
          outlives the step. If materialisation itself fails partway through
          (e.g. an unresolvable `${secret:NAME}` in a later skill, or an
          unsafe `files` path), the scratch dir is removed here before the
          exception propagates — it is never attached to payload metadata in
          that case, so the `run()`/`capture()` `finally` cleanup would never
          otherwise see it.
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
        try:
            scratch_dir_resolved = scratch_dir.resolve()
            for skill in applicable:
                skill_dir = scratch_dir / ".claude" / "skills" / skill["name"]
                for rel_path, content in skill["files"].items():
                    if Path(rel_path).is_absolute():
                        raise ValueError(f"unsafe skill file path: {rel_path!r}")
                    target = (skill_dir / rel_path).resolve()
                    if not target.is_relative_to(scratch_dir_resolved):
                        raise ValueError(f"unsafe skill file path: {rel_path!r}")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(_resolve_skill_text(content, catalog), encoding="utf-8")
                system_prompt = skill.get("system_prompt")
                if system_prompt:
                    prompts.append(_resolve_skill_text(system_prompt, catalog))
        except Exception:
            # A failure mid-materialisation (unresolvable secret ref, unsafe
            # path, disk error, ...) must not leave a scratch dir — possibly
            # containing already-resolved secret content from an earlier
            # skill in this loop — orphaned on disk after the exception
            # propagates past the point where it would normally be attached
            # to payload metadata for run()/capture()'s finally-block cleanup.
            shutil.rmtree(scratch_dir, ignore_errors=True)
            raise

        metadata[_SKILL_SCRATCH_DIR_KEY] = str(scratch_dir)
        if prompts:
            metadata[_SKILL_SYSTEM_PROMPT_KEY] = "\n\n".join(prompts)
        return replace(payload, metadata=metadata)

    def run(self, payload: RunnerPayload) -> None:
        # mode: api routes through the Anthropic Messages API instead of the
        # CLI binary. The default (cli) path below is byte-identical to before.
        if self._mode(payload) == "api":
            env = merge_environments(payload.project.env, self.definition.env, payload.secrets)
            self._run_api(payload, env)
            return
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
        env_overlay = {
            **gather_overrides(payload.project.env, self.definition.env, payload.secrets),
            **self._effort_env_overlay(payload),
        }
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
        except subprocess.CalledProcessError as exc:
            # `run()` never captures stdout/stderr (it streams straight to the
            # parent's inherited fds for live/interactive visibility) -- so,
            # unlike `capture()` below, there is no stderr text to excerpt or
            # match a known-hint signature against here. Still logs every
            # OTHER field (exit code, runner/model/role/task/stage/project/
            # permission_mode/skill_applied) so a `run()`-path failure is no
            # longer just "returned non-zero exit status 1." with nothing else.
            context = self._runner_failure_context(
                payload,
                exit_code=exc.returncode,
                stderr_text="",
                skill_applied=bool(scratch_dir),
            )
            logger.error("claude_runner.run_failed", **context)
            raise RunnerExecutionError(
                f"claude exited {exc.returncode} (stderr not captured on the run() path — "
                "see logs for full context, or use a capture()-based step for stderr detail)",
                context=context,
            ) from exc
        finally:
            # Ephemeral skill scratch (see apply_skill) must not outlive the
            # step — removed here on BOTH success and exception.
            if scratch_dir:
                shutil.rmtree(scratch_dir, ignore_errors=True)
        logger.info("claude_runner.end", project=payload.project_name, step=payload.step.name)

    def _dispatch(
        self,
        argv: list[str],
        *,
        cwd: str | None,
        env: dict[str, str] | None,
        timeout: float | None,
    ) -> subprocess.CompletedProcess:
        """Run one agent invocation and return what it produced.

        Both of `capture()`'s branches go through here so the pane mode is one
        decision rather than two, and so everything downstream -- the usage
        envelope, the cost, the permission-denial reporting, the failure
        context and its hints -- keeps consuming the same
        `CompletedProcess` and never learns which way the process ran.

        Default OFF. This changes how every step executes, and a box with no
        herdr server must keep working exactly as before rather than fail on a
        missing binary. `run()` is deliberately NOT routed through here: it
        streams to the parent's inherited descriptors on purpose, and capturing
        it to a file to gain a pane would take away the live visibility that is
        its entire reason for existing.
        """
        # The `surface:` axis. A task's declaration wins over the process-wide
        # setting; `"derive"` (the default, which never reaches here because
        # the orchestrator only forwards a DECLARED value) falls through to it,
        # so a host that set `claude_pane_mode` is unaffected.
        #
        # `"inline"` is not the same as leaving it unset: it OVERRULES the
        # setting for this task. That asymmetry is the reason a declared value
        # is checked before the boolean rather than or-ed with it.
        _surface = self.definition.options.get("surface")
        if _surface == "herdr":
            _in_pane = True
        elif _surface == "inline":
            _in_pane = False
        else:
            _in_pane = bool(getattr(self.settings, "claude_pane_mode", False))

        if not _in_pane:
            return subprocess.run(
                argv,
                cwd=cwd,
                env=env,
                check=False,
                text=True,
                capture_output=True,
                timeout=timeout,
                stdin=subprocess.DEVNULL,
            )

        capture_dir = getattr(self.settings, "claude_pane_capture_dir", "") or os.path.join(
            tempfile.gettempdir(), "hivepilot-panes"
        )
        # 0700: the step's environment lands here as a sourceable file for the
        # length of the call, and it carries the step's secrets.
        os.makedirs(capture_dir, mode=0o700, exist_ok=True)
        try:
            return run_in_pane(
                argv,
                capture_dir=capture_dir,
                cwd=cwd,
                env=env,
                timeout=timeout,
                split_direction=self.settings.herdr_split_direction,
                # The run's own workspace when it has one, so the step lands where
                # the run lives rather than in whatever workspace the operator
                # happens to be looking at. `None` keeps the `--current` fallback,
                # which is what a run without a workspace needs.
                target_pane=current_pane(),
            )
        except PaneExecutionError as exc:
            # A pane is a place to WATCH a step from. Being unable to open one
            # is not a reason to skip the step -- and it was: on run 704 the
            # auditor stage, which runs outside any run workspace, died on
            # `--current requires HERDR_PANE_ID`, which is every
            # non-interactive host.
            #
            # Same polarity `run_workspace` already had and this did not: a
            # dead herdr costs visibility, never a run. Loud, though -- losing
            # the pane silently would leave an operator watching a workspace
            # nothing will ever appear in.
            #
            # ONLY `PaneExecutionError`, which is raised for pane
            # infrastructure alone. An agent that ran and exited non-zero comes
            # back as a CompletedProcess and still fails the step: re-running
            # it here would dispatch the same prompt twice, and for a developer
            # role that means committing and pushing the work again.
            # No `payload` here: `_dispatch` takes argv/cwd/env/timeout and
            # nothing else. Referencing it was the same slip as the truncation
            # recorder earlier today -- a name from the caller's scope used in
            # a branch that only runs when something has already gone wrong.
            logger.warning("claude_runner.pane_unavailable", error=str(exc))
            return subprocess.run(
                argv,
                cwd=cwd,
                env=env,
                check=False,
                text=True,
                capture_output=True,
                timeout=timeout,
                stdin=subprocess.DEVNULL,
            )

    def capture(self, payload: RunnerPayload) -> str:
        """Run claude and return its stdout (so the agent's output can be surfaced
        in the interaction log / live stream, not just discarded)."""
        # Clear any stale usage from a prior capture() call up front — every
        # code path below either overwrites this with fresh usage (well-formed
        # JSON) or must leave it None (flag off / any degradation path).
        set_last_usage(None)
        # mode: api routes through the Anthropic Messages API (which reports
        # usage in the SAME response, so no second --output-format json call is
        # needed). The stale-usage clear above applies to both branches.
        if self._mode(payload) == "api":
            env = merge_environments(payload.project.env, self.definition.env, payload.secrets)
            return self._run_api(payload, env)
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
        env_overlay = {
            **gather_overrides(payload.project.env, self.definition.env, payload.secrets),
            **self._effort_env_overlay(payload),
        }
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
                json_result = self._dispatch(json_argv, cwd=cwd, env=run_env, timeout=timeout)
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
                    stderr_text = (json_result.stderr or json_result.stdout or "").strip()
                    context = self._runner_failure_context(
                        payload,
                        exit_code=json_result.returncode,
                        stderr_text=stderr_text,
                        skill_applied=bool(scratch_dir),
                    )
                    # A failing dispatch still reports a full envelope: real
                    # operator output (step 393, run 360) carries `is_error:
                    # true` alongside a complete `modelUsage` block, its
                    # `permission_denials`, and `"result": "Prompt is too
                    # long"`. Both reads below used to sit AFTER this raise,
                    # so every one of the 211 failed steps on the box recorded
                    # `cost_usd = NULL` for money that was genuinely spent,
                    # and `extract_permission_denials` never fired on the
                    # failing case its own docstring was written for.
                    #
                    # Diagnostics only — neither read may change the outcome.
                    # The step failed; only its accounting changes.
                    context["denied_tools"] = [
                        redact_text(d) for d in extract_permission_denials(json_result.stdout)
                    ]
                    if context["denied_tools"]:
                        logger.warning(
                            "claude_runner.permission_denied",
                            project=payload.project_name,
                            step=payload.step.name,
                            # `context["role"]`, not `payload.step.metadata` —
                            # the role lives in the PAYLOAD-level metadata
                            # (orchestrator sets `metadata["role"]`), and the
                            # step's own dict never carries it. The success
                            # path below reads the wrong dict, which is why
                            # every denial it has ever reported said
                            # `"role": null` while its own remediation text
                            # tells the reader to go fix "this role's"
                            # allowed_tools. Reusing the already-resolved
                            # context keeps one source of truth.
                            #
                            # Still not airtight: the orchestrator only sets
                            # that key when context injection is enabled, so a
                            # flag-off run reports None here honestly rather
                            # than guessing.
                            role=context["role"],
                            denied=context["denied_tools"],
                            remediation=(
                                "add the matching pattern to this role's "
                                "`allowed_tools` (governance prefixes shell "
                                "commands with `rtk `, so the grant must too)"
                            ),
                        )
                    _failed_parse = _parse_usage_envelope(json_result.stdout)
                    if _failed_parse is not None:
                        set_last_usage(_failed_parse[1])
                    logger.error("claude_runner.run_failed", **context)
                    err = redact_text(stderr_text)[-2000:]
                    hint_suffix = f" | hint: {context['hint']}" if context.get("hint") else ""
                    raise RunnerExecutionError(
                        f"claude exited {json_result.returncode}: {err}{hint_suffix}",
                        context=context,
                    )
                # Report refused tools BEFORE branching on success. The
                # failing dispatch is the lucky one: an agent refused a tool
                # that still finishes looks like a success, having done the
                # expensive thing instead — and it will keep doing that on
                # every run until somebody happens to look. Run 356's CTO
                # was refused `rtk fd`, explored a monorepo by hand, and died
                # at 84 969 input tokens on "Prompt is too long"; the same
                # denial on a smaller repo would simply have cost more,
                # forever, in silence.
                denied = extract_permission_denials(json_result.stdout)
                if denied:
                    logger.warning(
                        "claude_runner.permission_denied",
                        project=payload.project_name,
                        step=payload.step.name,
                        # Same correction as the failure path above: the role
                        # is on the payload metadata, never on the step's.
                        role=payload.metadata.get("role"),
                        denied=denied,
                        remediation=(
                            "add the matching pattern to this role's "
                            "`allowed_tools` (governance prefixes shell "
                            "commands with `rtk `, so the grant must too)"
                        ),
                    )

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

            result = self._dispatch(argv, cwd=cwd, env=run_env, timeout=timeout)
            if result.returncode != 0:
                stderr_text = (result.stderr or result.stdout or "").strip()
                context = self._runner_failure_context(
                    payload,
                    exit_code=result.returncode,
                    stderr_text=stderr_text,
                    skill_applied=bool(scratch_dir),
                )
                # Always present, so a consumer never has to tell "no denials"
                # apart from "this path does not report them". Flag-off runs
                # dispatch without `--output-format json`, so stdout is raw
                # text and this is structurally always empty — that is a fact
                # about the dispatch, not a missing feature.
                context["denied_tools"] = []
                logger.error("claude_runner.run_failed", **context)
                err = redact_text(stderr_text)[-2000:]
                hint_suffix = f" | hint: {context['hint']}" if context.get("hint") else ""
                raise RunnerExecutionError(
                    f"claude exited {result.returncode}: {err}{hint_suffix}",
                    context=context,
                )
            return result.stdout
        finally:
            # Ephemeral skill scratch (see apply_skill) must not outlive the
            # step — removed here on BOTH success and exception.
            if scratch_dir:
                shutil.rmtree(scratch_dir, ignore_errors=True)

    # ── mode: api (Anthropic Messages API) ────────────────────────────────────

    def _run_api(self, payload: RunnerPayload, env: dict[str, str]) -> str:
        """Execute this step against the Anthropic Messages API and return the
        assistant's reply text (the same ``str`` shape ``capture()``'s CLI path
        returns), stashing usage via ``set_last_usage`` for the orchestrator to
        pop right after.

        SECURITY (fail closed + mask at the runner):
        - The API key is read ONLY from the resolved ``env`` (populated from
          ``${secret:ANTHROPIC_API_KEY}`` upstream). A missing key raises a
          clear error and performs NO request — never falls back to an empty
          key that would hit the API.
        - The key travels only in the ``x-api-key`` header, never in argv (there
          is no subprocess here), an exception message, or a log line.
        - The resolved key is registered with ``register_secret_value`` and the
          returned text is passed through ``redact_text`` AT the runner, so the
          key can never surface in ``RunResult.detail`` even if the provider
          reflected it back — this does not rely on any downstream sink.
        """
        prompt = self._assemble_prompt(payload)
        model = self._resolve_model(payload)
        set_last_resolved_model(model)
        if not model:
            raise RuntimeError(
                "Claude API mode requires a model (set the runner model, step "
                "metadata 'model', a profile, or settings.default_model)."
            )
        api_key = env.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set — refusing to run claude in API "
                "mode without a key (fail closed)."
            )
        # Register the resolved key so it is redacted everywhere downstream, and
        # so the redact_text call on the returned text below actually masks it.
        register_secret_value(api_key)

        headers: dict[str, str] = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        # Split stable system (cacheable) + volatile user trigger, mirroring
        # PromptCliRunner._run_api. _assemble_prompt already orders the prompt
        # stable→volatile, so the whole thing is safe to cache as the system block.
        if self.settings.anthropic_prompt_cache:
            headers["anthropic-beta"] = "prompt-caching-2024-07-31"
            api_payload: dict[str, Any] = {
                "model": model,
                "max_tokens": _ANTHROPIC_API_MAX_TOKENS,
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
                "max_tokens": _ANTHROPIC_API_MAX_TOKENS,
                "messages": [{"role": "user", "content": prompt}],
            }
        timeout = payload.step.timeout_seconds or self.definition.timeout_seconds
        result = self._post_json(
            url="https://api.anthropic.com/v1/messages",
            headers=headers,
            payload=api_payload,
            timeout=timeout,
        )
        text = self._extract_api_text(result)
        usage = self._extract_api_usage(result)
        if usage is not None:
            set_last_usage(usage)
        # Mask AT the runner — RunResult.detail is known-unredacted at the
        # choke point, so never depend on a downstream sink to catch the key.
        return redact_text(text)

    def _post_json(
        self,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str],
        timeout: int | None = None,
    ) -> dict[str, Any]:
        # Log metadata only — never the response body (it can reflect the
        # prompt / knowledge context) and never the headers (they carry the key).
        logger.info("claude_runner.api_request", url=url, model=payload.get("model"))
        response = requests.post(url, json=payload, headers=headers, timeout=timeout or 60)
        if not response.ok:
            # response.text can echo request content but NOT the key (the key is
            # a header, not in the body); still, keep it bounded.
            raise RuntimeError(
                f"Anthropic API request failed: {response.status_code} "
                f"{redact_text((response.text or '')[-2000:])}"
            )
        result = response.json()
        logger.info(
            "claude_runner.api_response",
            status_code=response.status_code,
            bytes=len(response.content),
        )
        return result if isinstance(result, dict) else {}

    def _extract_api_text(self, result: Any) -> str:
        """Extract the assistant reply text from an Anthropic Messages response.

        Defensive: never raises — an unexpected shape yields "" rather than
        crashing the step (the HTTP call already succeeded by the time this runs).
        """
        if not isinstance(result, dict):
            return ""
        blocks = result.get("content")
        if not isinstance(blocks, list):
            return ""
        return "".join(
            b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"
        )

    def _extract_api_usage(self, result: Any) -> UsageInfo | None:
        """Build a ``UsageInfo`` from an Anthropic response's ``usage`` block,
        or ``None`` when absent. Never invents values; ``cost_usd`` stays None
        (Anthropic doesn't report cost in the response body — a price-map can
        estimate it downstream from tokens+model)."""
        if not isinstance(result, dict):
            return None
        usage = result.get("usage")
        if not isinstance(usage, dict):
            return None
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        if input_tokens is None and output_tokens is None:
            return None
        model = result.get("model")
        return UsageInfo(
            input_tokens=input_tokens if isinstance(input_tokens, int) else None,
            output_tokens=output_tokens if isinstance(output_tokens, int) else None,
            cost_usd=None,
            model=model if isinstance(model, str) else None,
        )

    def _build_prompt(
        self, payload: RunnerPayload, instructions: str, knowledge_context: str | None
    ) -> str:
        # Stable sections first so Anthropic/OpenAI prefix caching covers the static prefix.
        sections = [
            f"Project: {payload.project_name}",
            f"Task: {payload.task_name}",
            f"Step: {payload.step.name}",
        ]
        # `RunnerPayload.project` is typed as a plain (non-Optional)
        # ProjectConfig -- every real call site (including the human-
        # challenge/ask feature and direct inter-agent requests, see
        # `orchestrator._synthetic_project`) always supplies one, even for
        # out-of-band, repo-less invocations (a minimal synthetic
        # ProjectConfig with no real repository). This `is not None` guard is
        # therefore pure defence-in-depth against some FUTURE caller
        # reintroducing `project=None` -- it must never crash
        # (`AttributeError: 'NoneType' object has no attribute 'path'`) even
        # if that contract is ever violated again.
        if payload.project is not None:
            sections.append(f"Repository path: {payload.project.path}")
            if payload.project.description:
                sections.append(f"Project description: {payload.project.description}")
            # inline-repo-instructions PRD: resolve + inline the CONTENT of
            # every declared repository instructions file (`claude_md` +
            # `instruction_files`) instead of only naming it -- see
            # `repo_instructions.build_repo_instructions_section` for
            # resolution/warn-on-missing/size-cap/redaction behavior.
            instructions_section = build_repo_instructions_section(payload.project, self.settings)
            if instructions_section:
                sections.append(instructions_section)
        # The role's own prompt file belongs HERE, in the stable prefix.
        #
        # It used to be appended after everything else, which defeated the
        # intent stated at the top of this function: a prefix cache stops at
        # the first byte that changed, so with the volatile sections in
        # front, the largest stable block in the whole prompt -- hundreds of
        # lines, identical every run -- was re-created every time.
        #
        # Measured on the noxys deployment, `ceo intake` over ten runs:
        # cache_creation ~43 000 against cache_read ~16 000, amortisation
        # 0.40. It created more cache than it ever read back, at 1.25x base
        # input for a creation against 0.1x for a read.
        #
        # The order also reads better than it caches. The role prompt is
        # CONTEXT ("you are the CEO, this is how you work"); `extra_prompt`
        # is the REQUEST. Putting the request last is the conventional shape
        # -- it was the caching accident that had it in the middle.
        target_repo = str(payload.project.path) if payload.project and payload.project.path else "."
        obsidian_vault = resolve_prompt_vault(self.settings, payload.project)
        instructions = render_prompt_vars(
            instructions,
            target_repo=target_repo,
            governance_repo=settings.governance_repo or "",
            obsidian_vault=obsidian_vault,
        )
        sections.append(f"Instructions:\n{instructions}")

        # Standing operator corrections, immediately after the role prompt
        # they modify: "this is how you work" then "and stop doing X".
        #
        # Deliberately inside the STABLE prefix, above knowledge/lessons/prior
        # context. They change only when the operator edits a file, so keeping
        # them here leaves the cached prefix contiguous -- moving them below
        # the volatile sections would invalidate the cache for their sake, the
        # same mistake the role prompt's placement used to make.
        corrections = load_corrections(payload.metadata.get("role"))
        if corrections:
            sections.append(corrections)

        if knowledge_context:
            sections.append(f"Knowledge context:\n{knowledge_context}")
        lessons_context = self._build_lessons_context(payload)
        if lessons_context:
            sections.append(f"Lessons learned:\n{lessons_context}")
        append = payload.step.append_prompt or self.definition.append_prompt
        if append:
            sections.append(f"Step-specific instructions: {append}")
        prior = payload.metadata.get("prior_context")
        if prior:
            sections.append(f"Outputs from previous agents:\n{prior}")
        # Volatile last, and the per-run request last of all.
        extra = payload.metadata.get("extra_prompt")
        if extra:
            sections.append(f"Extra instructions from user: {extra}")
        return "\n".join(sections)

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

    def _build_lessons_context(self, payload: RunnerPayload) -> str:
        """Return the stable 'Lessons learned' block for this step's
        project/role/task (Auto-Learning Lessons Loop PRD, Sprint 3) --
        see `knowledge_service.build_lessons_context` for the fail-closed
        gate/ranking contract. ``role`` comes from ``payload.metadata``
        (``Orchestrator._execute_task_body`` threads ``task.role`` in
        there for exactly this purpose) -- ``None`` when unavailable
        (non-role task, or a payload built by a call site that predates
        this Sprint), which degrades retrieval to project+task keying
        rather than crashing.
        """
        from hivepilot.services.knowledge_service import build_lessons_context

        role = payload.metadata.get("role")
        return build_lessons_context(
            payload.project_name, role, payload.task_name, effective=payload.lessons
        )
