# Plugins

HivePilot loads arbitrary Python code to extend it with new runner kinds, new
notifier channels, and pipeline-lifecycle hooks. This page covers the trust
model, how to author a plugin, how to package one, and how loading behaves
when something goes wrong.

## Trust model

A plugin is arbitrary Python code — it runs with the same privileges as the
`hivepilot` process itself. There are exactly two trusted sources; nothing
else is ever consulted:

1. **Local files** — `plugins/*.py` under the project `base_dir`, or under the
   synced `config_repo`. Same local-filesystem trust as `tasks.yaml` /
   `projects.yaml` — if you can edit those, you can already run arbitrary code
   in this process.
2. **Installed packages** — any Python package in the current environment that
   declares a `hivepilot.plugins` entry point. Trust here is "you, or your
   package manager, chose to `pip install` it" — the same boundary as any
   other dependency.

There is **no network fetch of plugin code, ever** — no URL, git-remote, or
artifact-registry download exists anywhere in `hivepilot/plugins.py`. A
plugin runner/notifier executes with the same process environment and
`settings.secrets_allowed_dirs` / env-merge access as a built-in runner (via
`RunnerPayload.secrets`) — no new secret surface is introduced.

Master switch: `settings.plugins_enabled: bool = True` (`hivepilot/config.py`).
Set to `False` to disable both discovery mechanisms; built-ins are unaffected.

## Authoring a plugin

A plugin is a module exposing a zero-arg `register()` function that returns a
`dict`. Every key is optional:

| Key | Type | Effect |
|---|---|---|
| `runners` | `dict[str, type[BaseRunner]]` | registered into `RUNNER_MAP` |
| `notifiers` | `dict[str, Callable[[str], None]]` | registered into `NOTIFIER_MAP` |
| `before_step` | `Callable[..., None]` | hook, fired before each step |
| `after_step` | `Callable[..., None]` | hook, fired after each step |
| `on_pipeline_start` | `Callable[..., None]` | hook, fired once when `run_pipeline` starts |
| `on_pipeline_end` | `Callable[..., None]` | hook, fired once when `run_pipeline` finishes (success or fail-fast) |
| `on_error` | `Callable[..., None]` | hook, fired when a stage fails without `continue_on_failure` |

Any key not in this table is still accepted and stored under
`PluginManager.hooks[key]` — forward-compatible, never an error. Only
`runners`/`notifiers` are eagerly popped out and routed to their own
registries; everything else accumulates as a list of hook callables, exactly
like `before_step`/`after_step` do today.

Hooks are called with keyword arguments only — write `**kwargs` or name the
ones you use:

| Hook | kwargs |
|---|---|
| `before_step` / `after_step` | `payload` (a `RunnerPayload`) |
| `on_pipeline_start` | `run_id`, `pipeline`, `projects` |
| `on_pipeline_end` | `run_id`, `pipeline`, `status` |
| `on_error` | `run_id`, `pipeline`, `stage` |

### Runner example

```python
# plugins/bedrock_runner.py
class BedrockRunner:
    def __init__(self, definition, settings):
        self.definition = definition
        self.settings = settings

    def run(self, payload):
        ...  # invoke the model, write output where the pipeline expects it


def register():
    return {"runners": {"bedrock": BedrockRunner}}
```

A runner class must satisfy the `BaseRunner` protocol
(`hivepilot/runners/base.py`): `__init__(definition, settings)`,
`run(payload) -> None`, and optionally `capture(payload) -> str`.

### Notifier example

```python
# plugins/pagerduty_notifier.py
def _send_pagerduty(message: str) -> None:
    ...  # POST to your PagerDuty integration


def register():
    return {"notifiers": {"pagerduty": _send_pagerduty}}
```

Raise `hivepilot.services.notification_service.NotConfigured` from a notifier
to signal "not configured, skip silently" — the same contract a built-in
channel (Slack/Discord/Telegram) uses.

### Hook example

```python
# plugins/audit_log.py
import json
from pathlib import Path


def _on_pipeline_start(*, run_id, pipeline, projects):
    _append({"event": "start", "run_id": run_id, "pipeline": pipeline, "projects": projects})


def _on_error(*, run_id, pipeline, stage):
    _append({"event": "error", "run_id": run_id, "pipeline": pipeline, "stage": stage})


def _on_pipeline_end(*, run_id, pipeline, status):
    _append({"event": "end", "run_id": run_id, "pipeline": pipeline, "status": status})


def _append(record: dict) -> None:
    with Path("audit.log").open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def register():
    return {
        "on_pipeline_start": _on_pipeline_start,
        "on_error": _on_error,
        "on_pipeline_end": _on_pipeline_end,
    }
```

A hook that raises is caught, logged (`plugins.hook_failed`), and never
propagates — it cannot crash a live pipeline run, the same guarantee a broken
vault-commit or auditor-observe call already has.

### Example: the `rtk` runner (`plugins/rtk.py`)

Ships in this repo as a reference runner plugin (not a built-in — it's a
local-file plugin, same trust tier as anything else in `plugins/`). It wraps
whatever command a shell-generic step would normally run with
[`rtk proxy`](https://github.com/rtkdev/rtk) — an external CLI that filters
noisy command output before it reaches the agent, cutting token usage on
command-heavy steps (test runs, linters, `git status`, etc.).

`RtkRunner` renders the step's `command` (or the runner definition's
`command`) template exactly like the built-in `shell` runner, then:

