# HivePilot — Kustomize

A plain-manifest [Kustomize](https://kustomize.io/) deployment for HivePilot,
mirroring the [Helm chart](../helm/hivepilot) at `deploy/helm/hivepilot`. Use
whichever fits your GitOps workflow better — the two are equivalent in
functionality (same Deployments/Service/ConfigMap/Secret/PVC/ServiceAccount/
RBAC shape), not layered on top of each other.

## Relationship to the Helm chart

| | Helm chart | Kustomize |
| --- | --- | --- |
| Templating | Go templates + `values.yaml` | Strategic-merge / JSON6902 patches over plain YAML |
| Best fit | `helm install`/`helm upgrade`, Helm-based GitOps (Flux `HelmRelease`, ArgoCD Helm source) | `kubectl apply -k`, kustomize-native GitOps (Flux `Kustomization`, ArgoCD Kustomize source), or teams that prefer no templating language |
| Config source | `values.yaml` (+ `--set`/`-f`) | `kustomization.yaml` generators/patches per overlay |

Both render the same core resources: API Deployment (single replica —
SQLite `state.db` is not multi-writer safe), Service, ServiceAccount, PVC,
ConfigMap (non-secret env), Secret (tokens), and an optional
Ingress/RBAC/scheduler/bot Deployment, all gated OFF by default the same way
the chart's `values.yaml` defaults them off.

## Layout

```
deploy/kustomize/
  base/
    kustomization.yaml        # wires in: serviceaccount, pvc, service, deployment,
                               # a configMapGenerator, a secretGenerator, images:
    serviceaccount.yaml
    pvc.yaml
    service.yaml
    deployment.yaml            # API Deployment
    deployment-scheduler.yaml  # OPTIONAL — not wired into base/kustomization.yaml
    deployment-bots-telegram.yaml  # OPTIONAL — not wired into base/kustomization.yaml
    ingress/                   # OPTIONAL sub-kustomization — pulled in by overlays/prod
      kustomization.yaml
      ingress.yaml
    rbac/                      # OPTIONAL sub-kustomization — pulled in by overlays/prod
      kustomization.yaml
      rbac.yaml
  overlays/
    dev/
      kustomization.yaml       # single replica, no ingress, no RBAC, no PVC (emptyDir)
    prod/
      kustomization.yaml       # ingress on, RBAC on, higher resource requests/limits, PVC on
```

`base/ingress` and `base/rbac` are their own small sub-kustomizations (not
bare files) because Kustomize's default load restrictions require any
cross-directory resource reference to be a directory containing its own
`kustomization.yaml` — a bare `../../base/ingress.yaml` file reference from
an overlay is rejected. `deployment-scheduler.yaml` and
`deployment-bots-telegram.yaml` follow the Helm chart's `scheduler.enabled:
false` / `bots.telegram.enabled: false` defaults: present in `base/` but not
referenced by any `kustomization.yaml` today. Enable one the same way
`overlays/prod` enables ingress/rbac — wrap it in its own
`<name>/kustomization.yaml` sub-directory and add that directory to your
overlay's `resources:` list.

## Usage

```bash
# Preview rendered manifests (no cluster required)
kubectl kustomize deploy/kustomize/overlays/dev
kubectl kustomize deploy/kustomize/overlays/prod

# Apply
kubectl apply -k deploy/kustomize/overlays/dev
kubectl apply -k deploy/kustomize/overlays/prod
```

Both overlays set `namespace:` (`hivepilot-dev` / `hivepilot-prod`) — create
it first (`kubectl create namespace hivepilot-dev`) or let your GitOps
controller do so.

## Configuring secrets

**Never commit real tokens/keys.** `base/kustomization.yaml`'s
`secretGenerator` produces an empty placeholder Secret
(`api_tokens.yaml: ""`, no `HIVEPILOT_*` token env vars). Before a real
deployment, either:

1. Point `secretGenerator` at a `.env`-style file you keep gitignored
   (`kustomize edit add secret hivepilot-secret --from-env-file=secrets.env`
   in a local, uncommitted overlay), or
2. Manage the Secret out-of-band (`kubectl create secret`, sealed-secrets,
   external-secrets-operator, or HivePilot's own Infisical/1Password
   secrets-provider plugins resolving `${secret:NAME}` at runtime — see the
   Helm chart README "Secrets options", the same guidance applies here).

See the Helm chart README's "Bootstrap the first admin token" section for
the exact `api_tokens.yaml` bootstrap flow — it's identical regardless of
whether the Secret is rendered by Helm or Kustomize.

## RBAC

`overlays/prod` opts into a namespaced Role/RoleBinding (read-only
`get`/`list`/`watch` on `pods`/`services`/`configmaps`/`events`/
`deployments`/`replicasets` — the same minimal default the Helm chart
ships). `overlays/dev` does not include RBAC at all. Read the Helm chart
README's [RBAC & the kubectl / helm / kustomize
runners](../helm/hivepilot/README.md#rbac--the-kubectl--helm--kustomize-runners)
section before widening these rules for a pipeline that runs
kubectl/helm/kustomize apply/install/upgrade in-cluster — the app-level
destructive-op approval gate is not a Kubernetes-level control.

## Validating changes

```bash
kubectl kustomize deploy/kustomize/overlays/dev >/dev/null
kubectl kustomize deploy/kustomize/overlays/prod >/dev/null
```

Both must exit 0. If `kubectl`/`kustomize` isn't available, at minimum parse
every file as YAML:

```bash
python3 -c "import yaml, glob; [yaml.safe_load(d) for f in glob.glob('deploy/kustomize/**/*.yaml', recursive=True) for d in __import__('yaml').safe_load_all(open(f))]"
```
