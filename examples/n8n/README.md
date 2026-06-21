# n8n example workflows

Two importable workflows that wire HivePilot's event webhook + HTTP API into an
external approval loop — approve/deny plans without opening Telegram.

See also: [`docs/v4/INTEGRATIONS.md`](../../docs/v4/INTEGRATIONS.md).

## Files
- **`hivepilot-events.json`** — receives HivePilot lifecycle events
  (`pipeline_start`, `checkpoint`, `approved`, `denied`, `complete`). On
  `checkpoint` it builds **approve/deny links** and hands off to your notifier
  (replace the `Notify me` NoOp with a Slack/email node using `{{message}}`,
  `{{approve_url}}`, `{{deny_url}}`).
- **`hivepilot-approve.json`** — a webhook that, when an approve/deny link is
  opened, calls `POST {HIVEPILOT_API_URL}/approvals/{run_id}` with the action.

## Setup
1. **Import** both JSON files in n8n (Workflows → Import from File) and activate them.
2. **Point HivePilot at the events webhook:**
   ```bash
   export HIVEPILOT_EVENT_WEBHOOK_URL="https://<your-n8n>/webhook/hivepilot-events"
   export HIVEPILOT_EVENT_WEBHOOK_TOKEN="optional-bearer"   # if you add an auth check in n8n
   ```
3. **n8n env** (Settings → Variables, or process env):
   ```
   N8N_BASE_URL           = https://<your-n8n>
   HIVEPILOT_API_URL      = https://<your-hivepilot-api>    # the `hivepilot api` server
   HIVEPILOT_API_TOKEN    = <a token with the "approve" role>
   HIVEPILOT_APPROVE_SECRET = <random shared secret guarding the approve webhook>
   ```

## Security notes
The approve webhook performs a privileged action, so the example is hardened:
- it's **POST** (a prefetched GET link can't trigger an approval);
- a shared **token** (`HIVEPILOT_APPROVE_SECRET`) is required, and `run_id`
  (`^[0-9]+$`) + `action` (`approve`/`deny`) are validated before any API call,
  so no path injection / unauthenticated approvals.
For stronger per-link binding, replace the shared token with an
`HMAC(secret, run_id|action|expiry)` generated in the events workflow and verified
here. Never expose the approve webhook publicly without these guards.
4. Run a real pipeline with a checkpoint:
   `hivepilot run-pipeline noxys noxys-v2 --no-dry-run` →
   you get a notification with **Approve / Deny** links → clicking resumes (or
   stops) the pipeline via the API.

## Flow
```
HivePilot ──event──▶ [hivepilot-events] ──checkpoint──▶ notify (Approve/Deny links)
                                                              │ click
                                                              ▼
                          [hivepilot-approve] ──▶ POST /approvals/{run_id} ──▶ HivePilot resumes
```

> The JSON uses recent node typeVersions; if your n8n is older, n8n still imports
> them and may prompt to upgrade the affected nodes.
