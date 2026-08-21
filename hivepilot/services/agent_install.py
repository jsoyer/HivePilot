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
    # "binary" -- the command drops an executable on PATH, which is every
    # coding-agent CLI and every terminal multiplexer here.
    #
    # "service" -- the tool is not a PATH entry at all. Orca is an AppImage
    # plus a systemd unit plus an update job plus a pairing step, and calling
    # that a binary install would let `plugins enable` report success over a
    # service that is not running. `_install_binary` declines these on
    # purpose; a service needs its own installer, and until that exists an
    # honest refusal beats a false success.
    kind: str = "binary"


AGENT_INSTALL_SPECS: dict[str, InstallSpec] = {
    # Not a coding agent, but a plugin prerequisite that `plugins enable` must
    # be able to satisfy: enabling a plugin and then telling the operator its
    # prerequisite is missing is not an install path. Registered here so both
    # go through ONE vetted execution route (`propose_install`, TTY-gated).
    #
    # herdr is registered per-ARCHITECTURE below (see `_HERDR_BY_ARCH`) rather
    # than here, because its release assets are per-arch and this table holds
    # one command per kind. `get_install_spec("herdr")` resolves it.
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
    # Source: https://docs.x.ai/build/overview and https://x.ai/news/grok-build-cli.
    # Fetched 2026-08-21, and the one-liner VERIFIED by running it on the box:
    # it is per-user (no sudo anywhere in the script) and lands the binary in
    # `$HOME/.grok/bin`, which is NOT on the systemd units' PATH — see
    # bundled_plugins/grok.py's health() for why that matters.
    #
    # Auth needs no API key: `grok login --device-auth` prints a URL to open
    # elsewhere and writes the token locally, which is the flow a headless box
    # needs. Proven with a scrubbed environment (`env -i`, zero XAI*/GROK_*KEY
    # vars) returning a real answer.
    "grok": InstallSpec(
        name="Grok Build CLI",
        binary="grok",
        vendor="xAI",
        docs_url="https://docs.x.ai/build/overview",
        command="curl -fsSL https://x.ai/cli/install.sh | bash",
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


# herdr ships static-pie release binaries, one per architecture — verified
# against `gh release view --repo ogulcancelik/herdr` on 2026-08-17 (v0.8.0,
# assets: herdr-linux-x86_64, herdr-linux-aarch64, herdr-macos-*). Apache-2.0,
# homepage https://herdr.dev.
#
# NOT brew: the operator's Debian box has no brew, and herdr was already
# installed there by hand (0.7.5) — a package manager we would have to install
# first is not an install path. A static-pie binary has no dependencies, which
# is exactly what a server wants.
#
# One vetted constant PER ARCH rather than one command with `$(uname -m)` in
# it: `propose_install` executes these as shell pipelines, and this module's
# rule is that nothing dynamic is ever concatenated into a command. Python
# picks among constants; the string that runs is always one we reviewed.
# Prerequisites that are NOT coding agents. Kept out of AGENT_INSTALL_SPECS
# because that registry is a closed set guarded by a test -- and rightly so:
# `doctor`, `MANDATORY_AGENTS` and the runner taxonomy all read it as "the
# coding agents we know". A terminal multiplexer and an IDE server are neither.
#
# `get_install_spec` searches agents first, then here, so one lookup serves
# both and `plugins enable` has a single install route.
_TOOL_INSTALL_SPECS: dict[str, InstallSpec] = {
    # Source: https://www.onorca.dev/ — release channel taken from the
    # operator's own working `update-orca.sh`, which is authoritative for this
    # deployment: it already fetches exactly this URL, replaces the AppImage
    # atomically, and restarts `orca-serve.service`.
    #
    # kind="service" because that is what Orca is. The command below is the
    # fetch-and-place step ONLY; it is deliberately never run by the binary
    # path, and it writes under /opt, so a service installer must own both the
    # privilege and the unit lifecycle around it.
    "orca": InstallSpec(
        name="Orca",
        binary="orca",
        vendor="Stably AI",
        docs_url="https://www.onorca.dev/",
        command=(
            "curl -fL --retry 3 --retry-delay 2 "
            "-o /opt/orca/orca-linux.AppImage "
            "https://github.com/stablyai/orca/releases/latest/download/orca-linux.AppImage"
            " && chmod +x /opt/orca/orca-linux.AppImage"
        ),
        kind="service",
    ),
}


_HERDR_URL = "https://github.com/ogulcancelik/herdr/releases/latest/download"
_HERDR_BY_ARCH: dict[str, InstallSpec] = {
    arch: InstallSpec(
        name="herdr",
        binary="herdr",
        vendor="herdr.dev",
        docs_url="https://herdr.dev",
        command=(
            f"mkdir -p ~/.local/bin && curl -fL --retry 3 --retry-delay 2 "
            f"-o ~/.local/bin/herdr {_HERDR_URL}/herdr-{asset} "
            f"&& chmod +x ~/.local/bin/herdr"
        ),
    )
    for arch, asset in (
        ("x86_64", "linux-x86_64"),
        ("amd64", "linux-x86_64"),
        ("aarch64", "linux-aarch64"),
        ("arm64", "linux-aarch64"),
    )
}


def get_install_spec(kind: str) -> InstallSpec | None:
    """Look up the `InstallSpec` for a runner kind, or None if unknown.

    `herdr` resolves per-architecture. An architecture with no published asset
    returns None — docs-only — rather than a command that would download a
    binary for the wrong machine and leave it on PATH, where the failure would
    surface much later as an exec format error.
    """
    if kind == "herdr":
        import platform

        return _HERDR_BY_ARCH.get(platform.machine().lower())
    spec = AGENT_INSTALL_SPECS.get(kind)
    if spec is not None:
        return spec
    return _TOOL_INSTALL_SPECS.get(kind)


def all_install_specs() -> dict[str, InstallSpec]:
    """Every registered spec — agents, tools, and this machine's herdr.

    Exists so the vetting tests (a citable source, no interpolated command)
    cover the tool registry too. Splitting the registries must not shrink what
    those tests see: an unvetted command is the one thing this module exists to
    prevent.
    """
    specs = dict(AGENT_INSTALL_SPECS)
    specs.update(_TOOL_INSTALL_SPECS)
    herdr = get_install_spec("herdr")
    if herdr is not None:
        specs["herdr"] = herdr
    return specs


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
    # keep it that way. No inline noqa/nosec is needed: ruff does not select
    # the flake8-bandit (S) family, and bandit's own B603 (subprocess call)
    # and B607 (partial exec path) are already skipped project-wide in
    # pyproject.toml [tool.bandit]. (B602 does NOT apply here — this call does
    # not pass shell=True.)
    result = subprocess.run(["bash", "-lc", spec.command], check=False)
    return InstallResult(
        ran=True,
        exit_code=result.returncode,
        message=f"{spec.name} installer exited with code {result.returncode}",
    )
