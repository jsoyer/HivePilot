"""Enabling an agent plugin is what installs it — not a step before, not
after.

Today an agent kind needs four disjoint things to line up: the plugin `.py`
in the plugins dir, the CLI binary on PATH, the `<kind>_enabled` flag, and a
credential. Nothing composed them, so every combination of three-out-of-four
failed silently in its own way — see the module docstring of
`tests/test_plugin_enable.py` for the exact incidents this closes.

`enable(kind)` does all of it, in order, refusing BEFORE touching anything
if the kind is unknown, its credential is missing, or the operator hasn't
consented, then:

1. installs the CLI binary if it isn't already on PATH (only via
   `hivepilot.services.agent_install`'s pinned, maintainer-vetted commands —
   never anything dynamic);
2. places the plugin `.py` (from this repo's `plugins/<kind>.py`) into the
   live, auto-scanned plugins directory;
3. writes the `<kind>_enabled` flag;
4. VERIFIES the kind actually resolves afterwards, through something that
   loads plugins (not a bare `import hivepilot.registry`, which only ever
   sees builtins) — writing the flag is not evidence that the thing works.

`disable(kind)` only flips the flag off. Removing software is a different,
deliberately separate act.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

# kind -> the exact env var name(s) its runner class documents as its ONLY
# auth mechanism (no interactive CLI login involved) — derived from each
# runner's own docstring in hivepilot/runners/prompt_cli_runner.py, not
# guessed. A binary installed without one of these registers the kind and
# then fails at RUN time, which is exactly the gap `enable()` exists to
# catch before it finishes.
#
# codex / cursor / opencode / ollama / antigravity: PATH-gated CLIs that
# authenticate through their own interactive/browser login flow
# (`codex login`, `cursor-agent login`, ...) — HivePilot reads no
# credential env var for any of them (grepped: no os.environ/os.getenv for
# a credential in CodexRunner, CursorRunner, OpenCodeRunner, OllamaRunner,
# AntigravityRunner). Genuinely no credential needed.
# gemini / qwen-code: their runner classes only read GOOGLE_API_KEY /
# OPENAI_API_KEY in `mode: api` (PromptCliRunner._run_api); the default
# `mode: cli` this enable path targets uses the CLI's own login instead, so
# no credential is required to enable the kind.
# pi / kimi-cli / vibe: these runners' docstrings describe env-var
# passthrough (`merge_environments`) as the ONLY documented auth path —
# there is no separate interactive login flow mentioned. `pi` accepts
# either ANTHROPIC_API_KEY or OPENAI_API_KEY depending on its own config.
_CREDENTIAL_ENV_VARS: dict[str, tuple[str, ...]] = {
    "codex": (),
    "cursor": (),
    "opencode": (),
    "ollama": (),
    "antigravity": (),
    "gemini": (),
    "qwen-code": (),
    "pi": ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"),
    "kimi-cli": ("KIMI_API_KEY",),
    "vibe": ("MISTRAL_API_KEY",),
}


@dataclass(frozen=True)
class EnableResult:
    """Outcome of `enable()` / `disable()`. `ok=True` means the kind was
    verified to actually resolve (for `enable`) — not merely that every step
    ran without raising."""

    ok: bool
    detail: str


def _is_agent_plugin_kind(kind: str) -> bool:
    """True iff `kind` is a known, PATH-gated optional agent plugin — the
    same source of truth `hivepilot.registry.resolve_runner_class` uses to
    tell "unknown kind" apart from "known but inactive"."""
    from hivepilot.registry import _OPTIONAL_AGENT_PLUGIN_KINDS

    return kind in _OPTIONAL_AGENT_PLUGIN_KINDS


def _binary_on_path(kind: str) -> bool:
    """Whether `kind`'s required CLI binary (per
    `registry._OPTIONAL_AGENT_PLUGIN_KINDS`) resolves on PATH."""
    from hivepilot.registry import _OPTIONAL_AGENT_PLUGIN_KINDS
    from hivepilot.services import agent_install

    _flag_name, binary = _OPTIONAL_AGENT_PLUGIN_KINDS[kind]
    return agent_install.is_on_path(binary)


def _install_binary(kind: str) -> bool:
    """Install `kind`'s CLI binary via the pinned, maintainer-vetted
    `InstallSpec.command` in `agent_install.AGENT_INSTALL_SPECS` — the ONLY
    command ever executed. A `command=None` spec is docs-only and this
    returns False without running anything (the caller reports the docs
    URL). Consent for the overall `enable()` call has already been obtained
    by the time this runs (see `enable()`'s confirmation gate), so this
    passes `assume_yes=True` to skip `propose_install`'s own y/N prompt —
    but `propose_install`'s stricter, non-overridable TTY check still
    applies: it refuses to run in a non-interactive/scheduled context
    regardless, so a headless caller cannot turn `enable(assume_yes=True)`
    into a remote-code-execution pipeline.
    """
    from hivepilot.services import agent_install

    spec = agent_install.get_install_spec(kind)
    if spec is None:
        return False
    result = agent_install.propose_install(spec, assume_yes=True)
    return result.ran and result.exit_code == 0


def _plugin_filename(kind: str) -> str:
    """Runner kind -> its `plugins/<file>.py` filename. Module filenames use
    underscores where the kind uses a hyphen (`qwen-code` -> `qwen_code.py`,
    `kimi-cli` -> `kimi_cli.py`) — confirmed against each plugin's own
    `_KIND` constant and `plugin_installer.AGENT_CLI_PLUGINS`."""
    return f"{kind.replace('-', '_')}.py"


def _place_plugin_file(kind: str) -> Path | None:
    """Copy this repo's `plugins/<kind>.py` into the live, auto-scanned
    plugins directory (`plugin_installer.installed_plugins_dir()` ==
    `settings.xdg_data_home / "plugins"`) — the gap that dropped `vibe`:
    the source `.py` does not ship in the wheel, so a flag with no file
    behind it silently no-ops. Returns the destination path, or None if the
    source file does not exist in this checkout.
    """
    from hivepilot.config import settings
    from hivepilot.services import plugin_installer

    filename = _plugin_filename(kind)
    source = settings.base_dir / "plugins" / filename
    if not source.exists():
        return None

    dest_dir = plugin_installer.installed_plugins_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / filename
    shutil.copy2(source, dest)
    return dest


def _write_target_caveat(flag_path: "Path") -> str:
    """Name where the flag landed, and warn when nothing will read it there.

    `_write_enable_flag` writes to the `.env` `Settings` reads — correct
    in-process, and NOT what a systemd deployment consults: those units load
    `EnvironmentFile=` files, so a flag written here is invisible to the
    running services until it is mirrored there. Reporting a bare success
    would be the same false success this command exists to remove.

    The path is also `base_dir`-anchored, and `base_dir` falls back to the
    process CWD when `HIVEPILOT_BASE_DIR` is unpinned — so the same command run
    from two directories writes two different files.
    """
    import os

    caveat = f" Flag written to {flag_path}."
    if not (os.environ.get("HIVEPILOT_BASE_DIR", "").strip()):
        caveat += (
            " HIVEPILOT_BASE_DIR is unpinned, so that path follows the current working directory."
        )
    caveat += (
        " If this deployment runs under systemd, mirror the flag into the"
        " EnvironmentFile the units load — they do not read this file."
    )
    return caveat


def _write_enable_flag(kind: str, value: bool) -> Path:
    """Upsert `HIVEPILOT_<KIND>_ENABLED=<true|false>` into the same `.env`
    file `Settings` reads its overrides from — same upsert shape as
    `hivepilot.ui.plugin_persist.persist_plugins_disabled` /
    `hivepilot.services.plugin_installer.persist_enabled`, generalized to
    also write `false` (those two only ever write `true`/a JSON list).
    Anchors a relative path to `settings.base_dir` via `settings.resolve_path`
    (never the CWD) — a relative path anchored to CWD has written a file no
    service reads before.
    """
    from hivepilot.config import Settings, settings
    from hivepilot.registry import _OPTIONAL_AGENT_PLUGIN_KINDS

    flag_name, _binary = _OPTIONAL_AGENT_PLUGIN_KINDS[kind]
    env_key = f"HIVEPILOT_{flag_name.upper()}"

    env_path = Path(str(Settings.model_config.get("env_file") or ".env"))
    if not env_path.is_absolute():
        env_path = settings.resolve_path(env_path)

    line = f"{env_key}={'true' if value else 'false'}"
    lines: list[str] = []
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()

    for i, existing in enumerate(lines):
        if existing.startswith(f"{env_key}="):
            lines[i] = line
            break
    else:
        lines.append(line)

    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return env_path


def _kind_resolves(kind: str) -> bool:
    """Verify `kind` actually resolves to a runner — through something that
    LOADS plugins, not a bare `import hivepilot.registry` (which only ever
    sees builtins and reports a false "unavailable" for anything
    plugin-contributed). Constructing `PluginManager()` performs the same
    scan+register pass `Orchestrator()` does, without also reloading
    projects/tasks/pipelines YAML — that side effect is what populates
    `hivepilot.registry.RUNNER_MAP` with plugin-contributed kinds; only
    after that does `resolve_runner_class` mean anything.

    Setting the flag is not evidence: this codebase has repeatedly mistaken
    "I wrote the config" for "the thing works".
    """
    from hivepilot import registry
    from hivepilot.plugins import PluginManager

    PluginManager()
    try:
        registry.resolve_runner_class(kind)
    except (KeyError, registry.RunnerPluginUnavailableError):
        return False
    return True


def _credential_present(kind: str) -> bool:
    """True if `kind` needs no credential (per `_CREDENTIAL_ENV_VARS`), or
    if at least one of its documented env vars is set. Checking env vars
    HivePilot never itself reads at run time (they're merely forwarded to
    the CLI) is deliberate: the point is to catch, BEFORE enabling
    finishes, exactly the failure mode this module exists to close — "a
    binary installed WITHOUT its credential registers the kind and then
    fails at RUN time"."""
    required = _CREDENTIAL_ENV_VARS.get(kind, ())
    if not required:
        return True
    return any(os.environ.get(var) for var in required)


def _default_confirm(prompt: str) -> bool:
    answer = input(f"{prompt} [y/N] ").strip().lower()
    return answer in ("y", "yes")


def enable(
    kind: str,
    *,
    assume_yes: bool = False,
    confirm: Callable[[str], bool] | None = None,
) -> EnableResult:
    """Enable agent plugin `kind`: install its binary if missing, place its
    plugin file, write its enable flag, then verify it actually resolves.
    Refuses BEFORE touching anything if `kind` is unknown, its credential is
    missing, or the operator hasn't consented (`assume_yes=True` or
    `confirm(prompt)` returning True)."""
    if not _is_agent_plugin_kind(kind):
        return EnableResult(False, f"'{kind}' is not a known agent plugin kind.")

    if not _credential_present(kind):
        required = _CREDENTIAL_ENV_VARS.get(kind, ())
        hint = f" (set {' or '.join(required)})" if required else ""
        return EnableResult(
            False,
            f"refusing to enable '{kind}': missing credential{hint} — a binary "
            "installed without it would register the kind and then fail at run "
            "time.",
        )

    if not assume_yes:
        asker = confirm if confirm is not None else _default_confirm
        prompt = f"Enable agent plugin '{kind}'? This installs its CLI binary if missing."
        if not asker(prompt):
            return EnableResult(False, f"enabling '{kind}' declined by operator.")

    if not _binary_on_path(kind):
        if not _install_binary(kind):
            from hivepilot.services import agent_install

            spec = agent_install.get_install_spec(kind)
            docs = spec.docs_url if spec is not None else "the vendor's docs"
            return EnableResult(
                False,
                f"could not install the '{kind}' binary automatically — install "
                f"it manually, see {docs}",
            )

    placed = _place_plugin_file(kind)
    if placed is None:
        return EnableResult(
            False,
            f"'{kind}' has no plugins/{_plugin_filename(kind)} in this checkout "
            "to place — cannot enable.",
        )

    flag_path = _write_enable_flag(kind, True)

    if not _kind_resolves(kind):
        return EnableResult(
            False,
            f"'{kind}' was installed and its flag was written, but it still "
            "does not resolve — run `hivepilot plugins health` to see why.",
        )

    caveat = _write_target_caveat(flag_path)

    # Say what is still owed. A kind whose vendor CLI carries its own login has
    # no env var for us to check, so `_credential_present` passed on an empty
    # tuple -- which is honest about what we verified and silent about what we
    # did not. Enabling it and reporting a bare success would recreate the very
    # gap this command exists to close: a registered kind that fails at RUN
    # time, after a pipeline has already paid to get there.
    if not _CREDENTIAL_ENV_VARS.get(kind):
        return EnableResult(
            True,
            f"'{kind}' is enabled and resolves. Its CLI carries its own login, which "
            f"HivePilot cannot verify from here -- run that vendor's own login (and "
            f"`hivepilot agents versions`) before routing a role to it." + caveat,
        )
    return EnableResult(True, f"'{kind}' is enabled and resolves." + caveat)


def disable(kind: str) -> EnableResult:
    """Turn off `kind`'s enable flag. Does NOT uninstall its CLI binary or
    remove its plugin file — removing software is a different act."""
    _write_enable_flag(kind, False)
    return EnableResult(
        True,
        f"'{kind}' disabled — its flag is off. The CLI binary (if installed) "
        "is left in place; disabling does not uninstall it.",
    )
