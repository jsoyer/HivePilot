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
- **View the Graph tab** — see [Graph view](#graph-view) below.
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

## Graph view

The Graph tab renders slices of HivePilot's own state as a node/edge graph
instead of a flat table, read-only. It is backed by the `GET /v1/graph/*`
API:

- `GET /v1/graph/sources` — lists every registered graph source
  (`name`/`title`/`min_role`/`params`). Any authenticated `read`-floor
  token sees the full list; a source's own `min_role` only gates fetching
  its data, not the listing itself.
- `GET /v1/graph/{source}` — a single source's full graph (nodes + edges +
  an optional `layout_hint`).
- `GET /v1/graph/{source}/node/{id}` — a single node's detail view, whose
  `sections` reuse the exact same closed `stat`/`table`/`text` shapes a
  Mirador panel's `PanelData` uses (see [PLUGINS.md](PLUGINS.md#dashboard-panels-plugins)),
  so it renders through the same `PanelRenderer`.

Built-in sources:

| Source | `min_role` | Scope | Notes |
|---|---|---|---|
| `plugins` | `read` | tenant-free (plugin/role/runner ecosystem is process config, not tenant data) | the same loaded-plugin/role/runner-binding data `plugins list` shows, as a graph |
| `pipeline` | `read` | tenant-scoped | requires `?pipeline=<name>`; renders the pipeline's stage DAG, each stage node coloured by its last run's outcome for the caller's tenant |
| `skills` | `admin` | **local host FS scan, NOT tenant state** | scans `HIVEPILOT_GRAPH_SKILLS_SCAN_PATH` (`settings.graph_skills_scan_path`) for `SKILL.md` files on the machine the API process runs on — admin-gated for the same reason `GET /v1/memories` is |

A plugin can contribute additional sources via the `graph_sources`
capability — see [PLUGINS.md](PLUGINS.md#graph-sources) for the contract and
the `run-lineage` example plugin.

**Security posture:** every `/v1/graph/*` route is read-only (`GET` only,
no side effects). The floor is a `read` bearer token; each source's own
`min_role` is enforced AFTER the source is resolved (it's data-dependent,
not a static route decorator), and an unrecognized `min_role` value is
treated as the highest possible bar rather than failing open. Responses are
tenant-scoped wherever the underlying data is tenant data (`pipeline`, and
any plugin-contributed source touching run/step/verdict rows) — `skills` is
the one built-in exception, since a local filesystem scan has no tenant
concept. An unknown source, or a `node_detail` call the source doesn't
support, is a `404`. A source whose `data()`/`node_detail()` raises, or
returns a malformed shape, degrades to a normalized `kind="error"` node —
never a `500`. No node, edge, or detail section in any `/v1/graph/*`
response ever contains a secret VALUE — only names/presence/status, same
discipline as plugin health and panel data.

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