- If `rtk` is found on `PATH` (`shutil.which("rtk")`), it runs
  `rtk proxy bash -lc "<rendered command>"`.
- If `rtk` is **not** installed, it logs a warning
  (`rtk_runner.rtk_not_found`) and falls back to running
  `bash -lc "<rendered command>"` directly — the step still executes, it
  just doesn't get the token-saving filtering. A step never fails just
  because `rtk` isn't on the host.

Point a role or runner definition at it the same way you'd point at any
other kind — set `kind: rtk` on a `RunnerDefinition` (e.g. in `roles.yaml` /
`tasks.yaml`) and give the step (or the runner definition) a `command`:

```yaml
# roles.yaml
runners:
  fast-tests:
    kind: rtk
    command: "pytest -q"
```

```yaml
# tasks.yaml
steps:
  - name: run-tests
    runner: fast-tests
```

Any step assigned to a runner of kind `rtk` gets its command proxied through
`rtk` automatically, with the same-directory graceful fallback described
above.

### Example: the `obsidian` plugin (`plugins/obsidian.py`)

Ships in this repo as a reference plugin that is BOTH a notifier and a pair
of lifecycle hooks — logging pipeline activity into the Obsidian vault. Both
surfaces append to the SAME daily journal note:

```
12 - HivePilot/Runs/YYYY-MM-DD.md
```

- Notifier `obsidian`: every `send_notification(message, channels=["obsidian"])`
  call (or a channel list that includes `"obsidian"`) appends a timestamped
  line for `message` to today's journal.
- Hooks `on_pipeline_end` / `on_error`: append a structured run-report block
  (`run_id`, `pipeline`, `status` or `stage`, and a UTC timestamp) to the same
  journal.

It targets `settings.obsidian_vault` (`hivepilot/config.py`), resolved lazily
inside each function — no vault path is cached at import time. All file I/O
goes through `hivepilot.services.obsidian_service.ObsidianService` (the same
path-guard + frontmatter discipline used by every other vault writer in
`hivepilot`, including the new `append_daily()` method it adds — never a raw
`open().write()`).

Configuration and failure behavior:

- If `settings.obsidian_vault` is unset or the path doesn't exist on disk,
  the notifier raises `NotConfigured` (skipped silently by
  `send_notification`, the standard contract) and the hooks are silent
  no-ops — a hook must never crash a run.
- `obsidian` does not collide with the built-in notifier channels
  (`KNOWN_NOTIFIER_NAMES = ("slack", "discord", "telegram")`).

```yaml
# .env / environment
HIVEPILOT_OBSIDIAN_VAULT=/path/to/your/Vault
```

```python
from hivepilot.services.notification_service import send_notification

send_notification("Deploy finished", channels=["obsidian"])
```

## Packaging

### Local file

Drop a `.py` file directly under `plugins/` in the project `base_dir` (or the
synced `config_repo`). Filenames starting with `_` are skipped. No packaging,
no install step — pick this for project-specific extensions.

### Installed package (entry point)

For a reusable/shareable plugin, declare a `hivepilot.plugins` entry point in
your **own** package's `pyproject.toml`:

```toml
[project.entry-points."hivepilot.plugins"]
my_plugin = "my_package:register"
```

`pip install` the package into the same environment as `hivepilot`, and it is
discovered automatically at process start — no config change needed.

## Collision & error handling

- **Kind/name collision** — if a plugin declares a `runners` or `notifiers`
  key whose name is already registered to a *different* implementation, that
  raises (`RunnerKindCollisionError` / `NotifierKindCollisionError`) and
  **aborts loading**. This is a hard stop by design: silently shadowing a
  built-in (e.g. redefining `claude`) is never the right default.
- **Broken plugin** — any other failure (import error, exception inside
  `register()`, a bad entry point) is logged
  (`plugins.load_failed` / `plugins.register_failed` /
  `plugins.entry_point_load_failed`) and that one plugin is skipped. It is
  isolated from the rest — one broken plugin never blocks another plugin, or
  the built-ins, from loading.
- **Broken hook at runtime** — see "Hook example" above: caught, logged,
  never propagates.

## Inspecting loaded plugins

```bash
hivepilot plugins list
```

Prints three tables:

- **Loaded Plugins** — every successfully-loaded `PluginRecord`: `name`,
  `source` (`local-file` | `entry-point`), `location`.
- **Runner Kinds** — every kind currently in `RUNNER_MAP`, labeled
  `built-in` or `plugin` by membership in `KNOWN_RUNNER_KINDS`.
- **Notifiers** — every notifier currently in `NOTIFIER_MAP`, labeled
  `built-in` or `plugin` by membership in `{slack, discord, telegram}`.

This is a v1 inventory, not a full join — it does not attribute which
specific runner kind or notifier came from which loaded plugin beyond what a
`PluginRecord` itself records. If a plugin contributes a runner kind or hook
and it doesn't show up as expected, check the process log for
`plugins.load_failed` / `plugins.register_failed` first.
