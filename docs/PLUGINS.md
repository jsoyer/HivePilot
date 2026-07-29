# Plugins

A plugin is a Python module that exposes a single function, `register() -> dict[str, Any]`. Each key in the returned dict is a contribution type (`runners`, `notifiers`, `secrets`, `health`, `panels`, `skills`) or, if the key isn't one of those, the name of a lifecycle hook (`before_step`, `after_step`, `on_pipeline_end`, `on_error`, …).

Registration is applied **atomically per plugin**: everything a single plugin stages — runners, notifiers, hooks, whatever — is committed together, or none of it is. If any part of a plugin's registration collides with an existing name, the entire plugin's staged contributions are rolled back. One misbehaving plugin cannot partially register.

## Contribution types

| Type | Key | Contract | Collision |
|---|---|---|---|
| Runners | `runners` | `{kind: RunnerClass(BaseRunner)}` → merged into `RUNNER_MAP` | Hard error |
| Notifiers | `notifiers` | `{name: Callable[[str], None]}` → merged into `NOTIFIER_MAP` | Hard error |
| Secrets | `secrets` | `{name: SecretsBackend}`, a Protocol with `resolve(ref, settings) -> str` → merged into `SECRETS_MAP` | Hard error |
| Health | `health` | `{name: Callable -> HealthStatus}`; result is normalized and the check never raises past the framework | Hard error on name collision |
| Panels | `panels` | `list[PanelSpec]`, each `{name, title, fetch, min_role?}`; contributes tabs to the Pollen dashboard | Invalid `min_role` (not in `ROLE_RANKS`) is a fail-closed registration error |
| Skills | `skills` | `list[SkillSpec]`, each `{name, description, provider, files, system_prompt?, applies_to?, min_role?}` | See [SKILLS.md](./SKILLS.md) |
| Graph sources | `graph_sources` | `list[GraphSourceSpec]`, each `{name, data, node_detail?, title?, min_role?, params?}` → registered into `hivepilot.graph`'s module-global source registry via `register_graph_source()` | Name collision with a built-in or another plugin → `GraphSourceNameCollisionError`, rolled back atomically |
| Capabilities | `capabilities` | `list[str]` from the closed `hivepilot.plugin_capabilities.PLUGIN_CAPABILITIES` vocabulary — an advisory manifest of what the plugin intends to do | Unknown token → `PluginCapabilityInvalidError`; token not in `settings.plugins_capability_policy` → `PluginCapabilityDeniedError`, rolled back atomically. See "Capability manifest & policy gate" below. |
| Lifecycle hooks | any other key | a callable (`before_step`, `after_step`, `on_pipeline_end`, `on_error`, …) | No collision check — every plugin's hook for a given name runs |

Secrets contributions are covered in more depth in [SECURITY.md](./SECURITY.md); panels and the graph view in [DASHBOARD.md](./DASHBOARD.md); skills in [SKILLS.md](./SKILLS.md).

## How plugins load

Plugins load from three sources. All three are gated by the master switch `plugins_enabled` (default `True`) and a per-plugin `plugins_disabled` skip-list that is checked **before the module executes** — a disabled plugin's code never runs.

- **local-file** (`source="local-file"`): every `*.py` file under `<base_dir>/plugins/` except files prefixed with `_`. Files are compiled directly from source, bypassing the `.pyc` cache, so hot-reload picks up edits immediately.
- **entry-point** (`source="entry-point"`): any installed package that declares an entry point in the `hivepilot.plugins` group, discovered via `importlib.metadata.entry_points(group="hivepilot.plugins")`.
- **explicit-entry** (`source="explicit-entry"`): a single pinned `module:attr` target set via the `HIVEPILOT_PLUGINS_ENTRY` environment variable.

### Multi-directory local-file search (`plugins_extra_dirs`)

`<base_dir>/plugins/` is always scanned first. Additional directories can be
scanned afterward via `settings.plugins_extra_dirs` (env `HIVEPILOT_PLUGINS_EXTRA_DIRS`,
an `os.pathsep`-separated list of directory paths — `:` on POSIX, `;` on
Windows). This is the fix for a specific deployment gap: a config repo that
sets `HIVEPILOT_BASE_DIR` to its own directory (to load its own
`plugins/vendored_skills.py`, say) can no longer see the engine's shipped
`plugins/*.py` — they live under the engine repo, which is no longer
`base_dir`. `plugins_extra_dirs` lets that same deployment point back at the
engine's `plugins/` directory too, so it gets BOTH sets of plugins instead of
having to choose one.

