# Debate, judge & the fail-closed PR gate

This is an opt-in adjudication layer. A CEO-style dual-model debate produces
positions that are synthesized into an Architecture Decision Record (ADR).
An optional independent LLM **judge** scores the debate's confidence, and a
**challenge arbiter** adjudicates any challenges raised against the ADR. The
resulting **verdict** can fail-closed gate PR promotion.

Default: off. All flags default to disabled and the per-pipeline `debate:`
block is absent unless you add it. When nothing is enabled, run behavior is
byte-identical to a HivePilot install with no debate layer at all.

## How it works

1. Two models take dual positions on the work under review.
2. Their positions are synthesized into an ADR.
3. If the judge is enabled, it scores the debate's confidence as a value in
   `(0, 1]`.
4. If the arbiter is enabled, it adjudicates any challenges raised against
   the ADR or the judge's score.
5. The result — confidence, approval/rejection, and supporting detail — is
   persisted as a **verdict** in the `verdicts` table, correlated by
   `run_id`.

Nothing here blocks anything by itself. The verdict is just a recorded
outcome until you also enable the PR gate described below.

## The fail-closed PR gate

When enabled, the verdict gates the `promote_pr` and `merge_pr` git actions.
Promotion is **blocked** whenever the verdict is:

- absent (no verdict was recorded for the run), or
- below the configured confidence threshold, or
- not an approval.

Fail-closed means the default on any doubt is to block, not to allow. A
missing verdict is treated the same as a rejected one — it never falls back
to "allow because we don't know." This is the safety-critical property of
the gate: a broken judge call, a missing runner, or a misconfigured pipeline
all resolve to "don't promote," not to "promote anyway."

## Configuration

There are two tiers: a global floor and an optional per-pipeline/per-stage
override.

**Global floor** (env / `Settings`):

- `enable_debate_judge`
- `judge_runner`
- `judge_model`
- `enable_challenge_arbiter`
- `judge_confidence_threshold` — validated to `(0, 1]`; a bad floor value
  (0, negative, or >1) is rejected at startup, not silently clamped.

**Per-pipeline / per-stage** `debate:` block (`DebateConfig`):

- `enable_judge`
- `enable_arbiter`
- `runner`
- `model`
- `confidence_threshold`

**Precedence (hybride, fail-closed):**

- Enable flags (`enable_judge`, `enable_arbiter`) are OR'd across floor,
  pipeline, and stage, and are **strengthen-only**: a pipeline or stage
  value of `false` — or simply leaving the field absent — can never turn a
  floor-level gate *off*. Only an explicit `true` anywhere in the chain
  turns it *on*.
- Scalars (`runner`, `model`, `confidence_threshold`) resolve
  stage > pipeline > floor, first non-`None` wins.
- A present-but-blank `runner` or `model` (empty string) is rejected at
  config load time — it is not treated as "unset."

Example `pipelines.yaml` fragment:

```yaml
stages:
  - name: implement
    debate:
      enable_judge: true
      enable_arbiter: true
      runner: claude
      model: claude-opus-4-6
      confidence_threshold: 0.75
```

See [CONFIGURATION.md](./CONFIGURATION.md) for the full settings reference.

## Enabling it

Two ways to turn this on:

1. Set the global flags — see `.env.example` for the exact variable names
   (`enable_debate_judge`, `judge_runner`, `judge_model`,
   `enable_challenge_arbiter`, `judge_confidence_threshold`).
2. Add a per-pipeline `debate:` block, as shown above, scoped to the
   pipeline or stage that needs it.

Because both tiers default off, adding neither leaves behavior unchanged —
no verdict is computed, and `promote_pr`/`merge_pr` behave exactly as they
did before this layer existed.

---

# Auto-learning lessons loop

This is a second, independent opt-in loop: it turns finished runs into
validated lessons that get injected into future runs. The pipeline is
**distill → validate → inject**.

Default: off. `enable_lesson_distillation` defaults to disabled, and with it
off no distillation call is made, nothing is written, and no prompt section
is injected — the flags-off path is byte-identical to a build without this
feature.

## Distill

At the end of a run, one LLM call turns that run's verdicts, interactions,
and outcomes into structured **candidate** lessons — text plus a category,
nothing more. The distiller's own self-reported score, if it produces one,
is never trusted; scoring is handled entirely by the validation step below.

The distiller prompt is redacted before it leaves the process — secrets are
masked before being sent to the distilling LLM.

Distillation is skipped entirely when there's no signal to work from: a run
with no verdicts and no interactions produces no candidates and makes no
LLM call.

## Validate (fail-closed anti-poisoning)

Each candidate lesson is validated against the run's **real outcome
signal** — a signal derived from what actually happened in the run, not
from an LLM's self-report of how well it thinks it did.

- Only a genuine positive outcome, at or above the configured score floor,
  validates a lesson.
- A rejected or blocked run can never validate lessons, regardless of what
  the distiller wrote.
- Unvalidated candidates are never injected into future runs — they're
  discarded, not queued or retried.

This is the anti-poisoning guarantee: a run that failed, or was denied by
the debate/judge layer, cannot leave behind lessons that quietly train
future runs to repeat its mistakes.

## Inject

Future runs retrieve only **validated** lessons, scoped by project, role,
and task, ranked by score then recency, and capped at `inject_limit`. They
are added to the prompt as a "Lessons learned" section.

With the feature flags off, this path is byte-identical: no DB query runs,
and no "Lessons learned" section is added to the prompt.

**Optional semantic retrieval** (`enable_semantic`) re-ranks the
already-validated pool using embeddings. This requires the optional
`hivepilot[langchain]` extra, which is lazily imported only when semantic
retrieval is enabled. If the extra is missing, or the embedding call fails
for any reason, retrieval falls back to the plain SQLite score/recency
ranking. Semantic retrieval only **reorders** the validated pool — it never
expands it to include unvalidated candidates.

## Configuration

**Global floor** (`Settings`):

- `enable_lesson_distillation`
- `lesson_distill_runner`
- `lesson_distill_model`
- `lesson_min_score` — validated to `(0, 1]`
- `lesson_inject_limit` — validated to `>= 1`
- `enable_semantic_lesson_retrieval`

**Per-pipeline** `lessons:` block (`LessonsConfig`) — pipeline-level only,
there is no stage tier for this config:

- `enable_distillation`
- `enable_semantic`
- `distill_runner`
- `distill_model`
- `min_score`
- `inject_limit`

**Precedence:** enable flags are strengthen-only, OR'd over the floor
(same rule as the debate layer — a pipeline `false`/absent value cannot
turn a floor-level `true` off). Scalars resolve pipeline > floor, first
non-`None` wins.

Example `pipelines.yaml` fragment:

```yaml
lessons:
  enable_distillation: true
  enable_semantic: false
  distill_runner: claude
  distill_model: claude-sonnet-5
  min_score: 0.6
  inject_limit: 5
```

See [CONFIGURATION.md](./CONFIGURATION.md) for the full settings reference.

## Relationship

The lessons loop consumes the debate layer's verdicts as one of its
outcome signals when distilling candidates. Enabling debate gives the
lessons loop richer signal to validate candidates against — but the two
systems are independently opt-in; you can run either one alone.

Both systems share the same posture: default-off, fail-closed on doubt, and
they add nothing to a run — no extra LLM calls, no prompt changes, no DB
writes — unless explicitly enabled.

## See also

- [CONFIGURATION.md](./CONFIGURATION.md)
- [PIPELINES-AND-ROLES.md](./PIPELINES-AND-ROLES.md)
- [SECURITY.md](./SECURITY.md)
