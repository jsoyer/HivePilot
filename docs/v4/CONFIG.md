# HivePilot V4 — Configuration

All config lives at the repo root (resolved via `settings.resolve_config_path`).

| File | Defines |
|---|---|
| `projects.yaml` | Target repos (path, default_branch, owner_repo, env) |
| `tasks.yaml` | `runners:` (CLI definitions) + `tasks:` (steps, prompt_file, **role**, git actions) |
| `pipelines.yaml` | Pipelines = ordered list of stages (name → task) |
| `roles.py` | The role registry: role → runner kind + model(s) (global defaults) |
| `policies.yaml` | Per-project policy + **role/runner overrides** |
| `config/model_profiles.yaml` | Documentary mirror of the role→runner/model map |
| `prompts/agents/<role>.md` | Each role's system prompt |

## roles.py (the company defaults)

```python
"ceo":            runner=opencode, models=["opencode-go/qwen3.7-max","opencode-go/kimi-k2.6"]  # dual → debate
"chief_of_staff": runner=cursor                                  # Jules (CSO)
"cto":            runner=opencode, models=["opencode-go/kimi-k2.7-code"]  # Blaise, single-model
"developer":      runner=claude
"reviewer":       runner=codex, model="gpt-5.5"                  # Victor
"ciso":           runner=opencode, models=["opencode-go/glm-5.2"]  # Hugo, single-model
"qa":             runner=cursor       # dedicated QA runner (distinct from docs)
"documentation":  runner=gemini
```
`resolve_runner(role, policy)` = these defaults + per-project overrides.

## policies.yaml (per-project)

```yaml
policies:
  default:
    allow_auto_git: true
    require_approval: false
    allow_containers: true
  projects:
    acme:
      allow_auto_git: true        # developer may push + open PR
      require_approval: true      # every run waits for human /approve
      allow_containers: false
      # Optional per-project model/runner overrides:
      # allowed_runners: [opencode, claude]      # whitelist; resolved runner must be in it
      # role_overrides:
      #   cto: { model: opencode-go/glm-5.2 }    # keep runner, change model
      #   qa:  { runner: claude }                # change runner
      # Optional pipeline CVE gate (Phase 21 Sprint 2 — see below):
      # block_on_severity: critical
      # scan_tool: grype                         # grype (default) or osv-scanner
```

- `require_approval: true` → runs queue (`approvals`) until a human approves; `--simulate` bypasses it.
- `allow_auto_git` is enforced: requesting `--auto-git` against a project that forbids it raises.
- `role_overrides` / `allowed_runners` are applied by `resolve_runner`.
- `block_on_severity` / `scan_tool` — the pipeline CVE gate; see "Pipeline CVE gate" below.

### Pipeline CVE gate (`policy.block_on_severity`)

Opt-in, off by default (`block_on_severity: None` — byte-identical behaviour
to before Phase 21 Sprint 2). When a project's policy sets `block_on_severity`
to one of `critical|high|medium|low|negligible|unknown`
(`hivepilot.services.scan_service.SEVERITY_LEVELS`), `Orchestrator._run_task_body`
runs a vulnerability scan (`scan_service.scan_vulnerabilities`, same scanner
`hivepilot scan vulns` uses — `grype` by default, or `osv-scanner` via
`scan_tool: osv-scanner`) against the project's `path` **before** executing
any step of a run, mirroring the `require_approval` pre-execution gate:

- Finding at/above `block_on_severity` → the run is **blocked**: recorded as
  a failed run (`state_service.complete_run(..., "failed", ...)`), a
  notification is sent, and no step/runner is ever invoked. The recorded
  detail carries only the `by_severity` **counts** (e.g. `{'critical': 1,
  ...}`) and the threshold — never raw scanner output or a specific
  package/CVE identifier.
- No finding at/above the threshold → the run proceeds normally.
- `block_on_severity` unset (default) → `scan_vulnerabilities` is never
  called; no overhead, no behaviour change.
- `--simulate` bypasses the gate entirely (no scan, no block) — same as
  `require_approval`.
- **Fail-closed:** if the scan itself fails (scanner not installed, timeout,
  unexpected exit code — anything `scan_vulnerabilities` raises), the run is
  **blocked**, not silently allowed to proceed. A CVE gate the operator opted
  into must never fail open.
- An invalid `block_on_severity` value is rejected eagerly — both by
  `policy_service.get_policy` (raises at the first run against that project)
  and by `hivepilot config validate` (flags it at config-lint time, before
  any run).

## tasks.yaml (a task)

```yaml
acme-developer:
  role: developer                 # role drives runner+model (overrides the step runner)
  steps:
    - name: implementation
      runner: claude              # fallback if no role
      prompt_file: prompts/agents/developer.md
  git:
    commit: true
    push: true
    create_pr: true               # opens a PR via gh (when --auto-git + policy allows)
    draft: true                   # open it as a draft (gh pr create --draft)
    pr_title: "HivePilot: company pipeline implementation"
    branch_prefix: hivepilot

acme-release-gate:
  role: ciso                      # any can_block role's stage may carry the gate's git actions
  steps:
    - name: security-clearance
      runner: opencode
      prompt_file: prompts/agents/ciso.md
  git:
    promote_pr: true              # gh pr ready <branch> — marks the draft PR ready for review
    merge_pr: true                # optional: also merge once ready (method below)
    merge_method: squash          # merge | squash | rebase
    branch_prefix: hivepilot
```

- `draft` (on `create_pr`): open the PR via `gh pr create --draft`. Pair with a
  later gate stage's `promote_pr` so the PR only becomes visible for review once
  a `can_block` role's own verdict clears it.
