"""GrokRunner — xAI's `grok` CLI, on the claude-shaped path.

Inherits `ClaudeRunner` rather than `PromptCliRunner`, and that choice is the
whole point of this file. `PromptCliRunner` applies neither `permission_mode`
nor `allowed_tools` (0 references, against 38 in `claude_runner`), so a
three-line subclass there would have run every restricted role unrestricted —
and, since #569, would simply be refused. It would also lose the elevated-mode
sandbox and environment scrub.

Grok's headless surface is Claude Code's, values included. Verified against
`grok --help`, 1.0.5, 2026-08-21:

    --permission-mode {default, acceptEdits, auto, dontAsk, bypassPermissions,
                       plan}      -- Claude's exact set, plus `auto`/`dontAsk`
    --allow <RULE>                -- compat alias `--allowedTools`
    --deny <RULE>                 -- compat alias `--disallowedTools`
    -m, --model                   -- grok-4.6 (default) | grok-4.5
    --tools, --agent, --system-prompt-override, --output-format, --prompt-file

The compat aliases are not cosmetic. `grok inspect` reports a *Harness
Compatibility* table with `claude -> skills/rules/agents/mcps/hooks/sessions:
on (default)`: it reads `~/.claude/settings.json` and loads what it finds
there. Measured on this machine — 49 hooks, including
`event=PreToolUse matcher='Bash' command='rtk hook claude'`, so **rtk works
unmodified**; and `49 loaded, 1 skipped` permission rules from
`.claude/settings.local.json` with `permissions.allow: NotebookEdit --
unsupported tool prefix`, so it PARSES and VALIDATES Claude's rule grammar
rather than ignoring it. `Bash(rtk git:*)` and `Read(./**)` arrive understood.

Two things grok has that claude does not, deliberately left for later:

  * `--prompt-file <PATH>` removes the `MAX_ARG_STRLEN` failure class (131072
    bytes per argv element). Worth adopting, but it is a change to the shared
    prompt path and belongs in its own change, not smuggled into a new runner.
  * `--system-prompt-override` is an OVERRIDE where claude's
    `--append-system-prompt` APPENDS. Mapping them would silently discard
    grok's default system prompt. Nothing routes through it today — no noxys
    role uses skills — so it is named here rather than guessed at.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from hivepilot.runners.claude_runner import ClaudeRunner


@dataclass
class GrokRunner(ClaudeRunner):
    # CLI only. `ClaudeRunner` advertises cli+api, but the API path here is
    # Anthropic's Messages API specifically — a resolved `mode: api` must fail
    # closed at orchestrator validation rather than dispatch to the wrong
    # vendor's endpoint.
    supported_modes: ClassVar[frozenset[str]] = frozenset({"cli"})

    # Inherited unchanged, and this is the reason for the inheritance: the
    # permission mode, the tool allow-list, the elevated-mode sandbox and the
    # env scrub all apply exactly as they do for claude.
    honoured_controls: ClassVar[frozenset[str]] = frozenset({"permission_mode", "allowed_tools"})

    #: `-p` is grok's `--single`; it takes the prompt as its VALUE where
    #: claude's `--print` is a boolean with the prompt positional. The base
    #: builder appends the prompt after the flags, which lands it in exactly
    #: the right place for both.
    #: Without this, a role-synthesized definition (`command=None`) fell
    #: through to `settings.claude_command` and LAUNCHED CLAUDE. The bug
    #: shipped in #570 and was caught by cursor's old `command_name` test.
    command_name: ClassVar[str | None] = "grok"
    print_flag: ClassVar[str] = "-p"
    #: `--allow`, whose own help calls `--allowedTools` a compat alias.
    allowed_tools_flag: ClassVar[str] = "--allow"
    #: Same spelling AND the same values as claude's.
    permission_mode_flag: ClassVar[str] = "--permission-mode"
