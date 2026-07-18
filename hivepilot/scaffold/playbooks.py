"""Multi-agent collaboration playbook library (Phase 16).

A **playbook** is a self-contained, discoverable TEMPLATE for a common
multi-agent collaboration flow — plan→build→review, fan-out/fan-in
investigation, adversarial propose→challenge→revise — expressed entirely as
config fragments (``pipeline.yaml`` / ``tasks.yaml`` / ``roles.yaml`` /
prompts / a ``README.md``) that are valid against the SAME pydantic models
(``PipelineConfig`` / ``TaskConfig`` / ``hivepilot.roles.Role``) the
orchestrator already loads. No engine code — playbooks are pure config,
built entirely on the existing pipeline/role/task machinery.

``scaffold_playbook(name, target_dir)`` writes a playbook's files into
``target_dir/playbooks/<name>/`` as a self-contained bundle — it never
merges into an operator's existing ``pipelines.yaml`` / ``tasks.yaml`` /
``roles.yaml``. Each playbook's ``README.md`` explains exactly how to merge
its fragments into a real deployment. This mirrors
``hivepilot.scaffold.templates.scaffold_config``'s conflict-check-then-write
shape (``FileExistsError`` unless ``force=True``).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Playbook dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Playbook:
    """A named, self-contained multi-agent collaboration flow template."""

    name: str
    title: str
    description: str
    flow_summary: str
    files: dict[str, str]


# ---------------------------------------------------------------------------
# Playbook 1: plan-build-review — the canonical dev loop.
# ---------------------------------------------------------------------------

_PBR_PIPELINE_YAML = """\
# Playbook: plan-build-review
# Merge the `plan-build-review` entry below into the top-level `pipelines:`
# key of your deployment's pipelines.yaml.
pipelines:
  plan-build-review:
    description: >-
      Canonical plan -> build -> review development loop. A Planner produces
      a technical spec, a Developer implements it (paused for human approval
      before it starts), and a Reviewer inspects the result before it ships.
    stages:
      - name: Planning
        task: plan-build-review-plan
      - name: Implementation
        task: plan-build-review-develop
        pause_before: true
      - name: Review
        task: plan-build-review-review
"""

_PBR_TASKS_YAML = """\
# Playbook: plan-build-review
# Merge these entries into the top-level `tasks:` key of your tasks.yaml.
tasks:
  plan-build-review-plan:
    role: planner
    description: Planner produces a technical spec from the objective.
    steps:
      - name: plan
        runner: claude
        prompt_file: prompts/agents/planner.md
        timeout_seconds: 3600
    git:
      commit: false
      push: false
      create_pr: false

  plan-build-review-develop:
    role: developer
    description: Developer implements the technical spec.
    steps:
      - name: implement
        runner: claude
        prompt_file: prompts/agents/developer.md
        timeout_seconds: 5400
    git:
      commit: true
      push: true
      create_pr: false
      commit_message: "feat: automated implementation"
      branch_prefix: hivepilot

  plan-build-review-review:
    role: reviewer
    description: Reviewer inspects the implementation against the spec.
    steps:
      - name: review
        runner: claude
        prompt_file: prompts/agents/reviewer.md
        timeout_seconds: 3600
    git:
      commit: false
      push: false
      create_pr: true
      pr_title: "HivePilot: automated implementation"
      branch_prefix: hivepilot
