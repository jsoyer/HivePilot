## Summary

HP-54 first slice: **mission « où elle en est » via Hindsight `reflect()`**.

- New `hivepilot/services/hindsight_reflect.py`: `reflect()` on the first spawned task's HP-51 episodic bank (`{project}:{task}:{role}`), `fact_types=["experience"]`, engine numeric status as context. Dormant unless `HIVEPILOT_HINDSIGHT_ENABLED`. Never invents prose when the client is missing or the call fails.
- `GET /v1/orchestrator/missions/{id}` now returns additive `narrative`. Cached on the mission row (`narrative` / `narrative_fingerprint` / `reflected_at`) so a poll that hasn't moved the runs does not spend another LLM call.
- End-of-mission Espace synthesis keeps the numeric header and appends the narrative when one exists.

Out of slice: Missions board UI (HP-29), multi-bank merge, mem0 retirement (HP-53), Disposition.

Linear: [HP-54](https://linear.app/js-workspace/issue/HP-54/statut-mission-ou-elle-en-est-via-reflect). Parent HP-33. Related HP-29 / HP-51 / HP-52.

## Testing

- [x] `pytest tests/test_hindsight_reflect.py tests/test_mission_plan.py tests/test_hindsight_role_sync.py -q`
- [ ] `hivepilot lint` — pre-existing missing example-site/acme-* paths

Replay: `export HIVEPILOT_HINDSIGHT_ENABLED=true` and point `HIVEPILOT_HINDSIGHT_BASE_URL` at a running Hindsight. Launch a mission, then `GET /v1/orchestrator/missions/{id}` — `narrative` fills after the first reflect. Disabled flag still returns `narrative: null`.
