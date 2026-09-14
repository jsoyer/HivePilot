# Skills

A skill is a named bundle: a description, a set of files (relative path → content), an optional system prompt, and optional targeting metadata. A pipeline stage or task step opts into a skill by name. Skills let you package reusable agent instructions and scaffolding files and apply them wherever a stage or step needs them, instead of duplicating that content across config.

Skills reach the engine from **two sources**:

1. a **plugin** contributes them programmatically (`register()["skills"]`) — content inlined in the spec;
2. a **skill directory** on disk — `<root>/skills/<name>/SKILL.md` — the format the wider agent-skill ecosystem uses. This is how a config repo ships skills.

Both land in the same registry, are referenced the same way from config, and are listed by the same `hivepilot skills list`.

**Skills-first (HP-103).** A capability is the skill: `SKILL.md` plus optional
scripts and references. The HivePilot runtime is confinement, approvals, and
audit — not a second place to encode domain playbooks as Python prompt
constants. See [adr/2026-09-13-skills-first.md](adr/2026-09-13-skills-first.md)
and the read-only [runtime-to-skill audit](runtime-to-skill-audit.md).
Pipeline stage-attach (`PipelineStage.skills` /
`hivepilot stage attach-skill`) is unchanged. HP-108 gates **concierge/chat
tool tokens** from enabled skills; it does not keyword force-load or
auto-attach a skill onto a pipeline stage.

For the general plugin loading and trust model, see [PLUGINS.md](PLUGINS.md).

## The SkillSpec contract

A skill plugin's `register()` returns a dict with a `"skills"` key: a list of `SkillSpec` entries.

Each `SkillSpec` has:

- `name` — unique identifier used to reference the skill from config
- `description` — human-readable summary, shown by `hivepilot skills list`
- `provider` — the plugin that contributed the skill
- `files` — dict of relative path → file content, materialized for the step that uses the skill
- `system_prompt` (optional) — instructions injected for the step
- `applies_to` (optional) — targeting metadata (e.g. which stages/roles the skill is intended for)
- `min_role` (optional) — minimum role required to use the skill. An `min_role` value that isn't a recognized role rank is a fail-closed registration error — the plugin fails to register rather than silently allowing an unranked role through.

Plugin loading itself (local file vs. installed package, trust checks) works the same as any other plugin type — see [PLUGINS.md](PLUGINS.md).

## Skill directories (`skills/<name>/SKILL.md`)

A skill can also be shipped as a plain directory. Each subdirectory of a scanned `skills/` root whose name matches the skill name and which contains a `SKILL.md` becomes one skill; every file under it (recursively, dot-prefixed paths excluded) becomes an entry in the skill's `files`.

```
skills/
  code-review/
    SKILL.md
    references/checklist.md
```

`SKILL.md` may carry optional YAML frontmatter. The **directory name is the skill name** — a `name:` that disagrees with it is a hard error, not a silent rename.

```markdown
---
description: Adversarial review pass over a produced diff
system_prompt: Review the diff below against the checklist.
applies_to: [claude]
min_role: admin
---

# Code review
```

### Where skill directories are searched

Scan order mirrors the plugin scan roots one-for-one:

1. `<base_dir>/skills` — the working-directory tier
2. `<config repo>/skills` — the config-repo clone (`$XDG_DATA_HOME/hivepilot/config-repo`) and/or `HIVEPILOT_CONFIG_REPO` when it points at a local directory
3. `$XDG_DATA_HOME/hivepilot/skills` — the managed tier

First root wins on a duplicate name. `hivepilot validate` prints the exact roots consulted under `Skill dirs scanned:`, and an unknown-skill error names every one of them — if a skill isn't resolving, that list is the ground truth of where the engine looked.

Nothing is copied: `hivepilot config sync` does **not** copy `skills/` (nor `plugins/`) out of the config repo. The clone itself is a search root, so the config repo stays the single source of truth and no stale copy can drift.

### Trust model for skill directories

The config-repo root is gated behind exactly the same two switches as the config repo's `plugins/` directory: `HIVEPILOT_CONFIG_REPO` must be set, and `HIVEPILOT_CONFIG_REPO_LOAD_PLUGINS` must be true (the default). `HIVEPILOT_PLUGINS_ENABLED=false` disables skill directories wholesale, just as it disables plugins. A directory skill is never granted a weaker gate than a plugin, and it can carry **text only** — it can never cause Python to be imported or executed.

A directory skill can never shadow a plugin-contributed skill of the same name: plugins are registered first and win; the directory skill is skipped with a warning.

