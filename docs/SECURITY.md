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

### Tool catalog (HP-95) — risk × policy × volatile × idempotency

`tool_catalog.yaml` classifies known tool tokens on four axes:

| Axis | Who owns it | Values |
| --- | --- | --- |
| `risk` | catalog only | `low` / `medium` / `high` / `critical` |
| `defaultPolicy` | catalog; `policies.yaml` `tool_policies` may override | `allow` / `deny` / `require_approval` |
| `volatile` | catalog only | bool |
| `idempotency` | catalog only | bool |

Unknown token → **deny** (fail-closed). Policies may tighten or restate policy; they **must not** mutate `risk`. A `tool_policies` override cannot widen a high/critical tool to `allow` unless the caller also supplies HP-86 `change_class=mechanical`.

This catalog is orthogonal to:

- outward tokens (`hivepilot/outward.py`) — visibility off this machine
- plugin capabilities (`network` / `filesystem` / `subprocess` / `secrets_access` / `env`)

Do not fold those axes into risk tiers. HP-58 `GET /v1/tools` stays a typed-tool listing; catalog resolution is a separate helper (`hivepilot.tool_catalog.resolve`) for the Approvals inbox `kind=tool` (HP-94). HP-61 `change_class` is unchanged.

### Workspace text + isolated JSON memory (HP-96)

`hivepilot/workspace_text.py` applies Coworker-style exact edits (rewritten in Python; no vendored TS/Electron):

- empty `old_text` → append
- a non-unique match → refuse (document unchanged)
- `expected_revision` is required; mismatch → stale refuse
- a result over `max_bytes` → overflow refuse

Isolated memory is JSON **data** (`role=data`), never system/instruction text and never merged into `extra_prompt`. A payload that *looks* like an instruction stays inside the data envelope. HITL memory proposals are HP-101 (`hivepilot/memory_proposals.py`).

### Workspace path confine + `schedules.create` HITL (HP-116)

`hivepilot/workspace_paths.py` is the choke point for agent file paths (Coworker workspace-path pattern, rewritten; no vendored TS):

- relative only (no `/`, `~`, drive letter)
- `..` is refused even when resolve would stay inside the root
- `realpath` / symlink resolution must remain inside the workspace root

HP-110 skill-evolution path checks reuse that lexical + realpath helper and still add hidden-file / `.` rules on top.

`schedules.create` (`hivepilot/schedule_create.py`) is a standing-automation tool. Catalog default is high / `require_approval`. The stored HP-86 class is always `product_fork` (never `mechanical`), so `match_auto` stays HITL even if a rule claims mechanical and `tool_policies` widens to `allow`. YAML is written only after PASS `APPROVED`.

### PASS store (HP-97) — one inbox, decide before side-effect

`hivepilot/pass_store.py` is the unified Approvals inbox (ADR HP-94 / plan coworker-openspace). It extends HP-61 rules rather than adding a second control plane.

| Field | Values |
| --- | --- |
| `kind` | `partition` / `tool` / `memory` / `skill_evolution` (HP-94). Partitioned from the HP-61 action token, which already used `kind`. |
| status | `PENDING` / `APPROVED` / `REJECTED` / `EDITED` / `EXPIRED` |

Rules:

1. **Persist first.** `create_pending` writes `PENDING`. `decide` commits approve/reject/edit/expire **before** any `side_effect` callback. A raising callback cannot roll back the decision. Applying tools is HP-100. Skill trust (provisional↔trusted) is HP-105; evolution drafts are HP-109 and atomic accept is HP-111. Memory apply-after-approve is HP-101.
2. **Edit cannot retarget.** `path`, `revision`, and `expected_revision` stay frozen. Body text / args may change.
3. **`match_auto` composes** tool-catalog policy + HP-61 rules + HP-86 `change_class`. Only `mechanical` may auto-approve. `product_fork` / `security` / `destructive` / `unknown` stay HITL. Catalog `deny` wins; catalog `require_approval` stays HITL (rules may still deny).
4. **Not WhatsApp, not HP-67.** Four Telegram doors stay the presenter surface.

