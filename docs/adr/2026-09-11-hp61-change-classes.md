---
title: Bind change classes to HP-61 approval rules
type: adr
status: accepted
created: 2026-09-11
agent: hivepilot
language: en
linear: HP-86
amends: HP-84
---

# Bind change classes to HP-61 approval rules

## Status:

accepted

## Context:

[HP-84](https://linear.app/js-workspace/issue/HP-84/patterns-firstmate-a-piquer-pas-dintegration-produit) / [docs/adr/2026-09-11-firstmate-patterns.md](./2026-09-11-firstmate-patterns.md) ranked **explicit escalation** as spike 2: mechanical auto-fix vs ask-a-human on product forks. [HP-61](https://linear.app/js-workspace/issue/HP-61/cartes-dapprobation-regles-par-action-inline-dans-le-chat) already has per-action `approve` / `deny` rules and pending cards. `match_auto` matches `project` / `task` / `action`; no match waits for a human.

A future zero-token watcher (HP-85) will wake on bus events. Without a **change class**, that wake is either too eager (auto-run) or too noisy (page every time). The class must live on the existing HP-61 rule — not a second control plane — and must not weaken `INVARIANTS.md` destructive / outward / `merge_pr` gates.

This spike records the taxonomy and lands the smallest hook. It does **not** auto-classify diffs, auto-fix lint, or wire the watcher.

## Options:

- New policy file / watcher allowlist that auto-approves mechanical wakes (second control plane).
- Infer `mechanical` from "a watcher woke" or from an event kind on the HP-40 bus.
- Optional `change_class` on the existing HP-61 rule; `auto=approve` only when the class is `mechanical`; unknown / contested / human-gate classes stay pending. `auto=deny` unchanged.
- Do nothing until ship/scout contracts (HP-84 rank 3) exist.

## Decision:

**Bind a small taxonomy to the existing HP-61 rule.** Same `approve` / `deny` / pending cards. No new engine.

| Class | When | Auto-approve? | Card |
| --- | --- | --- | --- |
| `mechanical` | lint, lockfile, changelog typo — operator-declared on the rule | only if `auto=approve` **and** the rule class is `mechanical` | skipped when both hold |
| `product_fork` | product behaviour / API / UX fork | never | pending (human) |
| `security` | auth, secrets, policy, supply chain | never | pending (human) |
| `destructive` | delete, migrate, infra mutate | never | pending (human) |
| `unknown` (default) | missing, unrecognized, or `contested` | never | pending (human) |

Rules:

1. **`change_class` is optional.** Empty / omitted stores as empty and resolves to `unknown`.
2. **Fail closed on approve.** `auto=approve` fires only when the winning rule's class is `mechanical`. Human-gate classes (`product_fork`, `security`, `destructive`, `unknown`) never auto-approve, even if `auto=approve`.
3. **A claimed class in metadata can only tighten.** Keys: `change_class`, `class`, or `contested: true`. If the claim is present and is not `mechanical`, approve is vetoed. Missing claim does **not** invent a class — the rule's class stands.
4. **Watcher wake is not a class.** `source`, `watcher`, `woke`, `wake`, `event`, `bus_kind`, `kind` are never read as `change_class`. A wake matching a rule with no class stays pending.
5. **`auto=deny` is unchanged.** Deny stays fail-closed and does not need a class.
6. **Winning rule does not fall through.** A class veto on approve returns pending; it does not try the next rule.
7. **Invariants stay above this hook.** `INVARIANTS.md` destructive / outward / `merge_pr` gates are a separate plane. A mechanical rule cannot waive them. This spike does not edit `INVARIANTS.md`.

Callers that already send `{project, task, action}` keep working. To opt a lint/docs-typo task into auto-approve, set `change_class: mechanical` on that rule. Full auto-fix / watcher classification is a later ticket.

## Consequences:

Positive:

- One field on the HP-61 rule; Pollen / API / CLI keep the same replace-rules surface.
- Unknown default makes old `auto=approve` rules fail closed until an operator labels them `mechanical` — the safer surprise.
- HP-85 can wake without deciding a class; pending is the default.

Negative / follow-up:

- Existing `auto=approve` rules without a class stop auto-approving (intentional). Operators must set `change_class: mechanical` on the ones they still want.
- No classifier yet: a lockfile bump is mechanical only if a human said so on the rule (or a later ticket labels the change).
- Ship/scout contracts and task-class dispatch (HP-84 ranks 3–4) remain separate.

## Security Impact:

- Fail closed: empty, typo'd, contested, or watcher-only metadata never auto-approves.
- `auto=deny` still applies without a class.
- Does not touch partition outward consent, outward allowlists, or the unconditional `merge_pr` refusal in `INVARIANTS.md`.
- Does not add a second place that can mutate git or skip a pending card.

## Review Date:

2026-10-11
