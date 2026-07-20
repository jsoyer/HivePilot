"""hivepilot.services.plugin_installer — one-command install for HivePilot's
curated built-in example plugins (`plugins/*.py` shipped in this repo).

`hivepilot plugins install <name>...` turns the previously clunky "manually
`wget` a `plugins/<name>.py` into your config repo, commit, `config sync`, set
the flag" flow into a single command: fetch the curated file into a managed
local directory (`settings.xdg_data_home / "plugins"`, auto-scanned by
`hivepilot.plugins._installed_plugins_dir`) and optionally persist its
`<STEM>_ENABLED=true` flag.

**Security posture (see docs/PLUGINS.md "Trust model" + "Installing built-in
example plugins"):**

- Only names present in `KNOWN_EXAMPLE_PLUGINS` (a maintainer-curated,
  in-repo constant) can ever be fetched — `fetch_plugin` rejects anything
  else BEFORE any network call, so there is no arbitrary-URL / arbitrary-path
  fetch surface (no SSRF, no "install code from anywhere").
- The fetch target is always `{repo}/{ref}/plugins/{name}.py` against the
  configured `settings.plugins_source_repo` (default: this project's own
  GitHub raw-content host) — plain HTTPS GET, nothing more.
- The fetched body is written to disk VERBATIM and is NEVER imported,
  `exec()`'d, or otherwise executed by this module. It only becomes live
  Python the next time HivePilot starts and its normal local-file plugin
  loader (`hivepilot.plugins._scan_local_plugins`) scans the managed
  directory — subject to the SAME `plugins_enabled` / `plugins_disabled` /
  `<stem>_enabled` / capability-policy gates as any other local plugin.
  `plugins install` therefore grants no new privilege beyond "an operator
  put a plugin file where the existing gated loader already looks" — it is
  a convenience over the manual copy-a-file workflow, not a new trust
  boundary.
- Confirm-then-run at the CLI layer (`hivepilot/cli.py`'s `plugins install`
  command) — this module itself performs no prompting; it is a plain,
  independently-testable fetch/persist helper.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import requests

from hivepilot.config import Settings, settings
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

PrereqKind = Literal["binary", "pip", "config", "none"]

# Bound the number of bytes ever buffered from a single plugin fetch — a
# compromised/MITM'd source host serving a huge body is a memory DoS
# otherwise. Every shipped example plugin is well under 100 KiB; 2 MiB is a
# generous ceiling. Mirrors the same defensive posture as
# `hivepilot.services.plugin_index.MAX_INDEX_BYTES`.
MAX_PLUGIN_BYTES = 2 * 1024 * 1024
_FETCH_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class ExamplePluginSpec:
    """One curated built-in example plugin's install metadata.

    `prereq_kind`/`prereq_detail` are derived from the ACTUAL plugin source
    under `plugins/<name>.py` (its `shutil.which(...)` PATH probe, or its
    lazy optional `import`, or -- for a plugin gated on a config value
    rather than a binary/library -- the env var it needs) — never guessed.
    """

    name: str
    description: str
    env_flag: str
    prereq_kind: PrereqKind
    prereq_detail: str


# ---------------------------------------------------------------------------
# Curated registry. Every entry below corresponds to a real `plugins/<name>.py`
# shipped in THIS repo (see `test_every_spec_has_a_real_plugin_source_file` /
# `test_every_spec_has_a_real_settings_enabled_flag` in
# tests/test_plugin_installer.py) — descriptions are drawn from each plugin's
# own module docstring; prereqs from its own `shutil.which(...)` / lazy
# `import` / documented config vars. Agent-runner CLI plugins (codex, cursor,
# gemini, ollama, opencode, kimi_cli, qwen_code, antigravity, pi) are
# deliberately NOT included here — they are already covered by the guided
# `hivepilot agents install` flow (hivepilot/services/agent_install.py), and
# demo-only plugins (sample, sample_skill, example_graph_source,
# drift_panel, drift_graph_source, autopilot_panel,
# secrets_trust_graph_source) are internal showcases, not something an
# operator would reach for via a one-command install.
# ---------------------------------------------------------------------------

KNOWN_EXAMPLE_PLUGINS: dict[str, ExamplePluginSpec] = {
    "rtk": ExamplePluginSpec(
        name="rtk",
        description=(
            "Wraps a shell-generic step's command with `rtk proxy` to cut token "
            "usage on command output."
        ),
        env_flag="HIVEPILOT_RTK_ENABLED",
        prereq_kind="binary",
        prereq_detail="the `rtk` binary on PATH",
    ),
    "herdr": ExamplePluginSpec(
        name="herdr",
        description=(
            "Executes each pipeline step inside a dedicated herdr pane "
            "(github.com/ogulcancelik/herdr) -- a terminal multiplexer built for "
            "coding agents."
        ),
        env_flag="HIVEPILOT_HERDR_ENABLED",
        prereq_kind="binary",
        prereq_detail="the `herdr` binary on PATH",
    ),
    "hugo": ExamplePluginSpec(
        name="hugo",
        description="Wraps the Hugo static-site-generator CLI (new/build/serve) as a runner.",
        env_flag="HIVEPILOT_HUGO_ENABLED",
        prereq_kind="binary",
        prereq_detail="the `hugo` binary on PATH",
    ),
    "gh": ExamplePluginSpec(
        name="gh",
        description="A plain, PATH-gated command runner that executes the GitHub CLI (`gh <args>`).",
        env_flag="HIVEPILOT_GH_ENABLED",
        prereq_kind="binary",
        prereq_detail="the `gh` (GitHub CLI) binary on PATH",
    ),
    "tmux": ExamplePluginSpec(
        name="tmux",
        description=(
            "Executes each pipeline step inside a dedicated, deterministically-named "
            "tmux session for live attach/observe + full scrollback capture."
        ),
        env_flag="HIVEPILOT_TMUX_ENABLED",
        prereq_kind="binary",
        prereq_detail="the `tmux` binary on PATH",
    ),
    "bitwarden": ExamplePluginSpec(
        name="bitwarden",
        description="A `secrets` provider backed by the operator's own Bitwarden vault.",
        env_flag="HIVEPILOT_BITWARDEN_ENABLED",
        prereq_kind="binary",
        prereq_detail="the official Bitwarden `bw` CLI on PATH, logged in/unlocked",
    ),
    "vaultwarden": ExamplePluginSpec(
        name="vaultwarden",
        description=(
            "A `secrets` provider backed by a self-hosted Vaultwarden server "
            "(the self-hosted sibling of `bitwarden`)."
        ),
        env_flag="HIVEPILOT_VAULTWARDEN_ENABLED",
        prereq_kind="binary",
        prereq_detail="the official Bitwarden `bw` CLI on PATH, configured against your Vaultwarden server",
    ),
    "infisical": ExamplePluginSpec(
        name="infisical",
        description="A `secrets` provider backed by Infisical (open-source, self-hostable config/secrets store).",
        env_flag="HIVEPILOT_INFISICAL_ENABLED",
        prereq_kind="pip",
        prereq_detail="`pip install infisicalsdk` (plus HIVEPILOT_INFISICAL_TOKEN/_WORKSPACE_ID/_ENVIRONMENT)",
    ),
    "onepassword": ExamplePluginSpec(
        name="onepassword",
        description="A `secrets` provider backed by 1Password (Connect server or service-account token).",
        env_flag="HIVEPILOT_ONEPASSWORD_ENABLED",
        prereq_kind="pip",
        prereq_detail=(
            "`pip install onepassword-sdk` for service-account mode "
            "(not required for Connect mode -- set HIVEPILOT_OP_CONNECT_HOST/_CONNECT_TOKEN instead)"
        ),
    ),
    "kms": ExamplePluginSpec(
        name="kms",
        description="A `secrets` provider backed by a cloud KMS (AWS/GCP/Azure) for ${secret:NAME} decryption.",
        env_flag="HIVEPILOT_KMS_ENABLED",
        prereq_kind="pip",
        prereq_detail=(
            "one of: `pip install boto3` (AWS, or `hivepilot[cloud]`), "
            "`pip install google-cloud-kms` (GCP), or "
            "`pip install azure-keyvault-keys azure-identity` (Azure) -- "
            "matching HIVEPILOT_KMS_PROVIDER"
        ),
    ),
    "obsidian": ExamplePluginSpec(
        name="obsidian",
        description="Logs pipeline runs into an Obsidian vault, and recalls relevant vault context into prompts.",
        env_flag="HIVEPILOT_OBSIDIAN_ENABLED",
        prereq_kind="config",
        prereq_detail="set HIVEPILOT_OBSIDIAN_VAULT to your Obsidian vault's directory (no external binary/lib)",
    ),
    "mem0": ExamplePluginSpec(
        name="mem0",
        description="Persistent cross-run agent memory via a recall/store hook pair backed by mem0.",
        env_flag="HIVEPILOT_MEM0_ENABLED",
        prereq_kind="pip",
        prereq_detail="`pip install mem0ai`",
    ),
    "headroom": ExamplePluginSpec(
        name="headroom",
        description="Compresses each step's prompt/context before execution to reduce token usage.",
        env_flag="HIVEPILOT_HEADROOM_ENABLED",
        prereq_kind="pip",
        prereq_detail='`pip install "headroom-ai[all]"`',
    ),
}


def installed_plugins_dir() -> Path:
    """The managed directory `plugins install` fetches into -- always
    `settings.xdg_data_home / "plugins"`, regardless of whether it exists yet
    (callers that need existence-gating use
    `hivepilot.plugins._installed_plugins_dir` instead)."""
    return settings.xdg_data_home / "plugins"


def is_installed(name: str) -> bool:
    """True if `<name>.py` is already present in the managed installed-plugins
    dir. Does not validate `name` against the curated registry — a stale file
    from a since-removed registry entry still counts as "installed" for
    display purposes."""
    return (installed_plugins_dir() / f"{name}.py").exists()


def is_enabled(name: str) -> bool:
    """Current value of `settings.<name>_enabled` for a curated plugin name.
    Assumes `name` is a valid Settings field (true for every
    `KNOWN_EXAMPLE_PLUGINS` key — see the registry sanity tests)."""
    return bool(getattr(settings, f"{name}_enabled", False))


def fetch_plugin(
    name: str,
    *,
    repo: str | None = None,
    ref: str | None = None,
    dest_dir: Path | None = None,
    timeout: int = _FETCH_TIMEOUT_SECONDS,
) -> Path:
    """Fetch `plugins/<name>.py` from the configured (or overridden) source
    repo/ref and write it VERBATIM into `dest_dir` (default:
    `installed_plugins_dir()`), overwriting any existing file of the same
    name (idempotent re-install).

    `name` MUST be a key of `KNOWN_EXAMPLE_PLUGINS` — anything else raises
    `ValueError` immediately, BEFORE any network call, listing every
    available name. This is the entire trust boundary: no caller can ever
    make this function fetch an arbitrary URL or write an arbitrary path.

    Raises `RuntimeError` (never the raw exception, to avoid leaking
    internal detail) on network failure, timeout, non-2xx HTTP status, or an
    oversized response body (see `MAX_PLUGIN_BYTES`). The fetched content is
    written to disk only — never imported, compiled, or `exec()`'d here.
    """
    if name not in KNOWN_EXAMPLE_PLUGINS:
        available = ", ".join(sorted(KNOWN_EXAMPLE_PLUGINS))
        raise ValueError(f"plugins install: unknown plugin {name!r}. Available: {available}")

    resolved_repo = (repo or settings.plugins_source_repo).rstrip("/")
    resolved_ref = ref or settings.plugins_source_ref
    resolved_dest = dest_dir if dest_dir is not None else installed_plugins_dir()

    url = f"{resolved_repo}/{resolved_ref}/plugins/{name}.py"

    try:
        response = requests.get(url, timeout=timeout)
    except requests.Timeout as exc:
        raise RuntimeError(f"plugins install: fetch of {name!r} timed out") from exc
    except requests.RequestException as exc:
        raise RuntimeError(
            f"plugins install: failed to reach source repo for {name!r} ({type(exc).__name__})"
        ) from exc

    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        raise RuntimeError(
            f"plugins install: source repo returned HTTP {response.status_code} for {name!r}"
        ) from exc

    body = response.text
    if len(body.encode("utf-8", errors="ignore")) > MAX_PLUGIN_BYTES:
        raise RuntimeError(f"plugins install: {name!r} response exceeds {MAX_PLUGIN_BYTES} bytes")

    resolved_dest.mkdir(parents=True, exist_ok=True)
    dest_path = resolved_dest / f"{name}.py"
    dest_path.write_text(body, encoding="utf-8")
    logger.info("plugin_installer.fetched", name=name, url=url, dest=str(dest_path))
    return dest_path


def _default_env_path() -> Path:
    """The same dotenv file `Settings` itself reads overrides from — mirrors
    `hivepilot.ui.plugin_persist.persist_plugins_disabled`'s default
    resolution exactly (see that module's docstring for why: there is no
    dedicated writer for scalar `Settings` fields, so this upserts the same
    file/format `Settings` already reads rather than inventing a new one)."""
    return Path(str(Settings.model_config.get("env_file") or ".env"))


def persist_enabled(name: str, *, env_path: Path | None = None) -> Path:
    """Upsert `<ENV_FLAG>=true` into the `.env` file `Settings` reads its
    overrides from (default: `_default_env_path()`), preserving every other
    line verbatim — same upsert shape as
    `hivepilot.ui.plugin_persist.persist_plugins_disabled`, but for a single
    scalar `<STEM>_ENABLED` flag instead of the `HIVEPILOT_PLUGINS_DISABLED`
    JSON list that helper owns; the two never touch the same line.

    Effective on next start only — Settings/PluginManager are constructed
    once per process, same caveat as `persist_plugins_disabled`.
    """
    if name not in KNOWN_EXAMPLE_PLUGINS:
        available = ", ".join(sorted(KNOWN_EXAMPLE_PLUGINS))
        raise ValueError(f"plugins install: unknown plugin {name!r}. Available: {available}")

    spec = KNOWN_EXAMPLE_PLUGINS[name]
    resolved_env_path = env_path if env_path is not None else _default_env_path()

    line = f"{spec.env_flag}=true"
    lines: list[str] = []
    if resolved_env_path.exists():
        lines = resolved_env_path.read_text(encoding="utf-8").splitlines()

    for i, existing in enumerate(lines):
        if existing.startswith(f"{spec.env_flag}="):
            lines[i] = line
            break
    else:
        lines.append(line)

    resolved_env_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return resolved_env_path