"""

_PBR_ROLES_YAML = """\
# Playbook: plan-build-review
# Merge these entries into the top-level `roles:` list of your roles.yaml.
# prompt_file paths are relative to prompts/agents/ — copy this playbook's
# prompts/ directory into your deployment's prompts/agents/ directory
# (or point prompt_file at wherever you place them).
roles:
  - name: planner
    display_name: "Planner"
    title: "Planner"
    prompt_file: "planner.md"
    model_profile: "architecture"
    inputs:
      - objective
    outputs:
      - technical_spec
    can_block: false
    order: 1
    runner: "claude"

  - name: developer
    display_name: "Developer"
    title: "Developer"
    prompt_file: "developer.md"
    model_profile: "coding"
    inputs:
      - technical_spec
    outputs:
      - implementation
      - test_suite
    can_block: false
    order: 2
    runner: "claude"
    permission_mode: "bypassPermissions"

  - name: reviewer
    display_name: "Reviewer"
    title: "Reviewer"
    prompt_file: "reviewer.md"
    model_profile: "coding"
    inputs:
      - implementation
      - technical_spec
    outputs:
      - review_report
    can_block: true
    order: 3
    runner: "claude"
"""

_PBR_PLANNER_PROMPT = """\
# Planner

You are the Planner agent. Your job is to analyse the task objective and
produce a clear, actionable technical specification for the Developer to
implement.

## Inputs
- Objective description
- Any relevant context from the codebase

## Outputs
- Technical specification document
- List of files to create or modify
- Acceptance criteria

## Instructions
1. Read the objective carefully.
2. Identify ambiguities and state your assumptions.
3. Produce a concise technical spec — avoid padding.
4. End with a numbered acceptance-criteria list.
"""

_PBR_DEVELOPER_PROMPT = """\
# Developer

You are the Developer agent. Your job is to implement the technical
specification produced by the Planner.

## Inputs
- Technical specification
- Existing codebase context

## Outputs
- Implementation (code changes)
- Test suite
- Brief implementation notes

## Instructions
1. Read the technical spec in full before writing any code.
2. Follow the project's coding conventions.
3. Write tests alongside the implementation.
4. Keep the implementation focused — do not gold-plate.
"""

_PBR_REVIEWER_PROMPT = """\
# Reviewer

You are the Reviewer agent. Your job is to assess the implementation against
the technical specification and produce an actionable review report.

## Inputs
- Implementation diff
- Technical specification
- Test results

## Outputs
- Review report (approved / changes-requested)
- List of issues found (CRITICAL / HIGH / MEDIUM / LOW)

## Instructions
1. Check the implementation against every acceptance criterion.
2. Flag security issues as CRITICAL.
3. If all criteria are met and no CRITICAL issues exist, output APPROVED.
4. Otherwise output CHANGES REQUESTED with a numbered list of issues.
"""

_PBR_README = """\
# Playbook: plan-build-review

The canonical multi-agent development loop: a Planner writes the spec, a
Developer implements it, a Reviewer checks the result.

## Flow

```
  Objective
     |
     v
+-----------+     technical_spec     +-----------+   implementation   +----------+
|  Planner  | ----------------------> | Developer | ------------------> | Reviewer |
+-----------+                         +-----------+                    +----------+
                                        ^ pause_before: true                 |
                                        (human approves the plan             v
                                         before implementation starts)   review_report
```

Stages: **Planning -> Implementation (paused for approval) -> Review**.

## Files provided

- `pipeline.yaml` — the `plan-build-review` pipeline (3 stages).
- `tasks.yaml` — `plan-build-review-plan` / `-develop` / `-review` tasks.
- `roles.yaml` — `planner`, `developer` (bypassPermissions), `reviewer`
  (can_block).
- `prompts/planner.md`, `prompts/developer.md`, `prompts/reviewer.md`.

## Wiring instructions

1. Copy `prompts/*.md` into your deployment's `prompts/agents/` directory.
2. Merge the `roles:` entries from `roles.yaml` into your `roles.yaml`
   (skip any role name you already define, or rename to avoid a clash).
3. Merge the `tasks:` entries from `tasks.yaml` into your `tasks.yaml`.
4. Merge the `pipelines:` entry from `pipeline.yaml` into your
   `pipelines.yaml`.
