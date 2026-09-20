## Summary

HP-120 — Pollen install/update of agent binaries, box only, HITL. The CLI
(`agents list/versions/install`) and the Pollen Health card already existed.
This slice puts **update** and **read-remote-version** on the registry as
nullable fields, records **who / binary / version before+after**, and keeps
the interactive TTY guard intact: Pollen replaces it with recorded outward
consent (`{"consent": true}`), never a silent bypass.

Owning issue: [HP-120](https://linear.app/js-workspace/issue/HP-120/r2-pollen-installupdate-agent-binaries-box-only-hitl)

Replay: `hivepilot agents list` / Pollen → Health → Agent CLI binaries (admin)

## Acceptance

1. **Registry** — `InstallSpec.update_command` and `InstallSpec.read_remote_version` are nullable argv tuples. Update argv for grok/claude/codex/cursor is the 2026-08-22 --help probe. `read_remote_version` is undeclared until a command is verified (no guessed `npm view`).
2. **Pollen** — Install/update buttons only when the capability is declared. Check-remote only when `has_remote_version`. Body is `{consent: true}` only; `extra="forbid"` rejects a URL/command from the UI. No run/orchestrator path calls `perform_agent_action`.
3. **Audit** — who, binary, version before and after. Installed version now reads `AgentCliProbe.version` (the old `installed` getattr was always None).
4. **Box only** — local subprocess of registry constants. No remote/cloud install path.

## What changed

1. **`InstallSpec`** — `update_command` / `read_remote_version` + `probe_remote_version`.
2. **`agent_admin`** — update argv from the spec; audit includes `binary`; GET remote-version is opt-in and refused when undeclared. Listing stays offline.
3. **API** — `GET /v1/agents/{kind}/remote-version` (admin). `AgentActionRequest` forbids extra fields.
4. **Pollen Health card** — Check remote only if declared.
5. **CLI** — `agents versions --check-latest` prefers a declared registry probe, else npm.

## Out of scope

- HP-126–128 ops
- Telegram 4-door cutover
- Triggering install/update from a run pipeline
- Inventing unverified remote-version argv

## Testing

- [ ] `pytest tests/test_agent_install.py tests/test_agent_admin.py tests/test_api_service.py tests/test_cli_agents.py tests/test_agent_auth.py -q`
- [ ] `cd web && npm test -- src/components/views/AgentBinariesCard.test.tsx src/components/views/HealthView.test.tsx`
- [ ] `ruff check` on touched Python
