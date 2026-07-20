# CLI Reference

HivePilot ships one console script: `hivepilot` (entry point `hivepilot.cli:app`).

This page is the consolidated command reference — one table per command group. It
lists purpose and whether a command mutates state (config, local files, or an
external service). It does not enumerate every flag.

Run `hivepilot --help` for the full command tree, and `hivepilot <group> --help`
(or `hivepilot <cmd> --help` for ungrouped commands) for the authoritative list
of flags on any given command.

Commands marked **Mutating** change local state (config files, database,
project files) or an external system (GitHub, Slack, Telegram, a cloud
provider). Commands marked **DESTRUCTIVE** are a stronger subset — they delete
or irreversibly change something. Many mutating and destructive commands are
approval-gated: they pause for an approval before taking effect. See
[SECURITY.md](./SECURITY.md) for the approval-gate model.

## Root commands

These commands are ungrouped — invoke them directly as `hivepilot <cmd>`.

| Command | Purpose | Mutating? |
| --- | --- | --- |
| `list-projects` | List configured projects. | No |
| `discover` | Scan local directories or a GitHub org for candidate projects and print config stanzas for them. | No |
| `list-tasks` | List configured tasks. | No |
| `list-pipelines` | List configured pipelines. | No |
| `run` | Run a task against one project, many projects, or all projects. | **Yes** |
| `run-pipeline` | Run a full multi-stage pipeline, optionally fanning out across a project group. Defaults to `--dry-run` (safe); pass `--no-dry-run` to execute for real. | **Yes** (when `--no-dry-run`) |
| `interactive` | Guided REPL for building and running tasks/pipelines. | Depends on actions taken inside |
| `doctor` | Environment and installation diagnostics. | No |
| `dashboard` | Textual TUI showing recent runs. Requires `HIVEPILOT_ENABLE_TEXTUAL_UI`. | No |
| `lint` | Lint config files for errors/warnings. | No |
| `init` | Scaffold a new HivePilot workspace. | **Yes** |
| `init-template` / `templates` | Scaffold from, or list, workspace templates. | `init-template` yes; `templates` no |
| `debate` | Run a multi-agent debate (judge/arbiter adjudicated consensus). | **Yes** |
| `audit` | Run an audit pass (config/security/governance, depending on flags). | No |
| `groups` | List configured project groups. | No |
| `worker` / `workers` | Manage worker processes for async/queued runs. | **Yes** |
| `validate` | Validate config files against schema. | No |
| `reload` | Hot-reload roles/projects/tasks/pipelines in a running `api serve` process, without a restart (calls `POST /v1/admin/reload`). Requires an admin token. | **Yes** |

### `run`

Key flags: `--extra-prompt` / `-e` (append to the task prompt), `--auto-git`
(auto-commit/push on success), `--all` (all projects), `--project` / `-p`
(target one or more specific projects), `--concurrency` / `-c` (parallel run
count), `--simulate` (dry run without invoking the agent), `--token` (auth
token for the run). See `hivepilot run --help` for the complete set.

```bash
hivepilot run refactor-tests --project api-service --auto-git
hivepilot run security-audit --all --concurrency 4 --simulate
```

### `run-pipeline`

```bash
# Safe by default — plans the run but does not execute it
hivepilot run-pipeline release-pipeline --project api-service

# Executes for real
hivepilot run-pipeline release-pipeline --project api-service --no-dry-run
```

## `gh` — GitHub

| Command | Purpose | Mutating? |
| --- | --- | --- |
| `repo-init` | Create or link a GitHub repo, set the remote, push initial content. | **Yes** |
| `issue` | Create a GitHub issue. | **Yes** |
| `release` | Create a GitHub release. | **Yes** |

## `approvals`

| Command | Purpose | Mutating? |
| --- | --- | --- |
| `list` | List pending approval requests. | No |
| `approve <id>` | Approve a gated action, releasing it to proceed. | **Yes** |
| `deny <id>` | Deny a gated action. | **Yes** |

## `api`

| Command | Purpose | Mutating? |
| --- | --- | --- |
| `serve` | Launch the FastAPI server. Flags include `--host`, `--port`, `--workers`. Long-running. | No (serves; effects come from requests it handles) |
| `systemd-unit` | Print/generate a systemd unit file for the API server. | No (prints; writing the file is up to you) |

