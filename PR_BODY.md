## Summary

HP-52 first slice: **each HivePilot role is a Hindsight identity bank** (`role:{name}`).

- New `hivepilot/services/hindsight_role_sync.py`: Mission = full role prompt; Directives = `get_rules_for_role()` (path-like rules become `MUST read before acting: {path}`, prose copied as-is). Disposition is **not** a Role field and is never sent.
- `refresh_roles()` (API CRUD, SIGHUP, daemon adopt) pushes after a successful swap. Fail-soft: a Hindsight outage cannot roll back the roster. No-op unless `HIVEPILOT_HINDSIGHT_ENABLED`.
- Two namespaces stay distinct: HP-51 episodic `{project}:{task}:{role}` vs HP-52 `role:{name}`. `HIVEPILOT_HINDSIGHT_BANK_ID` overrides only the episodic bank.
- Managed directives are content-addressed (`hp-rule-<sha12>`) and tagged `hivepilot`. Operator-written directives are left alone.

Out of slice: `reflect()` (HP-54), mem0 migration (HP-53), Disposition traits, live Hindsight, parsing `## Mission` out of the prompt.

Linear: [HP-52](https://linear.app/js-workspace/issue/HP-52/role-banque-memoire-missiondirectivesdisposition). Parent HP-32.

## Testing

- [x] `pytest tests/test_hindsight_role_sync.py tests/test_hindsight.py tests/test_roles.py tests/test_roles_store.py tests/test_roles_api.py tests/test_roles_config_owned.py tests/test_agent_rules.py::TestGetRulesForRole -q` (98+ related tests)
- [ ] `hivepilot lint` — pre-existing missing example-site/acme-* paths

Replay: `export HIVEPILOT_HINDSIGHT_ENABLED=true` and point `HIVEPILOT_HINDSIGHT_BASE_URL` at a running Hindsight (`http://127.0.0.1:8888`). Then `hivepilot` daemon start or any role CRUD — `refresh_roles()` upserts `role:{name}` banks. Dry-run / disabled flag still no-ops.
