# Integrations (n8n, Zapier, dashboards…)

HivePilot plugs into external automation in two directions.

## Outbound — lifecycle events → your webhook

Set a webhook URL and HivePilot POSTs structured JSON events as a pipeline runs.
Best-effort and a silent no-op when unset; never blocks a run.

```bash
export HIVEPILOT_EVENT_WEBHOOK_URL="https://n8n.example.com/webhook/hivepilot"
export HIVEPILOT_EVENT_WEBHOOK_TOKEN="optional-bearer"   # sent as Authorization: Bearer …
```

Events (payload is `{"event": <name>, …}`):

| event | when | key fields |
|---|---|---|
| `pipeline_start` | a pipeline run starts | `run_id`, `pipeline`, `projects` |
| `checkpoint` | paused for plan approval | `run_id`, `pipeline`, `next_stage`, `components`, `status` |
| `approved` | a checkpoint is approved | `run_id`, `pipeline`, `approver` |
| `denied` | a checkpoint is denied | `run_id`, `pipeline`, `approver` |
| `complete` | the run finishes | `run_id`, `pipeline`, `status` |

Example `checkpoint` payload:
```json
{"event":"checkpoint","run_id":42,"pipeline":"default","next_stage":"Implementation",
 "components":["acme-api"],"status":"awaiting_approval"}
```

In **n8n**: a *Webhook* node receives these; branch on `{{$json.event}}` to notify,
log to Sheets, post to Slack, or — for `checkpoint` — send yourself an approve/deny
prompt that calls back the HivePilot API (below).

## Inbound — trigger / approve from n8n

HivePilot already exposes an authenticated HTTP API (`hivepilot api` / `api_service`).
From an n8n *HTTP Request* node:

- **Trigger a run** — `POST /run` (role `run`):
  ```json
  { "project": "acme", "task": "default", "extra_prompt": "ship the X feature" }
  ```
- **Approve / deny a checkpoint** — `POST /approvals/{run_id}` (role `approve`):
  ```json
  { "action": "approve" }
  ```
- **Read** — `GET /projects`, `GET /tasks`, `GET /approvals`, `GET /health`.

Auth: send the API token (`Authorization: Bearer <token>`); see CONFIG.md.

## Pattern: external approval loop
`checkpoint` event → n8n notifies you (email/Slack with two buttons) → your click
hits `POST /approvals/{run_id}` → HivePilot resumes (or stops) the pipeline. This
lets you approve plans without opening Telegram.
