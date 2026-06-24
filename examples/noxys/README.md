# Noxys Deployment Example

The live Noxys deployment configuration lives at the **repository root** — not in this
directory.  The following files are the real, actively-used config for the Noxys
product:

```
projects.yaml   — project definitions (noxys + all component repos)
roles.yaml      — agent role definitions (CEO, CTO, developer, reviewer, …)
policies.yaml   — per-project policy overrides
groups.yaml     — component groups (noxys group → all noxys-* repos)
pipelines.yaml  — pipeline definitions (company, noxys-v2, …)
tasks.yaml      — task definitions wired to each pipeline stage
prompts/        — agent prompt files
```

These files are the authoritative reference for a production HivePilot deployment.
Do **not** move, copy, or modify them from this examples directory — edit them directly
at the repo root.

## Starting a fresh deployment

To scaffold a generic HivePilot config for a new deployment, run:

```bash
mkdir my-deployment && cd my-deployment
hivepilot init .
```

This creates neutral placeholder config files that you can customise for your
organisation.  After editing, validate the cross-references with:

```bash
hivepilot validate --dir .
```
