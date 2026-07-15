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

Per-plugin switch: `settings.plugins_disabled: list[str] = []`
(`hivepilot/config.py`, env `HIVEPILOT_PLUGINS_DISABLED`) — names of
individual plugins to skip, even when `plugins_enabled` is `True`. Checked in
**all three load paths**, before a plugin's module is loaded or its
`register()` is invoked — a disabled plugin contributes no
runners/notifiers/hooks and has no import-time side effects either:

- local-file scan — matched by file stem (e.g. `rtk` for `plugins/rtk.py`)
- entry-point discovery — matched by entry-point name
- the explicit `plugins_entry` pin (a single plugin loaded directly via
  `HIVEPILOT_PLUGINS_ENTRY`/`settings.plugins_entry`, bypassing discovery) —
  matched by either the full `plugins_entry` string (what the TUI shows/
  toggles for this plugin) or just its module-name portion before the `:`
  attribute separator (the short form an operator would more naturally use
  when setting `plugins_disabled` directly via config/env)

See "TUI plugin manager" below for the interactive `space` toggle.

## Authoring a plugin

A plugin is a module exposing a zero-arg `register()` function that returns a
`dict`. Every key is optional:

| Key | Type | Effect |
|---|---|---|
| `runners` | `dict[str, type[BaseRunner]]` | registered into `RUNNER_MAP` |
| `notifiers` | `dict[str, Callable[[str], None]]` | registered into `NOTIFIER_MAP` |
| `secrets` | `dict[str, SecretsBackend]` | registered into `SECRETS_MAP` |
| `before_step` | `Callable[..., None]` | hook, fired before each step |
| `after_step` | `Callable[..., None]` | hook, fired after each step |
| `on_pipeline_start` | `Callable[..., None]` | hook, fired once when `run_pipeline` starts |
| `on_pipeline_end` | `Callable[..., None]` | hook, fired once when `run_pipeline` finishes (success or fail-fast) |
| `on_error` | `Callable[..., None]` | hook, fired when a stage fails without `continue_on_failure` |

Any key not in this table is still accepted and stored under
`PluginManager.hooks[key]` — forward-compatible, never an error. Only
`runners`/`notifiers`/`secrets` are eagerly popped out and routed to their own
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

### Secrets backend example

Unlike `runners` (a `dict[str, type[BaseRunner]]` — classes) and `notifiers`
(a `dict[str, Callable]` — plain functions), `secrets` values are backend
**instances**, matching how `SECRETS_MAP: dict[str, SecretsBackend]`
(`hivepilot/registry.py`) stores the built-in `env`/`file`/`vault`/`sops`
backends — construct the instance yourself in `register()`:

```python
# plugins/infisical_secrets.py
class InfisicalSecretsBackend:
    def resolve(self, ref, settings):
        ...  # look up ref.spec (e.g. project/environment/path/key) and return the secret value


def register():
    return {"secrets": {"infisical": InfisicalSecretsBackend()}}
```