```bash
# .env
HIVEPILOT_PLUGINS_EXTRA_DIRS=/opt/hivepilot/plugins:/srv/config-repo/plugins
```

Rules:

- Directories are scanned in order: `base_dir/plugins` first, then each
  `plugins_extra_dirs` entry in the order listed.
- **Dedup by module stem, first-wins**: if the same `<name>.py` stem appears
  in more than one scanned directory, only the FIRST occurrence loads — later
  ones are skipped (logged at info level), never a collision error. Since
  `base_dir/plugins` is always scanned first, it always wins over any extra
  directory, so a deployment's own `base_dir/plugins` can deliberately
  override a same-named shipped plugin.
- A `plugins_extra_dirs` entry that doesn't exist on disk is silently
  skipped — same as a missing `base_dir/plugins` always has been.
- `plugins_enabled` and `plugins_disabled` apply identically across every
  scanned directory — a disabled plugin is skipped regardless of which
  directory it was found in.
- Additive/opt-in: empty (the default) is byte-identical to the
  single-directory scan that existed before this setting.

Entry-point declaration in `pyproject.toml`:

```toml
[project.entry-points."hivepilot.plugins"]
myplugin = "my_package.plugin:register"
```

Minimal local plugin, `plugins/myplugin.py`:

```python
def register() -> dict:
    def my_notifier(message: str) -> None:
        print(f"[myplugin] {message}")

    return {
        "notifiers": {"myplugin": my_notifier},
    }
```

## Trust model (fail-closed)

Plugin code reaches the process from exactly three trust sources:

1. Local files under `plugins/` — the same trust boundary as editing `tasks.yaml`. Anyone who can write to the repo can write a plugin.
2. Installed packages — the `pip install` trust boundary. If you trust what you installed, you trust its registered plugin.
3. `hivepilot plugins install <name>...` — the ONE narrow, curated exception to "no network fetch of plugin code": it fetches a maintainer-vetted `plugins/<name>.py` from a fixed, in-repo allow-list (`hivepilot.services.plugin_installer.KNOWN_EXAMPLE_PLUGINS`) over HTTPS and writes it into the managed plugins dir. No arbitrary URL or path is ever reachable, and the fetched file is written to disk only — it is picked up by the same local-file loader (source 1 above) on the next process start, subject to every gate below exactly like a hand-copied plugin file. See "Installing built-in example plugins" for the full walkthrough and its own trust discussion.

A plugin runs with full process privileges. There is no sandbox, no permission model, and no capability restriction on what a plugin's code can do once loaded.

Gating happens at three independent layers:

1. `plugins_enabled` — master on/off switch for the whole plugin system.
2. `plugins_disabled` — a per-name skip list checked before a plugin's module is even imported.
3. Per-plugin `<name>_enabled` flags — read inside each plugin's own `register()`, so a plugin can no-op itself out even when its file is present and not globally disabled.

If a plugin's `register()` call or its module import raises, the failure is logged and that plugin is **skipped** — it never kills the host process. The one exception is a name or kind **collision**, which is a hard, propagating failure for that plugin's registration (see Contribution types above).

Hot-reload (`PluginManager.reload()`) is staging-then-commit: a full re-scan of all plugin sources builds a candidate state without touching the live global maps, and that candidate is only committed if the whole re-scan succeeds. Reload is only effective when explicitly invoked (a scheduler tick or `SIGHUP`) — flipping a flag or dropping a new binary on `PATH` otherwise takes effect at the next process start, not live.

### Capability manifest & policy gate (Phase 26b)

**This is advisory admission control, NOT runtime sandboxing.** A plugin declaring `capabilities = []` (or nothing at all) still runs with full process privileges once loaded — nothing in this repo can interpreter-level-enforce what a plugin's code actually does. True process isolation (subprocess sandboxing, seccomp, OS-level capability dropping) is future work, not shipped here.

