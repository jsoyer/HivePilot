# Mirador web UI

Mirador is HivePilot's browser-based insight dashboard — a dark, tabbed
shell (Analytics / Cost / Health / Mem0) served directly by the FastAPI
API process. This page covers install, the auth model, and reverse-proxy
notes. See `docs/v4/RUNBOOK.md`'s "Mirador web UI surface" section for the
underlying `/v1/plugins/health` and `/v1/memories` API endpoints the UI
calls.

> **Sprint status:** the app shell, dark theme, tab layout, and the token
> gate are shipped (Sprint 2). The four tabs currently render placeholder
> panels — real data wiring lands in Sprint 3.

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

## Auth: bring your own `read` token

Mirador has no separate login system. On first load it asks for a
HivePilot API token — the **same tokens** the CLI/Telegram/API already use
(`hivepilot token create --role read`, see `docs/v4/RUNBOOK.md`). The gate
itself only requires a `read` token (validated against `GET
/v1/plugins/health`, which every `read` token can call). Individual
endpoints behind the tabs enforce their own role as usual — notably `GET
/v1/memories` (the Mem0 tab's data source) requires `admin`, not `read`,
because mem0 has no per-tenant partitioning (see RUNBOOK's "Memories
scoping rule"). A `read`-only token can sign in but will see a permission
error on any view that calls an `admin`-gated endpoint.

The token is validated by calling `GET /v1/plugins/health` with it as a
bearer token; on `401`/`403` you're shown an error and asked to re-enter
it. Once accepted, the token is stored **only** in the browser's
`localStorage` (key `hivepilot.webui.token`) — it is never sent anywhere
except as the `Authorization: Bearer <token>` header on same-origin
`/v1/*` requests, and it never appears in any HTML/JS/CSS served by the
app (the served bundle is static and identical for every visitor).

## Reverse proxy

Mirador is served same-origin with the API (`/ui` for the shell, `/v1/*`
for data) — no separate origin, no extra CORS configuration beyond what
`api_allowed_origins` already requires for the API itself. Put this
behind the same Caddy/nginx TLS termination already documented for the
API in `docs/v4/RUNBOOK.md` (`hivepilot caddy setup ...`); no additional
proxy rules are needed since both live under one process/port.

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
