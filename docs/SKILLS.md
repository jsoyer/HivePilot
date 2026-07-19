# Skills

A skill is a named bundle a plugin contributes: a description, a set of files (relative path → content), an optional system prompt, and optional targeting metadata. A pipeline stage or task step opts into a skill by name. Skills let you package reusable agent instructions and scaffolding files and apply them wherever a stage or step needs them, instead of duplicating that content across config.

For the general plugin loading and trust model, see [PLUGINS.md](PLUGINS.md) — this doc covers the `skill` plugin type specifically.

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
- `min_role` gating is validated at plugin registration time (invalid role rank → registration fails closed).
- Skill names must be unique across registered skills; config validation rejects unknown names on stages/steps.

## See also

- [PLUGINS.md](PLUGINS.md) — general plugin model, loading, and trust
- [CONFIGURATION.md](CONFIGURATION.md) — full config file reference
- [PIPELINES-AND-ROLES.md](PIPELINES-AND-ROLES.md) — pipeline stages, task steps, and roles
