"""CursorRunner — `cursor-agent`, on the claude-shaped path.

Moved off `PromptCliRunner`, which applies neither `permission_mode` nor
`allowed_tools`, and which therefore gave cursor none of the shared
infrastructure: the herdr pane path, the container workspace, the
resolved-model stamp, `--add-dir` for a skill's scratch directory.

Its headless surface IS claude's. Read from `cursor-agent --help`, 2026-08-21:

    -p, --print          same spelling AND same shape — a boolean, prompt
                         positional. Its own help notes this mode "has access
                         to all tools, including write and shell".
    --model <model>      e.g. `gpt-5`, `sonnet-4-thinking` — MULTIPLE VENDORS
                         through one runner
    --add-dir <path>     additional workspace root, same as claude's
    --output-format      text | json | stream-json

What it does NOT have, and why this declares nothing honoured:

    there is no `--allowed-tools`. Nothing accepts a per-invocation tool
    whitelist, so a role's `allowed_tools` cannot be applied — and
    `allowed_tools_flag = None` makes the base emit nothing rather than pass
    a flag cursor would reject.

    there is no `--permission-mode`. The nearest kin are `--mode plan|ask`
    (genuinely read-only), `--sandbox enabled|disabled` and `--force`
    ("Force allow commands unless explicitly denied" — so a DENY mechanism
    exists, config-level, not as a flag). None is a drop-in for claude's
    values, and mapping `bypassPermissions` onto `--force` by eye would be
    exactly the guess this codebase keeps paying for.

So `honoured_controls` stays empty and a restricted role is REFUSED on cursor,
as it has been since #569. That is unchanged, not a regression — what changes
is everything else it now inherits.

Two things worth knowing for later. `cursor-agent` has its own `-w/--worktree`,
which overlaps `workspace: worktree`; the engine's should win, and nothing here
passes cursor's. And `--mode plan` is a real read-only mode with no claude
equivalent — a better fit for a reviewer role than any permission setting, once
somebody probes it.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from typing import ClassVar

from hivepilot.runners.claude_runner import ClaudeRunner


@dataclass
class CursorRunner(ClaudeRunner):
    # CLI only. `ClaudeRunner` advertises cli+api, but its API path is
    # Anthropic's Messages API specifically — a resolved `mode: api` must fail
    # closed at validation rather than dispatch to the wrong vendor.
    supported_modes: ClassVar[frozenset[str]] = frozenset({"cli"})

    #: Empty, and honestly so: cursor-agent has no per-invocation tool
    #: whitelist and no permission-mode flag. Declaring either without wiring
    #: it is the one way to make `assert_runner_honours` lie.
    honoured_controls: ClassVar[frozenset[str]] = frozenset()

    #: Same spelling and same shape as claude's — a boolean with the prompt
    #: positional, unlike grok's `-p <PROMPT>`.
    #: Same fallback the old PromptCliRunner-based version declared — and the
    #: reason its `command_name` test survives this move unchanged.
    command_name: ClassVar[str | None] = "cursor-agent"
    print_flag: ClassVar[str] = "--print"
    #: No such flag on cursor-agent. `None` makes the base emit nothing.
    allowed_tools_flag: ClassVar[str | None] = None
    permission_mode_flag: ClassVar[str | None] = None
    #: `mcp` subcommand / `--approve-mcps`, not `--mcp-config`.
    mcp_config_flag: ClassVar[str | None] = None
    strict_mcp_config_flag: ClassVar[str | None] = None
    append_system_prompt_flag: ClassVar[str | None] = None

    def _check_binary(self) -> None:
        """Raise a RuntimeError naming the binary, rather than letting a
        subprocess fail cryptically."""
        if shutil.which("cursor-agent") is None:
            raise RuntimeError(
                "cursor-agent binary not found on PATH. "
                "Install the Cursor CLI to use the CursorRunner."
            )

    def run(self, payload) -> None:  # type: ignore[override]
        self._check_binary()
        super().run(payload)