What IS shipped: a plugin MAY declare `register()["capabilities"] = [...]` from a closed vocabulary (`hivepilot.plugin_capabilities.PLUGIN_CAPABILITIES`): `network`, `filesystem`, `subprocess`, `secrets_access`, `env`. This is checked at LOAD TIME against `settings.plugins_capability_policy` (env `HIVEPILOT_PLUGINS_CAPABILITY_POLICY`, comma-separated or JSON array, e.g. `network,env` or `["network","env"]`, same convention as `plugins_disabled`) — the set of capability tokens the operator is willing to ALLOW a plugin to declare. Empty (default) = fail-closed, deny every declared capability.

- **Default `[]` (fail-closed, deny-declared):** ANY plugin declaring ANY capability is denied at load until the operator explicitly opts that token in.
- **A plugin declaring NO capabilities at all is completely unaffected**, regardless of policy — this is purely additive; every plugin shipped before this manifest existed keeps working exactly as before.
- A denied or malformed (`PluginCapabilityDeniedError` / `PluginCapabilityInvalidError`) capability manifest fails that plugin's **whole registration**, atomically rolling back its OTHER contributions — the same fail-closed, atomic-per-plugin-rollback shape as an invalid panel/skill `min_role` (see Contribution types above).

`hivepilot plugins audit` is a companion **read-only static scanner**: it `ast`-parses every local plugin's source TEXT (never imports/execs it, never calls `register()`) to flag risky imports/calls — `subprocess`, network sockets, `os.system`, `eval`/`exec`, write-mode `open`, `ctypes`, and similar — and cross-references them against that plugin's own declared `capabilities` manifest (itself extracted statically, best-effort, from a literal `"capabilities": [...]` entry in its `register()` source) to surface **under-declaration**: code that appears to use a capability the plugin didn't declare. This is advisory, not exhaustive — a dynamically constructed capabilities list, or a risky call the scanner doesn't recognize, won't be caught. Pass `--strict` to exit non-zero (CI-friendly) when any under-declaration is found; the default is a 0-exit report.

## Shipped plugins (inventory)

33 modules ship under `plugins/*.py`. The tables below group the main ones by what they contribute.

**Agent runners** (PATH-gated: flag AND binary must both be present):

| Plugin | Contributes | Default |
|---|---|---|
| `gemini` | runner | ON, PATH-gated |
| `opencode` | runner | ON, PATH-gated |
| `ollama` | runner | ON, PATH-gated |
| `pi` | runner | ON, PATH-gated |
| `qwen_code` (kind `qwen-code`) | runner | ON, PATH-gated |
| `kimi_cli` (kind `kimi-cli`) | runner | ON, PATH-gated |
| `antigravity` | runner | ON, PATH-gated |
| `codex` | runner | ON, PATH-gated |
| `cursor` | runner | ON, PATH-gated |

**Infra runners** (runner + health):

| Plugin | Contributes | Default |
|---|---|---|
| `rtk` | runner + health | ON |
| `herdr` | runner + health | ON |
| `hugo` | runner + health | ON, PATH-gated |
| `tmux` | runner + health | ON |
| `gh` | runner + health | ON, PATH-gated |

**Secrets** (secrets backend + health) — see [SECURITY.md](./SECURITY.md):

| Plugin | Contributes | Default |
|---|---|---|
| `bitwarden` | secrets + health | ON |
| `vaultwarden` | secrets + health | ON |
| `infisical` | secrets + health | ON |
| `onepassword` | secrets + health | ON (Connect + direct service-account) |
| `kms` | secrets + health | ON (cloud-KMS envelope/direct; `hivepilot[kms]`) |

**Notifier + hooks:**

| Plugin | Contributes | Default |
|---|---|---|
| `obsidian` | notifier + `before_step`/`after_step`/`on_pipeline_end`/`on_error` hooks | ON |

**Detection only (health, no runner):**

| Plugin | Contributes | Default |
|---|---|---|
| `pinokio` | health `pinokio` | ON, presence-gated |

