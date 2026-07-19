# Deployment

HivePilot runs as a set of long-running processes — the HTTP API, the scheduler
daemon, and optional chat bots — behind a reverse proxy, on a host or in
Kubernetes. State is kept in a SQLite store; secrets come from a secrets
backend rather than from files or environment variables checked into config.

## The HTTP API

Start the API with:

```bash
hivepilot api serve --host 0.0.0.0 --port 8000 --workers 4
```

This is a FastAPI application. It serves the Mirador web command center and
the `/v1/*` endpoints (runs, approvals, analytics, plugins, graph).

For a persistent deployment on a Linux host, generate a systemd unit instead
of running the server in a terminal:

```bash
hivepilot api systemd-unit
```

Authentication is token-based: create, list, rotate, and remove tokens with
`hivepilot tokens add / list / rotate / remove`. Tokens are tenant-scoped —
every request is resolved to a tenant and a role, and role determines which
endpoints and admin operations are reachable.

See [DASHBOARD.md](DASHBOARD.md) for the Mirador UI and [SECURITY.md](SECURITY.md)
for the auth and token model.

## Scheduler daemon

Scheduled tasks (defined in `schedules.yaml`) run under a separate daemon
process, not inside the API server:

```bash
hivepilot schedule daemon
```

Generate a systemd unit for it the same way as the API:

```bash
hivepilot schedule systemd-unit
```

Operational commands:

```bash
hivepilot schedule health        # daemon/schedule health check
hivepilot schedule list          # list configured schedules
hivepilot schedule run <name>    # trigger a schedule immediately
hivepilot schedule retry-list    # inspect the retry queue
hivepilot schedule dlq-list      # inspect the dead-letter queue
hivepilot schedule dlq-purge     # DESTRUCTIVE: clears the dead-letter queue
```

`schedule dlq-purge` is destructive and irreversible — confirm the entries are
no longer needed before running it. See
[CONFIGURATION.md#schedulesyaml](CONFIGURATION.md#schedulesyaml) for the
schedule file format.

## Reverse proxy (Caddy)

Put Caddy in front of the API for TLS termination and routing:

```bash
hivepilot caddy generate   # generate a Caddyfile
hivepilot caddy setup      # install/configure Caddy
hivepilot caddy reload     # reload the running Caddy config
hivepilot caddy status     # check Caddy's status
hivepilot caddy teardown   # remove the Caddy configuration
```

See [INTEGRATIONS.md](INTEGRATIONS.md) for other integration points.

## Kubernetes

A Helm chart ships at `deploy/helm/hivepilot`. CI lints and renders it on
every change (`helm lint`, `helm template`), so the chart in that path is kept
deployable. Deploy or preview manifests with the standard Helm workflow:

```bash
helm template hivepilot deploy/helm/hivepilot
helm install hivepilot deploy/helm/hivepilot
```

Consult the chart's own values file for the configurable options — this
document does not enumerate them.

## Configuration in production

Config files resolve in this order: `$XDG_CONFIG_HOME/hivepilot/<file>` →
config repo → `base_dir`. Environment variables use the `HIVEPILOT_` prefix
and are typically supplied via `.env` — see the repo's `.env.example` for the
exhaustive list of supported variables.

For GitOps-style deployments, keep the YAML configuration in a dedicated
config repo and sync it onto the host:

```bash
hivepilot config sync     # pull the config repo onto this host
hivepilot config push     # push local config changes back to the repo
hivepilot config status   # show sync/drift status
```

See [CONFIGURATION.md](CONFIGURATION.md) for the full config resolution and
file reference.

## Secrets in production

Resolve secrets through a secrets backend plugin — Infisical, 1Password,
Bitwarden, or Vaultwarden — using `${secret:NAME}` references in config rather
than embedding values directly.

`secrets_fail_mode: closed` is the default: any unresolved secret reference
aborts the run rather than falling back to an empty or literal value. Secret
values are masked wherever output is captured or displayed (logs, DB, notifier
sinks). See [SECURITY.md](SECURITY.md) for the secrets model in detail.

## Multi-tenancy

The state store and the API are tenant-scoped: every run, approval, and token
is associated with a tenant. Tokens carry a role, and admin-only operations
(plugin toggling, token rotation, etc.) are role-gated at the endpoint.

## Observability

- Logs are structured JSON.
- Run history and results live in the SQLite state store and in
  `runs/<timestamp>/summary.json` per run.
- OpenTelemetry tracing is opt-in: set `HIVEPILOT_ENABLE_TRACING` and
  `HIVEPILOT_OTEL_EXPORTER_OTLP_ENDPOINT` to enable it; the reported service
  name comes from `HIVEPILOT_OTEL_SERVICE_NAME` (default `hivepilot`).
- Prometheus-style metrics are exposed by the API.
- A read-only analytics API reports durations (p50/p95/p99), provider
  breakdowns, and cost — see [DASHBOARD.md](DASHBOARD.md).

## Diagnostics

```bash
hivepilot doctor           # environment, binary, and agent availability checks
hivepilot plugins health   # plugin health checks; exits non-zero on any error (CI-friendly)
hivepilot validate         # validate configuration
```

Run `hivepilot plugins health` in CI or as a deploy-time gate — a non-zero
exit means at least one plugin failed its health check.

## See also

- [CONFIGURATION.md](CONFIGURATION.md)
- [SECURITY.md](SECURITY.md)
- [DASHBOARD.md](DASHBOARD.md)
- [INTEGRATIONS.md](INTEGRATIONS.md)
- [CLI-REFERENCE.md](CLI-REFERENCE.md)
