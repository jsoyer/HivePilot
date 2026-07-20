# Security & Safety Model

HivePilot runs AI agents that can modify code and infrastructure, so its controls are
fail-closed by design: an absent, empty, or errored value resolves to the stricter
outcome, never the more permissive one. This doc consolidates every safety and
security mechanism in one place — read it before relying on any of them.

## Dry-run & simulate

Start here before running anything against a real project.

- `hivepilot run-pipeline` defaults to `--dry-run` — safe, no agent calls, no writes.
  Pass `--no-dry-run` explicitly to execute for real.
- `hivepilot run --simulate` previews the run plan with **no real agent calls** at all.

```bash
# Preview only — no execution
hivepilot run-pipeline my-pipeline

# Execute for real
hivepilot run-pipeline my-pipeline --no-dry-run
```

## Approval gates (three tiers + auto-gating)

Three independent, composable places to require a human in the loop:

1. **Policy-level** — `require_approval` in `policies.yaml`, applies broadly.
2. **Stage-level** — `pause_before: true` on a pipeline stage, e.g. as a plan-review
   checkpoint before a stage runs.
3. **Step-level** — `require_approval: true` on an individual step.

On top of these, HivePilot **auto-gates destructive operations**. A runner can
declare a step destructive via `is_destructive(payload)`. A step is gated if
`require_approval` is set **or** the runner reports the step as destructive — and
this check is fail-closed: if `is_destructive` itself raises an error, the step is
treated as destructive and gated anyway.

The destructiveness scan happens statically, before the step enters an isolated git
worktree. A pause triggered mid-worktree would be silently lost when the worktree
cleanup runs, so gating is decided up front instead.

Resolve a paused run with:

```bash
hivepilot approvals list
hivepilot approvals approve <id>
hivepilot approvals deny <id>
```

## Runner allow-listing

Policy field `allowed_runners` controls which runners a stage/step may use:

- `None` (unset) — unconstrained, any resolved runner is allowed.
- `[]` (explicit empty list) — **deny all**. An empty list is a real constraint, not
  a falsy value to skip past.

The allow-list is checked against the **final resolved runner**, after
`role_overrides` have already been applied — and policy is applied **last** in the
resolution chain, so a stage author cannot pick an override that escapes it.

## Secrets management

`${secret:NAME}` references inside a project's `secrets:` block or `env` are
resolved **lazily**, at step-assembly time — never at config load time.

`secrets_fail_mode` (policy, default `closed`):

- `closed` — abort the run on any unresolved or errored secret reference.
- `fallback` — try env/file providers first, but still abort if nothing resolves.

Error messages name only the reference name and provider, never the resolved value
or the store path.

**Value masking**: every resolved secret value is registered and substring-masked in
every sink — logs, notifications, the state DB, orchestrator error details, Obsidian
notes, distilled lessons, and artifacts. Cache hits (see the TTL cache below) register
the value for masking too, so a cached value can never leak.

Secrets backends ship as plugins: Infisical, 1Password (Connect **and** direct
service-account), Bitwarden, Vaultwarden (via the `bw` CLI, masked), and **KMS**
(cloud-KMS envelope/direct decryption).

### KMS envelope-encryption backend (`source: kms`)

Decrypts operator-provided **ciphertext** at runtime via the operator's OWN cloud KMS
(`plugins/kms.py`). Nothing sensitive is stored in HivePilot config — only ciphertext
the operator produced with their key. Two spec modes, auto-detected by the `ref.spec`
keys present:

- **direct** — `{"ciphertext": "<base64>"}`: a KMS-encrypted blob (≤4KB) decrypted
  straight by the provider Decrypt API; the plaintext IS the secret.
- **envelope** — `{"encrypted_data_key","ciphertext","iv"[,"tag"]}`: the provider
  KMS-decrypts a small wrapped data key, then the local ciphertext is
  AES-256-GCM-decrypted with it (via `cryptography`). `tag` is optional — when absent,
  the GCM tag is assumed appended to `ciphertext`.

Providers (`ref.spec["provider"]` or `HIVEPILOT_KMS_PROVIDER`): `aws` (boto3), `gcp`
(`google-cloud-kms`), `azure` (`azure-keyvault-keys` + `azure-identity`). `key_id`
comes from `ref.spec["key_id"]` or `HIVEPILOT_KMS_KEY_ID` (required for gcp/azure;
AWS direct mode embeds it in the ciphertext). Install with `pip install
hivepilot[kms]` (add `[cloud]` for the AWS boto3 client).

**Anti-leak:** a decrypted plaintext or data key is never logged or returned in an
error — every KMS error names only the provider / library / spec-field. A tampered
ciphertext fails GCM authentication and leaks nothing.

### 1Password direct service-account (non-Connect) mode

The `onepassword` backend now supports a second auth mode. When `op_connect_host` is
set it uses the Connect SDK as before; when `op_connect_host` is **unset** but
`op_service_account_token` is set, it resolves `op://vault/item/field` directly against
`api.1password.com` via the official async `onepassword-sdk` (`pip install
hivepilot[onepassword]`) — no self-hosted Connect server. Selection is
backward-compatible: an existing Connect host always keeps the Connect path.