These conditions reject a skill **entirely** (it stays unregistered, so a reference to it still fails validation loudly — the check is never loosened):

- no `SKILL.md`, or a name that isn't a plain identifier
- a file that escapes the skill directory through a symlink
- non-UTF-8 content, a file over 1 MiB, or more than 200 files
- malformed frontmatter, a `name:` that disagrees with the directory, or a `min_role` that is not a recognized role

## Attaching skills to a stage/step

`PipelineStage.skills` and `TaskStep.skills` are each an ordered, de-duplicated list of skill names to apply. Skill names are cross-referenced against registered skills at config-validation time: an unknown skill name fails validation (fail-closed) rather than being silently ignored.

Example `pipelines.yaml` stage:

```yaml
stages:
  - name: implement
    role: developer
    skills:
      - my-skill
```

Manage skill attachments from the CLI — both commands mutate config:

```bash
hivepilot stage attach-skill implement my-skill
hivepilot stage detach-skill implement my-skill
```

See [CONFIGURATION.md](CONFIGURATION.md) for the broader config file layout and validation rules.

## Listing skills

```bash
hivepilot skills list
```

Prints registered skills with name, description, provider, and `applies_to`.

## Authoring a skill plugin

A minimal skill plugin's `register()` returns one `SkillSpec` with a couple of files and a system prompt:

```python
def register():
    return {
        "skills": [
            {
                "name": "my-skill",
                "description": "Adds project conventions to the step context",
                "provider": "my_skill_plugin",
                "files": {
                    "CONVENTIONS.md": "# Conventions\n\nUse snake_case for Python, camelCase for TS.\n",
                },
                "system_prompt": "Follow the attached CONVENTIONS.md for this step.",
            }
        ]
    }
```

The plugin loads like any other HivePilot plugin — as a local file or an installed package. See [PLUGINS.md](PLUGINS.md) for load mechanisms and the fail-closed trust model.

The bundled `sample_skill` plugin is a default-OFF demo — enable it explicitly to see a working example before writing your own.

## Notes & limits

- Skills are materialized per step from the `SkillSpec.files` dict — each step that references the skill gets its own copy of those files.
- `min_role` gating is validated at registration time, for both sources (invalid role rank → fails closed).
- Skill names must be unique across registered skills; config validation rejects unknown names on stages/steps.
- Plugin-contributed skills take precedence over same-named skill directories.

## Skill catalog (HP-98)

`hivepilot/skill_catalog.py` scans the same two sources **read-only** into an
in-memory revision DAG. It does not write `.skill_id` sidecars (OpenSpace
does; HivePilot assigns a deterministic logical id from the skill name
instead).

- Origins: `IMPORTED`, `FIXED`, `DERIVED`, `CAPTURED`.
- Exactly one active revision per logical skill.
- A content-hash change is a new **revision** (`FIXED`), not a new logical
  skill id. `DERIVED` / `CAPTURED` create a new logical skill (roots have
  no parents; `DERIVED` points at one or more parent revisions).

HP-99 evidence (`hivepilot/evidence.py`) is a separate tenant-scoped
registry: evolution claims must cite existing refs; missing refs make a
`skill_evolution` PASS proposal not admissible. Workshop accept/reject
(HP-79) still writes directory files only after an operator approval; a
later scan then sees the new bytes as `FIXED`.

## Skill cycle events (HP-104)

`hivepilot/skill_events.py` records the OpenSpace skill-cycle pattern
(rewritten; no cloud, no pickle):

- Types: `selected`, `invoked`, `applied`, `completed`, `fallback`, `excluded`.
- Idempotent per `(revision_id, run_id, step, event_type)`. A replay returns
  the existing row.
- Absence of measurement is **not** zero. Rankings omit skills with no
  `selected` count (`rate is None`). A skill that was selected and never
  completed is a real `0`.
- Pollen panel `skill-cycle` (opt-in `HIVEPILOT_SKILL_EVENTS_PANEL_ENABLED`)
  shows top/bottom measured skills.

HP-79 `skill_usage_events` stays an append-only workshop log.

## Skill trust (HP-105)

`hivepilot/skill_trust.py` is the OpenSpace trust-lifecycle pattern
(rewritten; no cloud, no pickle):

- New registered revisions start **provisional**. `enabled` is orthogonal
  and can be flipped without changing trust (and the reverse).
- An unknown revision is not implicitly trusted and not implicitly
  enabled. Lookup does not create a row.
- Promotion counts distinct HP-104 `completed` events across runs
  (`HIVEPILOT_SKILL_TRUST_PROMOTION`, default 2). After a demotion, only
  successes since the last attributed failure count.
