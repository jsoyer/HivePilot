# Acme Deployment Example

The bundled `acme` example deployment configuration lives at the **repository root** —
not in this directory. The following files are the real, working example config that
ships with HivePilot, modeling a fictional company called "Acme":

```
projects.yaml   — project definitions (acme + all component repos)
roles.yaml      — agent role definitions (CEO, CTO, developer, reviewer, …)
policies.yaml   — per-project policy overrides
groups.yaml     — component groups (acme group → all acme-* repos)
pipelines.yaml  — pipeline definitions (company, default, …)
tasks.yaml      — task definitions wired to each pipeline stage
prompts/        — agent prompt files
```

These files are a working reference for a production HivePilot deployment.
Do **not** move, copy, or modify them from this examples directory — edit them directly
at the repo root if you want to adapt the bundled example for your own use.

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
