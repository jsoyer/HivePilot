## Summary

HP-115: run-scoped browser grant on the existing HP-68 loopback CDP. A grant is tied to `run_id`, issued through the HP-97 PASS inbox (`kind=tool`, token `BrowserCDP`), and dies on `complete_run`. No grant ⇒ no CDP action. HivePilot does not embed Chromium and does not reopen HP-67.

Owning issue: [HP-115](https://linear.app/js-workspace/issue/HP-115/u-21-browser-grant-task-scoped-cdp-hp-68)

ADR: [HP-94](https://linear.app/js-workspace/issue/HP-94/u-00-adr-patterns-coworkeropenspace-only-hitl-obligatoire-4-doors) / plan `coworker-openspace`. Builds on HP-68 host_browser CDP, HP-97 PASS, HP-95 catalog. Patterns only from Coworker browser grant.

Replay: `pytest tests/test_browser_grant.py tests/test_hp68_host_processes.py tests/test_pass_store.py`

## What changed

1. **`hivepilot/browser_grant.py`** — `request` / `approve` / `get_live` / `act` / `revoke_for_run`. Live grant is loopback-only.
2. **`state_service.complete_run`** — fail-safe revoke so end of run = grant dead.
3. **`tool_catalog.yaml`** — `BrowserCDP` (high / require_approval / volatile).
4. **Docs** — SECURITY / ARCHITECTURE record the grant; HP-67 stays no-go.

## Out of scope

- HP-67 Docker/E2B sandbox reopen
- Shipping Chromium inside the product
- WhatsApp, autonomous evolve, cloud OpenSpace

## Testing

- [x] `pytest tests/test_browser_grant.py tests/test_hp68_host_processes.py tests/test_pass_store.py` — 47 passed
- [x] `ruff check` + `ruff format --check` clean on touched Python
- [x] `mypy hivepilot/browser_grant.py` — no issues