### Secret TTL cache / rotation (opt-in)

`secrets_cache_ttl_seconds` (default `0` = **disabled** = always-live, byte-identical
to prior behaviour). When set > 0, resolved secret values are cached **in memory,
process-local**, for that many seconds so a run doesn't re-hit the provider for every
step, and a rotated secret is picked up after the TTL expires.

Security tradeoff (deliberate, opt-in): cached values are plaintext held **only in
memory** — never persisted to disk or the state DB, TTL-bounded (an entry older than
the TTL is discarded and re-fetched), and every hit re-registers the value for masking.
Force an immediate flush after rotating a secret with:

```bash
hivepilot secrets cache-clear
```

See [CONFIGURATION.md](./CONFIGURATION.md) and [PLUGINS.md](./PLUGINS.md).

## Supply-chain / CVE gate

Policy fields `block_on_severity` (unset by default) and `scan_tool` (`grype` or
`osv-scanner`) run a vulnerability scan before any step executes. A finding at or
above the configured severity — or a scanner failure itself — blocks the run
(fail-closed).

Scans are also available on demand:

```bash
hivepilot scan vulns
hivepilot scan sbom
hivepilot scan licenses
```

## License gate

Policy fields `denied_licenses` and `allowed_licenses` (both unset/`None` by default)
run a license-compliance check before any step executes, derived from the same SBOM
`generate_sbom`/the CVE gate already produce (no second scanner tool — `syft` is
reused). `denied_licenses` blocks a run if any dependency carries one of the listed
license ids; `allowed_licenses` blocks a run if any dependency carries a license
*not* in the list (an unrecognized/"UNKNOWN" license counts as a violation). If both
are set, `denied_licenses` takes precedence. Each list must be a **non-empty** list of
non-empty strings when set — an empty list (`[]`) is rejected at config-load/`config
validate` time, since `[]` is falsy and would otherwise be silently indistinguishable
from "gate disabled" (worst case: an intentional empty `allowed_licenses: []`, meaning
"allow nothing", would instead let every run through unchecked). A scanner failure —
including an SBOM that can't be parsed — also blocks the run (fail-closed) — same
guarantee as the CVE gate. The block detail sent to notifications/state only ever
carries a violation *count* or a fixed generic scan-failure message, never package
names or license ids; run `hivepilot scan licenses` for the full detail.

SPDX compound expressions (e.g. `"MIT OR GPL-3.0"`) are split into individual license
IDs before matching: a denied license is caught no matter which operand it appears as
(fail-closed), while an allowlist requires *every* operand to be individually listed —
an `OR` is not satisfied by a single allowed operand.

```bash
hivepilot scan licenses <project> --deny GPL-3.0 --fail-on-violation
hivepilot scan licenses <project> --allow MIT --allow Apache-2.0
```

## Adjudication gates

The opt-in debate judge/arbiter can fail-closed gate `promote_pr` / `merge_pr`
steps: an absent verdict, a low-confidence verdict, or a non-approval verdict all
block the promotion.

The lessons loop only validates candidate lessons against real observed outcomes —
never against an LLM's self-report of success — so the feedback loop cannot be
poisoned by a model claiming success.

Both features are opt-in and default-off. See
[DEBATE-AND-LESSONS.md](./DEBATE-AND-LESSONS.md).

## Prompt-injection validation

Agent inputs pass through prompt-injection validation before use. Container
isolation for agent execution is available via policy (`allow_containers`). This
section is intentionally brief — treat it as a pointer to the mechanism, not a full
spec of its coverage.

## Plugin trust model

Plugins run with full process privileges. HivePilot never fetches plugin code over
the network — the trust boundary is local files (e.g. editing `tasks.yaml`) or
packages already installed via pip.

A plugin that fails to load is skipped, not fatal to startup. Only a name/kind
collision between plugins aborts loading. Plugin availability is further controlled
by `plugins_enabled` and `plugins_disabled`.

See [PLUGINS.md](./PLUGINS.md).

## API authentication & multi-tenancy

The HTTP API authenticates via tokens:

```bash
hivepilot tokens add
hivepilot tokens list
hivepilot tokens rotate <id>
hivepilot tokens remove <id>
```

Tokens are stored as hashes in `api_tokens.yaml`, each bound to a role. The state
store and API are tenant-scoped. Admin-only operations — for example toggling a
plugin — are role-gated.

See [DEPLOYMENT.md](./DEPLOYMENT.md) and [DASHBOARD.md](./DASHBOARD.md).

## Fail-closed checklist

- Empty `allowed_runners: []` = deny all, not "no constraint."
- An unresolved secret reference = abort the run.
- A scanner failure (not just a finding) = block the run.
- An error inside a destructiveness check = treat the step as destructive.
- An absent debate/lessons override = inherit the stricter floor value.
- A missing adjudication verdict = block PR promotion.

## See also

- [CONFIGURATION.md](./CONFIGURATION.md)
- [DEBATE-AND-LESSONS.md](./DEBATE-AND-LESSONS.md)
- [PLUGINS.md](./PLUGINS.md)
- [DEPLOYMENT.md](./DEPLOYMENT.md)