### Idempotency + checkpoint resume (HP-100)

`hivepilot/side_effects.py` + `hivepilot/checkpoints.py` (Coworker tool-gateway / checkpoint pattern, rewritten; no vendored TS):

- ``idempotency_key`` is unique on table ``side_effects``. Resume reuses the same key.
- A tool that needs PASS parks checkpoint ``kind=pending_tool`` **before** the effect.
- HP-95 ``volatile`` tools complete without an effect cache.
- Crash mid-approval → exactly one resume executes the effect.

Applying skill-doctrine is HP-103. Presenter parity is HP-102.
HP-104 skill-cycle events (`hivepilot/skill_events.py`) are a local
idempotent log (OpenSpace `record_skill_event` pattern, rewritten; no
cloud, no pickle). Absence of a measurement is not stored as zero.
HP-105 trust (`hivepilot/skill_trust.py`) is a local two-state ladder
(provisional↔trusted) with `enabled` orthogonal. Unknown revisions are
not implicitly trusted or enabled. Promotion uses distinct completed
inter-runs; attributed failure demotes; ambiguous failure opens a PASS
`trust_review` and does not auto-demote. HP-106
(`hivepilot/skill_signals.py`) classifies tool / env / permission /
skill-defect; a network outage is `env` and must not demote trust. FIX
requires revision + causal event + representative result (drafts are
HP-109; atomic accept is HP-111). HP-107 skill retrieval (`hivepilot/skill_ranker.py`) is local
Okapi BM25 over enabled (and optionally provisional) active revisions —
0 model queries, no pickle, body only after `disclose`.
HP-114 hybrid RRF (`hivepilot/skill_embeddings.py`) is opt-in: flag off
⇒ BM25 unchanged and 0 network. Cache rows are JSON vectors keyed by
`(revision_hash, model, dims)` — never pickle.
HP-108 (`hivepilot/skill_capabilities.py`) maps skill → HP-95 tool tokens
for concierge/chat only. Skill off (HP-105 `enabled=False`) drops those
tokens from the chat allowlist. Cataloged skills with no trust row stay
on. No keyword force-load.
The classifier stays `--tools ""`. Pipeline stage-attach is unchanged.
HP-112 host skills (`hivepilot/host_skills.py`) search that local catalog
and delegate through HivePilot `run_subagent` / `spawn_peer` / pipelines.
HP-117 (`hivepilot/skill_taxonomy.py`) is a tenant-scoped logical package
tree: reclassify updates `logical_id → category_path` only and never
moves skill files. Ambiguous classifier output is `needs_review` (PASS
`taxonomy_review`); there is no disk-layout helper and no cloud browse /
auth / upload surface.
HP-115 (`hivepilot/browser_grant.py`) is a short-lived grant on the
existing HP-68 loopback CDP: tied to `run_id`, issued via PASS
`kind=tool` / `BrowserCDP`, dead when `complete_run` fires. No grant ⇒
no CDP action. HivePilot does not embed Chromium and does not reopen
HP-67.
HP-116 confines workspace paths (`hivepilot/workspace_paths.py`) and
routes `schedules.create` through PASS with class ≠ mechanical
(`hivepilot/schedule_create.py`).
HP-118 (`hivepilot/trace_export.py`) assembles a redacted project-run
projection (metadata / tools / skills / evidence packet) into a **local
ZIP on demand**. Critical findings block the export (no archive written).
There is no cloud reporter or upload path.

### Task traces export (HP-118) — local ZIP, no upload

`hivepilot traces export <run_id> --output traces.zip` is on-demand only:

- Projection is tenant-scoped and run through `redact_value` before ZIP.
- Tools come from persisted steps plus HP-99 `tool_event` refs for that run.
- Skills come from HP-104 `list_skill_events(run_id=…)`.
- Critical structured-verdict findings refuse the write. Destination must
  be a local filesystem path.

### Memory proposals HITL (HP-101)

`hivepilot/memory_proposals.py` (Coworker memory HITL pattern, rewritten; no vendored TS):

