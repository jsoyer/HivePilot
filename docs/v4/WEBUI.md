# Mirador web UI

Mirador is HivePilot's browser-based insight dashboard — a dark, tabbed
shell (Analytics / Cost / Health / Mem0) served directly by the FastAPI
API process, wired to the read-only `/v1/analytics/*`, `/v1/plugins/health`,
and `/v1/memories` endpoints. This page covers install, the auth model, the
four views, and reverse-proxy/deployment notes. See `docs/v4/RUNBOOK.md`'s
"Mirador web UI surface" section for the underlying `/v1/plugins/health` and
`/v1/memories` API endpoint contracts.

## Install

```bash
pip install "hivepilot[webui]"
```

**No Node/npm is required at install or run time.** The `webui` extra pulls
in only what's already needed to run the FastAPI server (`fastapi`,
`uvicorn`, `prometheus-client`, `itsdangerous` — the same set as the `api`
extra); the frontend is a pre-built, pre-committed static bundle under
`hivepilot/webui/static/` (`index.html` + a hashed `assets/` directory),
shipped inside the wheel.

## Enable

```bash
export HIVEPILOT_ENABLE_WEBUI=1
hivepilot api serve
```

Then open `http://<host>:<port>/ui`. Off by default — mirrors
`HIVEPILOT_ENABLE_TEXTUAL_UI`'s opt-in pattern for the Textual dashboard.
The UI route is also unavailable (`404`) if the flag is on but no build is
present (e.g. a partial/corrupted install) — see
`hivepilot/webui/__init__.py`'s `static_available()`.

## Auth: bring your own token, `read` to sign in, `admin` for Mem0

Mirador has no separate login system. On first load it asks for a
HivePilot API token — the **same tokens** the CLI/Telegram/API already use
(`hivepilot token create --role read`, see `docs/v4/RUNBOOK.md`). The token
is validated by calling `GET /v1/plugins/health` with it as a bearer token;
on `401`/`403` you're shown an error and asked to re-enter it. Once
accepted, the token is stored **only** in the browser's `localStorage` (key
`hivepilot.webui.token`) — it is never sent anywhere except as the
`Authorization: Bearer <token>` header on same-origin `/v1/*` requests, and
it never appears in any HTML/JS/CSS served by the app (the served bundle is
static and identical for every visitor). A `401` from any endpoint at any
point (not just the initial gate check) clears the stored token and returns
the user to the token prompt.

**The read-vs-admin distinction matters — read this before handing out
tokens.** The gate itself only requires a `read` token, and a `read` token
unlocks three of the four tabs: **Analytics**, **Cost**, and **Health**.
The **Mem0** tab is different: its data source, `GET /v1/memories`, is
gated behind `require_role("admin")`, not `read`, because mem0 has no
tenant->project mapping to scope memory search results per caller (see
RUNBOOK's "Memories scoping rule" for the full reasoning). Concretely:

- A `read` (or `run`/`approve`) token signs in fine and the other three
  tabs work normally.
- Switching to the Mem0 tab and searching returns `403 Forbidden`. The UI
  recognizes this specific case (`ApiForbiddenError`, see `lib/api.ts`) and
  renders an in-tab "this view requires an admin token" message instead of
  treating it like an invalid token — the other three tabs are unaffected
  and the user is **not** kicked back to the token prompt.
- An `admin` token unlocks all four tabs, including Mem0 search.

So: issue `read` tokens for general dashboard access, and reserve `admin`
tokens for whoever actually needs to search mem0 memories through the UI.

## The four views

- **Analytics** — run volume and outcome breakdown (`GET
  /v1/analytics/summary`), a runs-per-day trend (`GET
  /v1/analytics/trends`), run duration percentiles (`GET
  /v1/analytics/durations`), step failure hotspots (`GET
  /v1/analytics/steps/failures`), and approval latency (`GET
  /v1/analytics/approvals/latency`). All five fetch independently, so one
  slow/failing endpoint never blanks the rest of the tab.
- **Cost** — token and cost totals overall and grouped by provider/model
  (`GET /v1/analytics/cost`, including an `unpriced_steps` count so a
  partial-coverage total is never presented as complete), plus provider/model
  step volume and outcome split (`GET /v1/analytics/providers`).
- **Health** — the same process-global plugin health data as `hivepilot
  plugins health` on the CLI (`GET /v1/plugins/health`): each plugin's
  name, `ok`/`degraded`/`error` status, and a human-readable detail string
  that is guaranteed never to contain a secret or token value.
- **Mem0** — a search box over the mem0 memory store (`GET
  /v1/memories?query=...&limit=20`), rendering each hit's category,
  project, task, timestamp, and memory text. Requires an `admin` token (see
  Auth above); gracefully shows "mem0 is not configured" if the server has
  no mem0 backend wired up, rather than an error.

## Deployment: TLS termination and same-origin serving

Mirador is served same-origin with the API (`/ui` for the shell, `/v1/*`
for data) — no separate origin, no extra CORS configuration beyond what
`api_allowed_origins` already requires for the API itself.

**Always put this behind a TLS-terminating reverse proxy in production.**
The bearer token in `Authorization: Bearer <token>` is a live credential —
never expose `/ui` or `/v1` over plain HTTP outside of local development,
since a token sent over HTTP can be captured in transit. Use the same
Caddy/nginx TLS termination already documented for the API in
`docs/v4/RUNBOOK.md` (`hivepilot caddy setup ...`); no additional proxy
rules are needed beyond forwarding the whole path to the API process, since
both the UI and the API live under one process/port. A minimal nginx
example:

```nginx
server {
    listen 443 ssl;
    server_name hivepilot.example.com;

    ssl_certificate     /etc/letsencrypt/live/hivepilot.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/hivepilot.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

`HIVEPILOT_ENABLE_WEBUI` stays off by default (see Enable above) — a fresh
deploy never exposes `/ui` until an operator opts in.

## Rebuilding the frontend

The build output committed under `hivepilot/webui/static/` is generated,
not hand-written — never edit it directly. To change the UI:

```bash
cd web
npm ci
npm run build   # writes hivepilot/webui/static/ (build.outDir in vite.config.ts)
git add hivepilot/webui/static
```

CI (`webui-build` job in `.github/workflows/ci.yml`) rebuilds from
`web/` on every push/PR and fails if the committed `hivepilot/webui/static/`
doesn't exactly match a fresh build — so a stale bundle can't merge. Node
is pinned via `web/.nvmrc`, consumed by both the CI step
(`node-version-file: web/.nvmrc`) and local `nvm use` in `web/`, so a Node
version drift between CI and a contributor's machine can't produce a
spurious diff.

The committed assets under `hivepilot/webui/static/assets/` use
content-hashed filenames (e.g. `index-a1b2c3d4.js`), so **any** source
change — even a whitespace-only one — changes the hash and therefore the
diff. If `webui-build` fails and the diff is hash-only churn with no
behavior change, that's not a functional regression: just rebuild
(`cd web && npm run build`) and recommit `hivepilot/webui/static/` as
shown above.

**Note on the toggle's discoverability:** the `/ui` routes are registered
unconditionally (`include_in_schema=False` only removes them from
`/openapi.json`/`/docs`); a request with an unsupported method (e.g.
`OPTIONS /ui`) still returns `405` rather than `404` even when
`HIVEPILOT_ENABLE_WEBUI` is off. This means the *existence* of the
feature flag is discoverable pre-auth — the served content behind it
never is (that stays behind `_webui_enabled()`'s runtime gate). Treated
as acceptable residual, not a vulnerability.