- Attributed failure demotes trusted → provisional. Ambiguous failure
  opens a HP-97 PASS review (`kind=skill_evolution`, `action=trust_review`)
  and does **not** demote. `not_skill` (env / tool / network / permission)
  is ignored.

## Causal attribution (HP-106)

`hivepilot/skill_signals.py` is the OpenSpace detector + linker pattern
(rewritten; no cloud, no pickle):

- Failure classes: `tool` / `env` / `permission` / `skill_defect`.
  A network outage is `env` and never a skill fault.
- Unique skill context + skill defect → `attributed` (HP-105 demote).
  Multiple or missing subjects → `ambiguous` (PASS review). Tool / env /
  permission → `not_skill` (trust unchanged).
- FIX is admissible only with a **revision + causal event + representative
  result**. This module only gates the triple. Persisting a PASS draft is
  HP-109; apply/commit is HP-111.

## Skill→tools gate (HP-108)

`hivepilot/skill_capabilities.py` is the Coworker skill-capabilities
pattern rewritten locally (no vendored TS):

- A skill maps to HP-95 catalog tokens (`Read`, `WebSearch`, `Bash`, …).
  Tokens come from `SKILL.md` `allowed-tools` / `allowed_tools`, an
  optional overlay, or the bundled `improve` default when that skill is
  in the catalog and has no frontmatter tools.
- Concierge/chat resolution (`resolve_chat_tools`,
  `concierge_service.resolve_role_chat_tools`) **drops** tokens whose
  owning skills are off. HP-105 `enabled=False` is the off switch.
  A cataloged skill with no trust row stays on. Unknown names grant
  nothing.
- Unmapped role tools pass through. A token claimed by several skills
  stays if any owner is on.
- No query/keyword argument loads a skill. The classifier stays
  `--tools ""`.
- `PipelineStage.skills` / `hivepilot stage attach-skill` are unchanged.

## BM25 retrieval (HP-107)

`hivepilot/skill_ranker.py` is the OpenSpace BM25 stage rewritten locally
(no vendor package, no embeddings, no pickle, no cloud):

- Corpus = HP-98 **active** revisions. HP-105 `enabled` / provisional
  filters run **before** scoring so unknown and disabled rows never enter
  IDF. Provisional stays eligible unless `include_provisional=False`.
- Ranking text is name + description only. `disclose` returns the
  `SKILL.md` body after selection (progressive disclosure).
- Order is deterministic (`-score`, then name, then revision id). Zero
  model queries. Hybrid embedding RRF is HP-114.

## Evolution drafts (HP-109)

`hivepilot/skill_evolution.py` is the OpenSpace EvolutionType pattern
(rewritten; no cloud, no pickle, no autonomous mode):

- Types: `FIX` / `DERIVED` / `CAPTURED`. Origins match HP-98
  (`fixed` / `derived` / `captured`).
- Admissible drafts land in the HP-97 PASS inbox (`kind=skill_evolution`).
  They never write skill files, `.skill_id` sidecars, or catalog revisions.
- `CAPTURED` requires independent validation: an execution evidence ref
  plus a **distinct** validation ref. Whole-task `completed` on the same
  run is not sufficient. A caller flag is not enough.
- Merge keys are deterministic and idempotent. A second propose with the
  same key returns the existing card.
- `create_pending` only — no `submit` / `match_auto`, no OpenSpace
  `autonomous` evolution mode. `apply_approved` is an HP-111 hook that
  always refuses mutation.

## Skill workshop (HP-79)

Skills improve by **proposal**, not by silent rewrite.

- A step that applies a skill records a `skill_usage_events` row (`skill.applied`).
- A failed step that used a skill may queue a `SKILL.md` note (unified diff) in
  `skill_patch_proposals`. An operator can also `POST /v1/skills/proposals`.
- Pollen → Operate → **Skills** lists proposed diffs. Accept (`approve` rank)
  writes **directory** skills under a configured `skills/` scan root. Plugin
  skills can be proposed for review but are never written back.
- Reject leaves the files untouched. There is no auto-apply path.

## See also

- [PLUGINS.md](PLUGINS.md) — general plugin model, loading, and trust
- [CONFIGURATION.md](CONFIGURATION.md) — full config file reference
- [PIPELINES-AND-ROLES.md](PIPELINES-AND-ROLES.md) — pipeline stages, task steps, and roles
- [adr/2026-09-13-skills-first.md](adr/2026-09-13-skills-first.md) — HP-103 doctrine (capability vs runtime)
- [runtime-to-skill-audit.md](runtime-to-skill-audit.md) — read-only top 5 still in the engine