- Every **model** write is a PASS proposal (`kind=memory`). Never silent. No Always-allow (`submit` / `match_auto` are not used on this path).
- `pending` / `reject` leave IsolatedJsonMemory and workspace text unchanged.
- `edit` stages user text on the PENDING card (`pass_store.stage_edit`; `decide('edit')` stays terminal and is not an apply signal). `approve` then applies that user text.
- Apply runs only after APPROVED is persisted. The reserved HP-100 `idempotency_key` completes the write at most once.
- **Recall** does not create or require a proposal.
- User **Pollen / vault** writes call `user_write` and mutate the corpus directly (no inbox row).

Presenter parity (Pollen ↔ Telegram cards) is HP-102 (`hivepilot/presenters.py`):
one ``decide_approval()``, same ``approval_id`` on both surfaces, keyboards and
cards only on the Approvals door, owner+TTL via ``pending_confirmation``. Four
Telegram doors stay inbox | approvals | runs | alerts.

### Eval contract + behavior memory (HP-113)

`hivepilot/eval_contract.py` is a deterministic, 0-model harness (Coworker
tool-safety eval pattern, rewritten; no vendored TS, no LLM). Default PR CI
runs it. Cases are explicit (`gate` × `surface` × `token`/`path`) — the
harness does not keyword-route a chat utterance onto a skill or tool.

| Gate | Effect |
| --- | --- |
| `denied` / `traversal` / `pending` | `effect_count == 0` |
| `approve_twice` | `effect_count == 1` via the HP-100 `idempotency_key` |

`hivepilot/eval_behavior_memory.py` is the richer HITL memory suite. It is
**not** default PR CI. Opt in with `HIVEPILOT_BEHAVIOR_MEMORY_EVAL=1` and
the `behavior_memory` pytest marker, or dispatch `.github/workflows/nightly.yml`
(scheduled runs also require repository variable `HIVEPILOT_BEHAVIOR_MEMORY_EVAL=1`).

### Evidence refs (HP-99) — tenant-scoped, redacted, watermarked

`hivepilot/evidence.py` is a local evidence registry (OpenSpace `evidence/*` pattern, rewritten; no cloud, no pickle):

- Refs are keyed by `(tenant, ref_id)`. A foreign-tenant id is missing.
- Ingest redacts registered secret values and secret-looking metadata keys before persist.
- Each ingest emits an HP-40 `change_log` row; the ref **watermark is `change_log.id`**.
- Packets are bounded (`max_chars` / `max_refs`); omitted refs are listed, not silently dropped.
- A `skill_evolution` claim that cites missing or empty refs is **not admissible**. `pass_store.submit` persists PENDING first, then rejects with that reason. HP-109 drafts FIX/DERIVED/CAPTURED into PASS (`hivepilot/skill_evolution.py`) without writing skill files. HP-110 (`hivepilot/skill_evolution_validator.py`) is a read-only safety load: traversal/symlink, size, UTF-8, frontmatter, and secrets reject; widening allowed-tools / hooks / shell / permissions without a specific approval is `needs_human_review`. The validator never writes. HP-111 (`hivepilot/skill_evolution_accept.py`) writes only after PASS approve: reject blocks, `needs_human_review` stays HITL, stale digest/etag is refused, double accept is idempotent, and a sibling journal recovers an interrupted multi-file commit.

### HP-61 change class (mechanical vs ask)

Per-action approval rules (`PUT /v1/approval-rules`) may set optional
`change_class`. This is a label on the **existing** rule, not a second
gate:

| `change_class` | `auto=approve` | otherwise |
| --- | --- | --- |
| `mechanical` | may skip the pending card | `auto=deny` still denies |
| `product_fork` / `security` / `destructive` / `unknown` / omitted | never — pending card | `auto=deny` still denies |

Unknown is the default. A watcher wake (`source` / `woke` / bus kind) is
**not** a class and cannot unlock auto-approve. A metadata claim
(`change_class`, `class`, or `contested`) can only tighten, never widen.
Partition destructive / outward / `merge_pr` invariants are a separate
plane and are not waived by a mechanical rule. See
[docs/adr/2026-09-11-hp61-change-classes.md](./adr/2026-09-11-hp61-change-classes.md).

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

