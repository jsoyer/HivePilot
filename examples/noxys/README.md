# Noxys Deployment Config

This directory contains the active HivePilot configuration for the **Noxys**
deployment.  It is also the canonical reference example for a production
HivePilot config.

```
projects.yaml       — project definitions (noxys + all component repos)
roles.yaml          — agent role definitions (CEO, CTO, developer, reviewer, …)
policies.yaml       — per-project policy overrides
groups.yaml         — component groups (noxys group → all noxys-* repos)
pipelines.yaml      — pipeline definitions (company, noxys-v2, …)
tasks.yaml          — task definitions wired to each pipeline stage
schedules.yaml      — scheduled pipeline runs
model_profiles.yaml — Claude model profile overrides per role
prompts/            — agent prompt files (prompts/agents/<role>.md)
```

## Activating this config

Point `HIVEPILOT_CONFIG_REPO` at this directory (relative to the repo root or
absolute):

```bash
export HIVEPILOT_CONFIG_REPO=examples/noxys
hivepilot run
```

Or add it to your `.env`:

```
HIVEPILOT_CONFIG_REPO=examples/noxys
```

HivePilot's config loader resolves files in this order:
1. XDG config dir (`~/.config/hivepilot/`)
2. `HIVEPILOT_CONFIG_REPO` directory (← this dir)
3. Repo base dir fallback

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