```bash
hivepilot api serve --host 0.0.0.0 --port 8080 --workers 2
```

## `schedule`

| Command | Purpose | Mutating? |
| --- | --- | --- |
| `daemon` | Run the scheduler daemon. Long-running. | No |
| `health` | Scheduler health check. | No |
| `systemd-unit` | Print/generate a systemd unit file for the scheduler. | No |
| `retry-list` | List jobs pending retry. | No |
| `dlq-list` | List jobs in the dead-letter queue. | No |
| `dlq-purge` | Purge the dead-letter queue. | **DESTRUCTIVE** |
| `list` | List scheduled jobs. | No |
| `run` | Trigger a scheduled job immediately. | **Yes** |

## `tokens`

| Command | Purpose | Mutating? |
| --- | --- | --- |
| `add` | Create an API token (returns secret material — handle carefully). | **Yes** |
| `list` | List tokens (hashes only, never secret values). | No |
| `rotate` | Rotate a token. | **Yes** |
| `remove` | Remove a token. | **DESTRUCTIVE** |

## `config` — config-repo GitOps

| Command | Purpose | Mutating? |
| --- | --- | --- |
| `sync` | Pull the config repo into the local workspace. | **Yes** (local) |
| `push` | Push local config changes to the remote config repo. | **Yes** (remote) |
| `status` | Show sync status between local and remote config. | No |
| `log` | Show config-repo change history. | No |
| `get` | Read a single config value. | No |
| `list` | List config keys/values. | No |

## `project`

| Command | Purpose | Mutating? |
| --- | --- | --- |
| `add` | Add a project to `projects.yaml`. | **Yes** |
| `rm` | Remove a project from `projects.yaml`. | **DESTRUCTIVE** |

## `task`

| Command | Purpose | Mutating? |
| --- | --- | --- |
| `set-role` | Assign a role to a task. | **Yes** |

## `role`

| Command | Purpose | Mutating? |
| --- | --- | --- |
| `wire` | Wire a role (runner/model binding) into config. | **Yes** |

## `stage`

| Command | Purpose | Mutating? |
| --- | --- | --- |
| `attach-skill` | Attach a plugin skill to a pipeline stage. | **Yes** |
| `detach-skill` | Detach a plugin skill from a pipeline stage. | **Yes** |

## `telegram`

| Command | Purpose | Mutating? |
| --- | --- | --- |
| `start` | Run the Telegram bot process. Long-running. | No |
| `chat-id` | Helper to discover a chat ID. | No |
| `systemd-unit` | Print/generate a systemd unit file for the bot. | No |
| `set-webhook` | Configure the Telegram webhook. | **Yes** (external) |
| `delete-webhook` | Remove the Telegram webhook. | **DESTRUCTIVE** (external) |
| `info` | Show bot/webhook info. | No |

## `caddy` — reverse proxy

| Command | Purpose | Mutating? |
| --- | --- | --- |
| `generate` | Write a Caddyfile. | **Yes** |
| `show` | Print the current/generated Caddyfile. | No |
| `setup` | Install/configure Caddy on the host. May need elevated privileges. | **Yes** |
| `reload` | Reload the live Caddy proxy. | **Yes** |
| `status` | Show proxy status. | No |
| `logs` | Show proxy logs. | No |
| `teardown` | Remove the Caddy setup. | **DESTRUCTIVE** |

## `slack`

| Command | Purpose | Mutating? |
| --- | --- | --- |
| `start` | Run the Slack bot process. Long-running. | No |
| `notify` | Send a Slack message. | **Yes** (external) |

## `discord`

| Command | Purpose | Mutating? |
| --- | --- | --- |
| `start` | Run the Discord bot process. Long-running. | No |
| `notify` | Send a Discord message. | **Yes** (external) |

## `linear`

| Command | Purpose | Mutating? |
| --- | --- | --- |
| `teams` | List Linear teams. | No |
| `issue` | Create or update a Linear issue. | **Yes** (external) |
| `states` | List workflow states. | No |
| `sync` | Sync HivePilot state with Linear. | **Yes** (external) |

