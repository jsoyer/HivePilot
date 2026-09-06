## Summary

HP-79: skills improve by **proposal**, never by silent rewrite.

- Usage: a step that applies a skill records `skill_usage_events` + `skill.applied`.
- Auto-improve v0: a failed step that used a skill queues a `SKILL.md` “Observed failure” note (unified diff). Operators can also `POST /v1/skills/proposals`.
- Workshop: Pollen Operate → **Skills** lists proposed diffs. Accept (`approve`) writes **directory** skills under a configured `skills/` scan root. Plugin skills can be reviewed but are never written back. Reject leaves files untouched.

Linear: [HP-79](https://linear.app/js-workspace/issue/HP-79/skills-auto-ameliorants-workshop-de-revue-des-changements).

Replay: `GET /v1/skills/proposals`, then `POST /v1/skills/proposals/{id}/accept` with an approve token.

## Testing

- [x] `pytest tests/test_skill_workshop_service.py tests/test_api_skill_proposals.py tests/test_skill_orchestrator_wiring.py tests/test_skill_application.py` (36 passed)
- [x] `cd web && npm test -- --run src/components/views/SkillsWorkshopView.test.tsx src/components/Pollen.test.tsx` (27 passed)
- [x] `cd web && npm run build` (Node 26.5.0 → `index-D5N9sf4B.js`)
- [x] `ruff check` on touched Python
