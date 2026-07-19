"""Guided agent-binary install helper.

⚠️ MAINTAINER MUST VET every pinned `command` here — each is run (with
explicit operator consent) as a shell pipeline. Sourced from the cited
official docs; verify before trusting.

HivePilot's runners shell out to third-party coding-agent CLIs (`claude`,
`codex`, `cursor-agent`, ...). When one is missing from PATH, this module lets
an operator be walked through installing it — but it NEVER installs anything
on its own:

  - No auto-install, ever. `propose_install` only executes a command after an
    explicit, interactive "yes" from a human at a terminal.
  - No install in a non-interactive / scheduled context (CI, cron, a
    pipeline run headlessly) — `propose_install` refuses to run even when
    `assume_yes=True` unless both stdin and stdout are attached to a TTY.
  - Only a pinned, maintainer-reviewed constant from `AGENT_INSTALL_SPECS` is
    ever executed. Nothing dynamic, user-supplied, or config-sourced is EVER
    concatenated into `spec.command` — keep it that way.
  - Where the official vendor docs do not offer a clean, verifiable
    `curl | sh`-style one-liner (e.g. it is package-manager-only: brew/apt/
    dnf/npm/uv), `command` is left `None` and only `docs_url` is provided —
    docs-only, manual install, no execution path at all.

Each `InstallSpec` below carries a comment citing the exact official-docs URL
it was sourced from (fetched 2026-07-19). If a vendor changes their install
flow, update the spec AND the citation together.
"""

from __future__ import annotations

import shutil
import subprocess  # nosec B404 - only ever invoked with a maintainer-vetted, in-repo constant command (see propose_install); no dynamic input.
import sys
from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class InstallSpec:
    """A single agent CLI's install metadata.

    `command`, when set, MUST be the exact official one-liner sourced from
    the vendor's own docs (cited in the comment above the spec) — never a
    guess. `None` means docs-only: no verified one-liner to run.
    """

    name: str
    binary: str
    vendor: str
    docs_url: str
    command: str | None = None


AGENT_INSTALL_SPECS: dict[str, InstallSpec] = {
    # Source: https://code.claude.com/docs/en/quickstart
    # ("Native Install (Recommended)" tab, macOS/Linux/WSL). Fetched 2026-07-19.
    "claude": InstallSpec(
        name="Claude Code",
        binary="claude",
        vendor="Anthropic",
        docs_url="https://code.claude.com/docs/en/quickstart",
        command="curl -fsSL https://claude.ai/install.sh | bash",
    ),
    # Source: https://github.com/openai/codex (README.md, macOS & Linux
    # shell installer). Fetched 2026-07-19.
    "codex": InstallSpec(
        name="OpenAI Codex CLI",
        binary="codex",
        vendor="OpenAI",
        docs_url="https://github.com/openai/codex",
        command="curl -fsSL https://chatgpt.com/codex/install.sh | sh",
    ),
    # Source: https://cursor.com/docs/cli/installation ("macOS and Linux"
    # section). Fetched 2026-07-19. Binary installed is `cursor-agent`
    # (matches hivepilot.runners.cursor_runner.CursorRunner.command_name).
    "cursor": InstallSpec(
        name="Cursor CLI",
        binary="cursor-agent",
        vendor="Cursor (Anysphere)",
        docs_url="https://cursor.com/docs/cli/installation",
        command="curl https://cursor.com/install -fsS | bash",
    ),
    # Source: https://github.com/google-gemini/gemini-cli
    # (docs/get-started/installation.md). Fetched 2026-07-19. The only
    # officially documented installs are npm/npx/Homebrew/MacPorts/conda —
    # no vendor curl-pipe one-liner. Package-manager-based → docs-only.
    "gemini": InstallSpec(
        name="Gemini CLI",
        binary="gemini",
        vendor="Google",
        docs_url="https://github.com/google-gemini/gemini-cli",
        command=None,
    ),
    # Source: https://opencode.ai/docs/ ("Install" section, "the easiest way
    # to install OpenCode"). Fetched 2026-07-19.
    "opencode": InstallSpec(
        name="opencode",
        binary="opencode",
        vendor="opencode",
        docs_url="https://opencode.ai/docs/",
        command="curl -fsSL https://opencode.ai/install | bash",
    ),
    # Source: https://docs.ollama.com/linux ("Install" section). Fetched
    # 2026-07-19.
    "ollama": InstallSpec(
        name="Ollama",
        binary="ollama",
        vendor="Ollama",
        docs_url="https://docs.ollama.com/linux",
        command="curl -fsSL https://ollama.com/install.sh | sh",
    ),
    # Source: https://github.com/MoonshotAI/kimi-cli (README / "Getting
    # Started": https://moonshotai.github.io/kimi-cli/en/guides/getting-started.html).
    # Fetched 2026-07-19. Official install is `uv tool install kimi-cli`
    # (a Python package manager, not a curl-pipe script) → docs-only.
    "kimi-cli": InstallSpec(
        name="Kimi Code CLI",
        binary="kimi",
        vendor="Moonshot AI",
        docs_url="https://github.com/MoonshotAI/kimi-cli",
        command=None,
    ),
    # Source: https://github.com/QwenLM/qwen-code (README, npm install
    # instructions). Fetched 2026-07-19. Official install is
    # `npm install -g @qwen-code/qwen-code` — package-manager-based → docs-only.
    "qwen-code": InstallSpec(
        name="Qwen Code",
        binary="qwen",
        vendor="Alibaba (QwenLM)",
        docs_url="https://github.com/QwenLM/qwen-code",
        command=None,
    ),
    # Source: https://docs.mistral.ai/vibe/code/cli/install-setup
    # ("Install and setup" page). Fetched 2026-07-19.
    "vibe": InstallSpec(
        name="Mistral Vibe CLI",
        binary="vibe",
        vendor="Mistral AI",
        docs_url="https://docs.mistral.ai/vibe/code/cli/install-setup",
        command="curl -LsSf https://mistral.ai/vibe/install.sh | bash",
    ),
    # Source: https://github.com/google-antigravity/antigravity-cli (README,
    # "Install" section, macOS/Linux). Fetched 2026-07-19.
    "antigravity": InstallSpec(
        name="Antigravity CLI",
        binary="agy",
        vendor="Google",
        docs_url="https://github.com/google-antigravity/antigravity-cli",
        command="curl -fsSL https://antigravity.google/cli/install.sh | bash",
    ),
    # Source: https://cli.github.com/ / https://github.com/cli/cli#installation.
    # Fetched 2026-07-19. GitHub CLI ships exclusively through package
    # managers (brew/apt/dnf/winget/zypper) and direct binary downloads —
    # no official curl-pipe script → docs-only, per operator request.
    "gh": InstallSpec(
        name="GitHub CLI",
        binary="gh",
        vendor="GitHub",
        docs_url="https://cli.github.com/",
        command=None,
    ),
}


