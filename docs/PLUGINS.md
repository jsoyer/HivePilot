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
| Panels | `panels` | `list[PanelSpec]`, each `{name, title, fetch, min_role?}`; contributes tabs to the Mirador dashboard | Invalid `min_role` (not in `ROLE_RANKS`) is a fail-closed registration error |
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

There is **no network fetch of plugin code, ever.** Plugin code reaches the process from exactly two trust sources:

1. Local files under `plugins/` — the same trust boundary as editing `tasks.yaml`. Anyone who can write to the repo can write a plugin.
2. Installed packages — the `pip install` trust boundary. If you trust what you installed, you trust its registered plugin.

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

24 plugins ship under `plugins/*.py`.

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

**Opt-in / default OFF:**

| Plugin | Contributes | Default |
|---|---|---|
| `headroom` | `before_step` context compression | OFF |
| `mem0` | `before_step`/`after_step` memory recall/store | OFF |
| `sample` | hooks + panel demo | OFF |
| `sample_skill` | skill demo | OFF |
| `example_graph_source` | graph source `run-lineage` (demo) | `example_graph_source_enabled`, OFF (opt-in) |

`gh`, `hugo`, and the seven agent-runner kinds are PATH-gated — they only activate when their flag is on **and** the corresponding binary is found on `PATH`. Everything else in the table is flag-gated only.

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

There is no CLI subcommand to enable or disable a plugin directly — toggle via `plugins tui` or by editing `HIVEPILOT_PLUGINS_DISABLED` in `.env`.

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

A plugin can contribute a node/edge graph to Mirador's Graph tab (see
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
