# HivePilot V4 — Phase 1 Plan (REVISED after ground-truth audit)

> Status: **EXECUTING Sprint 1.0** (vault foundation, metadata-only). Source spec: `docs/v4/`.
> Phase 1 only (v4 `CLAUDE.md`: "implement Phase 1 only"). NOT the product need yet.

## Corrected facts (invalidated earlier assumptions)

- The vault **IS git-versioned**: repo root is the PARENT `~/Documents/Github/jsoyer/obsidian-vault/.git`, remote `git@github.com:jsoyer/obsidian-vault.git`, branch `main`, clean + synced. `Acme/` is a subfolder. → No `git init`; use a branch.
- The vault is an **org-wide source of truth referenced by absolute path** from the acme monorepo: `08 - Security` (rules, specs, code, INVARIANTS), `03 - Decisions` (sync-adrs.sh, ML readme), `02 - Architecture` (ML readme), `01 - Journal` (agent pushes), and the whole repo (acme-web CI). → **Renaming/renumbering breaks cross-repo refs and is NOT git-reversible.**
- `sales-questionnaires` is the SoT mirrored to `acme-eu/acme-sales-questionnaires` for external sharing. → **Leave embedded; do NOT de-embed.**

## Locked decisions

- **Vault:** `…/obsidian-vault/Acme` (git-versioned via parent repo, remote, on `main`).
- **Reorg intensity:** **Niveau A — metadata-only, ZERO folder renames** (data changed: renaming would break the org).
- **`sales-questionnaires`:** leave as-is (preserve external mirror); just exclude from agent navigation with a note.
- **Safety net:** work on git **branch** `hivepilot/phase1-vault-foundation` + timestamped filesystem **backup**; review diff before any merge to `main`.
- **HivePilot output:** dedicated subtree `12 - HivePilot/` → `Agents/ Tasks/ Reports/ Runs/ Interactions/`; ADRs to existing `03 - Decisions/`.
- **Models:** all 8 roles bound to Claude profiles; OpenRouter-ready (Phase 2).
- **Agent =** prompt + Claude model binding + I/O contract.
- **Write safety:** `obsidian_service` dry-run by default.
- **Rules wiring:** reference-by-path (no copy); artifacts in **English** (acme `.cursorrules`).

## Sprint 1.0 — Vault foundation (metadata-only, no renames)  ✅ DONE (branch, awaiting merge review)
- [x] 1.0.1 Confirm vault git clean + synced (clean, in sync with origin/main)
- [x] 1.0.2 Create working branch `hivepilot/phase1-vault-foundation`
- [x] 1.0.3 Timestamped filesystem backup → `obsidian-vault-backup-20260618-154024` (56M)
- [x] 1.0.4 Create `12 - HivePilot/` subtree (Agents/Tasks/Reports/Runs/Interactions) + per-folder READMEs + FRONTMATTER-CONVENTION
- [x] 1.0.5 Author `NAVIGATION.md` master index (frozen folders, duplicate-prefix disambiguation, write rules)
- [x] 1.0.6 Author frontmatter convention doc (English-only)
- [x] 1.0.7 Refresh vault root `README.md` to real 22-folder taxonomy + frozen notice
- [x] 1.0.8 Removed only `test_git.txt` (0-byte stray). `05 - GTM` and `01 - Knowledge` are REAL content (have subdirs) — NOT deleted, documented in NAVIGATION instead.
- [x] 1.0.9 Commit `c718940` on branch; `main` untouched (a69385d). **Review gate: merge to main pending user approval.**

## Sprint 1.1 — `obsidian_service` + audit  ✅ DONE (Batch A, eb5c80e)
- [x] `hivepilot/services/obsidian_service.py`: dry-run-first vault I/O, frontmatter + ADR helpers, path guard, read-only audit
- [x] `hivepilot obsidian audit` CLI; config `obsidian_vault` setting
- [x] 27 tests (test_obsidian_service/test_config/test_cli)

## Sprint 1.2 — Migration plan  ✅ ABSORBED into Sprint 1.0
- [x] Superseded by the metadata-only decision: coexistence mapping is documented in vault `NAVIGATION.md` (frozen folders, duplicate-prefix disambiguation, `12 - HivePilot/` as the write target). No separate migration doc needed since no files are moved.

## Sprint 1.3 — Role-agent abstraction + 8 prompts  ✅ DONE (Batch A, eb5c80e)
- [x] `hivepilot/roles.py`: `Role` = prompt + Claude model_profile + I/O contract; `ROLES` registry (8), `get_role`/`list_roles`
- [x] 8 prompts in `prompts/agents/`; `model_profiles.yaml` `role_profiles` (additive)
- [x] 15 tests (test_roles)

## Sprint 1.5 — Agent rules ingestion (reference-by-path)  ✅ DONE (Batch B, 06fc135)
- [x] `hivepilot/agent_rules.py`: role→rule-sources manifest (reference-by-path), `get_rules_for_role`
- [x] "Rules you MUST read" section appended to all 8 prompts; cross-cutting rules captured (English, graph-first, detection-fabric, sovereign-first, privacy-by-design)
- [x] 19 tests (test_agent_rules)

## Sprint 1.4 — Company pipeline + state enum  ✅ DONE (Batch B, 06fc135)
- [x] `company` pipeline (10 stages CEO Intake→…→Approval) in `pipelines.yaml` + `company-*` tasks in `tasks.yaml`
- [x] `RunStatus` enum in `state_service` (NEW…COMPLETE + 4 failure states), backward-compatible
- [x] `pipelines.write_stage_artifact()` → `12 - HivePilot/Runs/` (dry-run default)
- [x] 31 tests (test_state_machine/test_company_pipeline)

## Status: Phase 1 COMPLETE on branch `feat/v4-phase1` (88 tests pass). Vault foundation merged to vault `main`. HivePilot code awaiting merge decision.

## Acceptance (Phase 1)
- Vault: NAVIGATION.md present; `12 - HivePilot/` subtree created; zero folder renames; zero cross-repo breakage; changes on a branch reviewed before merge.
- `obsidian audit` runs read-only; migration plan reviewed before any autonomous vault write.
- 8 Claude-bound role prompts; rules manifest by reference; company pipeline defined; new enum non-breaking; tests green.

## Review
_(to be filled after implementation)_

## Process lesson
- RTK proxy filters `rg`/shell output → caused a FALSE-NEGATIVE freeze-list scan (0 hits when refs existed). For correctness-critical scans, run via `rtk proxy <cmd>` to get unfiltered output, and sanity-check on a known-present string.