5. Run `hivepilot validate` to confirm the merged config is consistent.
6. Run the pipeline: `hivepilot run plan-build-review <project>`.
"""

_PLAN_BUILD_REVIEW = Playbook(
    name="plan-build-review",
    title="Plan -> Build -> Review",
    description="Canonical plan -> build -> review development loop "
    "(Planner -> Developer -> Reviewer).",
    flow_summary="Planning -> Implementation (pause_before: true) -> Review",
    files={
        "pipeline.yaml": _PBR_PIPELINE_YAML,
        "tasks.yaml": _PBR_TASKS_YAML,
        "roles.yaml": _PBR_ROLES_YAML,
        "prompts/planner.md": _PBR_PLANNER_PROMPT,
        "prompts/developer.md": _PBR_DEVELOPER_PROMPT,
        "prompts/reviewer.md": _PBR_REVIEWER_PROMPT,
        "README.md": _PBR_README,
    },
)

# ---------------------------------------------------------------------------
# Playbook 2: explore-synthesize — fan-out/fan-in investigation.
# ---------------------------------------------------------------------------

_ES_PIPELINE_YAML = """\
# Playbook: explore-synthesize
# Merge the `explore-synthesize` entry below into the top-level `pipelines:`
# key of your deployment's pipelines.yaml.
pipelines:
  explore-synthesize:
    description: >-
      Fan-out / fan-in investigation. Three read-only Explorer agents
      independently investigate different angles of a question; a
      Synthesizer agent merges their findings into one report.
    stages:
      - name: Explore Architecture
        task: explore-synthesize-explore-architecture
      - name: Explore Tests
        task: explore-synthesize-explore-tests
      - name: Explore Dependencies
        task: explore-synthesize-explore-deps
      - name: Synthesize
        task: explore-synthesize-synthesize
"""

_ES_TASKS_YAML = """\
# Playbook: explore-synthesize
# Merge these entries into the top-level `tasks:` key of your tasks.yaml.
tasks:
  explore-synthesize-explore-architecture:
    role: explorer-architecture
    description: Investigate the architecture angle of the objective.
    steps:
      - name: explore-architecture
        runner: claude
        prompt_file: prompts/agents/explorer.md
        timeout_seconds: 2400
    git:
      commit: false
      push: false
      create_pr: false

  explore-synthesize-explore-tests:
    role: explorer-tests
    description: Investigate the test-coverage angle of the objective.
    steps:
      - name: explore-tests
        runner: claude
        prompt_file: prompts/agents/explorer.md
        timeout_seconds: 2400
    git:
      commit: false
      push: false
      create_pr: false

  explore-synthesize-explore-deps:
    role: explorer-deps
    description: Investigate the dependency/integration angle of the objective.
    steps:
      - name: explore-deps
        runner: claude
        prompt_file: prompts/agents/explorer.md
        timeout_seconds: 2400
    git:
      commit: false
      push: false
      create_pr: false

  explore-synthesize-synthesize:
    role: synthesizer
    description: Merge the three explorer reports into one synthesis report.
    steps:
      - name: synthesize
        runner: claude
        prompt_file: prompts/agents/synthesizer.md
        timeout_seconds: 3600
    git:
      commit: false
      push: false
      create_pr: false