**Telegram door tokens (HP-130a)**: `telegram_bot_token` and the optional
`telegram_bot_token_{inbox,approvals,runs,alerts}` fields are secret-typed
(masked in `Settings` repr and `hivepilot config get`). Prefer the four env
vars (`HIVEPILOT_TELEGRAM_BOT_TOKEN_INBOX` …) over a JSON map so systemd
`EnvironmentFile` stays `KEY=value`. Unset door tokens fall back to
`HIVEPILOT_TELEGRAM_BOT_TOKEN`. Polling may start one Application per
distinct token (HP-130b). Multi-token send/receive picks the door bot
via `telegram_bot_token_for_door` and does not use forum thread ids for
the four doors (HP-130c). Deploy packaging (HP-130d) keeps one telegram
unit and documents the four env vars in shared.env; never log the token
values. No automatic cutover — leftover forum topics 2118–2121 stay
until HP-130e.

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

MCP / OpenAPI credentials (HP-58) are optional Fernet blobs keyed by
`HIVEPILOT_CREDENTIALS_KEY`. The fetch path refuses userinfo, remote HTTP,
redirects, and DNS answers in private/link-local/metadata ranges. GET APIs never
return ciphertext or plaintext — only `has_credentials`. Without the key, literal
secrets are refused and `${env:NAME}` refs stay the only option.

See [DEPLOYMENT.md](./DEPLOYMENT.md) and [DASHBOARD.md](./DASHBOARD.md).

## Fail-closed checklist

- Empty `allowed_runners: []` = deny all, not "no constraint."
- An unresolved secret reference = abort the run.
- A scanner failure (not just a finding) = block the run.
- An error inside a destructiveness check = treat the step as destructive.
- An absent debate/lessons override = inherit the stricter floor value.
- A missing adjudication verdict = block PR promotion.
- A missing `HIVEPILOT_CREDENTIALS_KEY` = refuse literal MCP/OpenAPI secrets (keep `${env:}` refs).
- An MCP/OpenAPI fetch that resolves to a private or metadata address = refuse (SSRF).
- An unknown tool-catalog token = deny (HP-95).
- A `tool_policies` override cannot lower a high/critical risk to automatic without HP-86 `mechanical`.
- A PASS `match_auto` approve requires HP-86 `mechanical`; non-mechanical never auto (HP-97).
- A PASS edit cannot retarget `path` / `revision` (HP-97).
- A PASS decision is persisted before any side-effect callback (HP-97).
- An ``idempotency_key`` is unique; resume reuses it (HP-100).
- A ``pending_tool`` checkpoint around PASS executes the effect at most once (HP-100).
- A volatile catalog tool does not cache its effect (HP-100).
- A model memory write is always a PASS `kind=memory` proposal; Always-allow is refused (HP-101).
- A pending or rejected memory proposal does not mutate IsolatedJsonMemory / workspace text (HP-101).
- Memory apply runs only after APPROVED; edit+approve applies the user text (HP-101).
- Memory recall and user Pollen/vault writes do not go through PASS (HP-101).
- A CDP action without a live run-scoped grant is refused (HP-115).
- End of run (`complete_run`) kills the browser grant; a finished run cannot receive a new one (HP-115).
- Browser grant stays loopback-only and never embeds Chromium or reopens HP-67 (HP-115).
- A workspace path that is absolute, contains `..`, or whose realpath/symlink leaves the root is refused (HP-116).
- `schedules.create` is PASS HITL; stored class is never `mechanical` (HP-116).
- Denied / traversal / pending contract cases execute zero side-effects; approve×2 is one HP-100 effect (HP-113).
- Behavior-memory eval is opt-in nightly and must not keyword-route skills or tools (HP-113).

## See also

- [CONFIGURATION.md](./CONFIGURATION.md)
- [DEBATE-AND-LESSONS.md](./DEBATE-AND-LESSONS.md)
- [PLUGINS.md](./PLUGINS.md)
- [DEPLOYMENT.md](./DEPLOYMENT.md)
