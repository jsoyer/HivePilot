# Remote agent execution — design

HivePilot can run agents on other machines. Three approaches; **(1) is shipped**,
**(2) and (3) are designed here**.

| # | Approach | Status | Effort | Best for |
|---|---|---|---|---|
| 1 | **SSH per role** | ✅ shipped (PR #28) | low | "CTO on B, dev on C" with hosts you own |
| 2 | **Remote container runtime** | ✅ shipped | low–med | reproducible, isolated remote execution |
| 3 | **Distributed HivePilot daemon** | 🟡 W1+W2 shipped | high | fleet / prod scale, no SSH into hosts |

Recap of (1): a role carries a `host`; its CLI runs via `ssh <host> 'cd <repo> && <cli>'`.
Auth = operator's `~/.ssh` (BatchMode), nothing secret stored. See USAGE.md.

---

## Approach 2 — Remote container runtime

Run an agent's **container** on a remote Docker/Podman host instead of (or on top
of) SSH. Builds directly on the already-shipped configurable container runtime
(`container_runtime` = docker | podman, PR #27).

### How
Docker and Podman both speak to a remote engine without us shelling into it:
- **Docker**: `DOCKER_HOST=ssh://user@hostB` (or `docker --context use hostB`).
- **Podman**: `podman --remote --url ssh://user@hostB/run/podman/podman.sock` (or `CONTAINER_HOST`).

So the container runner only needs a per-runner **`host`/`context`** option that it
translates into the right env var / flag before `docker|podman run`. The image,
volumes, env, and command logic are unchanged.

### Config sketch
```yaml
# a runner definition's options
options:
  runtime: docker          # or podman
  host: ssh://user@hostB   # remote engine endpoint
  image: hivepilot/agent-claude:1.0
```
Implementation: in `container_runner.run`, if `options.host` is set, export
`DOCKER_HOST` (docker) or pass `--remote --url` (podman) for that subprocess.
~30–50 lines + tests; no new architecture.

### Pros / cons
- ➕ Reproducible (pinned image), isolated, reuses existing container + runtime work.
- ➕ Composes with Approach 1 (the *engine* is remote; or SSH + local container).
- ➖ Needs a Docker/Podman daemon reachable on the remote host (+ images present/pullable).
- ➖ Auth strategy for the registry + the remote socket to define.

### Open decisions
- Registry auth for pulling `hivepilot/agent-*` images on the remote host.
- One shared remote engine vs per-agent engines.
- Mount strategy for the repo + agent CLI auth inside the container (see the
  Docker-per-agent plan in Obsidian: mount `~/.claude` etc. read-only).

---

## Approach 3 — Distributed HivePilot daemon

A long-running **HivePilot worker** on each machine; the orchestrator dispatches
tasks to workers over the network instead of SSH-ing per call. This is the
"real distributed system" option.

### Architecture
```
┌────────────┐      dispatch (HTTP/queue)      ┌──────────────┐
│ Orchestr.  │ ───────────────────────────────▶│ worker @ A   │ runs ceo, cos
│  (hub)     │ ◀───────────────────────────────│  (HivePilot) │
│            │      results + live stream        └──────────────┘
│  registry  │ ───────────────────────────────▶┌──────────────┐
│  of workers│ ◀───────────────────────────────│ worker @ B   │ runs cto
└────────────┘                                  └──────────────┘
                                                 ┌──────────────┐
                                                 │ worker @ C   │ runs developer
                                                 └──────────────┘
```

- **Worker**: `hivepilot worker --listen :PORT` (or pulls from a queue). Holds the
  repo(s), the agent CLIs (authed), executes a task, streams output back.
- **Routing**: a role → worker mapping (extends `host` to a worker id). The
  orchestrator picks the worker, sends `{task, payload, prior_context}`.
- **Transport**: start with HTTP (FastAPI — already a dependency) + a shared token;
  later a queue (Redis/NATS) for backpressure + retries.
- **Discovery/health**: workers register with the hub + heartbeat; the hub marks
  unreachable workers and can fail over / queue.
- **Result streaming**: worker streams stdout chunks → hub → existing Telegram
  live stream + interaction log (reuse `stream_agent_turn`).
- **State**: the hub owns `state.db`, approvals, checkpoints; workers are stateless
  executors. The plan checkpoint stays a hub-side gate.

### Phasing
1. **W1** ✅ — `hivepilot worker` HTTP server exposing `POST /run-step` (bearer
   token); a `RemoteWorkerRunner` on the hub, routed automatically when a role's
   `host` is an `http(s)://` URL. Reuses `RunnerPayload` over the wire.
2. **W2** ✅ — worker health in `state_service` (pull model: the hub pings each
   worker's `/health`); `hivepilot workers` lists them with live/unreachable status.
3. **W3** — streamed output (SSE/websocket) → live stream; retries + failover.
4. **W4** — queue transport + concurrency limits per worker.

### Pros / cons
- ➕ No SSH into hosts at call time; persistent, observable, retryable; scales to a fleet.
- ➕ Natural home for per-worker concurrency, health, and load balancing.
- ➖ Real distributed system: auth, network security (mTLS/token), versioning
  (hub/worker must agree on the payload schema), failure handling, deployment.
- ➖ Repo provisioning on workers (clone/sync) still required.

### Open decisions
- Transport: HTTP-only first vs queue from the start.
- Worker auth + network security (shared token vs mTLS).
- How repos get onto workers (pre-provisioned vs hub-driven `git` sync step).
- Hub/worker version compatibility policy.

---

## Recommendation
Ship **Approach 2** next (small, builds on PR #27, immediately useful for isolated
remote runs). Treat **Approach 3** as its own milestone (W1→W4) when you need a
real fleet — start with W1 (HTTP worker + `RemoteWorkerRunner`) behind the same
`host` abstraction so roles don't need to change.