"""

_ES_ROLES_YAML = """\
# Playbook: explore-synthesize
# Merge these entries into the top-level `roles:` list of your roles.yaml.
# prompt_file paths are relative to prompts/agents/ — copy this playbook's
# prompts/ directory into your deployment's prompts/agents/ directory
# (or point prompt_file at wherever you place them).
#
# Three per-angle Explorer roles (NOT one shared `explorer` role) — each
# declares a single `outputs` key. This matters under
# `context_routing_mode="keyed"` (opt-in): the orchestrator maps a stage's
# whole output blob to EVERY key its producing role declares (when the
# stage's output has no `## KEY` section headers). A single role sharing
# all three keys would have each successive Explore stage clobber the
# others' keys, so by the time Synthesize runs, all three would resolve to
# the last explorer's output. Three single-key roles make the fan-in
# correct under both `full` (prior_chunks) and `keyed` routing.
roles:
  - name: explorer-architecture
    display_name: "Explorer (Architecture)"
    title: "Explorer"
    prompt_file: "explorer.md"
    model_profile: "coding"
    inputs:
      - objective
    outputs:
      - architecture_findings
    can_block: false
    order: 1
    runner: "claude"

  - name: explorer-tests
    display_name: "Explorer (Tests)"
    title: "Explorer"
    prompt_file: "explorer.md"
    model_profile: "coding"
    inputs:
      - objective
    outputs:
      - test_findings
    can_block: false
    order: 1
    runner: "claude"

  - name: explorer-deps
    display_name: "Explorer (Dependencies)"
    title: "Explorer"
    prompt_file: "explorer.md"
    model_profile: "coding"
    inputs:
      - objective
    outputs:
      - deps_findings
    can_block: false
    order: 1
    runner: "claude"

  - name: synthesizer
    display_name: "Synthesizer"
    title: "Synthesizer"
    prompt_file: "synthesizer.md"
    model_profile: "architecture"
    inputs:
      - architecture_findings
      - test_findings
      - deps_findings
    outputs:
      - synthesis_report
    can_block: false
    order: 2
    runner: "claude"
"""

_ES_EXPLORER_PROMPT = """\
# Explorer

You are an Explorer agent. Your job is to investigate ONE angle of the
objective and report findings — you do not implement or modify anything.

## Inputs
- Objective description
- The angle assigned to this stage (architecture / tests / dependencies)

## Outputs
- A findings report scoped to your assigned angle

## Instructions
1. Read the objective and identify what your assigned angle needs to answer.
2. Investigate READ-ONLY — do not edit files, do not run mutating commands.
3. Report concrete findings with file/line references where relevant.
4. Flag open questions and unknowns explicitly rather than guessing.
5. Keep the report focused on your angle — the Synthesizer will merge it
   with the other Explorers' reports.
"""

_ES_SYNTHESIZER_PROMPT = """\
# Synthesizer

You are the Synthesizer agent. Your job is to merge the findings from every
Explorer stage into one coherent report.

## Inputs
- Architecture findings
- Test findings
- Dependency findings

## Outputs
- A single synthesis report

## Instructions
1. Read every Explorer's findings in full before writing anything.
2. Identify agreements, contradictions, and gaps across the reports.
3. Produce one coherent synthesis — do not just concatenate the inputs.
4. Call out any question none of the Explorers answered.
5. End with a recommendation or a numbered list of next steps.
"""

_ES_README = """\
# Playbook: explore-synthesize

Fan-out / fan-in investigation: several read-only Explorer agents each
investigate a different angle in parallel-in-spirit stages, then one
Synthesizer agent merges their findings into a single report.

## Flow

```
                +----------------------------------------------+
                |  Explore Architecture  (explorer-architecture) | --> architecture_findings
Objective ----->+  Explore Tests         (explorer-tests)        | --> test_findings          --> Synthesize --> synthesis_report
                |  Explore Dependencies  (explorer-deps)          | --> deps_findings
                +----------------------------------------------+
                 (three distinct, single-output Explorer roles — read-only investigation)
```

Stages run in this order today (**Explore Architecture -> Explore Tests ->
Explore Dependencies -> Synthesize**) — the current pipeline model executes
stages sequentially, but each Explore stage's task is independent of the
others' outputs, so they can be reordered freely. The Synthesize stage's
task consumes all three explorer outputs (`architecture_findings`,
`test_findings`, `deps_findings`) as its inputs.

