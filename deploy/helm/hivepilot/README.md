# HivePilot Helm chart

Deploys HivePilot's API server, scheduler daemon, and optional chat-ops bots
(Telegram/Slack/Discord) to Kubernetes. This chart is a new deploy artifact —
it does not change HivePilot's Python runtime; it mirrors the existing
`Dockerfile` / `docker-compose.yml` launch commands and the systemd-unit
generators already in `hivepilot/cli.py` (`hivepilot api systemd-unit`,
`hivepilot telegram systemd-unit`).

## Contents

- [Install](#install)
- [Build & push an image](#build--push-an-image)
- [Single-replica & HA caveat](#single-replica--ha-caveat)
- [Configuration (ConfigMap)](#configuration-configmap)
- [Secrets options](#secrets-options)
- [Bootstrap the first admin token](#bootstrap-the-first-admin-token)
- [Enabling components (bots, scheduler)](#enabling-components-bots-scheduler)
- [Ingress & TLS](#ingress--tls)
- [Probes](#probes)
- [RBAC & the kubectl runner](#rbac--the-kubectl-runner)
- [Values reference](#values-reference)

## Install

```bash
helm install hivepilot deploy/helm/hivepilot \
  --namespace hivepilot --create-namespace \
  --set image.repository=<your-registry>/hivepilot \
  --set image.tag=<your-tag> \
  -f my-values.yaml   # your non-secret overrides (config.files, ingress, ...)
```

Render without installing (useful in CI or to review before applying):

```bash
helm template deploy/helm/hivepilot -f my-values.yaml
```

## Build & push an image

There is no public HivePilot image. Build from the repo's own `Dockerfile`
and push to a registry the cluster can pull from:

```bash
docker build -t <your-registry>/hivepilot:<tag> .
docker push <your-registry>/hivepilot:<tag>
```

The chart's `image.repository`/`image.tag` values point at this image; all
Deployments (api, scheduler, bots) use the same one.

## Single-replica & HA caveat

HivePilot's state (`state.db`) is SQLite — **not multi-writer safe**. The API
and scheduler Deployments are hardcoded to `replicas: 1` (not a values
override — this is intentional, see `templates/deployment-api.yaml` and
`templates/deployment-scheduler.yaml`). `hivepilot api serve --workers>1`
itself prints the same warning (`hivepilot/cli.py`). Both Deployments use
`strategy: Recreate` so Kubernetes tears down the old pod before starting the
new one during a rollout, instead of briefly running two writers.

A single PVC (`persistence.*`, default `ReadWriteOnce`, 2Gi) is mounted into
**both** the api and scheduler pods at `/app/state-data`
(`HIVEPILOT_STATE_DB=state-data/state.db`, plus `runs/`/`logs/` under the
same path) so they observe the same database file. Two pods sharing one RWO
volume works on most CSI drivers only when both pods land on the same node —
pin them with a shared `nodeSelector`/`affinity` (see `api.nodeSelector` /
`scheduler.nodeSelector` values), or use a `ReadWriteMany`-capable
`storageClass` if your cluster spans nodes.

**Scaling beyond one replica requires an external DB** — set
`HIVEPILOT_DATABASE_URL=postgresql://...` (via `secrets.data` or
`api.extraEnv`/`scheduler.extraEnv`) and a Postgres-backed `psycopg[binary]`
install in your image; this chart does not currently expose a `replicaCount`
because doing so without that migration would silently corrupt state.

## Configuration (ConfigMap)

The image bakes in the repo's own example config files (`projects.yaml`,
`tasks.yaml`, `roles.yaml`, `pipelines.yaml`, `policies.yaml`, `groups.yaml`,
`schedules.yaml`, `model_profiles.yaml`) at `/app/<file>` (see `Dockerfile`
`COPY . .`). This chart lets you override any subset of those files with your
own content via `config.files`, rendered into a ConfigMap and mounted with a
per-file `subPath` **over** the baked-in copy — anything you don't set falls
back to the image default:

```yaml
config:
  files:
    projects.yaml: |
      projects:
        my-app:
          path: /workspace/my-app
          description: My application.
    tasks.yaml: |
      runners:
        claude-docs:
          kind: claude
          command: claude
      tasks: {}
    schedules.yaml: |
      schedules: {}
```

To manage the ConfigMap yourself (GitOps, Kustomize, etc.) instead of letting
Helm render it, set `config.existingConfigMap: <name>` — the chart still
needs `config.files` set with the **same keys** (content is irrelevant,
`--set config.files."projects\.yaml"=x` is enough) so it knows which
`subPath` mounts to emit.

## Secrets options

**Never put real tokens in a committed `values.yaml`.** The chart's defaults
(`secrets.data: {}`, `secrets.apiTokensYaml: ""`) are empty placeholders.
Three ways to supply real values:

1. **`--set`/`-f` at install time**, from a file that is itself gitignored
   (e.g. `-f secrets.local.yaml`, never committed):

   ```yaml
   secrets:
     apiTokensYaml: |
       tokens:
         - token_hash: "<sha256 hex>"
           role: admin
           note: bootstrap
     data:
       HIVEPILOT_API_TOKEN: "<plaintext token matching the hash above>"
       HIVEPILOT_TELEGRAM_BOT_TOKEN: "<bot father token>"
       ANTHROPIC_API_KEY: "<if your runner CLIs need it in-cluster>"
   ```

2. **`secrets.existingSecret: <name>`** — reference a Secret you manage
   out-of-band (`kubectl create secret`, Sealed Secrets, External Secrets
   Operator, SOPS, ...). Must contain an `api_tokens.yaml` key plus whichever
   env keys you reference from `extraEnv`/`envFrom`.

3. **HivePilot's own secrets-provider plugins** (Infisical / 1Password —
   `plugins/infisical.py`, `plugins/onepassword.py`) resolve `${secret:NAME}`
   references **inside HivePilot's own config** (`tasks.yaml` runner env,
   etc.) at run time, fetched from an external secrets manager — this is a
   different mechanism from the Kubernetes Secret above (which supplies
   process env vars and the tokens file at pod start). You can combine both:
   keep `HIVEPILOT_INFISICAL_URL`/`HIVEPILOT_INFISICAL_TOKEN` (or the 1Password
   equivalents) in the k8s Secret, and reference `${secret:...}` inside your
   `config.files` task/runner definitions for everything else — meaning only
   the secrets-manager credential itself needs to live in the cluster,
   nothing else in plaintext.

## Bootstrap the first admin token

`api_tokens.yaml` stores only **hashed** tokens; there's no way to generate
one from Helm values alone. `token_service.load_tokens()` / the `tokens add`
CLI allow adding the very first token to an **empty** file without any
existing admin token (bootstrap case) — generate it once, outside the
cluster, using the same image:

```bash
docker run --rm -v "$PWD/api_tokens.yaml:/app/api_tokens.yaml" \
  <your-registry>/hivepilot:<tag> \
  hivepilot tokens add --role admin --note "k8s bootstrap"
# -> prints the plaintext token ONCE; api_tokens.yaml now has the hash.
```

Then supply both to the chart:

```bash
helm upgrade --install hivepilot deploy/helm/hivepilot \
  --set-file secrets.apiTokensYaml=./api_tokens.yaml \
  --set secrets.data.HIVEPILOT_API_TOKEN=<the plaintext token printed above>
```

`secrets.data.HIVEPILOT_API_TOKEN` is required whenever `scheduler.enabled`
is true — the scheduler daemon calls `_require_cli_role("run", token)` on
startup and refuses to run without a token of role `run` or higher (`admin`
qualifies). This is exactly why `scheduler.enabled` defaults to `false`: a
bare `helm install` has no `secrets.data`, so a default-enabled scheduler
would CrashLoopBackOff. Until you complete this bootstrap, the API starts
with an empty token store — every `/v1` write endpoint returns `401`
(fail-closed by design), but the unauthenticated `/healthz`/`/readyz`/
`/metrics` endpoints still work, so the pod stays healthy/ready even
pre-bootstrap. Once you've bootstrapped a token, enable the scheduler with
`--set scheduler.enabled=true --set secrets.data.HIVEPILOT_API_TOKEN=<token
with role run or higher>`.

Adding/rotating tokens later means repeating this process (generate outside
the cluster against a copy of the current `api_tokens.yaml`, then
`helm upgrade`) — the Secret-backed mount is read-only in-cluster, so
`hivepilot tokens add` run *inside* a live pod would not persist across a
restart. This is an intentional, documented tradeoff for a first Helm chart;
wiring an external secrets manager (option 2/3 above) if you need live token
rotation without a chart upgrade.

## Enabling components (bots, scheduler)

- `api.enabled` (default `true`) — the control API + `/ui` Mirador frontend
  (if `env.enableWebui: true`). Doesn't need a token to start.
- `scheduler.enabled` (default `false`) — the schedule/retry daemon. Opt-in
  because it needs a run-role `HIVEPILOT_API_TOKEN` (see "Bootstrap the
  first admin token" above) to start; without one it CrashLoopBackOffs. Once
  you've bootstrapped and set `secrets.data.HIVEPILOT_API_TOKEN`, enable it
  with `--set scheduler.enabled=true`.
- `bots.telegram.enabled` / `bots.slack.enabled` / `bots.discord.enabled`
  (all default `false`) — each is an independent Deployment running
  `hivepilot <name> start --mode <mode>` (blocking, long-lived). Set the
  matching credentials in `secrets.data` (e.g.
  `HIVEPILOT_TELEGRAM_BOT_TOKEN`) — the bot process fails at startup without
  them (`RuntimeError`, surfaced in `kubectl logs`/pod restarts).

## Ingress & TLS

```yaml
ingress:
  enabled: true
  className: nginx
  annotations:
    cert-manager.io/cluster-issuer: letsencrypt-prod
  host: hivepilot.example.com
  tls:
    enabled: true
    secretName: hivepilot-tls   # created by cert-manager, or bring your own
```

Exposes the control API (`/v1/*`, `/metrics`, `/healthz`, `/readyz`) and, if
`HIVEPILOT_ENABLE_WEBUI` is set, the Mirador web UI at `/ui`. `/run` and
other write endpoints still require a Bearer token (see bootstrap section
above) — the Ingress does not add authentication of its own; put it behind a
network policy / auth proxy if you need defense in depth beyond HivePilot's
own token auth.

## Probes

The API Deployment's liveness/readiness probes hit HivePilot's own
unauthenticated endpoints (`hivepilot/services/api_service.py`):

- **Liveness** — `GET /healthz`: a pure "process is up" ping.
- **Readiness** — `GET /readyz`: checks the state DB is reachable and
  `projects.yaml` loads; returns `503` (probe fails, pod removed from the
  Service endpoints) if either check fails.

The scheduler and bot Deployments have no HTTP server, so they rely on
Kubernetes' default container-liveness (process exit == restart); there is
no meaningful HTTP/TCP endpoint to probe for a blocking polling/gateway
process.

## RBAC & the kubectl / helm / kustomize runners

`rbac.create` defaults to **`false`** — no Role/RoleBinding is created, and
the ServiceAccount has no permissions beyond what any unauthenticated
in-cluster identity would have. When enabled, the chart's default
`rbac.rules` grants only `get`/`list`/`watch` on `pods`, `services`,
`configmaps`, `events` (core API group) and `deployments`, `replicasets`
(`apps` API group) — read-only, no write verbs, no wildcard resources. This
section covers the three IaC-adjacent runner plugins that can go beyond
that default if a pipeline exercises their mutating operations:
`hivepilot/runners/kubectl_runner.py`, `hivepilot/runners/helm_runner.py`,
and `hivepilot/runners/kustomize_runner.py`.

**kubectl & helm — same in-cluster blast-radius story.** Both runners shell
out to a real cluster-talking binary (`kubectl`, `helm`) from whichever
pipeline step invokes them. `kubectl apply`/`delete` and `helm
install`/`upgrade`/`rollback`/`uninstall` can create, mutate, or delete
*any* resource kind the underlying RBAC identity is allowed to touch —
including CRDs and other cluster-scoped resources if the identity has
cluster-scoped permissions. Both runners surface their mutating operations
via `is_destructive()`, gated behind HivePilot's own step-level approval
flow (`hivepilot.orchestrator.step_requires_approval`), but **that is an
application-level gate, not a Kubernetes-level one** — a compromised pod or
a bug in the approval logic is bounded only by what RBAC actually grants
this ServiceAccount, never by the approval prompt itself.

**kustomize — local-file-only, but feeds kubectl.** The kustomize runner
never talks to the Kubernetes API itself: `kustomize build` renders
manifests to stdout and `kustomize edit set image`/`set namespace` only
mutate the overlay's `kustomization.yaml` on disk. The actual cluster
mutation happens when a pipeline pipes that rendered output into a
*subsequent* `kubectl apply` step — so the real RBAC boundary for a
kustomize-based pipeline is still the kubectl runner's ServiceAccount, not
the kustomize step itself.

If you enable any of these runners in a pipeline that runs inside this
Deployment, know that:

- Neither the `kubectl` nor the `helm` CLI auto-detects in-cluster
  ServiceAccount credentials the way client-go controllers do — you must
  generate a kubeconfig pointing at the mounted SA token
  (`/var/run/secrets/kubernetes.io/serviceaccount/token`) and the cluster CA,
  and set `KUBECONFIG` to it (e.g. via an `extraVolumeMounts`/`extraEnv` +
  an init step, or bake a kubeconfig template into your own image layer).
  Both runners accept an explicit `kubeconfig` runner option for the same
  purpose.
- Set `rbac.create: true` and start from the minimal, read-only
  `rbac.rules` default in `values.yaml` — widen only as far as the
  pipelines you actually run require (e.g. add `apps`/`get,list,watch,
  create,update,patch,delete` on `deployments` for a `kubectl apply`
  pipeline), and **never grant `cluster-admin`**.
- Prefer `rbac.scope: Role` (namespaced) over `ClusterRole` unless a
  pipeline genuinely needs cross-namespace access — this applies equally to
  kubectl, helm (chart installs that create cluster-scoped CRDs are the
  main reason a pipeline might reach for `ClusterRole`; grant it only for
  that specific need), and any downstream `kubectl apply` step consuming
  kustomize output.

## Values reference

See the fully-commented [`values.yaml`](values.yaml) — every key documents
its default and purpose inline. Key sections: `image`, `serviceAccount`,
`rbac`, `api`, `scheduler`, `bots`, `probes`, `service`, `ingress`,
`persistence`, `config`, `secrets`, `env`, `podSecurityContext`.