- `promote_pr`: `gh pr ready <branch>` — promotes an existing draft PR to ready.
  **Gated on the stage's own agent report**: `promote_pr` runs *unless* that
  stage's parsed `status:` is an explicit blocking verdict — one of
  `BLOCK | BLOCKED | REJECT | REJECTED | REQUEST_CHANGES | CHANGES_REQUESTED |
  NEEDS_HUMAN | FAIL | FAILED | DENY | DENIED` — in which case it is skipped
  (logged as `git.promote_skipped_blocked`). The agent status vocabulary is
  heterogeneous: `PASS`, `APPROVE`, `APPROVED`, `CLEARED`, `ADVISORY`, `OK` all
  mean "proceed", so a blocking-verdict **blacklist** (not a PASS-only
  whitelist) is used — the release gate approving with `status: APPROVE` still
  promotes. Absent/unstructured stage output is likewise non-blocking (legacy
  behaviour for tasks that aren't `can_block` roles).
- `merge_pr` (previously undocumented): `gh pr merge <branch> --<merge_method>`
  — Jules' autonomous final approval, since GitHub forbids approving your own
  PR. `merge_method` is `merge` (default) | `squash` | `rebase`. `merge_pr` is
  gated by the same explicit-blocking-verdict check as `promote_pr`, and (when
  both flags are set) `promote_pr` always runs before `merge_pr`.

## Skills (`skills:` field + CLI commands)

A task step (`tasks.yaml`, `TaskStep.skills`) or a pipeline stage
(`pipelines.yaml`, `PipelineStage.skills`) may optionally declare a `skills:`
list — plugin-contributed content (see "Skills" in `docs/v4/PLUGINS.md`) a
runner MAY apply to its own invocation. Absent (`None`) by default: a config
that never references `skills` behaves byte-identically to before this field
existed. When present, it is an ordered list of skill names, deduped on
parse (`b, a, b, c` -> `b, a, c`):

```yaml
# tasks.yaml
acme-developer:
  role: developer
  steps:
    - name: implementation
      runner: claude
      prompt_file: prompts/agents/developer.md
      skills: [sample-skill]        # applied by runners that implement apply_skill
```

```yaml
# pipelines.yaml
pipelines:
  default:
    stages:
      - name: Implementation
        task: acme-developer
        skills: [sample-skill]      # same field, one level up (per-stage)
```

**Fail-closed cross-reference.** `hivepilot config validate` cross-checks
every `skills:` entry against the live skill catalog
(`PluginManager.list_skills()`): an unregistered name is a hard error
("references unknown skill '\<name\>'"), and a skill declaring `min_role`
requires the referencing step/stage's resolved role (the owning task's
`role:`) to satisfy it — never a silent pass on an unrecognized role on
either side of that comparison. See "Skills" in `docs/v4/PLUGINS.md` for the
full contract, and note that **a runner without `apply_skill` support
silently ignores every skill it is handed** — declaring `skills:` on a step
whose runner doesn't implement that optional contract has no effect (no
error, either), the same fail-open-by-design tolerance `applies_to` mismatch
handling has at the runner level.

### Attaching skills to a pipeline stage (`hivepilot stage ...`)

Guided, validated mutation commands (same family as `hivepilot task set-role`
/ `hivepilot role wire` — round-trip YAML writer, dry-run support, refuse
rather than write on any validation failure):

```bash
hivepilot stage attach-skill <pipeline> <stage> <skill>   # append (idempotent, deduped)
hivepilot stage detach-skill <pipeline> <stage> <skill>   # remove (no-op if already absent)
```

Both commands:

- Refuse (exit `1`, write nothing) when *pipeline* or *stage* is unknown,
  listing the valid names.
- Refuse (exit `1`, write nothing) when the prospective result would fail
  `hivepilot config validate` — an unknown *skill* name is caught here via
  the SAME fail-closed cross-reference check described above (reused via
  `apply_and_validate`, never reimplemented in the CLI layer).
- Are idempotent: attaching an already-present skill, or detaching an
  already-absent one, prints "No changes." and exits `0` without writing.
- Support `--dry-run` to print the unified diff without writing.
- Never leave a stage with `skills: []` — `detach-skill` drops the key
  entirely once its last skill is removed, so a fully-detached stage
  round-trips byte-identical to one that never had any skill attached.

```bash
hivepilot stage attach-skill default Implementation sample-skill
# Skill 'sample-skill' attached to pipeline 'default' stage 'Implementation'.

hivepilot stage detach-skill default Implementation sample-skill
# Skill 'sample-skill' detached from pipeline 'default' stage 'Implementation'.
```

Inspect what's actually registered (and therefore attachable) with:

```bash
hivepilot skills list
```

See "`skills list`" in `docs/v4/PLUGINS.md` for its output columns.

## Runner non-interactive invocation