Each Explore stage is bound to its OWN role (`explorer-architecture` /
`explorer-tests` / `explorer-deps`), not a single role shared across all
three. This is deliberate, not incidental: under the opt-in
`context_routing_mode="keyed"`, the orchestrator maps a stage's whole
output blob to every key its producing role declares when the output has
no `## KEY` section headers. A single `explorer` role declaring all three
output keys would have each successive Explore stage silently clobber the
previous ones' keys, so by Synthesize time every key would resolve to the
last explorer's output — losing the Architecture and Tests findings. Three
single-output-key roles make the fan-in correct under both the default
`full` (prior_chunks) routing and the opt-in `keyed` routing.

## Files provided

- `pipeline.yaml` — the `explore-synthesize` pipeline (4 stages).
- `tasks.yaml` — three `explore-synthesize-explore-*` tasks + one
  `explore-synthesize-synthesize` task.
- `roles.yaml` — `explorer-architecture`, `explorer-tests`, `explorer-deps`
  (each can_block: false, read-only investigation, single output key) and
  `synthesizer`.
- `prompts/explorer.md` (shared by all three Explorer roles),
  `prompts/synthesizer.md`.

## Wiring instructions

1. Copy `prompts/*.md` into your deployment's `prompts/agents/` directory.
2. Merge the `roles:` entries from `roles.yaml` into your `roles.yaml`.
3. Merge the `tasks:` entries from `tasks.yaml` into your `tasks.yaml`.
4. Merge the `pipelines:` entry from `pipeline.yaml` into your
   `pipelines.yaml`.
5. Run `hivepilot validate` to confirm the merged config is consistent.
6. Run the pipeline: `hivepilot run explore-synthesize <project>`.
"""

_EXPLORE_SYNTHESIZE = Playbook(
    name="explore-synthesize",
    title="Explore -> Synthesize",
    description="Fan-out/fan-in investigation: parallel read-only Explorers "
    "feeding one Synthesizer.",
    flow_summary="Explore Architecture + Explore Tests + Explore Dependencies -> Synthesize",
    files={
        "pipeline.yaml": _ES_PIPELINE_YAML,
        "tasks.yaml": _ES_TASKS_YAML,
        "roles.yaml": _ES_ROLES_YAML,
        "prompts/explorer.md": _ES_EXPLORER_PROMPT,
        "prompts/synthesizer.md": _ES_SYNTHESIZER_PROMPT,
        "README.md": _ES_README,
    },
)

# ---------------------------------------------------------------------------
# Playbook 3: propose-challenge-revise — adversarial proposal loop.
# ---------------------------------------------------------------------------

_PCR_PIPELINE_YAML = """\
# Playbook: propose-challenge-revise
# Merge the `propose-challenge-revise` entry below into the top-level
# `pipelines:` key of your deployment's pipelines.yaml.
pipelines:
  propose-challenge-revise:
    description: >-
      Adversarial proposal loop. An Author produces a proposal, a
      Challenger (can_block) adversarially probes it, and the Author
      revises based on the challenge before it ships.
    stages:
      - name: Propose
        task: propose-challenge-revise-propose
      - name: Challenge
        task: propose-challenge-revise-challenge
        pause_before: true
      - name: Revise
        task: propose-challenge-revise-revise
"""

_PCR_TASKS_YAML = """\
# Playbook: propose-challenge-revise
# Merge these entries into the top-level `tasks:` key of your tasks.yaml.
tasks:
  propose-challenge-revise-propose:
    role: author
    description: Author produces an initial proposal for the objective.
    steps:
      - name: propose
        runner: claude
        prompt_file: prompts/agents/author.md
        timeout_seconds: 3600
    git:
      commit: false
      push: false
      create_pr: false

  propose-challenge-revise-challenge:
    role: challenger
    description: Challenger adversarially probes the proposal.
    steps:
      - name: challenge
        runner: claude
        prompt_file: prompts/agents/challenger.md
        timeout_seconds: 3600
    git:
      commit: false
      push: false
      create_pr: false

  propose-challenge-revise-revise:
    role: author
    description: Author revises the proposal based on the challenge.
    steps:
      - name: revise
        runner: claude
        prompt_file: prompts/agents/author.md
        timeout_seconds: 3600
    git:
      commit: false
      push: false
      create_pr: false
