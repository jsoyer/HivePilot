# Kubernetes Deployment

HivePilot ships a Helm chart for deploying the API server, the scheduler
daemon, and the optional Telegram/Slack/Discord bots to Kubernetes.

Chart location: [`deploy/helm/hivepilot/`](../../deploy/helm/hivepilot/).
Full install/config/secrets walkthrough (including the single-replica/SQLite
caveat, the ingress+TLS setup, the RBAC blast-radius discussion, and how to
bootstrap the first admin API token): see
[`deploy/helm/hivepilot/README.md`](../../deploy/helm/hivepilot/README.md).

Quick start:

```bash
helm install hivepilot deploy/helm/hivepilot \
  --set image.repository=<your-registry>/hivepilot \
  --set image.tag=<your-tag>
```

For the systemd-based (non-Kubernetes) deployment path, see
[DEPLOYMENT-EXAMPLE.md](DEPLOYMENT-EXAMPLE.md) and
`hivepilot api systemd-unit` / `hivepilot telegram systemd-unit`.