Each CLI runner is invoked headlessly (so real runs don't hang); overridable via
the runner's `options`:

| Runner | Invocation |
|---|---|
| claude / cursor-agent | `--print` flag |
| gemini | `-p "<prompt>"` |
| codex | `exec` subcommand |
| opencode | `run` subcommand, model as `provider/model` (e.g. `opencode-go/kimi-k2.7-code`) |
| vibe (Mistral) | `--prompt "<prompt>"` + `--auto-approve`; no `--model` (model via its own config / `MISTRAL_API_KEY`) |

Override example: `options: { subcommand: exec, model_flag: "-m", prompt_flag: "-p" }`.

### Headless permission mode (autonomous dev)

`claude --print` cannot show an interactive permission prompt, so an agent that
needs to edit files / run commands **hangs to timeout writing nothing** unless a
permission mode is passed. The developer role (Gustave) ships with
`permission_mode="bypassPermissions"` so it writes code and runs the test suite
autonomously — gated by the human plan checkpoint that precedes the Implementation
stage, and scoped to the component repo.

Precedence (first wins): step `metadata.permission_mode` → runner
`options.permission_mode` → role `permission_mode` (roles.py) → global
`HIVEPILOT_CLAUDE_PERMISSION_MODE`. Values: `acceptEdits` (edits only, shell still
gated), `bypassPermissions` (full autonomy), `plan`, `default`. Unset = no flag
(safe for read-only planning agents).

### Reasoning effort

`effort: low|medium|high|xhigh|max` (the closed `EffortLevel` set) can be set at
four levels, resolved by a single **unified precedence**:

```
policy.role_overrides.effort  >  stage/pipeline  >  role  >  runner-default
```

- **policy** — `policies.yaml` `role_overrides[<role>].effort` (top control; a
  stage or step can never override it).
- **stage / pipeline** — `PipelineStage.effort` (per-stage), falling back to
  `PipelineConfig.effort` (pipeline-wide default).
- **role** — `roles.yaml` `Role.effort`.
- A per-step `TaskStep.effort` is a **fallback** applied only when nothing was
  resolved above (it never overrides a stage- or policy-mandated effort).

The resolved level is authoritative on `RunnerDefinition.effort` and reaches
**two** runners:

- **Claude** — translated to the `MAX_THINKING_TOKENS` env var on the `claude`
  subprocess:

  | Effort | `MAX_THINKING_TOKENS` |
  |---|---|
  | `low` | 4000 |
  | `medium` | 12000 |
  | `high` | 24000 |
  | `xhigh` | 40000 |
  | `max` | 63999 |

- **Codex** — translated to the `-c model_reasoning_effort=<level>` CLI flag
  (default `medium` when nothing is resolved and no `options["cli_flags"]`
  escape hatch is set). `xhigh` is passed through literally.

`xhigh` is a HivePilot superset level between `high` and `max`. Every other
runner (gemini, opencode, cursor, ...) has no effort concept and ignores it.
When no `effort` is declared anywhere (the default), Claude sets no
`MAX_THINKING_TOKENS` and Codex emits its `medium` default — byte-identical to
the pre-unification behaviour of each runner.

### Debate / consensus per-pipeline override (`debate:`)

A `debate:` block on `PipelineConfig` (pipeline-wide) and/or `PipelineStage`
(per-stage) opt-INTO the debate judge / challenge arbiter / fail-closed PR
gate (see "Debate judge, challenge arbiter & the fail-closed PR gate" in
[USAGE.md](USAGE.md)) **per pipeline**, instead of only via the global
`Settings` flags. Absent (the default — no `debate:` key at all) is
byte-identical to before this field existed: every value inherits the global
floor.

```yaml
pipelines:
  release:
    description: "release pipeline with debate on"
    debate:
      enable_judge: true          # bool | None, default None
      enable_arbiter: true        # bool | None, default None
      runner: claude              # str | None, default None
      model: claude-opus-4-6      # str | None, default None
      confidence_threshold: 0.7   # float | None, default None — must be in (0, 1]
    stages:
      - name: release-review
        task: release-review-task
        debate:
          confidence_threshold: 0.9   # this stage alone wants a stricter bar
```

All five fields are optional and independently overridable; a block that
sets only one field leaves the rest `None` ("inherit").

**Precedence — resolved by `hivepilot.models.resolve_debate_config`, the
single source of truth also used by the orchestrator
(`Orchestrator._effective_debate`):**

- **`enable_judge` / `enable_arbiter`** — **OR across floor + pipeline +
  stage, STRENGTHEN-ONLY.** A pipeline/stage value of `false` (or absent)
  can **never** turn OFF a global-floor `true`; only an explicit `true` at
  any layer can turn a floor `false` ON. A `debate:` block can only ADD
  gating, never remove operator-mandated gating (fail-closed by
  construction — see the "empty-value-fail-open" bug class this design
  specifically avoids).
- **`runner` / `model` / `confidence_threshold`** — **stage overrides
  pipeline overrides the global floor**, first non-`None` value wins (same
  shape as `model`/`effort` resolution above).

**Threshold validation (fail closed, never silently disables the gate):**
`confidence_threshold` must be a finite number in `(0, 1]` — `0`, negative,
`> 1`, `NaN`, and `inf` are all **rejected at YAML-load time** (a pydantic
`field_validator` on `DebateConfig`, re-checked defense-in-depth by
`hivepilot.services.pipeline_service.validate_debate_config` for both the
pipeline level and every stage level). Loading a `pipelines.yaml` with a bad
threshold raises immediately — it can never reach the gate as a value that
silently means "always pass". Absent (`None`) is always valid — it means
"inherit the floor's `judge_confidence_threshold`", never "no threshold".

**The global `Settings` flags remain the floor.** `enable_debate_judge`,
`enable_challenge_arbiter`, `judge_runner`, `judge_model`,
`judge_confidence_threshold` (env vars `HIVEPILOT_ENABLE_DEBATE_JUDGE` etc.,
see USAGE.md) are the operator-level default every pipeline/stage `debate:`
block resolves against — an operator can still mandate gating fleet-wide via
the floor even if every individual pipeline's YAML is silent on `debate:`.

### Usage capture (tokens/cost/actual-model) — opt-in

`HIVEPILOT_CLAUDE_CAPTURE_USAGE` (default `false`) enables per-step token/cost/
actual-model capture from the claude runner (Phase 24b.2a). Default **off** is
byte-identical to today's behaviour: `capture()` invokes `claude` without
`--output-format json` and returns raw stdout, exactly as before this flag
existed.

When **on**, `capture()` adds `--output-format json`, parses the CLI's JSON
envelope, and:

- still returns only the agent's `result` text as the step output (unchanged
  from the caller's point of view — turning this flag on never changes what
  an agent's output looks like downstream)
- additionally records `input_tokens` / `output_tokens` (from the envelope's
  `usage` object), `total_cost_usd`, and the `model` actually used — persisted
  on `steps.input_tokens` / `steps.output_tokens` / `steps.cost_usd`, and the
  actual `model` overrides the config-resolved model recorded on `steps.model`
  (closing the gap where a profile- or default-model claude step otherwise
  persisted `NULL` for `model` — see the provider/model persistence above)

**Graceful degradation guarantee:** this flag can only ever make a step
behave like flag-off — it can never make a working step fail, and it can
never corrupt step output. If the JSON is malformed, missing the `result`
field, or the CLI errors on the `--output-format json` flag itself (e.g. an
older claude CLI build that doesn't support it), the runner falls back to
raw-text output with `NULL` usage and logs a one-line warning (step/project
name + failure kind only — never output content, tokens, or secrets). A step
that would have succeeded with the flag off always still succeeds with the
flag on.

**Cost is CLI-self-reported only** at capture time — a runner/provider that
doesn't self-report `total_cost_usd` persists `NULL` for `cost_usd`. The
price-map fallback (Phase 24b.2b) doesn't backfill `steps.cost_usd` itself;
it's applied read-only, at query time, by `GET /v1/analytics/cost` (see
"Price map & cost analytics" below).

### Usage capture for non-claude runners — automatic, no flag

Prompt-CLI runners configured in **API mode** (`mode: api` — step metadata or
runner `options`, covering `codex`/`gemini`/`opencode`/`vibe`/`ollama`-kind
runners pointed at `api_provider: openai|anthropic|google|mistral|perplexity|
openrouter`) capture token usage the same way the claude runner does —
persisted on `steps.input_tokens` / `steps.output_tokens` / `steps.model` —
but **with no opt-in flag**. Unlike `claude_capture_usage` (which re-invokes
the CLI a second time with `--output-format json` to obtain usage), an API
call already returns usage in the very same request/response that produces
the reply text, so there is no re-invocation and no behaviour change to the
run itself — capturing it is non-invasive by construction.

**Migration note:** this change also makes `options.mode: api` reachable via
`capture()` for the FIRST time in the primary execution path — before this,
`capture()` ignored `mode` entirely and always ran the CLI-subprocess branch
regardless of an `api`-mode config, so a misconfigured `mode: api` step was
silently running its CLI fallback instead. Operators with an existing
`mode: api` prompt-cli config should verify the provider's `*_API_KEY`
(`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, `MISTRAL_API_KEY`,
`PERPLEXITY_API_KEY`, or `OPENROUTER_API_KEY`) is actually set before
upgrading — those steps now hard-fail with `"<PROVIDER>_API_KEY missing"`
instead of quietly falling back to the CLI.

- **Providers whose response is read for usage:** `openai`, `mistral`,
  `perplexity`, `openrouter` (OpenAI-compatible `usage.prompt_tokens` /
  `usage.completion_tokens`), `anthropic` (`usage.input_tokens` /
  `usage.output_tokens`), `google`/Gemini (`usageMetadata.promptTokenCount` /
  `usageMetadata.candidatesTokenCount`).
- **`model`** is set only when the provider's response body echoes it back
  (openai/anthropic-shaped responses do; Gemini's `generateContent` response
  does not, so `steps.model` stays whatever the config already resolved).
- **`cost_usd` stays `NULL`** for every one of these providers — none of them
  report cost in the plain request shape used here — so the price-map
  fallback (`HIVEPILOT_LLM_PRICE_MAP`, above) is what estimates cost for
  these steps at query time, exactly as it already does for claude steps
  that didn't self-report cost.
- **Never invented, never crash-prone:** only fields the response actually
  carries are persisted. A response without a `usage` object, or with an
  unexpected shape, degrades to no usage captured (existing behaviour,
  unchanged output text) rather than failing the step; any degradation is
  logged as a one-line warning with the provider name only — never response
  content, prompt text, or API keys.

### Price map & cost analytics (Phase 24b.2b — closes Phase 24)

`hivepilot.services.pricing` supplies a small default USD-per-1M-token price
table (`input`/`output` rate per model), used as a **fallback** estimate by
`GET /v1/analytics/cost` (`hivepilot.services.analytics_service.cost_summary`)
whenever a step has no self-reported `cost_usd`. **The defaults are
indicative and dated (2026-07-15), not a maintained live price feed** —
override or extend them via `HIVEPILOT_LLM_PRICE_MAP`, a JSON object merged
**over** the defaults per-model:

```bash
HIVEPILOT_LLM_PRICE_MAP='{"claude-sonnet-4-6": {"input": 3.0, "output": 15.0}, "my-custom-model": {"input": 1.0, "output": 2.0}}'
```

**Per-step cost precedence** (see `analytics_service._step_cost`):

1. self-reported `steps.cost_usd` (authoritative, when `claude_capture_usage` captured it)
2. estimated from the price map (`pricing.estimate_cost`), when tokens are recorded and the model is priced
3. unpriced — contributes `0.0` to the total, counted in the response's `unpriced_steps` coverage number so a dashboard never presents an incomplete total as if it were exhaustive

See `docs/v4/RUNBOOK.md` "Cost analytics" for the `GET /v1/analytics/cost` endpoint shape.

## IaC runners (terraform / opentofu / pulumi)

Three built-in runner **kinds** wrap the corresponding CLI binary directly
(no capture/parsing of their output — see "Output is not captured" below):
`kind: terraform` (binary `terraform`), `kind: opentofu` (binary `tofu`),
`kind: pulumi` (binary `pulumi`). Declare an instance under `runners:` like
any other runner, then point a task step at it:

```yaml
# tasks.yaml
runners:
  tofu-infra:
    kind: opentofu
    options:
      var_file: prod.tfvars
      parallelism: 4
      workspace: prod

tasks:
  infra-plan:
    description: "Plan infrastructure changes"
    steps:
      - name: plan
        runner: opentofu
        runner_ref: tofu-infra
        command: plan          # -> tofu plan -no-color -var-file=prod.tfvars -parallelism=4
    git: { commit: false, push: false, create_pr: false }
```

### Operation selection

The operation run each invocation is resolved in this precedence order
(first match wins): `step.command` -> `definition.command` ->
`options.operation` -> default (`plan` for terraform/opentofu, `preview`
for pulumi).

### Terraform / OpenTofu operations

| Operation | Command executed | Notes |
|---|---|---|
| `init` | `terraform\|tofu init [-backend-config=<backend_config>]` | Must run once (per fresh checkout / wiped state cache) before `plan`/`apply`/`destroy`/`drift`. |
| `plan` | `... plan -no-color [-var-file=...] [-parallelism=...]` | |
| `apply` | `... apply -auto-approve [-var-file=...] [-parallelism=...]` | Destructive — auto-gates for approval, see "Approval gates" below. |
| `destroy` | `... destroy -auto-approve [-var-file=...] [-parallelism=...]` | Destructive — auto-gates for approval, see "Approval gates" below. |
| `output` | `... output -json` | |
| `validate` | `... validate` | |
| `drift` | `... plan --detailed-exitcode -no-color [-var-file=...] [-parallelism=...]` | Exit code 2 (changes present) is turned into a `RuntimeError("Drift detected: ...")`, not a silent success. |
| `cost` | delegates to `infracost breakdown --path .` | Requires the separate `infracost` CLI on `PATH`. Its stdout is captured internally only for infracost's own debug log — never returned, persisted, or forwarded to any sink (it can reflect secret-backed TF var values). |

### Pulumi operations

| Operation | Command executed | Notes |
|---|---|---|
| `preview` | `pulumi preview [--stack <stack>] [--config k=v ...]` | |
| `up` | `pulumi up --yes [--stack ...] [--config ...]` | Destructive — auto-gates for approval, see "Approval gates" below. |
| `destroy` | `pulumi destroy --yes [--stack ...] [--config ...]` | Destructive — auto-gates for approval, see "Approval gates" below. |
| `output` | `pulumi stack output --json [--stack ...]` | |
| `refresh` | `pulumi refresh --yes [--stack ...] [--config ...]` | Destructive — auto-gates for approval, see "Approval gates" below. |

### Options table

| Option | Applies to | Meaning |
|---|---|---|
| `operation` | all | Fallback operation when neither `step.command` nor `definition.command` is set. |
| `var_file` | terraform/opentofu | Appends `-var-file=<path>` to `plan`/`apply`/`destroy`/`drift`. |
| `backend_config` | terraform/opentofu | Appends `-backend-config=<path>` — **`init`-only**. Passing it with any other operation is a usage error in Terraform/OpenTofu itself, so the runner never adds it outside `init`. |
| `parallelism` | terraform/opentofu | Appends `-parallelism=<n>` to `plan`/`apply`/`destroy`/`drift`. |
| `workspace` | terraform/opentofu | Runs `terraform\|tofu workspace select <workspace>` before the operation. |
| `stack` | pulumi | Appends `--stack <name>` to every pulumi subcommand. |
| `config` | pulumi | `dict[str, str]` — each entry becomes `--config key=value`. |

### Secrets and environment

The runner environment is layered `project.env` -> `definition.env` ->
`payload.secrets` (later layers win, on top of the process's own `os.environ`).
`payload.secrets` is populated from a step's `secrets:` block
(`{ENV_VAR_NAME: {source: env, key: ...}}`, resolved through the configured
secrets backend) and from any `${secret:NAME}` references embedded in
`project.env`. Use it to inject `TF_VAR_*` variables or cloud credentials
without hardcoding them in `tasks.yaml`:

```yaml
tasks:
  infra-apply:
    steps:
      - name: apply
        runner: opentofu
        runner_ref: tofu-infra
        command: apply
        secrets:
          TF_VAR_db_password:
            source: env
            key: PROD_DB_PASSWORD
          AWS_SECRET_ACCESS_KEY:
            source: env
            key: PROD_AWS_SECRET_ACCESS_KEY
```

### `init` must precede `plan`/`apply` on a fresh checkout

There is no implicit `init`. Running `plan`/`apply`/`destroy`/`drift` against
a fresh checkout (or after the local `.terraform`/Pulumi state cache was
wiped) fails with the underlying CLI's own "not initialized" error — run an
`init` step/task first.

### Missing binary

If the required CLI (`terraform`, `tofu`, `pulumi`, or `infracost` for the
`cost` operation) isn't on `PATH`, the runner raises a clear `RuntimeError`
identifying the missing binary and the runner kind, before ever spawning a
subprocess (`shutil.which` guard) — never a raw `FileNotFoundError`
traceback.

### Output is not captured (v1)

`run()` always executes with `capture_output=False` — `plan`/`apply`/
`preview`/`up`/etc. output streams live to the parent process's stdout and is
**not** returned, stored, or forwarded to any sink (CLI response, the
`/v1/run` API body, Slack/Discord/Telegram notifications). This is
deliberate: plan/apply output can echo `TF_VAR_*` values or Pulumi stack
config, and the `RunResult.detail` path those sinks read from is not
redacted. A safe, counts-only plan-summary capture (no diff body) is
deferred to the Mirador panel sprint (A3) — until then, operators should
watch the run (terminal / systemd journal) rather than expect a persisted
plan artifact.

## Kubernetes (`kubectl`) runner

One built-in runner **kind** wraps the `kubectl` CLI directly (no
capture/parsing of its output — see "Output is not captured" below):
`kind: kubectl` (binary `kubectl`). Declare an instance under `runners:` like
any other runner, then point a task step at it:

```yaml
# tasks.yaml
runners:
  prod-cluster:
    kind: kubectl
    options:
      namespace: payments
      context: prod-eu-west
      kubeconfig: /etc/hivepilot/kubeconfig-prod

tasks:
  k8s-get-pods:
    description: "List pods in the payments namespace"
    steps:
      - name: get
        runner: kubectl
        runner_ref: prod-cluster
        command: get           # -> kubectl get pods -o wide -n payments --context prod-eu-west
        options:
          resource: pods
          output: wide
    git: { commit: false, push: false, create_pr: false }
```

### Operation selection

The operation run each invocation is resolved in this precedence order
(first match wins): `step.command` -> `definition.command` ->
`options.operation` -> default (`get`).

### Operations

| Operation | Command executed | Notes |
|---|---|---|
| `apply` | `kubectl apply -f <manifest>` or `kubectl apply -k <kustomize>` (`kustomize` wins if both set) | Destructive — auto-gates for approval, see "Approval gates" below. |
| `delete` | `kubectl delete -f <manifest>` if `manifest` is set, else `kubectl delete <resource> [<name>]` | Requires either `manifest` or `resource`; raises `ValueError` if neither is set. Destructive — auto-gates for approval. |
| `get` | `kubectl get <resource> [<name>] -o <output>` | Requires `options.resource`; `output` defaults to `wide`. |
| `diff` | `kubectl diff -f <manifest>` | |
| `rollout` | `kubectl rollout <sub> <resource>` where `<sub>` is `options.rollout` (default `status`) | Requires `options.resource`. `<sub>` must be one of `status`/`history`/`pause`/`resume`/`restart`/`undo` — anything else raises `ValueError` (fails closed). `restart`/`undo`/`pause`/`resume` are destructive and auto-gate; `status`/`history` are read-only and never gate. |
| `describe` | `kubectl describe <resource> [<name>]` | Requires `options.resource`. |

Every command additionally appends `-n <namespace>` and `--context <context>`
when those options are set.

### Options table

| Option | Meaning |
|---|---|
| `operation` | Fallback operation when neither `step.command` nor `definition.command` is set. |
| `namespace` | Appends `-n <namespace>` to every command. |
| `context` | Appends `--context <context>` to every command. |
| `kubeconfig` | Sets the `KUBECONFIG` env var for the subprocess (path to a mounted kubeconfig file) rather than a CLI flag. |
| `manifest` | Path passed to `apply -f` / `delete -f` / `diff -f`. |
| `kustomize` | Path passed to `apply -k` (takes precedence over `manifest` for `apply`). |
| `resource` | Resource type for `get`/`delete`/`rollout`/`describe` (e.g. `deployment`, `pods`, `svc`). Required by those operations — raises `ValueError` if missing. |
| `name` | Optional resource name appended after `resource` for `get`/`delete`/`describe`. |
| `output` | `-o <output>` for `get` (default `wide`, e.g. `yaml`, `json`, `name`). |
| `rollout` | Sub-command for `rollout` (`status`/`history`/`pause`/`resume`/`restart`/`undo`, default `status`). |

### Cluster access: in-cluster service account vs mounted kubeconfig

`kubectl` resolves cluster access the same way it always does — no
HivePilot-specific auth path. Two common setups:

- **In-cluster service account** — when HivePilot itself runs inside the
  target cluster (e.g. as a Deployment), `kubectl` auto-discovers the
  in-cluster config from the pod's mounted service-account token; no
  `kubeconfig` option is needed.
- **Mounted kubeconfig** — for out-of-cluster operation (HivePilot running
  elsewhere, targeting one or more remote clusters), set `options.kubeconfig`
  to a path to a kubeconfig file mounted into the HivePilot process/container,
  optionally combined with `options.context` to select a specific context
  inside a multi-cluster kubeconfig.

A cluster-scoped bearer token can also be injected as an env var via
`${secret:}` (see "Secrets and environment" below) if the target cluster's
auth plugin reads its token from the environment rather than the kubeconfig
file itself.

### Secrets and environment

The runner environment is layered `project.env` -> `definition.env` ->
`{KUBECONFIG: options.kubeconfig}` (if set) -> `payload.secrets` (later
layers win, on top of the process's own `os.environ`). Use a step's
`secrets:` block or `${secret:NAME}` references in `project.env` to inject a
cluster token or other credential without hardcoding it in `tasks.yaml`:

```yaml
tasks:
  k8s-apply:
    steps:
      - name: apply
        runner: kubectl
        runner_ref: prod-cluster
        command: apply
        options:
          manifest: deploy/payments.yaml
        secrets:
          KUBE_TOKEN:
            source: env
            key: PROD_CLUSTER_TOKEN
```

### Missing binary

If `kubectl` isn't on `PATH`, the runner raises a clear `RuntimeError`
identifying the missing binary and the runner kind, before ever spawning a
subprocess (`shutil.which` guard) — never a raw `FileNotFoundError`
traceback.

### Output is not captured (v1)

`run()` always executes with `capture_output=False` — every operation's
output streams live to the parent process's stdout and is **not** returned,
stored, or forwarded to any sink (CLI response, the `/v1/run` API body,
Slack/Discord/Telegram notifications). This is deliberate: read operations
such as `kubectl get secret -o yaml` or `kubectl describe` can base64-dump or
echo secret material sourced from the cluster itself (Kubernetes `Secret`
objects), and the `RunResult.detail` path those sinks read from is not
redacted for cluster-sourced values — the Phase 10c choke point
(`redact_text`) only masks values explicitly registered via `${secret:}`
resolution. Operators should watch the run (terminal / systemd journal)
rather than expect captured output in the run record or a notification.

### Auto-gating of destructive operations

`apply`, `delete`, and the mutating `rollout` sub-commands
(`restart`/`undo`/`pause`/`resume`) are classified destructive via the
runner's `is_destructive(payload)` method — the same optional structural
contract the IaC runners implement (see "Approval gates" below for the full
mechanics). This means a step running `kubectl apply`/`delete`, or
`kubectl rollout restart`/`undo`/`pause`/`resume`, **auto-gates behind
approval automatically** — in a pipeline today, and via any future direct
CLI path — with no `require_approval` flag needed. `get`/`diff`/`describe`
and `rollout status`/`rollout history` never auto-gate (read-only).

```yaml
tasks:
  k8s-rollout:
    steps:
      - name: check-status        # rollout status -> never gates
        runner: kubectl
        runner_ref: prod-cluster
        command: rollout
        options: { resource: deployment/payments-api, rollout: status }

      - name: apply-manifest       # apply -> auto-gates, pauses for approval
        runner: kubectl
        runner_ref: prod-cluster
        command: apply
        options: { manifest: deploy/payments.yaml }
```

## Approval gates: task, stage, and step-level

HivePilot has three approval mechanisms, from coarsest to finest:

1. **`policy.require_approval`** (per **project**) — the whole task run
   queues for human approval before any step executes. See "policies.yaml"
   above.
2. **`pause_before`** (per **pipeline stage**, `pipelines.yaml`) — the whole
   run pauses before that stage. See "tasks.yaml" above.
3. **`TaskStep.require_approval`** (per **step**, Phase 17a Part B) — a
   single step within a task pauses for approval, without gating the other
   steps in the same task. Covered in this section.

### Step-level approval gate (`TaskStep.require_approval`)

Every step has a `require_approval: bool` field (default `false`) that gates
that ONE step, runner-agnostic:

```yaml
tasks:
  infra-rollout:
    steps:
      - name: notify-oncall
        runner: shell
        command: "curl -X POST https://hooks/oncall"
        require_approval: true   # pauses before this step regardless of runner
```

**Auto-gating of destructive runner operations.** A step gates iff
`step.require_approval` is `True` **or** the runner declares its
currently-resolved operation destructive via an optional
`is_destructive(payload) -> bool` method (a runner without that method is
never treated as destructive). The IaC and `kubectl` runner kinds implement
it:

| Runner kind | Operations that auto-gate |
|---|---|
| `terraform` / `opentofu` | `apply`, `destroy` |
| `pulumi` | `up`, `destroy`, `refresh` |
| `kubectl` | `apply`, `delete`, `rollout restart`/`undo`/`pause`/`resume` |

`plan`/`preview`/`validate`/`output`/`init`/`drift`/`cost` (IaC runners) and
`get`/`diff`/`describe`/`rollout status`/`rollout history` (`kubectl`) never
auto-gate (read-only/non-mutating). This means a step running
`terraform apply`, `pulumi up`/`destroy`/`refresh`, or `kubectl apply`/
`delete`/`rollout restart`/`undo`/`pause`/`resume` pauses for approval **even
without setting `require_approval` at all** — no config change is needed to
get this protection on those operations. Fail-closed: if `is_destructive`
itself raises, the step is treated as destructive rather than silently
letting a potentially-destructive operation through ungated.

**How approval works at step granularity.** When a gating step is reached,
the run PAUSES at that exact step (run status becomes `PAUSED`) and an
approval request is recorded — the same channels as any other checkpoint:

```bash
hivepilot approvals approve <run_id> --approver alice --token <token>
hivepilot approvals deny    <run_id> --reason "not now" --token <token>
```
or the Telegram `/approve <run_id>` button, or `POST /approvals/<run_id>`
(requires `require_role("approve")`). On **approval**, the task RESUMES from
the paused step — prior steps are **not** re-run and their accumulated
output is preserved. On **reject**, the run is marked `denied` and aborts;
the gating step (and anything after it) never runs.

### The plan -> approve -> apply pattern — now a single task

Because `apply`/`destroy`/`up`/`refresh` auto-gate, `plan` and `apply` can
now live in **one task** — the old requirement to split them into two
tasks (one ungated for `plan`, one `require_approval`-gated for `apply`) is
**no longer required**, though still valid (see below):

```yaml
# tasks.yaml
runners:
  tofu-infra:
    kind: opentofu
    options:
      var_file: prod.tfvars

tasks:
  infra-rollout:
    description: "Plan and apply infrastructure changes in one task"
    steps:
      - name: plan
        runner: opentofu
        runner_ref: tofu-infra
        command: plan            # read-only — never gates
      - name: apply
        runner: opentofu
        runner_ref: tofu-infra
        command: apply            # destructive — auto-gates, no require_approval needed
    git: { commit: false, push: false, create_pr: false }
```

```yaml
# policies.yaml — no per-task gate needed; the step itself gates.
policies:
  default:
    require_approval: false
```

`hivepilot run acme-infra infra-rollout` executes `plan` immediately (output
streams live — see "Output is not captured" above), then pauses (run status
`PAUSED`) before `apply`. A human reviews the plan output and approves via
`hivepilot approvals approve <run_id> --approver alice --token <token>` (or
the Telegram `/approve <run_id>`) — `apply` then runs; `plan` is never
re-executed.

**Alternative: splitting into two tasks/stages is still valid** — e.g. when
a pipeline needs the *whole run* to pause (not just one step), or when
`plan` and `apply` should live in genuinely separate projects/tasks for
other reasons. Two supported ways to split, both unchanged from before this
sprint:

- **`pause_before: true`** on a separate `apply` pipeline stage — pauses the
  whole run before that stage. See "Plan checkpoint" in `docs/v4/RUNBOOK.md`.
- **A separate, `require_approval: true` project entry** pointing at the
  same checkout, with `plan` run against an ungated project and `apply`
  against the gated one.

Neither is the *only* way to gate `apply` anymore — the single-task example
above is now the simpler default.

### Worktree isolation is incompatible with step-level approval

**Step-level approval is NOT supported in a task that uses git worktree
isolation** (`auto_git` + `task.git.commit`/`task.git.push`, when
`settings.worktree_isolation` is enabled). A `StepApprovalPending` pause
unwinds through the worktree's `with` block, whose `finally` unconditionally
runs `git worktree remove --force` — a mid-task pause would silently discard
every prior step's file edits, and a resume would then run against a fresh
worktree from unchanged `HEAD`.

HivePilot detects this up front (before the worktree is even created) and
**refuses to start** such a task, rather than starting it and losing work
later. The exact error:

> `Step-level approval (destructive op / require_approval) is not supported
> in a task that uses git worktree isolation (auto_git + git.commit/push),
> because a mid-task pause would discard the worktree. Move the destructive
> step into its own task or a pipeline stage with pause_before.`

If you hit this, move the gating step into its own task (no `auto_git` +
`git.commit`/`git.push` on that task), or fall back to a `pause_before`
pipeline stage checkpoint (see "The plan -> approve -> apply pattern" above),
which pauses the whole run *between* stages/tasks rather than mid-task, so no
worktree is ever left mid-flight.

## Key environment variables / settings

| Setting | Env | Default |
|---|---|---|
| obsidian_vault | `HIVEPILOT_OBSIDIAN_VAULT` | `…/obsidian-vault/Acme` |
| container_runtime | `HIVEPILOT_CONTAINER_RUNTIME` | `docker` (or `podman`; per-runner override via `options.runtime`) |
| claude_permission_mode | `HIVEPILOT_CLAUDE_PERMISSION_MODE` | — (global fallback; developer role already sets `bypassPermissions`) |
| claude_capture_usage | `HIVEPILOT_CLAUDE_CAPTURE_USAGE` | `false` — opt-in per-step token/cost/actual-model capture; see "Usage capture" above |
| llm_price_map | `HIVEPILOT_LLM_PRICE_MAP` | — (JSON object, merged over `pricing.DEFAULT_PRICE_MAP`); see "Price map & cost analytics" above |
| state_db | `HIVEPILOT_STATE_DB` | `state.db` |
| telegram_bot_token | `HIVEPILOT_TELEGRAM_BOT_TOKEN` / `TELEGRAM_BOT_TOKEN` | — |
| telegram_allowed_chat_ids | `HIVEPILOT_TELEGRAM_ALLOWED_CHAT_IDS` | `[]` (open) |
| telegram_stream_live | `HIVEPILOT_TELEGRAM_STREAM_LIVE` | `true` (live-stream each agent turn to Telegram; silent no-op if Telegram/notification chat id unset) |
| telegram_stream_topics | `HIVEPILOT_TELEGRAM_STREAM_TOPICS` | `false` — When `true` AND `telegram_stream_chat_id` is set, each agent's live-stream turns are routed to their own forum topic in the supergroup. The bot must be admin of the forum supergroup with the `manage_topics` permission. Topic thread IDs are persisted to `.hivepilot/stream_topics.json`. |
| gh_command / git_command | — | `gh` / `git` |

(Settings are `pydantic-settings`; any field is overridable via `HIVEPILOT_<NAME>`.)

## Token-saving caching (L1–L3)

| Setting | Env | Default | Description |
|---|---|---|---|
| `anthropic_prompt_cache` | `HIVEPILOT_ANTHROPIC_PROMPT_CACHE` | `True` | When True, sends prompts as a cacheable system block with `cache_control: ephemeral` to Anthropic. Disable to use plain messages format. |
| `prior_context_mode` | `HIVEPILOT_PRIOR_CONTEXT_MODE` | `cap` | How to build the inter-agent prior_context. `cap`: truncate to `max_prior_context_chars` keeping the tail. `synthesis`: keep only the Plan Synthesis chunk + last chunk. `full`: original join-all behaviour. |
| `max_prior_context_chars` | `HIVEPILOT_MAX_PRIOR_CONTEXT_CHARS` | `8000` | Max characters for `prior_context_mode=cap`. Content beyond this limit is trimmed from the head. |
| `stage_cache_enabled` | `HIVEPILOT_STAGE_CACHE_ENABLED` | `False` | Opt-in SQLite stage memoization. When True, skips the runner on a cache hit and stores results on miss. Disabled when `simulate=True` or `auto_git=True`. |
| `cache_backend` | `HIVEPILOT_CACHE_BACKEND` | `sqlite` | Cache storage backend. `sqlite` reuses `state.db` (zero infra). `redis` requires `redis_url`. |
| `redis_url` | `HIVEPILOT_REDIS_URL` | — | Redis connection URL (e.g. `redis://localhost:6379`). Required when `cache_backend=redis`. |

**Default is SQLite (zero infra, reuses state.db).** Redis is opt-in for the distributed-workers setup (`cache_backend=redis` + `redis_url=redis://...`).