"""

_PCR_ROLES_YAML = """\
# Playbook: propose-challenge-revise
# Merge these entries into the top-level `roles:` list of your roles.yaml.
# prompt_file paths are relative to prompts/agents/ — copy this playbook's
# prompts/ directory into your deployment's prompts/agents/ directory
# (or point prompt_file at wherever you place them).
#
# This is the config-level pattern only: `challenger.can_block` and the
# `pause_before` on the Challenge stage use the EXISTING can_block +
# AgentReport.challenge fields via prose instructions in the prompt below —
# no new engine code. A future debate->judge playbook (with an LLM judge)
# will build on the separately-shipping debate-judge engine.
roles:
  - name: author
    display_name: "Author"
    title: "Author"
    prompt_file: "author.md"
    model_profile: "coding"
    inputs:
      - objective
      - challenge_report
    optional_inputs:
      - challenge_report
    outputs:
      - proposal
      - revised_proposal
    can_block: false
    order: 1
    runner: "claude"

  - name: challenger
    display_name: "Challenger"
    title: "Challenger"
    prompt_file: "challenger.md"
    model_profile: "architecture"
    inputs:
      - proposal
    outputs:
      - challenge_report
    can_block: true
    order: 2
    runner: "claude"
"""

_PCR_AUTHOR_PROMPT = """\
# Author

You are the Author agent. Your job is to produce a proposal for the
objective, and later revise it based on the Challenger's feedback.

## Inputs
- Objective description
- (Revise stage only) the Challenger's challenge report

## Outputs
- A proposal (Propose stage) or a revised proposal (Revise stage)

## Instructions
1. On the Propose stage: read the objective, produce a clear, concrete
   proposal, and state your key assumptions and open questions.
2. On the Revise stage: read the challenge report in full. Address every
   point raised — either revise the proposal or explain why the challenge
   does not apply. Do not silently ignore a challenge.
3. Keep the proposal actionable — avoid padding.
"""

_PCR_CHALLENGER_PROMPT = """\
# Challenger

You are the Challenger agent. Your job is to adversarially probe the
Author's proposal and surface every weakness before it ships.

## Inputs
- The Author's proposal

## Outputs
- A challenge report

## Instructions
1. Read the proposal in full before writing anything.
2. Actively look for: unstated assumptions, edge cases, security/privacy
   risks, scalability concerns, and simpler alternatives the Author missed.
3. Do not rubber-stamp. If the proposal is genuinely solid, say so
   explicitly and explain why you could not find a substantive weakness.
4. Rank each issue you raise (CRITICAL / HIGH / MEDIUM / LOW).
5. This stage pauses for human review (`pause_before: true`) before it
   runs — your challenge report is the artifact a human (or the Author's
   revise step) acts on next.
"""

_PCR_README = """\
# Playbook: propose-challenge-revise

An adversarial proposal loop: an Author proposes, a Challenger adversarially
probes the proposal, and the Author revises based on the challenge.

## Flow

```
              proposal                    challenge_report
+--------+ -------------> +------------+ ------------------> +--------+
| Author |                | Challenger |                      | Author |
+--------+                +------------+                      +--------+
                            ^ pause_before: true                (Revise)
                            (human reviews before the
                             challenge stage runs)
```

Stages: **Propose -> Challenge (paused for human review) -> Revise**.

This is the config-level pattern only — `challenger.can_block: true` and
the Challenge prompt use the EXISTING `can_block` + `AgentReport.challenge`
fields via prose instructions, no new engine code. A future
`debate-judge` playbook (with an LLM judge arbitrating rounds) will build on
the separately-shipping debate-judge engine; this playbook does not depend
on it.

## Files provided