See [Pinokio detection](#pinokio-detection) below for why this one contributes no runner.

**Opt-in / default OFF:**

| Plugin | Contributes | Default |
|---|---|---|
| `headroom` | `before_step` context compression | OFF |
| `mem0` | `before_step`/`after_step` memory recall/store | OFF |
| `sample` | hooks + panel demo | OFF |
| `sample_skill` | skill demo | OFF |
| `example_graph_source` | graph source `run-lineage` (demo) | `example_graph_source_enabled`, OFF (opt-in) |

`gh`, `hugo`, and the seven agent-runner kinds are PATH-gated — they only activate when their flag is on **and** the corresponding binary is found on `PATH`. `pinokio` is *presence*-gated on a filesystem layout rather than on `PATH` (see below). Everything else in the table is flag-gated only.

### Pinokio detection

[Pinokio](https://pinokio.co) is a launcher/manager for local AI applications: it downloads app repositories and runs them behind local web UIs. `plugins/pinokio.py` answers **"is Pinokio installed on this host, and what has it installed?"** — nothing more.

**It contributes no runner, on purpose.** Unlike `ollama`, Pinokio ships no headless prompt-in/text-out CLI, and on Linux there is no `pinokio` binary on `PATH` to gate on at all: it installs as a `.deb` / `.rpm` / `.AppImage` Electron app, and its only CLI (`pterm`) is an optional on-demand install placed *inside* the Pinokio home (`PINOKIO_HOME/bin/npm/bin/pterm`). There is nothing to route a task to, so the plugin registers a `health` check and stops there.

**Home resolution** mirrors Pinokio's own precedence (`pinokiod`'s `kernel/index.js`: `this.store.get("home") || process.env.PINOKIO_HOME`), then adds the documented default:

1. Electron-store settings file `$XDG_CONFIG_HOME/Pinokio/config.json` (or `$HOME/.config/Pinokio/config.json`), key `home`
2. `$PINOKIO_HOME`
3. `$HOME/pinokio`

`$XDG_DATA_HOME` is deliberately **not** consulted: Pinokio rewrites `XDG_DATA_HOME`/`XDG_CONFIG_HOME` to `PINOKIO_HOME/cache/...` for the apps it launches, so inside any Pinokio-spawned process it points at a cache, not an install.

**Fail-closed.** A candidate only counts when it is a readable directory containing both files `pinokiod` writes unconditionally on every init: `ENVIRONMENT` and `key.json`. `api/` is *not* required — it is created lazily on the first app download, so a fresh home legitimately has none. A missing marker, a non-directory, an unreadable path, a malformed or `home`-less `config.json`, or a blank env value all report `degraded` naming exactly what was checked; the check never reports `ok` on partial evidence.

**Side-effect free.** `register()` runs on every process start, so detection is `stat`/`listdir` only — no network, no port probing, no process spawning — and nothing discovered is ever executed. Enumerating installed apps is a directory listing of `PINOKIO_HOME/api`.

`HIVEPILOT_PINOKIO_ENABLED` defaults `true` as a *permission* gate ("report Pinokio if you find it"), never as an assertion that the operator installed it. Set it `false` to suppress detection entirely.

HivePilot never installs Pinokio; there is no `hivepilot agents install pinokio` recipe.

## Plugin CLI

```bash
hivepilot plugins list
```

Prints loaded plugins plus breakdown tables: agent runners (built-in vs. plugin, active vs. inactive vs. API-only), other runner kinds, notifiers, secrets backends, and health checks.

```bash
hivepilot plugins health
```

Runs every registered health check and prints a table. Exits non-zero if any check errors — safe to wire into CI.

```bash
hivepilot plugins audit [--strict]
```

Read-only static scan of every local plugin's source (see "Capability manifest & policy gate" above). Prints a per-plugin table of risky findings, declared capabilities, and under-declared capabilities. Exits 0 by default (advisory report); `--strict` exits 1 if any plugin under-declares a capability it appears to use — safe to wire into CI.

```bash
hivepilot plugins tui
```

Textual-based interactive browser for inspecting plugins. Pressing `space` toggles the selected plugin in `plugins_disabled`, persisted to `.env`; the change takes effect on the next process start, not live. Requires `HIVEPILOT_ENABLE_TEXTUAL_UI`.

```bash
hivepilot plugins search <query>
```

Metadata-only search against a plugin index. No code is fetched — only name/description/install-command metadata.

```bash
hivepilot plugins info <name>
```

Prints index metadata for a plugin plus the exact `pip`/`git` command to install it. HivePilot never executes that command itself — you run it yourself. Install targets returned from the index are validated and control characters are stripped before display.

```bash
hivepilot plugins available
```

Read-only table of every CURATED built-in example plugin `plugins install` can fetch — name, description, `<NAME>_ENABLED` flag, prerequisite, and whether it's currently installed + enabled. Never fetches anything over the network. See "Installing built-in example plugins" below.

```bash
hivepilot plugins install <name>... [--enable/--no-enable] [--ref REF] [--yes]
```

Fetches one or more curated built-in example plugins into the managed plugins dir and, by default, persists each `<NAME>_ENABLED=true`. See "Installing built-in example plugins" below for the full walkthrough.

There is no CLI subcommand to enable or disable an ALREADY-INSTALLED plugin directly — toggle via `plugins tui` or by editing `HIVEPILOT_PLUGINS_DISABLED`/`HIVEPILOT_<NAME>_ENABLED` in `.env`. (`plugins install` is the exception for a plugin it just fetched — see `--enable`/`--no-enable` above.)

## Installing built-in example plugins

Before this command existed, trying one of the built-in example plugins (`rtk`, `herdr`, `mem0`, `headroom`, `hugo`, `obsidian`, `kms`, …) meant manually downloading its `plugins/<name>.py` into your config repo, committing, `hivepilot config sync`-ing, and setting its `<NAME>_ENABLED` flag by hand. `hivepilot plugins install` collapses that into one command.

```bash
# See what's available
hivepilot plugins available

# Install one or more, review the confirm-then-run prompt, then confirm
hivepilot plugins install rtk herdr mem0 headroom

# Skip the confirmation prompt (e.g. scripted setup)
hivepilot plugins install rtk --yes

# Fetch without persisting the enable flag
hivepilot plugins install kms --no-enable

# Fetch from a pinned ref instead of HIVEPILOT_PLUGINS_SOURCE_REF
hivepilot plugins install hugo --ref v0.3.0
```

**Where it fetches from and where it writes to.** Each name is resolved to `{HIVEPILOT_PLUGINS_SOURCE_REPO}/{HIVEPILOT_PLUGINS_SOURCE_REF}/plugins/<name>.py` (default: this project's own `raw.githubusercontent.com/jsoyer/HivePilot` host, ref `main`) and written into the managed directory `xdg_data_home/plugins` — `~/.local/share/hivepilot/plugins/` by default (`$XDG_DATA_HOME/hivepilot/plugins` if set). That directory is auto-added to the local-file plugin scan path (`hivepilot.plugins._installed_plugins_dir`, scanned right after the config repo's own `plugins/` dir, before `plugins_extra_dirs`) — no manual `HIVEPILOT_PLUGINS_EXTRA_DIRS` needed.

**Only curated names, never an arbitrary fetch.** `plugins install`/`plugins available` only ever know about the fixed allow-list in `hivepilot.services.plugin_installer.KNOWN_EXAMPLE_PLUGINS` — an unknown name is rejected before any network call, with the full list of valid names printed. There is no way to point this command at an arbitrary URL or path.

**Still gated exactly like any other plugin.** Fetching (and even enabling) a plugin this way does **not** bypass any of the three gating layers in "Trust model" above: the fetched file only becomes live Python the next time HivePilot starts, when it's picked up by the normal local-file loader — still subject to `plugins_enabled`, `plugins_disabled`, and its own `<name>_enabled` check inside `register()`. And most of these plugins additionally no-op at runtime until their prerequisite is actually present:

| Plugin | Prerequisite |
|---|---|
| `rtk`, `herdr`, `hugo`, `gh`, `tmux` | the matching binary on `PATH` |
| `bitwarden`, `vaultwarden` | the official Bitwarden `bw` CLI on `PATH` |
| `mem0` | `pip install mem0ai` |
| `headroom` | `pip install "headroom-ai[all]"` |
| `infisical` | `pip install infisicalsdk` (+ `HIVEPILOT_INFISICAL_TOKEN`/`_WORKSPACE_ID`/`_ENVIRONMENT`) |
| `onepassword` | `pip install onepassword-sdk` for service-account mode (Connect mode needs no extra package) |
| `kms` | `boto3` (AWS) / `google-cloud-kms` (GCP) / `azure-keyvault-keys`+`azure-identity` (Azure), matching `HIVEPILOT_KMS_PROVIDER` |
| `obsidian` | none — just set `HIVEPILOT_OBSIDIAN_VAULT` to your vault's directory |

`plugins install` prints each plugin's exact prerequisite after fetching; it never installs a binary or `pip install`s anything on your behalf — that stays an explicit operator decision, same as `hivepilot agents install`'s guided (never automatic) posture for agent CLIs. Restart HivePilot's services after installing for the plugin (and, if `--enable` persisted a flag, the flag) to take effect.

**Trust note.** This is the one narrow exception to "no network fetch of plugin code" in the Trust model above — see that section for the honest accounting of what it does and doesn't change about the security posture.

## Writing a plugin

```python
# plugins/slack_ping.py
import os


def register() -> dict:
    if not os.getenv("SLACK_PING_ENABLED"):
        return {}

    def slack_notifier(message: str) -> None:
        # send `message` to Slack
        ...

    def before_step(context: dict) -> None:
        # runs before every pipeline step
        ...

    return {
        "notifiers": {"slack_ping": slack_notifier},
        "before_step": before_step,
    }
```

Notes:

- Gate optional behavior with a `<name>_enabled`-style flag read inside `register()` — returning `{}` is a clean no-op.
- The `register()` contract is fixed: no arguments in, a `dict[str, Any]` out.
- A name collision on `runners`, `notifiers`, `secrets`, `health`, or an invalid `min_role` on a `panels`/`skills` entry aborts the load for that plugin — nothing it contributes gets registered, and other plugins are unaffected.

## Graph sources

A plugin can contribute a node/edge graph to Pollen's Graph tab (see
[DASHBOARD.md](./DASHBOARD.md#graph-view)) the same way it contributes a
panel — via `register()["graph_sources"] = [GraphSourceSpec, ...]`.
`GraphSourceSpec` is a frozen dataclass defined once in `hivepilot/graph.py`
(reused by plugins, never redefined) with fields `name`, `data`,
`node_detail?`, `title?`, `min_role?` (default `"read"`), `params?`.

A plugin's staged `graph_sources` are committed under the SAME
`_owned_*` ownership model `runners`/`notifiers`/`secrets` already use:
disabling and reloading the plugin removes the source it contributed, and
reloading a still-enabled plugin does not self-collide with its own
previous registration.

Collision and fail-closed behavior:

- A name collision with a built-in source or another plugin's source raises
  `GraphSourceNameCollisionError` — the plugin's ENTIRE registration is
  rolled back atomically (same all-or-nothing rule as every other
  contribution type).
- Unlike `panels`/`skills`, `min_role` is **not** validated at registration
  time. It is resolved fail-closed at fetch time by
  `_resolve_graph_min_role_rank` (`hivepilot/services/api_service.py`): an
  unrecognized role name is treated as the highest possible bar, so it is
  unsatisfiable by any caller — including `admin` — rather than
  accidentally failing open.
- A disabled plugin contributes nothing; its module is never executed.
- A `data()`/`node_detail()` call that raises is caught by
  `run_graph_fetch`/`run_graph_node_detail` and normalized into a single
  `kind="error"` node (or an error `GraphDetail`) — it never surfaces as a
  500, and only the exception TYPE name is ever included, never its
  message.

`plugins list`'s per-plugin "contributes" column enumerates each plugin's
graph sources too — `_CONTRIBUTION_RENDER_ORDER` includes `graph_sources`,
and `plugin_index.graph_source_contributions(plugin_manager)` returns
`{plugin_name: [source_name, ...]}` for the plugins that registered one.

Reference implementation: `plugins/example_graph_source.py` contributes
`run-lineage` (opt-in, default OFF via `example_graph_source_enabled`) — a
`?run=<id>` query renders one run's lineage (run → steps → verdicts) as a
DAG, read-only, tenant-scoped via `state_service` membership checks, and
never includes a secret value.

## See also

- [RUNNERS.md](./RUNNERS.md)
- [SKILLS.md](./SKILLS.md)
- [SECURITY.md](./SECURITY.md)
- [DASHBOARD.md](./DASHBOARD.md)