def is_on_path(binary: str) -> bool:
    """Return True if `binary` resolves on PATH via `shutil.which`."""
    return shutil.which(binary) is not None


def get_install_spec(kind: str) -> InstallSpec | None:
    """Look up the `InstallSpec` for a runner kind, or None if unknown."""
    return AGENT_INSTALL_SPECS.get(kind)


def missing_agents(kinds: Iterable[str]) -> list[str]:
    """Return the subset of `kinds` whose agent binary is NOT on PATH.

    Uses each kind's `InstallSpec.binary` when a spec is registered (the
    binary name can differ from the runner kind, e.g. "cursor" -> binary
    "cursor-agent"); falls back to the kind string itself for unregistered
    kinds. Preserves input order.
    """
    missing = []
    for kind in kinds:
        spec = get_install_spec(kind)
        binary = spec.binary if spec is not None else kind
        if not is_on_path(binary):
            missing.append(kind)
    return missing


@dataclass(frozen=True)
class InstallResult:
    """Outcome of a `propose_install` call."""

    ran: bool
    exit_code: int | None
    message: str


def propose_install(
    spec: InstallSpec,
    *,
    assume_yes: bool = False,
    interactive: bool | None = None,
) -> InstallResult:
    """Show `spec.command` to the operator and run it ONLY on explicit,
    interactive consent. Never auto-installs; never runs in a non-interactive
    / scheduled context (a headless caller must not be able to trigger a
    remote-code-execution pipeline just by passing `assume_yes=True`).

    - `spec.command is None` -> docs-only, never executes; points at
      `spec.docs_url`.
    - Not interactive (default: `sys.stdin.isatty() and sys.stdout.isatty()`)
      -> refuses WITHOUT executing, even if `assume_yes=True`.
    - Interactive: prints the command + a loud warning, then prompts for
      y/N (default No) unless `assume_yes` is set. Only on "yes" does this
      function execute anything.
    """
    if interactive is None:
        interactive = sys.stdin.isatty() and sys.stdout.isatty()

    if spec.command is None:
        return InstallResult(
            ran=False,
            exit_code=None,
            message=(
                f"{spec.name}: no verified official one-liner — install manually, "
                f"see {spec.docs_url}"
            ),
        )

    if not interactive:
        return InstallResult(
            ran=False,
            exit_code=None,
            message=f"refused: non-interactive; run manually: {spec.command}",
        )

    print(f"About to install {spec.name} using the official {spec.vendor} installer:")
    print(f"  {spec.command}")
    print(
        f"⚠️  This downloads and runs an install script from {spec.vendor}. "
        "Review it before continuing."
    )

    if not assume_yes:
        answer = input("Proceed? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            return InstallResult(ran=False, exit_code=None, message="declined by operator")

    # spec.command is a frozen, maintainer-vetted, in-repo constant from
    # AGENT_INSTALL_SPECS above — NEVER dynamic/user/config/network input.
    # Official installers are `curl ... | sh` pipes, which cannot be
    # expressed as a plain list-argv subprocess call, so we hand the exact
    # pinned string to `bash -lc`. This is safe ONLY because the string is a
    # vetted constant, not because shell interpretation is generally safe —
    # keep it that way.
    result = subprocess.run(  # noqa: S602 - vetted in-repo constant, see comment above  # nosec B602
        ["bash", "-lc", spec.command], check=False
    )
    return InstallResult(
        ran=True,
        exit_code=result.returncode,
        message=f"{spec.name} installer exited with code {result.returncode}",
    )
