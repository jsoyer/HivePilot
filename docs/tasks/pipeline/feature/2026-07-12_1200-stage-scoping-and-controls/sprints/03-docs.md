# Sprint 3: Docs + groups.yaml example tags

> Self-contained. Load ONLY this file. Part of PRD A1.
> Repo: `/home/jeromesoyer/Documents/Github/jsoyer/HivePilot`.

## Objective

Document the three new `PipelineStage` fields and `groups.yaml` `tags`, and add an illustrative
`tags:` block to the example `groups.yaml`. Names must match the shipped model exactly.

## Estimated effort: S · Dependencies: Sprint 1, Sprint 2 · Model: sonnet

## Anchors

- Docs live at `docs/v4/RUNBOOK.md` and `docs/v4/USAGE.md` (they document `PipelineStage` fields and `groups.yaml`).
- `commits_vault` is currently undocumented — optional to add while here.
- Example `groups.yaml` is at repo root.
- Final field names come from Sprint 1's `hivepilot/models.py` (read-only here): `only_components`, `only_tags`, `continue_on_failure`, `Group.tags`.

## File Boundaries

files_to_create:
- (none)

files_to_modify:
- `docs/v4/RUNBOOK.md`
- `docs/v4/USAGE.md`
- `groups.yaml`

### Read-Only & Shared Contracts
- read-only: hivepilot/models.py
- shared_contracts: field names from PRD §12

## Tasks

- [ ] Document `only_components`, `only_tags`, `continue_on_failure` on `PipelineStage`: purpose, defaults, skip semantics (skip iff target non-empty ∧ disjoint from touched components), and the fail-closed undefined-tag behaviour.
- [ ] Document `groups.yaml` `tags: { <tag>: [<component>, ...] }` and how `only_tags` resolves through it.
- [ ] Add an illustrative `tags:` block to the example `groups.yaml` (e.g. `tags: { ui: [console, extension, vscode, website] }`) — illustrative only; the Noxys values are owned by PRD B.
- [ ] (Optional) Document the pre-existing `commits_vault` field.

## Acceptance Criteria

- [ ] Docs describe all three new stage fields + group tags with an example; names match the model exactly.

## Verification

- [ ] `cd /home/jeromesoyer/Documents/Github/jsoyer/HivePilot && grep -n "only_tags\|only_components\|continue_on_failure" docs/v4/RUNBOOK.md docs/v4/USAGE.md` returns hits.
- [ ] Manual review that example `groups.yaml` parses (`python -c "import yaml,sys; yaml.safe_load(open('groups.yaml'))"`).