## `iac` — Infrastructure as Code

| Command | Purpose | Mutating? |
| --- | --- | --- |
| `plan` | Show an infra change plan. | No |
| `apply` | Apply infra changes. Approval-gated. | **DESTRUCTIVE** |
| `destroy` | Tear down infra. Approval-gated. | **DESTRUCTIVE** |
| `drift` | Detect drift between declared and actual infra state. | No |
| `output` | Show infra outputs. | No |
| `cost` | Estimate infra cost. | No |

## `notion`

| Command | Purpose | Mutating? |
| --- | --- | --- |
| `status` | Show Notion integration status. | No |
| `setup` | Configure the Notion integration. | **Yes** |
| `sync` | Sync HivePilot state with Notion. | **Yes** (external) |

## `obsidian`

| Command | Purpose | Mutating? |
| --- | --- | --- |
| `audit` | Audit an Obsidian vault (read-only check). | No |

## `plugins`

| Command | Purpose | Mutating? |
| --- | --- | --- |
| `list` | Table of loaded plugins/runners/notifiers/secrets providers/health. | No |
| `health` | Health-check plugins; exits non-zero if any check errors (CI-friendly). | No |
| `tui` | Interactive plugin manager. Requires `HIVEPILOT_ENABLE_TEXTUAL_UI`. | Depends on actions taken inside |
| `search <query>` | Search the metadata-only plugin index. No code is fetched. | No |
| `info <name>` | Show index metadata for a plugin plus the exact pip/git command to install it yourself. | No |

## `skills`

| Command | Purpose | Mutating? |
| --- | --- | --- |
| `list` | List plugin-contributed skills. | No |

## `scan` — supply-chain security

| Command | Purpose | Mutating? |
| --- | --- | --- |
| `vulns` | Run a vulnerability scan. | No |
| `sbom` | Generate a Software Bill of Materials. Writes a file. | **Yes** (writes output) |

## `drift` — infrastructure drift

| Command | Purpose | Mutating? |
| --- | --- | --- |
| `scan` | Scan for infra drift. | No |
| `status` | Show current drift status. | No |
| `report` | Generate a drift report. Writes a file. | **Yes** (writes output) |

## `playbooks` — multi-agent collaboration templates

| Command | Purpose | Mutating? |
| --- | --- | --- |
| `list` | List available playbooks. | No |
| `show` | Show a playbook's contents. | No |
| `scaffold` | Scaffold files from a playbook. | **Yes** (writes output) |

## `agents`

| Command | Purpose | Mutating? |
| --- | --- | --- |
| `list` | List agent-CLI availability on the host. | No |
| `install <name>` | Guided, host-modifying install of an agent CLI binary. Treat as requiring explicit user consent. | **Yes** (writes to host) |

## `ownership` — agent file-ownership conflicts

Read-only manual check against an optional `ownership.yaml`. See [PIPELINES-AND-ROLES.md](./PIPELINES-AND-ROLES.md#agent-file-ownership-conflicts). Automatic merge-time enforcement is planned but not yet wired.

| Command | Purpose | Mutating? |
| --- | --- | --- |
| `check` | Report changed files owned by another role (`--role`), or advisory-list all changed files matched by any role's ownership globs (no `--role`). Exits `1` on a `--role`-scoped conflict. | No |

## Approval-gated & destructive commands

The following are the destructive commands called out above:
`iac apply`, `iac destroy`, `project rm`, `tokens remove`, `schedule dlq-purge`,
`caddy teardown`, `telegram delete-webhook`.

Destructive and many other mutating operations are subject to HivePilot's
approval-gate policy — a gated action pauses mid-run until released via
`hivepilot approvals approve <id>` (or denied via `hivepilot approvals deny <id>`).
See [SECURITY.md](./SECURITY.md) for how the gate is configured and which
actions it covers by default.

## See also

- [SECURITY.md](./SECURITY.md) — approval-gate model, secrets handling, fail-closed policies
- [USAGE.md](./GETTING-STARTED.md) — task/pipeline/config concepts and everyday workflows
- [PLUGINS.md](./PLUGINS.md) — plugin types (runner/notifier/secrets/panel/skill), install and trust model
