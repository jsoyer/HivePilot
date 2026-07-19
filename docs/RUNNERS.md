# Runners

A runner executes one step of a task. The `RunnerRegistry` dispatches by `kind` to a
`BaseRunner` subclass. A runner is referenced from a task step (`runner` / `runner_ref`)
or defined once in `tasks.yaml` under `runners:` as a `RunnerDefinition`:

```yaml
runners:
  cto:
    name: cto
    kind: claude
    command: claude
    model: claude-opus-4
    effort: high
    agent: cto
    append_prompt: "Focus on architecture and risk."
    timeout_seconds: 900
    host: local
    env:
      ANTHROPIC_API_KEY: ${secret:ANTHROPIC_API_KEY}
    options:
      permission_mode: bypassPermissions
```

Fields: `name`, `kind`, `command`, `model`, `effort`, `agent`, `append_prompt`,
`timeout_seconds`, `host`, `env`, `options`.

## Agent runners

Coding-agent CLIs that execute a role's prompt.

### Built-in kinds

Registered at import time — no PATH check required. Each can be opted out via
`HIVEPILOT_<KIND>_ENABLED=false`:

- `claude`
- `vibe`
- `openrouter` — **API-only**: `supported_modes == {"api"}`, no CLI binary exists for it.

### PATH-gated plugin agent kinds

Registered only if the opt-out flag is true **and** the binary is found on `PATH`
(via `shutil.which`). `codex` and `cursor` are default-on agents delivered as
PATH-gated plugins (not compiled-in built-ins):

| kind         | binary | flag                     |
| ------------ | ------ | ------------------------ |
| `gemini`     | gemini | `gemini_enabled`         |
| `opencode`   | opencode | `opencode_enabled`     |
| `ollama`     | ollama | `ollama_enabled`         |
| `pi`         | pi     | `pi_enabled`             |
| `qwen-code`  | qwen   | `qwen_code_enabled`      |
| `kimi-cli`   | kimi   | `kimi_cli_enabled`       |
| `antigravity`| agy    | `antigravity_enabled`    |
| `codex`      | codex  | `codex_enabled`          |
| `cursor`     | cursor-agent | `cursor_enabled`   |

An inactive kind raises an actionable `RunnerPluginUnavailableError` naming the flag
and the missing binary — never a bare `KeyError`.

```bash
hivepilot agents list              # show availability per kind
hivepilot agents install <name>    # guided, host-modifying install
```

See [PLUGINS.md](PLUGINS.md) for the plugin model these kinds are registered under.

## Task engines

`TaskConfig.engine` selects the execution engine for a task:

- `native` (default) — no extra dependencies, always available
- `langgraph` — pulls the LangGraph optional extra
- `crewai` — pulls the CrewAI optional extra

Optional engines are pulled in as extras so the core install stays lightweight.
Shell and LangChain runner kinds also exist for non-agent steps (running commands or
LangChain chains directly, without going through an agent CLI).

## CLI vs API mode

Every CLI-capable runner can flip to API mode. `mode` is `cli` (default) or `api`, and
can be set on a pipeline or overridden per stage:

```yaml
mode: cli   # pipeline-level default

stages:
  - name: review
    mode: api   # stage-level override
```

Resolution order: `stage.mode or pipeline.mode or "cli"`.

In API mode, API-capable runners (e.g. `claude`, or a prompt-cli style runner) call the
provider's HTTP API directly instead of invoking the CLI binary. Headless CLI agents may
still need an explicit permission mode to run non-interactively (e.g. claude's
`--permission-mode`) — see [CONFIGURATION.md](CONFIGURATION.md).

## Reasoning effort

One closed enum: `low | medium | high | xhigh | max`. Settable on `RunnerDefinition`,
`TaskStep`, `PipelineStage`, `PipelineConfig`, or `Role`. Resolution order:

```
policy > stage > role > runner-default
```

The resolved value maps to provider-native knobs — e.g. Claude's thinking-token budget,
Codex's effort flag. See [PIPELINES-AND-ROLES.md](PIPELINES-AND-ROLES.md) for how
roles and pipeline stages set effort.

## Infrastructure runners

HivePilot ships infra runner kinds for IaC and Kubernetes operations.

### IaC (terraform / opentofu / pulumi)

Driven via `hivepilot iac` commands:

```bash
hivepilot iac plan       # read-only
hivepilot iac apply      # DESTRUCTIVE
hivepilot iac destroy    # DESTRUCTIVE
hivepilot iac drift      # read-only
hivepilot iac output     # read-only
hivepilot iac cost       # read-only estimate
```

`apply` and `destroy` are destructive operations and are auto-gated for human approval
— see [SECURITY.md](SECURITY.md).

### kubectl runner

A runner kind for Kubernetes operations. Destructive operations go through the same
auto-gating as IaC, and secret values used by the runner are masked in all sinks. Exact
operation and option fields live in [CONFIGURATION.md](CONFIGURATION.md); the approval
gate is described in [SECURITY.md](SECURITY.md).

### Drift detection

```bash
hivepilot drift scan     # run a drift scan
hivepilot drift status   # current drift status
hivepilot drift report   # detailed report
```

Scheduled scans and gated auto-remediation exist, with a fail-closed preflight check
before any remediation runs. See [DEPLOYMENT.md](DEPLOYMENT.md).

## Infra/utility plugin runners

Several non-agent runner kinds ship as plugins:

- `rtk` — shell-fallback runner
- `herdr` — terminal multiplexer runner
- `hugo` — static-site runner (PATH-gated)
- `tmux` — execution wrapper runner
- `gh` — command-based GitHub CLI runner (PATH-gated)

Most default on and are flag-gated like the agent plugin kinds above; `hugo` and `gh`
are additionally PATH-gated. See [PLUGINS.md](PLUGINS.md) for the full plugin
inventory and trust model.

## See also

- [CONFIGURATION.md](CONFIGURATION.md)
- [PLUGINS.md](PLUGINS.md)
- [PIPELINES-AND-ROLES.md](PIPELINES-AND-ROLES.md)
- [SECURITY.md](SECURITY.md)
