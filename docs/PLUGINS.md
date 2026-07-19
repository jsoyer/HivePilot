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
| Lifecycle hooks | any other key | a callable (`before_step`, `after_step`, `on_pipeline_end`, `on_error`, …) | No collision check — every plugin's hook for a given name runs |

Secrets contributions are covered in more depth in [SECURITY.md](./SECURITY.md); panels in [DASHBOARD.md](./DASHBOARD.md); skills in [SKILLS.md](./SKILLS.md).

## How plugins load

Plugins load from three sources. All three are gated by the master switch `plugins_enabled` (default `True`) and a per-plugin `plugins_disabled` skip-list that is checked **before the module executes** — a disabled plugin's code never runs.

- **local-file** (`source="local-file"`): every `*.py` file under `<base_dir>/plugins/` except files prefixed with `_`. Files are compiled directly from source, bypassing the `.pyc` cache, so hot-reload picks up edits immediately.
- **entry-point** (`source="entry-point"`): any installed package that declares an entry point in the `hivepilot.plugins` group, discovered via `importlib.metadata.entry_points(group="hivepilot.plugins")`.
- **explicit-entry** (`source="explicit-entry"`): a single pinned `module:attr` target set via the `HIVEPILOT_PLUGINS_ENTRY` environment variable.

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

## Shipped plugins (inventory)

23 plugins ship under `plugins/*.py`.

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
| `onepassword` | secrets + health | ON |

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

## See also

- [RUNNERS.md](./RUNNERS.md)
- [SKILLS.md](./SKILLS.md)
- [SECURITY.md](./SECURITY.md)
- [DASHBOARD.md](./DASHBOARD.md)
