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

## Agent runner taxonomy: built-in vs. plugin

Coding-agent runner kinds ship in two tiers, both dispatched through the same
`RunnerRegistry` / `kind:` config field — the tier only affects *where* the
runner class is registered from, and whether it can ever be absent.

**Built-in agent kinds** — `{claude, codex, vibe, openrouter}`
(`hivepilot.registry._BUILTIN_RUNNERS`) are unconditionally registered at
import time, no `PATH` check — always present in `RUNNER_MAP`.

| kind | binary | notes |
|---|---|---|
| `claude` | `claude` | `mode: cli` and `mode: api` (Anthropic Messages API) |
| `codex` | `codex` | `mode: cli` and `mode: api` |
| `vibe` | `vibe` | `mode: cli` and `mode: api`; has no `--model` flag — the model comes from its own config / `MISTRAL_API_KEY` |
| `openrouter` | — (API-only) | `supported_modes == {"api"}` — no CLI binary, never spawns a subprocess |

**Plugin agent kinds** — `{gemini, opencode, ollama, pi, qwen-code, kimi-cli}`
(one file per kind under `plugins/`, all following the same canonical
gated-agent-plugin skeleton — see `plugins/gemini.py`'s module docstring)
are registered into `RUNNER_MAP` only when BOTH its per-plugin enable flag is
`True` (default: all six default **ON**, opt-out) AND its CLI binary is found
on `PATH` (`shutil.which`) at process start. Either condition failing means
the kind is simply **absent** from `RUNNER_MAP` — a config that still
references it resolves to the actionable `RunnerPluginUnavailableError`
(naming the exact flag + binary), never a bare `KeyError`.

| kind | binary | enable flag | env override | install |
|---|---|---|---|---|
| `gemini` | `gemini` | `gemini_enabled` | `HIVEPILOT_GEMINI_ENABLED` | see the Gemini CLI's own install docs |
| `opencode` | `opencode` | `opencode_enabled` | `HIVEPILOT_OPENCODE_ENABLED` | see opencode's own install docs |
| `ollama` | `ollama` | `ollama_enabled` | `HIVEPILOT_OLLAMA_ENABLED` | see Ollama's own install docs |
| `pi` | `pi` | `pi_enabled` | `HIVEPILOT_PI_ENABLED` | `npm i -g @earendil-works/pi-coding-agent` |
| `qwen-code` | `qwen` (binary `qwen`, kind `qwen-code` — deliberately diverges, like `ollama`'s pair) | `qwen_code_enabled` | `HIVEPILOT_QWEN_CODE_ENABLED` | `npm i -g @qwen-code/qwen-code` |
| `kimi-cli` | `kimi` (binary `kimi`, kind `kimi-cli`) | `kimi_cli_enabled` | `HIVEPILOT_KIMI_CLI_ENABLED` | `uv tool install kimi-cli` |

**PATH-activation rule.** Activation is evaluated ONCE, at `PluginManager()`
construction (process start): installing/removing a binary, or flipping its
enable flag, only takes effect on the **next** process start — the same
"effective on next start only" limitation the TUI's `space` toggle documents
below. Check current activation any time with `hivepilot plugins list` (see
"Inspecting loaded plugins" below — the **Agent Runners** table tags every
plugin agent kind `active`/`inactive`) or `hivepilot plugins health`.

**Mandatory-agent install requirement.** HivePilot needs **at least one** of
the three mandatory built-in agent CLIs — `claude` / `codex` / `vibe`
(`hivepilot.services.agent_checks.MANDATORY_AGENTS`) — on `PATH` to run a
pipeline at all; `claude` is the strongest/most-tested prerequisite.
`hivepilot init` and `hivepilot doctor` both scan for these and print a
warning — **never a hard failure** — when none is found: `init`'s whole job
is to scaffold the config you need before you can install an agent CLI into
it, so hard-failing there would be a chicken-and-egg regression on a fresh
machine or in CI.

> **Known gap: default role mappings depend on OPTIONAL plugin binaries.**
> The shipped default role mappings (`hivepilot/roles.py`) route `ceo` /
> `cto` / `ciso` to `runner="opencode"` and `documentation` to
> `runner="gemini"` — both are OPTIONAL, PATH-gated plugin agent kinds from
> the table above, **not** part of the mandatory set. On a host where the
> `opencode` or `gemini` binary isn't on `PATH` (or its enable flag was
> turned off), a task using one of these default roles fails at dispatch
> with `RunnerPluginUnavailableError`. The mandatory-agent check `init` /
> `doctor` runs only guarantees a **built-in** agent (`claude`/`codex`/
> `vibe`) is present — it does **not** guarantee `opencode` or `gemini` are
> installed. If you use these default roles, install the corresponding CLI
> (see the table above) or repoint the role at a different runner kind in
> your own `roles.yaml`.

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

Per-plugin enable flags: every bundled plugin has its own `<name>_enabled`
boolean (`hivepilot/config.py`, env `HIVEPILOT_<NAME>_ENABLED`). The two
context plugins default **OFF (opt-in, dormant)** — `headroom_enabled` /
`mem0_enabled` — while the seven others default **ON (opt-out)**:
`herdr_enabled`, `hugo_enabled`, `infisical_enabled`, `obsidian_enabled`,
`onepassword_enabled`, `rtk_enabled`, `sample_enabled`. A plugin whose flag is
`False` early-returns `{}` from `register()` — it contributes no
runner/notifier/hook/secret/panel/health. Toggle e.g. `rtk` off with
`HIVEPILOT_RTK_ENABLED=false`.

See "TUI plugin manager" below for the interactive `space` toggle.

## Authoring a plugin

A plugin is a module exposing a zero-arg `register()` function that returns a
`dict`. Every key is optional:

| Key | Type | Effect |
|---|---|---|
| `runners` | `dict[str, type[BaseRunner]]` | registered into `RUNNER_MAP` |
| `notifiers` | `dict[str, Callable[[str], None]]` | registered into `NOTIFIER_MAP` |
| `secrets` | `dict[str, SecretsBackend]` | registered into `SECRETS_MAP` |
| `health` | `dict[str, Callable[..., HealthStatus \| dict]]` | registered into `PluginManager.health` — see "Health checks" below |
| `panels` | `list[PanelSpec]` | registered into `PluginManager.panels` — see "Panels (Mirador)" below |
| `skills` | `list[SkillSpec]` | registered into `PluginManager.skills` — see "Skills" below |
| `before_step` | `Callable[..., None]` | hook, fired before each step |
| `after_step` | `Callable[..., None]` | hook, fired after each step |
| `on_pipeline_start` | `Callable[..., None]` | hook, fired once when `run_pipeline` starts |
| `on_pipeline_end` | `Callable[..., None]` | hook, fired once when `run_pipeline` finishes (success or fail-fast) |
| `on_error` | `Callable[..., None]` | hook, fired when a stage fails without `continue_on_failure` |

Any key not in this table is still accepted and stored under
`PluginManager.hooks[key]` — forward-compatible, never an error. Only
`runners`/`notifiers`/`secrets`/`health` are eagerly popped out and routed to
their own collections; everything else accumulates as a list of hook
callables, exactly like `before_step`/`after_step` do today.

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

### Health checks

A plugin may optionally expose a **health check** — a zero/kwarg-only
callable that reports whether the thing it wraps (an external binary, a
library, a configured backend) is actually usable right now, surfaced by
`hivepilot plugins list` / `hivepilot plugins health` / the TUI (the
`store ok / mem0 ok / headroom ok` pattern):

```python
# plugins/bedrock_runner.py (continued from the runner example above)
from hivepilot.plugins import HealthStatus


def health(**kwargs):
    import boto3

    try:
        boto3.client("bedrock")
    except Exception as exc:
        return HealthStatus("error", f"boto3 client failed: {exc}")
    return HealthStatus("ok", "bedrock client reachable")


def register():
    return {"runners": {"bedrock": BedrockRunner}, "health": {"bedrock": health}}
```

`register()["health"]` is `dict[str, Callable[..., HealthStatus | dict]]` —
name -> health-check callable. The callable must be **keyword-tolerant**
(accept `**kwargs`, even if unused today — future callers may pass context)
and return one of:

- a `HealthStatus` — `from hivepilot.plugins import HealthStatus`, a
  `NamedTuple` with `status: Literal["ok", "degraded", "error"]` and
  `detail: str`; or
- a plain `{"status": ..., "detail": ...}` dict, the no-import fallback for a
  plugin that would rather not depend on `hivepilot.plugins`'s import
  surface.

Both shapes are accepted and normalized by the collector. Any other return
value (wrong type, an invalid `status`) normalizes to
`HealthStatus("error", "invalid health check result...")` rather than
crashing anything downstream.

**Never-raise.** Running a health check (`PluginManager.run_health_check` /
`check_all()`) never propagates an exception — a raising callable is caught
and reported as `HealthStatus("error", "<ExceptionType>: <short message>")`.
The same guarantee every other plugin hook in this repo has: a broken health
check cannot crash `plugins list`, `plugins health`, or the TUI.

**No secrets in a health detail.** A health check's `detail` string must
**never** contain a secret/token value (the same Phase 19 no-leak discipline
used for resolved `${secret:NAME}` values) — report presence/config booleans
and names only (e.g. `"hosted mode configured"`, not the API key itself).
See the `mem0` example below.

**Collision & routing.** `health` names are collected into
`PluginManager.health` (an instance dict — no process-global map, health is
scoped to the manager exactly like `PluginManager.hooks` is) the same way
runners/notifiers/secrets are: a name that collides with an already-
registered health check (built-in or another plugin's) is a hard stop
(`HealthNameCollisionError`), rolling back this plugin's other contributions
atomically — see "Collision & error handling" below.

A plugin without a `health` key simply doesn't appear in the health surface
— fully backward-compatible with every plugin shipped before this feature.

### Panels (Mirador)

A plugin may optionally contribute a **panel** — a renderer-agnostic view
shown in **both** Mirador surfaces: the Textual TUI dashboard (extra tabs,
`hivepilot/ui/dashboard.py`) and the web UI (extra tabs, `docs/v4/WEBUI.md`).
A panel author writes ONE `fetch()` function; both renderers turn its output
into their own native widgets — no HTML or Textual markup is ever authored by
a plugin.

`register()["panels"]` is `list[PanelSpec]`, where each `PanelSpec` is a
plain dict:

```python
class PanelSpec(TypedDict, total=False):
    name: str                       # required — stable id, collision-checked
    title: str                      # required — display title
    fetch: Callable[[], PanelData]  # required — no-arg, returns PanelData
    min_role: str                   # optional — default "read"
```

`fetch()` returns a `PanelData` — a dict with a single `sections` key, a list
of section dicts drawn from the **closed** set of kinds `stat` / `table` /
`text`:

| kind | fields |
|---|---|
| `stat` | `label: str`, `value: str`, `status: "ok" \| "warn" \| "error" \| None` |
| `table` | `columns: list[str]`, `rows: list[list[str]]` |
| `text` | `content: str` |

Any other shape (wrong top-level type, missing `sections`, an unknown
`kind`, or a section missing/mistyping a required field) is rejected by
`normalize_panel_data` (`hivepilot/plugins.py`) — the one exception is an
unrecognized `stat` `status`, which normalizes to `None` (no badge) rather
than rejecting the whole section.

```python
# plugins/sample.py
def _sample_fetch():
    return {
        "sections": [
            {"kind": "stat", "label": "steps run", "value": "42", "status": "ok"},
            {
                "kind": "table",
                "columns": ["project", "status"],
                "rows": [["demo-project", "ok"], ["other-project", "warn"]],
            },
            {"kind": "text", "content": "Sample panel contributed by plugins/sample.py."},
        ]
    }


def register():
    return {
        "panels": [
            {"name": "sample_stats", "title": "Sample Stats", "fetch": _sample_fetch},
        ],
    }
```

**Never-raise — don't rely on `fetch()` raising.** A panel's `fetch()` is
never called directly by a renderer — always through
`PluginManager.run_panel_fetch`, which never propagates an exception. A
raising or malformed `fetch()` degrades to a single `stat` section
(`{"label": "error", "value": "<ExceptionType>", "status": "error"}`) showing
the exception's **type name only** — the exception message itself is logged
server-side but never returned to a caller. Since panel data may be served to
any token whose role clears `min_role` (see below), **never put a secret in
an exception message, and never put a secret in panel data either.**

**Section content is untrusted, rendered as plain text — but don't emit
secrets anyway.** `label`/`value`/`content`/table cells are plugin-authored
and are treated as untrusted display text by both renderers: the web
renderer (`PanelRenderer.tsx`) interpolates them through plain JSX (React
escapes automatically; the code never uses `dangerouslySetInnerHTML`), and
the TUI renderer (`hivepilot/ui/dashboard.py`) renders them literally as
plain widget text, never as markup. This protects against XSS/markup
injection, not against a panel author choosing to display a secret — that
responsibility is entirely on the author (see `min_role` below).

**`min_role` — the only access control a panel has.** Optional, defaults to
`"read"`. It **must** be one of the four real roles
(`token_service.ROLE_RANKS`: `read` / `run` / `approve` / `admin`) — an
invalid value (a typo, an empty string, a non-string) makes the **whole
plugin fail to register**, fail-closed (`PanelInvalidMinRoleError`), exactly
like a name collision (see "Collision & error handling" below). This closes
a fail-open gap: `token_service.role_rank` returns `-1` for any unrecognized
role, which would otherwise make the endpoint's `role_rank(caller) <
role_rank(min_role)` gate compare `0 < -1` — always false, serving a
meant-to-be-restricted panel to anyone.

`min_role` gates the **web** endpoint only — `GET /v1/panels/{name}`
(`hivepilot/services/api_service.py`) enforces it after resolving the panel
(the required role is data-dependent, so it can't be a static
`Depends(require_role(...))`); a token whose role ranks below `min_role`
gets `403`. The TUI dashboard runs in-process with no separate token/role
check, so `min_role` has no effect there — it's a web-only gate.

> **No automatic tenant scoping.** Unlike `/v1/analytics/*` and `/v1/runs`,
> panel data has **no** `tenant` concept at this layer — `fetch()` returns
> whatever the plugin computes, entirely unfiltered, and `min_role` is the
> **only** access control this endpoint applies. If your panel could expose
> cross-tenant or otherwise sensitive data, it is **your** responsibility as
> the panel author to filter it yourself and/or raise `min_role` (e.g.
> `"admin"`) for anything sensitive — there is no framework-level guardrail
> beneath `min_role`, unlike the Mem0 tab's `admin` gate (see
> `docs/v4/WEBUI.md`).

**Collision & routing.** Panel `name`s are collected into
`PluginManager.panels` (an instance dict, scoped to the manager like
`PluginManager.health`) the same way health checks are: a `name` that
collides with an already-registered panel (built-in or another plugin's) is
a hard stop (`PanelNameCollisionError`), rolling back this plugin's other
contributions atomically — see "Collision & error handling" below. A panel
plugin honors `plugins_enabled` / `plugins_disabled` exactly like every
other contribution type — a disabled plugin contributes no panels.

### Skills

A plugin may optionally contribute a **skill** — a named bundle of files (and
an optional appended system-prompt snippet) that a runner MAY apply to its
own invocation, e.g. writing reference material an agent runner reads before
acting. Unlike a runner/notifier/secrets backend, a skill has **no runtime
behavior of its own** — it is pure declarative content; whether and how it
does anything depends entirely on the runner that consumes it.

`register()["skills"]` is `list[SkillSpec]`, where each `SkillSpec` is a
plain dict (`hivepilot/plugins.py`):

```python
class SkillSpec(TypedDict, total=False):
    name: str                  # required — stable id, collision-checked
    description: str           # required — human-readable summary
    provider: str               # required — the contributing plugin's identity
    files: dict[str, str]       # required — rel-path under .claude/skills/<name>/ -> content
    system_prompt: str          # optional — text a runner may append to its prompt
    applies_to: list[str]       # optional — runner kinds this skill targets; absent = any
    min_role: str                # optional — token_service role gate; absent = ungated/public
```

```python
# plugins/sample_skill.py
def register():
    return {
        "skills": [
            {
                "name": "sample-skill",
                "description": "Trivial example skill demonstrating the SkillSpec contract.",
                "provider": "sample_skill",
                "files": {"SKILL.md": "# Sample Skill\n\n..."},
            }
        ]
    }
```

**Attaching a skill to a task step or pipeline stage.** `TaskStep.skills` /
`PipelineStage.skills` (`hivepilot/models.py`) is an optional, ordered,
deduped `list[str]` of skill names — absent (`None`) by default, so a config
that never references `skills` behaves byte-identically to before this
feature existed. Declare it directly in `tasks.yaml` / `pipelines.yaml`, or
manage it via `hivepilot stage attach-skill` / `hivepilot stage detach-skill`
(see "Attaching skills to a pipeline stage" in `docs/v4/CONFIG.md`).

**Fail-closed per-stage/step selection.** Every `skills:` reference is
cross-checked by `hivepilot config validate`
(`hivepilot.services.config_validation.validate_config`) against the live
skill catalog (`PluginManager.list_skills()`):

- A name that doesn't match any registered skill is a **hard validation
  error** ("references unknown skill '\<name\>'") — never silently ignored.
- When a skill declares `min_role`, the referencing step/stage's resolved
  role (the owning task's `role:`) must satisfy it
  (`token_service.role_rank`), exactly like `PanelSpec.min_role`'s
  fail-closed comparison — an unrecognized role on **either** side of the
  comparison (rank `-1`) is always a denial, never a silent pass. A skill
  with no `min_role` is intentionally public/ungated.
- This check is dormant (no `PluginManager()` construction, zero added cost)
  for any config that never references `skills` anywhere — see
  `test_no_skills_config_is_byte_identical_to_pre_sprint3_behavior`.

**How a runner applies a skill.** A runner class OPTIONALLY implements
`apply_skill(self, payload: RunnerPayload, skills: list[SkillSpec]) ->
RunnerPayload` (`hivepilot.runners.base.BaseRunner` — structural, not part
of the `Protocol`'s required surface, exactly like `capture()` /
`is_destructive()`). Callers dispatch through the single choke point
`apply_skill_if_supported(runner, payload, skills)`, which returns *payload*
unchanged when the runner doesn't implement `apply_skill` — **a runner
without skill support silently ignores every skill it is handed**, it never
errors. `ClaudeRunner.apply_skill` is the reference implementation:

- Skips any skill whose `applies_to` is set and does not include this
  runner's `definition.kind` (logged at info, not an error — a routing
  filter, not a validation failure).
- Materialises each applicable skill's `files` into a FRESH, EPHEMERAL
  scratch directory (`tempfile.mkdtemp()`) under
  `<scratch>/.claude/skills/<name>/<relpath>` — **never** the target
  repo's own real `.claude/skills/` directory, which is never written to.
  The scratch directory is removed in a `finally` block once the step's
  subprocess call completes (success or exception) — it never outlives the
  step, and is removed immediately if materialisation itself fails partway
  through (never left orphaned with partially-resolved secret content).
- Routes both `files` content and `system_prompt` through the EXISTING
  `${secret:NAME}` resolution + masking choke point
  (`hivepilot.services.secret_refs.resolve_secret_refs` — the same one
  `Orchestrator._resolve_secrets` uses) before anything reaches disk, the
  appended prompt, or a log line — a skill can safely reference a project's
  named secret catalog without ever leaking the resolved value raw.
- Never mutates the caller's `payload` in place — always returns a new
  `RunnerPayload` (immutable-update pattern, same discipline as everywhere
  else in this codebase).

**Collision & routing.** Skill `name`s are collected into
`PluginManager.skills` (an instance dict, scoped to the manager exactly like
`PluginManager.panels` / `PluginManager.health`): a `name` that collides
with an already-registered skill (built-in — there are none — or another
plugin's) is a hard stop (`SkillNameCollisionError`), and an invalid
`min_role` is a hard stop (`SkillInvalidMinRoleError`) — both roll back this
plugin's other contributions atomically, see "Collision & error handling"
below. A skill plugin honors `plugins_enabled` / `plugins_disabled` exactly
like every other contribution type — a disabled plugin contributes no
skills, and disappears from `hivepilot skills list`.

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

**Health check** — `register()["health"]["rtk"]` reports `ok` when
`shutil.which("rtk")` finds the binary, `degraded` ("rtk not on PATH — falls
back to raw execution") otherwise; never `error` (a missing `rtk` binary is
graceful degradation, not a failure — see above).

### Example: the `herdr` runner (`plugins/herdr.py`)

Ships in this repo as a reference runner plugin (not a built-in — it's a
local-file plugin, same trust tier as anything else in `plugins/`). It
executes each pipeline step **inside a dedicated pane** of
[`herdr`](https://github.com/ogulcancelik/herdr) — a terminal multiplexer
built for coding agents (workspaces -> tabs -> panes, agent-status
detection, opaque hierarchy ids) — giving you live parallel-pane visibility
of a running pipeline while letting HivePilot drive herdr's CLI.

`HerdrRunner` renders the step's `command` (or the runner definition's
`command`) template exactly like the built-in `shell` runner, then:

- If `herdr` is found on `PATH` (`shutil.which("herdr")`), it drives the CLI:
  1. `herdr pane split --current --direction <herdr_split_direction> --no-focus`
     — the returned pane id is **parsed from the JSON stdout**, never
     hand-built (hierarchy ids are opaque per herdr's own docs).
  2. `herdr pane run <pane-id> "<rendered command>"`.
  3. `herdr wait agent-status <pane-id> --status idle --timeout <herdr_wait_timeout_ms>`
     — any non-`idle` outcome (blocked, unknown, or a timeout) is treated as
     a step **failure**, fail-closed; it never silently succeeds.
  4. `herdr pane read <pane-id> --source recent-unwrapped --lines <herdr_read_lines>`
     — the pane's captured output becomes the step's result (surfaced in the
     interaction log / live stream, same as the built-in `claude` runner's
     captured stdout).
- If `herdr` is **not** installed, it logs an INFO message
  (`herdr_runner.herdr_not_found`) and falls back to running
  `bash -lc "<rendered command>"` directly — the step still executes, it
  just doesn't get a dedicated pane. A step never fails just because
  `herdr` isn't on the host.

**Env / secrets into the pane:** `herdr pane run` executes in the pane's own
shell, which does not automatically inherit the runner's env overlay
(`project.env` + runner `env` + resolved secrets). Instead of putting values
on the command line (which would leak into `ps`/`/proc/<pid>/cmdline` for
the lifetime of the `herdr` CLI invocation), the overlay is written to a
private (mode `0600`) temp file as `export KEY=value` lines, and the pane
command is prefixed with `set -a; source <path>; set +a; ` — only the file
*path*, never a secret value, ever appears on an argv. The file is deleted
immediately after the step completes.

**Already inside herdr (`HERDR_ENV=1`):** this runner does not special-case
running from inside a herdr-managed pane — it always splits a fresh pane per
step, keeping each step's output cleanly isolated for `pane read` to capture
accurately.

Config (env `HIVEPILOT_HERDR_*`, all optional):

| Setting | Default | Meaning |
| --- | --- | --- |
| `herdr_wait_timeout_ms` | `300000` | Timeout for `wait agent-status --status idle` (5 min) |
| `herdr_read_lines` | `200` | Lines captured by `pane read --lines` |
| `herdr_split_direction` | `"right"` | Direction passed to `pane split --direction` |

Point a role or runner definition at it the same way you'd point at any
other kind — set `kind: herdr` on a `RunnerDefinition` and give the step
(or the runner definition) a `command`:

```yaml
# roles.yaml
runners:
  parallel-tests:
    kind: herdr
    command: "pytest -q"
```

```yaml
# tasks.yaml
steps:
  - name: run-tests
    runner: parallel-tests
```

Any step assigned to a runner of kind `herdr` gets its command executed in a
dedicated herdr pane automatically, with the same-directory graceful
fallback described above.

### Example: the `hugo` runner (`plugins/hugo.py`)

Ships in this repo as a reference runner plugin (not a built-in — it's a
local-file plugin, same trust tier as anything else in `plugins/`). Opt-in by
default (`hugo_enabled`, default `True`, env `HIVEPILOT_HUGO_ENABLED`) and
PATH-gated at run time (`shutil.which("hugo")`), it wraps the
[Hugo](https://gohugo.io) static-site-generator CLI as a first-class
`kind: "hugo"` runner: `new` / `build` / `serve`. Non-destructive — every
operation only touches local files (rendered site output, new content
scaffolding) or starts a local dev server; deploying the generated site
stays with whatever `GitActions`/`git push` step already handles it.

`HugoRunner` resolves the operation exactly the same way the IaC/Helm
runners do (`hivepilot.runners.iac_runner`/`helm_runner`) — a single
`_resolve_operation` is the source of truth: `payload.step.command` wins,
falling back to the runner definition's `command`, falling back to
`options.operation`, defaulting to `"build"`.

```yaml
# roles.yaml
runners:
  site-build:
    kind: hugo
    options:
      operation: build
      minify: true
      destination: public
      base_url: "https://example.com"
      environment: production
```

```yaml
# tasks.yaml
steps:
  - name: build-site
    runner: site-build
```

| Operation | Command | Notes |
| --- | --- | --- |
| `build` (default) | `hugo --minify` | `--minify` is added unless `options.minify` is explicitly `false`. Optional `--destination <options.destination>`, `--baseURL <options.base_url>`, `--environment <options.environment>` when those option keys are present. |
| `new` | `hugo new <options.path>` | Requires `options.path` (content path, e.g. `posts/my-post.md`) — a missing/empty path raises `ValueError`. Optional `--kind <options.archetype>`. |
| `serve` | `hugo serve` | Optional `--bind <options.bind>`, `--port <options.port>`. **Blocks** — starts a long-running local dev server; intended for local/dev use, not one-shot automation. |

An unrecognized operation raises `ValueError` (fail-closed) rather than
silently falling back to `build`.

**Health check** — `register()["health"]["hugo"]` reports `ok` when
`shutil.which("hugo")` finds the binary, `error` ("hugo not on PATH —
install Hugo to use this runner") otherwise — unlike `rtk`/`herdr` above,
this runner has no raw-command fallback, so a missing `hugo` binary means
the runner cannot execute at all.

Disable it the same way as any other bundled plugin: `HIVEPILOT_HUGO_ENABLED=false`,
or add `"hugo"` to `HIVEPILOT_PLUGINS_DISABLED`.

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

**Health check** — `register()["health"]["headroom"]` reports `error` when
`headroom-ai` isn't importable, `degraded` ("installed but disabled") when
importable but `headroom_enabled` is `False` (the default, dormant steady
state), `ok` when importable and enabled.

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

**Typed PROVENANCE metadata (Sprint 1 of the mem0-typed-and-plugin-health
spec).** `store` attaches a structured `metadata` dict to `client.add(...)`
(mem0's `add()` accepts per-memory `metadata` on both the hosted
`MemoryClient` and self-host `Memory` clients), so persisted memories are
typed/filterable — inspired by a memory-dashboard view. Built by
`_provenance_metadata()` in `plugins/mem0.py`, **real values only, no
fabrication:**

| Key          | Source                                                        | Included when                          |
| ------------ | -------------------------------------------------------------- | --------------------------------------- |
| `source`     | always `"hivepilot"`                                          | always                                  |
| `project`    | `payload.project_name`                                         | always                                  |
| `task`       | `payload.task_name`                                            | always                                  |
| `role`       | the `role` kwarg (threaded by `Orchestrator._execute_task`)    | when supplied                           |
| `step`       | `payload.step.name`                                            | when set (effectively always)           |
| `category`   | `payload.step.metadata.get("memory_category")`                 | always — defaults to `"run"`            |
| `ts`         | `datetime.now(timezone.utc).isoformat()` at store time         | always                                  |
| `run_id`     | —                                                               | **never** — not threaded into the `after_step` `run_hook(...)` call today; omitted rather than forcing an orchestrator signature change this sprint (follow-up) |
| `confidence` | —                                                               | **never** — no genuine signal exists for it; deliberately not fabricated |

This is the same `client.add(...)` call `store` already makes (still skipped
entirely when there's no salient content beyond bare task identity) — no new
mem0 calls, just a richer payload on the existing one.

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
> content. **The structured PROVENANCE `metadata` dict (`source`/`project`/
> `task`/`role`/`step`/`category`/`ts` — see above) is sent alongside it on
> the same `client.add(...)` call** — lower-risk than `output` (it's
> identity/timing data, not agent-generated content), but it IS still
> off-machine data about your project/task names and role. Do NOT enable
> hosted mode on projects where step output OR project/task naming may
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

**Health check** — `register()["health"]["mem0"]` reports `error` when
`mem0ai` isn't importable, `degraded` ("installed but disabled") when
importable but `mem0_enabled` is `False`, `error` when enabled but
`_get_client()` can't build a client, otherwise `ok` with a `detail` of
`"hosted mode configured"` or `"self-host"` — **never** the API key/token
itself (see "Health checks" above and the data-egress warning above — the
same no-leak discipline applies to health details as to everything else this
plugin sends off-machine).

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

**Health check** — `register()["health"]["obsidian"]` reports `ok` when
`settings.obsidian_vault` is set (differs from its field default,
`Path("obsidian-vault")`) AND exists on disk; `error` when it's set but the
path is missing; `degraded` ("not configured") when it's still the field
default. Only the path's existence is reported, never its contents.

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

### Example: the `sample_skill` plugin (`plugins/sample_skill.py`)

Ships in this repo as the reference **skill** plugin — the minimal
`register()["skills"]` shape (see "Skills" above): `name`, `description`,
`provider`, and a single `files` entry (`SKILL.md`). It declares no
`system_prompt`, `applies_to`, or `min_role` — a fully public, ungated
skill any runner with `apply_skill` support may apply.

```python
# plugins/sample_skill.py
def register():
    return {
        "skills": [
            {
                "name": "sample-skill",
                "description": "Trivial example skill demonstrating the SkillSpec contract.",
                "provider": "sample_skill",
                "files": {"SKILL.md": "# Sample Skill\n\n..."},
            }
        ]
    }
```

Like `plugins/sample.py` (the panel/hook reference plugin), enable/disable is
handled ENTIRELY by the central plugin gate — `settings.plugins_enabled` /
`settings.plugins_disabled` (keyed off the file stem `sample_skill`) — it
declares no per-plugin `sample_skill_enabled` setting of its own.

Built as a plain **dict literal**, never a local `@dataclass`: `SkillSpec` is
a `TypedDict` (a type-checking-only construct — a plain dict at runtime), and
local-file plugins are exec'd via `importlib.util.spec_from_file_location()`
(`hivepilot.plugins._scan_local_plugins`), which never registers the module
in `sys.modules` — combined with `from __future__ import annotations`, a
local `@dataclass` on that load path trips a real CPython 3.14 `dataclasses`
bug (see "Example: the `rtk` runner" above for the full write-up). A dict
literal sidesteps it entirely — the same discipline every contribution type
in this repo's example plugins follows.

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

- **Kind/name collision** — if a plugin declares a `runners`, `notifiers`,
  `secrets`, `health`, `panels`, or `skills` key whose name is already
  registered to a *different* implementation, that raises
  (`RunnerKindCollisionError` / `NotifierKindCollisionError` /
  `SecretsBackendCollisionError` / `HealthNameCollisionError` /
  `PanelNameCollisionError` / `SkillNameCollisionError`) and **aborts
  loading**. This is a hard stop by design: silently shadowing a built-in
  (e.g. redefining `claude`, or a secrets backend named `vault`) — or
  silently shadowing another plugin's health check, panel, or skill — is
  never the right default. A `panels` entry has one more failure mode:
  `PanelInvalidMinRoleError` when its `min_role` isn't a recognized role
  (see "Panels (Mirador)" above); a `skills` entry mirrors that with
  `SkillInvalidMinRoleError` (see "Skills" above) — both hard, fail-closed
  stops. Registration of a single plugin's
  runners+notifiers+secrets+health+panels+skills is atomic: if any entry
  collides (or fails `min_role` validation), every entry that plugin already
  added (to the process-global maps, or to `PluginManager.health` /
  `PluginManager.panels` / `PluginManager.skills`) in this same load is
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

Prints six tables:

- **Loaded Plugins** — every successfully-loaded `PluginRecord`: `name`,
  `source` (`local-file` | `entry-point`), `location`.
- **Agent Runners** — the coding-agent taxonomy from "Agent runner taxonomy"
  above, sourced live from the registry: every built-in agent kind
  (`claude`/`codex`/`vibe` tagged `built-in`+`active`, `openrouter` tagged
  `built-in`+`API-only`), plus every plugin agent kind
  (`gemini`/`opencode`/`ollama`/`pi`/`qwen-code`/`kimi-cli`) tagged `plugin`
  and `active` (currently in `RUNNER_MAP` — flag on + binary on `PATH`) or
  `inactive` (flag off, or binary absent), each with its `HIVEPILOT_<NAME>_
  ENABLED` enable-flag env var so an inactive row is immediately actionable.
- **Other Runner Kinds** — every remaining (non-agent) kind currently in
  `RUNNER_MAP` — `shell`, `langchain`, `internal`, `container`, `cursor`, the
  IaC runners, etc. — labeled `built-in` or `plugin` by membership in
  `KNOWN_RUNNER_KINDS`.
- **Notifiers** — every notifier currently in `NOTIFIER_MAP`, labeled
  `built-in` or `plugin` by membership in `{slack, discord, telegram}`.
- **Secrets Backends** — every backend currently in `SECRETS_MAP`, labeled
  `built-in` or `plugin` by membership in `KNOWN_SECRET_BACKENDS`
  (`{env, file, vault, sops}`).
- **Health** — every registered health check name -> a colored status badge
  (green `ok` / yellow `degraded` / red `error`) + its one-line `detail`,
  sourced from `PluginManager.check_all()` (never-raise — see "Health
  checks" above). Empty (no plugin declares `health`) shows a `-` placeholder
  row, same convention as an empty **Loaded Plugins** table.

This is a v1 inventory, not a full join — it does not attribute which
specific runner kind or notifier came from which loaded plugin beyond what a
`PluginRecord` itself records. If a plugin contributes a runner kind or hook
and it doesn't show up as expected, check the process log for
`plugins.load_failed` / `plugins.register_failed` first.

### `skills list`

```bash
hivepilot skills list
```

Prints a single **Skills** table, sourced from `PluginManager.list_skills()`
(sorted by name): `name`, `description`, `provider` (the contributing
plugin's identity string, not necessarily the same as the loaded plugin's
`PluginRecord.name`), and `applies_to` — the comma-joined runner-kind list a
skill declares, or `any` when it doesn't restrict which runner kinds it
targets. Empty (no plugin declares `skills`) shows a `-` placeholder row,
same convention as an empty **Loaded Plugins** / **Health** table. A skill
contributed by a plugin listed in `plugins_disabled` never appears here —
see "Skills" above.

### `plugins health`

```bash
hivepilot plugins health
```

Prints only the Health table (same data/format as the one in `plugins list`)
and, unlike `plugins list` (which always exits `0`), **exits non-zero if any
check reports `error`** — a focused command for monitoring/CI use, e.g. a
periodic job that pages when a configured backend (mem0, a secrets provider,
obsidian) stops being reachable.

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

**Health (Sprint 2 of the plugin-health spec)** — the details pane also
shows a `Health: <status> — <detail>` line for the highlighted plugin, when a
health check is registered under the SAME name as the plugin (the convention
the example plugins below follow, e.g. `rtk`'s health check is named `rtk`).
Sourced from the same `PluginManager.check_all()` used by `plugins list` /
`plugins health` — read-only, no toggle here.

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
