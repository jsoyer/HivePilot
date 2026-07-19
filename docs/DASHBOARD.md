# Mirador Dashboard

Mirador is HivePilot's dashboard for observing and acting on runs. It comes in two
forms — a Textual terminal UI (TUI) and a web command center — both reading the same
SQLite state store, tenant-scoped.

## Terminal UI (TUI)

```bash
hivepilot dashboard
```

Lists recent runs and lets you inspect them. Requires:

- `HIVEPILOT_ENABLE_TEXTUAL_UI=1` set in the environment
- the `textual` dependency installed

There is also a dedicated plugin browser:

```bash
hivepilot plugins tui
```

Tab layout and navigation are specific to the running version — check the TUI itself
for the current set of views rather than relying on this doc for exact tab names.

## Web command center

The web UI is served by the HTTP API process. Start the API server:

```bash
hivepilot api serve --host 0.0.0.0 --port 8000 --workers 1
```

Then open the served web UI in a browser. It's a React/Vite/Tailwind single-page app
bundled with the API — no separate frontend server to run. Unlike the TUI, the web
command center is actionable, not just a viewer: you can approve gated actions,
launch runs, and toggle plugins directly from the UI.

## What you can do (web)

All actions below are backed by the HTTP API, tenant-scoped, and role-gated where
noted. Destructive or admin actions fail closed — unknown targets return `404`
without side effects.

- **See who you are** — whoami view shows your identity and resolved role.
- **Approve or deny gated actions** — the approval queue, subject to your role.
- **Launch runs asynchronously** — `POST /v1/runs`, then watch progress in the UI.
- **Stop / cancel a running pipeline** — halt an in-flight run.
- **Toggle plugins on/off** — admin-gated, `POST /v1/plugins/{name}/toggle`. The
  health view can also re-enable a previously disabled plugin.
- **View pipeline and skills graphs** — via `/v1/graph/*` sources. The pipeline
  graph source is tenant-scoped; the skills graph source is admin-only and backed by
  a local scan.
- **View analytics** — see [Analytics](#analytics) below.

## Analytics

The API exposes read-only analytics under `/v1/analytics/*`, tenant-scoped and
exportable as CSV:

- run summary
- trends
- durations (p50 / p95 / p99)
- providers
- cost

Cost analytics depend on an opt-in per-run token/cost usage capture (for example,
Claude usage) plus a configurable price map set via `HIVEPILOT_LLM_PRICE_MAP`. The
default price map is indicative and dated, not a live pricing feed — override it for
accurate cost figures. See [DEPLOYMENT.md](DEPLOYMENT.md) for how to run the API in
a deployed environment.

## Dashboard panels (plugins)

The `panel` plugin type contributes renderer-agnostic tabs to Mirador. A plugin
registers a `PanelSpec` with a `fetch` function and an optional `min_role`; an
invalid `min_role` is a fail-closed registration error (the panel won't load). This
is how plugins — for example the mem0 memory plugin — surface their own tab inside
the dashboard instead of requiring a separate UI. See
[PLUGINS.md](PLUGINS.md) for the full plugin type reference.

## Access control

The web API is token-authenticated and tenant-scoped. Admin-only operations —
plugin toggle, the skills graph source — are role-gated on top of that. See
[SECURITY.md](SECURITY.md) for the authentication and authorization model.

## See also

- [CLI-REFERENCE.md](CLI-REFERENCE.md)
- [DEPLOYMENT.md](DEPLOYMENT.md)
- [PLUGINS.md](PLUGINS.md)
- [SECURITY.md](SECURITY.md)