A secrets backend must satisfy the `SecretsBackend` protocol
(`hivepilot/registry.py`): `resolve(ref: SecretRef, settings: Settings) -> str`.
Same fail-closed trust model as runners/notifiers — a `secrets` name that
collides with an already-registered backend (built-in or another plugin's)
aborts the load (`SecretsBackendCollisionError`), rolling back this plugin's
other contributions; see "Collision & error handling" below.

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

### Example: the `headroom` plugin (`plugins/headroom.py`)

Ships in this repo as a reference `before_step` hook plugin that compresses a
step's prompt/context **before** the step runs, using
[headroom](https://github.com/headroomlabs-ai/headroom)
(`pip install "headroom-ai[all]"` — NOT a hivepilot dependency, and not
installed by this plugin).

**Complementarity with `rtk`:** the two token-saving plugins target opposite
ends of a step. `rtk` (above) compresses **command output** tokens —
whatever a shell command prints, before it reaches the agent as tool
output. `headroom` compresses **agent input/context** tokens — the
prompt the agent is about to receive, before the runner sends it to the
model. They compose cleanly: a step can use both without conflict.

**What it does:** `before_step` receives the same `RunnerPayload` object
(`payload=payload`) that the orchestrator subsequently hands to the runner
with no copy in between (`Orchestrator._execute_task`,
`hivepilot/orchestrator.py`), so an in-place edit to
`payload.metadata` here is picked up by the runner's prompt builder — e.g.
`ClaudeRunner._build_prompt` (`hivepilot/runners/claude_runner.py`) reads
`payload.metadata["extra_prompt"]` and `payload.metadata["prior_context"]`
straight off that same object. `headroom.compress(...)` is run against
`prior_context` (the accumulated output of every upstream stage in a
multi-stage pipeline — usually the largest chunk of a step's prompt, and
the same context PRD A2's keyed routing targets) and `extra_prompt` (the
run's free-text user instructions), whichever are present as non-empty
strings, and the compressed result replaces the field in place — but only
when it's actually shorter (a `compress()` result that isn't smaller than
the original is discarded, original kept). Each compression logs a
`plugin.headroom.compressed` event with `chars_before`/`chars_after`/`ratio`.

An optional model hint is passed through: if the step declares
`metadata.model` (`step.metadata["model"]` — the same per-step model
override `ClaudeRunner._resolve_model` reads), it's forwarded as
`compress(text, model=step.metadata["model"])` so headroom can tune its
compression to the target model's tokenizer/context window. Omitted
(`model=None`) when the step doesn't set it.

**Opt-in — dormant by default:** gated on `settings.headroom_enabled`
(`hivepilot/config.py`, default `False`, env `HIVEPILOT_HEADROOM_ENABLED`)
— mirrors PRD A2's `context_routing_mode` opt-in pattern. The plugin ships
dormant even when this file is present and `headroom-ai` is installed; an
operator must explicitly set `HIVEPILOT_HEADROOM_ENABLED=true` to activate
it.

**Idempotency — shared `metadata` dict:** `Orchestrator._execute_task`
builds ONE `metadata` dict per *task* and reuses that same dict object, by
reference, across every step's `RunnerPayload` in a multi-step task.
Compressing unconditionally on every `before_step` call would re-compress
already-compressed text from step 2 onward — lossy-on-lossy, degrading
without bound. A private sentinel key (`_headroom_compressed`) is set on
the shared `metadata` dict the first time compression runs for it;
subsequent `before_step` calls for that same dict see the sentinel and
skip straight through. The sentinel is safe to leave on `metadata` — every
runner that reads prompt-relevant fields off it reads specific keys
(`extra_prompt`, `prior_context`) rather than iterating or serializing the
whole dict, so it never reaches a rendered prompt.

**Lazy import / no-op behavior:**

- `settings.headroom_enabled` is `False` (the default) → silent no-op.
- `headroom` isn't installed → `before_step` is a silent no-op (the import
  is wrapped in `try/except ImportError`, no crash at plugin load time).
- No `payload` kwarg, no compressible field present/non-empty on
  `payload.metadata`, or the shared `metadata` dict was already compressed
  → silent no-op.
- Any internal error (including a raising `compress()` call) is caught,
  logged (`plugin.headroom.before_step_failed`), and never propagates — a
  hook must never crash a pipeline step.

```yaml
# .env / environment
HIVEPILOT_HEADROOM_ENABLED=true
```

```bash
pip install "headroom-ai[all]"
```

```bash
pip install "headroom-ai[all]"
```

### Example: the `mem0` plugin (`plugins/mem0.py`)

Ships in this repo as a reference plugin that gives agents persistent
cross-run memory, using [mem0](https://github.com/mem0ai/mem0)
(`pip install mem0ai` — NOT a hivepilot dependency, and not installed by
this plugin). Unlike `headroom` and `rtk` above (each a single hook), this
plugin wires TWO lifecycle hooks:

- `before_step` (**recall**): searches mem0 for memories relevant to the
  current project/task and injects them into
  `payload.metadata["extra_prompt"]` — the same field
  `ClaudeRunner._build_prompt` (`hivepilot/runners/claude_runner.py`) reads
  verbatim into the rendered prompt ("Extra instructions from user: ..."),
  on the SAME `RunnerPayload` object the orchestrator hands straight
  through to the runner with no copy in between (exactly headroom's
  mechanism — see above).
- `after_step` (**store**): persists the available salient content for
  that step back to mem0.

**Complementarity with `headroom`:** headroom *compresses* context already
on the payload; mem0 *enriches* it with recalled memory — opposite
directions, same payload. If both plugins are enabled, `recall` should
run **before** headroom's compression pass, so injected memories are
subject to the same compression as the rest of the prompt rather than
bypassing it. Local-file plugins are discovered in
`sorted(plugin_dir.glob("*.py"))` order
(`hivepilot.plugins._scan_local_plugins`) and hooks run in that discovery
order — `"headroom.py"` sorts BEFORE `"mem0.py"` alphabetically, so **as
shipped, hook ordering is the wrong way round**: headroom compresses first,
then mem0 injects fresh, uncompressed memories afterward. Operators running
both plugins together and wanting recall-before-compress should rename
files to control `sorted()` order (e.g. `a_mem0.py` / `b_headroom.py`).

**`store` persists the step's real output.** `Orchestrator._execute_task`
threads the runner's captured return value into the `after_step` call —
`self.plugins.run_hook("after_step", payload=payload, dry_run=dry_run,
role=task.role, output=outputs[-1] if outputs else None)` — the same value
just appended to its local `outputs` list for that step. `store()` reads
`kwargs.get("output")` and persists it (labeled `output: ...`) **in
addition to** task/step identity and the step's *input* context
(`extra_prompt` / `prior_context`): `extra_prompt`/`prior_context` capture
what the task was asked to do, `output` captures what actually happened —
both are kept as complementary, not mutually exclusive. Note this applies
to the `before_step`/`after_step` fire site inside the per-step loop
specifically; the stage-cache-hit and non-native-engine (`langgraph`/
`crewai`) paths in `Orchestrator._execute_task` return early and don't run
that loop, so `recall`/`store` don't fire for those — a pre-existing gap,
unrelated to this change.

**Recall/store keying.** `RunnerPayload` still doesn't carry the task's
`role` (`role` lives on `TaskConfig`, one level above `RunnerPayload` in
`Orchestrator._execute_task`) — rather than widen that shared dataclass,
`role` is threaded straight into the hook call instead: `run_hook(
"before_step"/"after_step", ..., role=task.role)`. `recall`/`store` both
read `kwargs.get("role")` and key memories by
`f"{project_name}:{task_name}:{role}"` (mem0's `user_id`) when `role` is
supplied, falling back to `f"{project_name}:{task_name}"` when it isn't (a
non-role task, or a caller that doesn't pass `role`) — so both functions
stay keyed the same way and previously-stored memories for non-role tasks
keep matching.

**Avoiding a recall/store feedback loop.** Because `recall` mutates
`extra_prompt` in place (appending a "Relevant memories:" block), `store`
reading the current `extra_prompt` would re-persist mem0's own recalled
memories back into mem0. `recall` snapshots the pre-mutation value under a
private key (`_mem0_original_extra_prompt`) the first time it runs for a
shared `metadata` dict; `store` prefers that snapshot when present.

**Idempotency — shared `metadata` dict:** same problem headroom solves,
same mechanism — `Orchestrator._execute_task` builds ONE `metadata` dict
per task and reuses it by reference across every step's `RunnerPayload`.
A private sentinel key (`_mem0_recalled`) is set on the shared dict after
the first `search()` call for it (regardless of whether any memories were
found); subsequent `before_step` calls for that dict skip straight
through. Neither private key (`_mem0_recalled`,
`_mem0_original_extra_prompt`) is ever rendered into a prompt — every
runner that reads prompt-relevant fields reads specific keys
(`extra_prompt`, `prior_context`), never iterating or serializing the
whole dict.

**Opt-in — dormant by default:** gated on `settings.mem0_enabled`
(`hivepilot/config.py`, default `False`, env `HIVEPILOT_MEM0_ENABLED`) —
mirrors `headroom_enabled`'s opt-in pattern. Two backends are supported:

- **Self-host:** leave `mem0_api_key` unset — uses `mem0.Memory()`,
  optionally customized via `mem0_config` (a dict passed to
  `Memory.from_config()`).
- **Hosted (mem0.ai):** set `settings.mem0_api_key` — uses
  `mem0.MemoryClient(api_key=...)`.

> ⚠️ **Data egress (hosted mode).** In hosted mode, `store` sends
> `extra_prompt`, `prior_context`, **and the step's `output`** — including
> whatever content upstream agent steps produced or the agent itself just
> generated (file contents, config dumps, or secrets an agent echoed) —
> **off-machine to mem0.ai's servers**, verbatim and un-redacted. `output`
> is the agent's actual generated result for the step and is *more* likely
> than `extra_prompt`/`prior_context` to contain secrets or sensitive
> content. Do NOT enable hosted mode on projects where step output may
> contain secrets or confidential data — use the self-host `Memory()`
> backend (leave `mem0_api_key` unset) instead. Self-host keeps everything
> local.

mem0's exact constructor/`search()`/`add()` signatures are not pinned by
this optional integration (`mem0ai` is never installed by this plugin) —
if the real API differs, the outer `try/except` in every function degrades
to a logged no-op, same as every other hook in this repo.

```yaml
# .env / environment
HIVEPILOT_MEM0_ENABLED=true
# Self-host (default) — no key needed. Hosted mem0.ai:
HIVEPILOT_MEM0_API_KEY=your-mem0-api-key
```

```bash
pip install mem0ai
```

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
- **Known limitation — dry-run:** the notifier and hooks write to the vault
  for real even when a pipeline runs in `--dry-run`/`--simulate` mode. Unlike
  the in-orchestrator vault writers (`ObsidianService`/`InteractionService`,
  which receive the run's `dry_run` flag), the notifier and lifecycle-hook
  contracts do not currently pass `dry_run` to handlers, so a plugin has no
  signal to honor it. Treat obsidian journaling as always-on. Threading
  `dry_run` through `send_notification` / `run_hook` is a tracked follow-up.

```yaml
# .env / environment
HIVEPILOT_OBSIDIAN_VAULT=/path/to/your/Vault
```

```python
from hivepilot.services.notification_service import send_notification

send_notification("Deploy finished", channels=["obsidian"])
```

### Example: the `infisical` secrets provider (`plugins/infisical.py`)

A first-party **secrets provider** plugin — it dogfoods the third plugin
provider type (`secrets`, alongside `runners`/`notifiers`). It fetches a named
value from [Infisical](https://infisical.com) (an open-source, self-hostable
config/value store) so pipeline configs can reference stored values instead of
inlining them. `register()` returns
`{"secrets": {"infisical": InfisicalBackend()}}`, which is loaded into
`SECRETS_MAP` under the fail-closed trust model (a name colliding with a
built-in — or another plugin's — backend aborts the load).

The Infisical Python SDK (`pip install infisicalsdk`) is **not** a hivepilot
dependency — it's imported lazily. If the SDK isn't installed, required config
is missing, or the client errors, `resolve()` raises a clear error naming
**only** the secret key + provider (`infisical`) — never the fetched value —
so a stage with `on_error: closed` aborts rather than proceeding with a
half-resolved config.

Configure via `HIVEPILOT_INFISICAL_*` — self-host is supported by setting
`HIVEPILOT_INFISICAL_URL` to your instance's base URL (leave it unset to use
the hosted Infisical default):

> **Caveat:** the SDK surface this plugin targets (`InfisicalSDKClient`,
> `client.secrets.get_secret_by_name(...)`, `.secretValue`) is an assumption,
> not verified against a pinned SDK version (`infisicalsdk` is never
> installed by this plugin) — confirm it matches your installed
> `infisicalsdk` version before relying on this provider in production.

```bash
# .env / environment
HIVEPILOT_INFISICAL_URL=https://infisical.example.com   # omit for hosted app.infisical.com
HIVEPILOT_INFISICAL_TOKEN=st.xxxxx                        # access / machine-identity token
HIVEPILOT_INFISICAL_WORKSPACE_ID=6410...                 # project (workspace) id
HIVEPILOT_INFISICAL_ENVIRONMENT=dev                       # environment slug
```

Reference a stored value from a config via `${secret:NAME}`, where `NAME`'s
spec declares `source: infisical`. The spec's `key` names the Infisical secret
to fetch; `environment`, `path`, and `workspace_id` are optional per-secret
overrides of the `HIVEPILOT_INFISICAL_*` defaults:

```yaml
# secrets.yaml (or the `secrets:` block of a project config)
secrets:
  DATABASE_URL:
    source: infisical
    key: DATABASE_URL          # the Infisical secret name to fetch
  STRIPE_KEY:
    source: infisical
    key: STRIPE_SECRET_KEY
    environment: prod          # override HIVEPILOT_INFISICAL_ENVIRONMENT
    path: /billing             # override the default "/" secret path
```

```yaml
# ... elsewhere in a config, the resolved value is referenced by name:
env:
  DATABASE_URL: ${secret:DATABASE_URL}
```

### Example: the `onepassword` secrets provider (`plugins/onepassword.py`)

A first-party **secrets provider** plugin (a structural sibling of the
`infisical` one above). It fetches a named value from
[1Password](https://1password.com) via a **1Password Connect** endpoint
(self-hostable) so pipeline configs can reference stored values instead of
inlining them. `register()` returns
`{"secrets": {"onepassword": OnePasswordBackend()}}`, which is loaded into
`SECRETS_MAP` under the fail-closed trust model (a name colliding with a
built-in — or another plugin's — backend aborts the load).

The 1Password Connect SDK (`pip install onepasswordconnectsdk`) is **not** a
hivepilot dependency — it's imported lazily. If the SDK isn't installed,
required config is missing, the client errors, or no usable value is found,
`resolve()` raises a clear error naming **only** the reference identity
(`op://vault/item/field`) + provider (`onepassword`) — never the token or the
fetched value — so a stage with `on_error: closed` aborts rather than
proceeding with a half-resolved config.

**Credential modes.** Both authenticate against a Connect API base URL
(`HIVEPILOT_OP_CONNECT_HOST`, self-hostable):

- **Connect** — `HIVEPILOT_OP_CONNECT_HOST` + `HIVEPILOT_OP_CONNECT_TOKEN`.
- **service-account** — `HIVEPILOT_OP_SERVICE_ACCOUNT_TOKEN`, presented to the
  same Connect endpoint (used only when no Connect token is set).

> **Caveat:** the SDK surface this plugin targets
> (`onepasswordconnectsdk.client.new_client(url, token)`,
> `client.get_item(item, vault)`, an item's `.fields[*].label` / `.id` /
> `.value`) is verified against `onepasswordconnectsdk` 2.1.0 — confirm it
> matches your installed version before relying on this provider in production.
> A hosted service account that does **not** front a Connect server would
> instead need the separate `onepassword` SDK (out of scope here).

```bash
# .env / environment
HIVEPILOT_OP_CONNECT_HOST=https://op-connect.example.com   # Connect API base URL
HIVEPILOT_OP_CONNECT_TOKEN=eyJhbGci...                      # Connect token
# ...or, instead of the Connect token, a service-account token:
HIVEPILOT_OP_SERVICE_ACCOUNT_TOKEN=ops_eyJ...              # service-account token
```

Reference a stored value from a config via `${secret:NAME}`, where `NAME`'s
spec declares `source: onepassword`. Address the value either with a full
`op://vault/item/field` reference **or** with discrete `vault` / `item` /
`field` keys (all three required):

> **Only 3-segment references are supported.** A section-qualified reference
> (`op://vault/item/section/field`) is **rejected** (fail-closed), not
> collapsed to `op://vault/item/field` by dropping the section — silently
> dropping the section could match the wrong field if two sections share a
> field label. If an item legitimately has two fields with the same label,
> the **first match wins**.

```yaml
# secrets.yaml (or the `secrets:` block of a project config)
secrets:
  DATABASE_URL:
    source: onepassword
    ref: op://Prod/database/connection-string   # full op:// reference
  STRIPE_KEY:
    source: onepassword
    vault: Prod                                  # ...or discrete vault/item/field
    item: stripe
    field: secret-key
```

```yaml
# ... elsewhere in a config, the resolved value is referenced by name:
env:
  DATABASE_URL: ${secret:DATABASE_URL}
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

- **Kind/name collision** — if a plugin declares a `runners`, `notifiers`, or
  `secrets` key whose name is already registered to a *different*
  implementation, that raises (`RunnerKindCollisionError` /
  `NotifierKindCollisionError` / `SecretsBackendCollisionError`) and
  **aborts loading**. This is a hard stop by design: silently shadowing a
  built-in (e.g. redefining `claude`, or a secrets backend named `vault`) is
  never the right default. Registration of a single plugin's
  runners+notifiers+secrets is atomic: if any entry collides, every entry
  that plugin already added to the process-global maps in this same load is
  rolled back before the error propagates — an aborted plugin never leaves
  orphaned, partially-applied registrations behind.
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

Prints four tables:

- **Loaded Plugins** — every successfully-loaded `PluginRecord`: `name`,
  `source` (`local-file` | `entry-point`), `location`.
- **Runner Kinds** — every kind currently in `RUNNER_MAP`, labeled
  `built-in` or `plugin` by membership in `KNOWN_RUNNER_KINDS`.
- **Notifiers** — every notifier currently in `NOTIFIER_MAP`, labeled
  `built-in` or `plugin` by membership in `{slack, discord, telegram}`.
- **Secrets Backends** — every backend currently in `SECRETS_MAP`, labeled
  `built-in` or `plugin` by membership in `KNOWN_SECRET_BACKENDS`
  (`{env, file, vault, sops}`).

This is a v1 inventory, not a full join — it does not attribute which
specific runner kind or notifier came from which loaded plugin beyond what a
`PluginRecord` itself records. If a plugin contributes a runner kind or hook
and it doesn't show up as expected, check the process log for
`plugins.load_failed` / `plugins.register_failed` first.

### TUI plugin manager

```bash
HIVEPILOT_ENABLE_TEXTUAL_UI=1 hivepilot plugins tui
```

An interactive browser/inspector over the same data as `plugins list` — a
**Loaded Plugins** table (name / source / status / type(s) / detail), with
`Enter` showing the selected plugin's best-effort runner kinds, notifier
names, and hook names in a details pane (`r` refreshes, `q` quits).
Attribution is derived by matching each contributed runner/notifier/hook's
`__module__` against a hint built from the plugin's own source/location —
best-effort, same v1 limitation as `plugins list` (see "Inspecting loaded
plugins" above and roadmap Phase 26a): when attribution can't be derived,
the row shows `unknown (see aggregate)` instead of guessing.

**Enable/disable (`space`, Phase 26b)** — pressing `space` on the
highlighted plugin flips its presence in `plugins_disabled` and persists the
change to the `.env` file `Settings` reads from (upserting the
`HIVEPILOT_PLUGINS_DISABLED` line; every other line is left untouched). The
row's **Status** column updates immediately to reflect the change. The
change is **effective on next start only** — `PluginManager` scans and
registers plugins once, at construction, so live hot-reload of a running
process is out of scope (see roadmap Phase 26b follow-ups). Because a
disabled plugin is skipped before it is even loaded, a plugin you disable
disappears from this table's "loaded" list after restart — re-enable it via
`plugins_disabled` directly (config/env) or by editing `.env`.

`plugins_disabled` can also be set directly via config or environment —
`HIVEPILOT_PLUGINS_DISABLED='["rtk", "obsidian"]'` — without going through
the TUI at all; it complements `plugins_enabled` (the master on/off switch
for ALL plugin loading) with a per-plugin skip list.

Requires the `dashboard`/`full` extra (`pip install "hivepilot[dashboard]"`
— ships `textual`); without it, and without the env var set, the command
prints a message and exits instead of crashing.
