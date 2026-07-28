# Skills

A skill is a named bundle: a description, a set of files (relative path → content), an optional system prompt, and optional targeting metadata. A pipeline stage or task step opts into a skill by name. Skills let you package reusable agent instructions and scaffolding files and apply them wherever a stage or step needs them, instead of duplicating that content across config.

Skills reach the engine from **two sources**:

1. a **plugin** contributes them programmatically (`register()["skills"]`) — content inlined in the spec;
2. a **skill directory** on disk — `<root>/skills/<name>/SKILL.md` — the format the wider agent-skill ecosystem uses. This is how a config repo ships skills.

Both land in the same registry, are referenced the same way from config, and are listed by the same `hivepilot skills list`.

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

## See also

- [PLUGINS.md](PLUGINS.md) — general plugin model, loading, and trust
- [CONFIGURATION.md](CONFIGURATION.md) — full config file reference
- [PIPELINES-AND-ROLES.md](PIPELINES-AND-ROLES.md) — pipeline stages, task steps, and roles
