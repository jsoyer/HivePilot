## Summary

HP-121 — per-project / per-tenant Obsidian vault routing. `obsidian_vault` stays a machine-wide default, but an explicit **mapping table** (`vault_routes.yaml`) is now the CoS routing SSOT when several tenants share one host: HivePilot work → Jsoyer vault, Noxys pipelines → Noxys vault.

Owning issue: [HP-121](https://linear.app/js-workspace/issue/HP-121/r4-vault-routing-per-project-jsoyer-vs-noxys)

ADR: [`docs/adr/2026-09-20-vault-routing-per-project.md`](docs/adr/2026-09-20-vault-routing-per-project.md)

Replay: `hivepilot lint` (loads `vault_routes.yaml` when present)

## Acceptance

1. **ADR (Accepted, CoS reco)** — alternatives recorded (global only, per-project override + global fallback, opaque plugin). Decision: mapping table, not a plugin.
2. **Mapping keys** — `by_project` (`project_id`) and `by_tenant` (`tenant`) → named vault id. Canonical ids: `jsoyer` ([github.com/jsoyer/obsidian-vault](https://github.com/jsoyer/obsidian-vault)), `noxys` (no published in-repo filesystem path; set `vaults.noxys.path` or `HIVEPILOT_VAULT_NOXYS`).
3. **Fail-closed** — while the table is active, unmapped or ambiguous lookups raise `VaultResolutionError`. No silent `HIVEPILOT_OBSIDIAN_VAULT` fallback. Project vs tenant disagreement, and table vs `obsidian_vault:` disagreement, refuse rather than pick a winner.
4. **Inactive table** — missing file or empty route maps keep the pre-HP-121 resolver (OSS example-api still inherits the global vault).
5. **Isolation tests** — Jsoyer and Noxys never cross-write; unmapped does not hit the global vault.

## What changed

1. **`vault_routes.yaml`** — optional config surface + `examples/vault_routes.yaml`.
2. **`hivepilot/services/vault_routes.py`** — load + fail-closed lookup.
3. **`obsidian_vault_resolver`** — consults the table; orchestrator / plugin / prompt vars pass `project_id` (and tenant when present).
4. **Lint / doctor** — validate the file; shared-vault finding names the table.

## Out of scope

- Telegram 4-door cutover
- HP-126–128 ops
- Inventing a Noxys checkout path
- Hardcoded Mac home directories

## Testing

- [x] `pytest tests/test_vault_routes.py tests/test_obsidian_vault_resolver.py tests/test_per_project_vault.py tests/test_config_doctor.py::TestSharedObsidianVaultLimitation -q` — 69 passed
- [x] `_lint_vault_routes()` — clean (no in-repo table file; example parses in tests)
- [x] `ruff check` + `ruff format --check` clean on touched Python
- [x] `hivepilot lint` — no vault_routes errors (example project paths missing, pre-existing)
