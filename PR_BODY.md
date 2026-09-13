## Summary

HP-103 (Linear acceptance): skills-first doctrine — capability = `SKILL.md` + scripts; runtime = confinement / approvals / audit — plus a read-only audit of the top 5 domain procedures still encoded in the engine.

Owning issue: [HP-103](https://linear.app/js-workspace/issue/HP-103/u-09-doctrine-skills-first-audit-top-5-runtimeskill)

ADR: [HP-94](https://linear.app/js-workspace/issue/HP-94/u-00-adr-patterns-coworkeropenspace-only-hitl-obligatoire-4-doors) / plan `coworker-openspace`.

Replay: inspect `docs/adr/2026-09-13-skills-first.md` and `docs/runtime-to-skill-audit.md`. Docs-only; `hivepilot stage --help` still lists `attach-skill` / `detach-skill`.

## What changed

1. **`docs/adr/2026-09-13-skills-first.md`** — Coworker AGENTS.md / repo-contracts **patterns** (skills are the capability SSOT; runtime is not a skill discovery root), rewritten. Status `proposed` until Jerome accepts (same path as HP-94). **Do not flip to accepted in this merge.**
2. **`docs/runtime-to-skill-audit.md`** — read-only top 5 (adversarial review, lessons distill, debate judge, rebuttal protocol, concierge classifier) with file pointers and what the runtime must keep. Implements nothing.
3. **Cross-links** — `docs/SKILLS.md`, `docs/adr/README.md`, `docs/ARCHITECTURE.md`, `docs/PIPELINES-AND-ROLES.md`, README docs table.

Pipeline `stage attach-skill` / `PipelineStage.skills` unchanged. Rebased onto `main` after HP-100 #675; no HP-100 code paths edited.

## Out of scope

- HP-108 skill→tools gate
- HP-100 / HP-101 / HP-102 / HP-105 code paths (cite only)
- WhatsApp
- HP-67 desktop-per-agent
- Extracting any of the five audit items into `skills/`

## Testing

- [x] Docs-only: no Python / YAML config modules added
- [x] `hivepilot stage --help` still lists `attach-skill` / `detach-skill` (CLI text unchanged)
- [x] ADR frontmatter `status: proposed` left unchanged
- [x] Rebased onto `origin/main` (HP-100 #675); `PR_BODY.md` conflict resolved to this HP-103 body
