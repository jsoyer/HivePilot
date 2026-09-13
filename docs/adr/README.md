# Architecture Decision Records

In-repo ADRs use the same sections as `ObsidianService.write_adr()` (the
vault copy under the `decisions` slot of `vault.yaml`):

- Status
- Context
- Options
- Decision
- Consequences
- Security Impact
- Review Date

Filename: `YYYY-MM-DD-<slug>.md`. Frontmatter must include `title`, `type: adr`,
`status`, `created`, `agent`, and `language: en`.

Amendments are **new dated files** that set `amends:` to the parent ticket
(or filename) and link back. Do not rewrite an accepted ADR in place except
for a one-line pointer to the amendment.

Coworker + OpenSpace frame (HP-94, accepted in the vault) is amended in-repo
by [2026-09-13-skills-first.md](./2026-09-13-skills-first.md) (HP-103):
capability = `SKILL.md` + scripts; runtime = confinement / approvals / audit.
The accompanying read-only audit is
[../runtime-to-skill-audit.md](../runtime-to-skill-audit.md).