- `pipeline.yaml` — the `propose-challenge-revise` pipeline (3 stages).
- `tasks.yaml` — `propose-challenge-revise-propose` / `-challenge` /
  `-revise` tasks.
- `roles.yaml` — `author` and `challenger` (can_block: true).
- `prompts/author.md`, `prompts/challenger.md`.

## Wiring instructions

1. Copy `prompts/*.md` into your deployment's `prompts/agents/` directory.
2. Merge the `roles:` entries from `roles.yaml` into your `roles.yaml`.
3. Merge the `tasks:` entries from `tasks.yaml` into your `tasks.yaml`.
4. Merge the `pipelines:` entry from `pipeline.yaml` into your
   `pipelines.yaml`.
5. Run `hivepilot validate` to confirm the merged config is consistent.
6. Run the pipeline: `hivepilot run propose-challenge-revise <project>`.
"""

_PROPOSE_CHALLENGE_REVISE = Playbook(
    name="propose-challenge-revise",
    title="Propose -> Challenge -> Revise",
    description="Adversarial proposal loop: Author proposes, Challenger "
    "(can_block) probes, Author revises.",
    flow_summary="Propose -> Challenge (pause_before: true) -> Revise",
    files={
        "pipeline.yaml": _PCR_PIPELINE_YAML,
        "tasks.yaml": _PCR_TASKS_YAML,
        "roles.yaml": _PCR_ROLES_YAML,
        "prompts/author.md": _PCR_AUTHOR_PROMPT,
        "prompts/challenger.md": _PCR_CHALLENGER_PROMPT,
        "README.md": _PCR_README,
    },
)

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

PLAYBOOKS: dict[str, Playbook] = {
    _PLAN_BUILD_REVIEW.name: _PLAN_BUILD_REVIEW,
    _EXPLORE_SYNTHESIZE.name: _EXPLORE_SYNTHESIZE,
    _PROPOSE_CHALLENGE_REVISE.name: _PROPOSE_CHALLENGE_REVISE,
}


def list_playbooks() -> list[Playbook]:
    """Return every registered playbook, sorted by name."""
    return sorted(PLAYBOOKS.values(), key=lambda p: p.name)


def get_playbook(name: str) -> Playbook | None:
    """Look up a playbook by name. Returns None if unknown."""
    return PLAYBOOKS.get(name)


def scaffold_playbook(name: str, target_dir: Path, *, force: bool = False) -> list[Path]:
    """Scaffold playbook *name*'s files into ``target_dir/playbooks/<name>/``.

    Mirrors ``hivepilot.scaffold.templates.scaffold_config``'s shape: a
    conflict-check pass before any write (unless ``force=True``), then
    writes every file and returns the absolute paths written.

    Parameters
    ----------
    name:
        The playbook's registered name (see :data:`PLAYBOOKS`).
    target_dir:
        The deployment config directory. Files land under
        ``target_dir/playbooks/<name>/`` — never merged into
        ``target_dir``'s own pipelines.yaml/tasks.yaml/roles.yaml.
    force:
        When ``True``, overwrite files that already exist. When ``False``
        (default), raise :exc:`FileExistsError` if any target file exists.

    Raises
    ------
    KeyError
        If *name* is not a registered playbook.
    FileExistsError
        If ``force=False`` and one or more target files already exist.
    """
    playbook = PLAYBOOKS.get(name)
    if playbook is None:
        raise KeyError(f"Unknown playbook: {name!r}. Known playbooks: {sorted(PLAYBOOKS)}")

    base = Path(target_dir) / "playbooks" / name

    if not force:
        conflicts = [base / rel for rel in playbook.files if (base / rel).exists()]
        if conflicts:
            raise FileExistsError(
                "Files already exist (use force=True to overwrite): "
                + ", ".join(str(p) for p in conflicts)
            )

    created: list[Path] = []
    for rel, content in playbook.files.items():
        dest = base / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content)
        created.append(dest)

    return created
