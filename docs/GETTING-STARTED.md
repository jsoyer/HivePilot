# Getting Started

This guide takes you from a clean environment to a first successful HivePilot pipeline run. You'll install the package, verify your environment, register a project, run a simulated pipeline, then a real one, and see how the approval flow gates anything destructive.

## Install

```bash
pip install hivepilot
```

Requires Python >=3.10.

Optional extras:

```bash
pip install "hivepilot[langchain]"       # LangChain integration
pip install "hivepilot[notifications]"   # Telegram notifications
pip install "hivepilot[full]"            # langchain + torch — large download
```

For development (editable install with all extras):

```bash
pip install -e ".[full]"
```

## Verify your environment

Run diagnostics before doing anything else:

```bash
hivepilot doctor
```

`doctor` checks config paths, external binaries (`git`, `gh`), agent CLIs, optional dependencies, config repo status, and Telegram configuration if enabled.

Check which agent CLIs are available on this host:

```bash
hivepilot agents list
```

To install a missing agent CLI, use the guided installer:

```bash
hivepilot agents install <name>
```

`agents install` modifies your host (installs a binary). Treat it as an action requiring your explicit consent — review what it does before running it, especially on a shared or production machine.

## Initialize a workspace

Scaffold the config files HivePilot needs:

```bash
hivepilot init
```

This creates `projects.yaml`, `tasks.yaml`, `roles.yaml`, `pipelines.yaml`, `policies.yaml`, and related config files. For starter examples instead of blank scaffolds, use:

```bash
hivepilot init-template
```

After editing config, validate it:

```bash
hivepilot validate
```

Config files are resolved in this order: `$XDG_CONFIG_HOME/hivepilot/<file>` → a configured config repo → the current directory. Environment variables use the `HIVEPILOT_` prefix and are read from a `.env` file — see `.env.example` at the repo root for the full list (roughly 166 variables).

## Register a project

Add a project interactively:

```bash
hivepilot project add
```

Or hand-edit `projects.yaml` directly:

```yaml
projects:
  acme-api:
    path: ~/dev/acme-api
    default_branch: main
    owner_repo: your-org/acme-api
```

Confirm it's registered:

```bash
hivepilot list-projects
```

If you don't already have `projects.yaml` entries for your repos, scan for them instead of writing by hand:

```bash
hivepilot discover
```

`discover` scans local directories (or a GitHub org) and prints project stanzas you can paste into `projects.yaml`.

## Your first run (safe)

HivePilot gives you two independent safety layers before anything real happens: `run --simulate` and `run-pipeline`'s dry-run default.

First, see what's available:

```bash
hivepilot list-tasks
hivepilot list-pipelines
```

Preview a single task with no real agent calls:

```bash
hivepilot run acme-api my-task --simulate
```

`--simulate` on `run` is a preview — it never invokes a real agent.

Run a pipeline. `run-pipeline` defaults to `--dry-run`, so this is safe by default:

```bash
hivepilot run-pipeline acme-api default
```

When you're ready to execute for real, pass `--no-dry-run` explicitly:

```bash
hivepilot run-pipeline acme-api default --no-dry-run
```

Note the distinction: `run`'s safety flag is `--simulate` (opt-in preview); `run-pipeline`'s safety flag is dry-run, which is *on by default* and must be explicitly turned off with `--no-dry-run`.

## The approval flow

HivePilot can pause a run and wait for a human decision at three levels:

- **Policy-level**: a policy sets `require_approval`, gating matching runs before they start.
- **Stage-level**: a pipeline stage sets `pause_before: true`, stopping at a plan checkpoint before the stage executes.
- **Step-level**: an individual step sets `require_approval: true`.

In addition, destructive operations (for example infrastructure applies) are auto-gated regardless of the above — they always require approval.

When a run pauses, list pending approvals and act on them:

```bash
hivepilot approvals list
hivepilot approvals approve <id>
hivepilot approvals deny <id>
```

See `SECURITY.md` for the full approval and gating model.

## Watch it run

Every run is recorded to a SQLite state store, and each run also writes `runs/<timestamp>/summary.json` plus structured JSON logs.

To watch runs live or review them after the fact, use the Mirador dashboard:

```bash
hivepilot dashboard
```

The TUI dashboard requires `HIVEPILOT_ENABLE_TEXTUAL_UI=1`.

For the web command center instead, start the API server:

```bash
hivepilot api serve
```

See `DASHBOARD.md` for details on both. You can also control and monitor runs remotely from Telegram — see `INTEGRATIONS.md`.

## Next steps

- `CONFIGURATION.md` — full config file reference and resolution order
- `PIPELINES-AND-ROLES.md` — defining pipelines, stages, roles, and models
- `CLI-REFERENCE.md` — complete command reference
- `SECURITY.md` — approval gates, policies, and destructive-operation handling
