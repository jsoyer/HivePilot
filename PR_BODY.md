## Summary

**HP-86** — bind a change-class taxonomy to existing HP-61 approval rules (spike).

- No second control plane. Optional `change_class` on the same `approve` / `deny` rule + pending cards.
- `mechanical` (lint, lockfile, changelog typo) may auto-approve when the rule says so.
- `product_fork` / `security` / `destructive` / `unknown` (default) never auto-approve.
- Watcher wake (`source` / `woke` / bus kind) is not a class and cannot unlock auto-approve.
- A metadata claim can only tighten. Class veto on approve does not fall through to a less-specific rule.
- `auto=deny` unchanged. `INVARIANTS.md` destructive / outward / `merge_pr` untouched.

ADR: `docs/adr/2026-09-11-hp61-change-classes.md` (amends HP-84). Thin hook: `approval_rules_service.match_auto`.

Linear: [HP-86](https://linear.app/js-workspace/issue/HP-86/spike-bind-mechanical-auto-fix-vs-product-fork-classes-to-hp-61)

Replay: `hivepilot run example-api docs --dry-run`

## Testing

- [ ] `pytest tests/test_hp61_approval_rules.py -q`
- [ ] `python scripts/export_openapi.py --check`
- [ ] `hivepilot lint` (no YAML surface change expected)
- [ ] `INVARIANTS.md` unchanged
