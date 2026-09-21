---
title: Per-project / per-tenant Obsidian vault routing
type: adr
status: accepted
created: 2026-09-20
agent: hivepilot
language: en
linear: HP-121
---

# Per-project / per-tenant Obsidian vault routing

## Status:

accepted

CoS recommendation: an explicit **mapping table** (`project_id` / `tenant` →
named vault), not an opaque plugin. Fail-closed when the lookup is unmapped or
ambiguous. No silent fallback to the global `HIVEPILOT_OBSIDIAN_VAULT`.

## Context:

`Settings.obsidian_vault` (`HIVEPILOT_OBSIDIAN_VAULT`) is a single machine-wide
path. The per-project-vault PRD later added `ProjectConfig.obsidian_vault` as
an opt-in override, still inheriting the global path when the key is absent.
On a host that runs HivePilot's own work next to Noxys pipelines, that
inheritance is a cross-tenant write: unmapped projects land in whichever vault
the box happened to set globally.

Jerome's split is:

- HivePilot work → the Jsoyer vault
- Noxys pipelines → the Noxys vault

The engine already treats a *pipeline* as the unit of configuration. Vault
destination belongs to that same unit (project / tenant), not to the
deployment. HP-126–128 and the Telegram door cutover are out of scope.

Canonical vault **identities** (paths stay operator-local config; no Mac home
directories in the engine):

| Vault id | Identity |
| --- | --- |
| `jsoyer` | [github.com/jsoyer/obsidian-vault](https://github.com/jsoyer/obsidian-vault). Path: `vaults.jsoyer.path` or `HIVEPILOT_VAULT_JSOYER`. |
| `noxys` | No published in-repo filesystem path. In-tree comments refer to a measured directory named `noxys-obsidian-vault` only. Path: `vaults.noxys.path` or `HIVEPILOT_VAULT_NOXYS` — do not invent a default. |

## Options:

- **Global only** (`HIVEPILOT_OBSIDIAN_VAULT`). Status quo for a single-vault
  box. Rejected for multi-tenant: HivePilot notes and Noxys notes share one
  tree.
- **Per-project override with global fallback** (today's
  `ProjectConfig.obsidian_vault`). Works when every project opts in. An
  unmapped project still inherits the global vault — silent cross-write.
- **Opaque plugin** that "knows" Jsoyer vs Noxys. Rejected by CoS: routing is
  a config decision, not plugin behaviour. A plugin can drift from writers
  (`ObsidianService`, debate ADRs, `{OBSIDIAN_VAULT}`) and cannot be linted as
  a table.
- **Mapping table** `project_id` / `tenant` → named vault, fail-closed on
  unmapped or ambiguous. CoS recommendation.

## Decision:

**Ship a dedicated `vault_routes.yaml` mapping table.** The Obsidian plugin
does not own routing. Every writer and the plugin `recall` / `store` path
still resolve through `obsidian_vault_resolver`.

File shape (same XDG → config_repo → `base_dir` chain as `vault.yaml`):

```yaml
vaults:
  jsoyer:
    repo: https://github.com/jsoyer/obsidian-vault
    path: ~/vaults/jsoyer          # operator-local; ~ expanded
  noxys:
    path: ~/vaults/noxys           # operator-local; no engine default

by_project:
  hivepilot: jsoyer
by_tenant:
  jsoyer: jsoyer
  noxys: noxys
```

Rules:

1. **Table inactive** when the file is missing or both `by_project` and
   `by_tenant` are empty. Pre-HP-121 behaviour is unchanged (per-project
   override, then global fallback). OSS example projects keep working.
2. **Table active** when any `by_project` or `by_tenant` route exists.
   Resolution collects candidates from the project key, the tenant key, and
   an explicit `ProjectConfig.obsidian_vault`. Zero candidates →
   `VaultResolutionError` (unmapped). Two or more distinct paths →
   `VaultResolutionError` (ambiguous). The global setting is **not**
   consulted.
3. **Named vault path** comes from `vaults.<id>.path`, else
   `HIVEPILOT_VAULT_<ID>` for the canonical ids `jsoyer` and `noxys`. Missing
   path, relative path, or a path that is not an existing directory fails
   closed. HivePilot never creates a vault directory.
4. **Route values must name a declared vault.** An unknown vault id refuses
   to load. Empty / whitespace keys and values refuse to load.
5. **One run, one vault.** `resolve_vault_for_projects` still fails if the
   run's targets resolve to different vaults.

## Consequences:

Positive:

- Jsoyer vs Noxys isolation is a reviewed YAML table, not a plugin or a
  Mac path in Python.
- Unmapped and ambiguous lookups fail before any stage writes.
- Single-vault deployments that never add the file stay byte-identical.

Negative / follow-up:

- A copied example table with `by_project: hivepilot: jsoyer` makes every
  *other* project unmapped until it is listed. That is the fail-closed
  surprise; it is documented on the example file.
- Noxys has no in-repo canonical checkout path. Operators must set one.
- Telegram / HP-126–128 cutover is not this ticket.

## Security Impact:

- Fail closed: no silent global fallback while the table is active.
- Ambiguous project-vs-tenant disagreement refuses the write rather than
  picking a winner.
- Paths stay config-owned; the engine does not embed a home directory.
- Does not change partition outward consent, vault-write gates, or
  `INVARIANTS.md`.

## Review Date:

2026-10-20
