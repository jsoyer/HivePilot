"""The ratification gate's fail-closed validation ORDER (propose -> ratify
-> dispatch PRD, Sprint 2, spec sections 5 and 6).

Every test here is a fail-OPEN regression guard. This repo has a documented
recurring bug class where an empty or absent value on a gate is read as "no
constraint" and the gate passes; the `outward_actions`/ceiling tests below
exist specifically so a future refactor cannot reintroduce it.

Config is written as REAL YAML and resolved through the REAL
`settings.resolve_config_path` chain rather than mocked, because the
property under test is precisely "validated against LIVE config, never
against the proposal" -- mocking the config loader would test the opposite.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from hivepilot.services import autopilot_queue, partition_service, state_service

PROJECTS_YAML = """
projects:
  acme-api:
    path: /tmp/acme-api
    modules:
      core: apps/core
  acme-web:
    path: /tmp/acme-web
"""

PIPELINES_YAML = """
pipelines:
  bugfix:
    description: quiet, inward-only bug fix
    stages:
      - name: fix
        task: implement
  ship-it:
    description: push a branch and open a PR
    stages:
      - name: ship
        task: ship
  auto-merge:
    description: pushes, opens a PR and merges it
    stages:
      - name: merge
        task: merger
  broken:
    description: names a task that does not exist in tasks.yaml
    stages:
      - name: gone
        task: no-such-task
"""

TASKS_YAML = """
tasks:
  implement:
    description: implement something locally
  ship:
    description: push and open a PR
    git:
      push: true
      create_pr: true
  merger:
    description: push, open a PR and merge it
    git:
      push: true
      create_pr: true
      merge_pr: true
"""


def _policies_yaml(**project_overrides: str) -> str:
    """`policies.yaml` with a fully-configured `acme-api` by default; each
    keyword replaces the whole `acme-api` block so a test can remove exactly
    one ceiling."""
    acme_api = project_overrides.get(
        "acme_api",
        """
      outward_actions:
        - git_push
        - forge_pr
      budget_daily_usd: 100.0
      max_partition_cost_usd: 10.0
      max_task_wall_clock_seconds: 3600""",
    )
    acme_web = project_overrides.get(
        "acme_web",
        """
      outward_actions: []
      budget_daily_usd: 100.0
      max_partition_cost_usd: 10.0
      max_task_wall_clock_seconds: 3600""",
    )
    return f"""
policies:
  default:
    require_approval: true
  projects:
    acme-api:{acme_api}
    acme-web:{acme_web}
"""


@pytest.fixture
def config(tmp_path, monkeypatch):
    """Returns a `write_policies(yaml_text)` callable so a test can swap the
    policy file mid-test and prove the gate re-reads LIVE config."""
    from hivepilot.config import settings
    from hivepilot.services import policy_service

    (tmp_path / "projects.yaml").write_text(PROJECTS_YAML, encoding="utf-8")
    (tmp_path / "pipelines.yaml").write_text(PIPELINES_YAML, encoding="utf-8")
    (tmp_path / "tasks.yaml").write_text(TASKS_YAML, encoding="utf-8")
    (tmp_path / "policies.yaml").write_text(_policies_yaml(), encoding="utf-8")
    monkeypatch.setattr(settings, "config_repo", str(tmp_path), raising=False)
    policy_service.reload_policies()

    def write_policies(text: str) -> None:
        (tmp_path / "policies.yaml").write_text(text, encoding="utf-8")
        policy_service.reload_policies()

    yield write_policies
    policy_service.reload_policies()


@pytest.fixture(autouse=True)
def _zero_spend(monkeypatch):
    monkeypatch.setattr(autopilot_queue, "spent_today_usd", lambda *, tenant="default": 0.0)


# ---------------------------------------------------------------------------
# Plan builders
# ---------------------------------------------------------------------------


def _task(task_id: str = "t1", **overrides: Any) -> dict[str, Any]:
    task: dict[str, Any] = {
        "id": task_id,
        "title": f"Task {task_id}",
        "project": "acme-api",
        "pipeline": "bugfix",
        "prompt": f"do the {task_id} work",
        "depends_on": [],
        "budget": {"wall_clock_seconds": 1500, "cost_usd": 1.5},
        "done_when": ["a repro test passes"],
        "outward": False,
    }
    task.update(overrides)
    return task


def _plan(*tasks: dict[str, Any]) -> dict[str, Any]:
    return {
        "partition_version": 1,
        "source": {"kind": "text", "ref": "docs/bug-1234.md", "digest": "sha256:aaa"},
        "proposer": {"role": "partitioner", "pipeline": "propose-partition", "run_id": 4711},
        "policy": {"max_parallel": 3, "on_task_failure": "continue"},
        "tasks": list(tasks) or [_task()],
    }


def _propose(*tasks: dict[str, Any]) -> tuple[str, str, str]:
    """Create an INNOCUOUS proposal (an inward-only `bugfix` task) and return
    `(partition_id, proposed_json, proposed_digest)`.

    The proposal is deliberately harmless in most tests so that when the
    ratify call is refused it is provably the EDIT that was refused, not the
    original plan -- which is the whole point of validating against live
    config rather than against the proposal.
    """
    proposed_json = json.dumps(_plan(*tasks))
    pid = partition_service.create_partition(plan_json=proposed_json)
    row = partition_service.get_partition(pid)
    assert row is not None
    return pid, proposed_json, str(row["proposed_digest"])


def _ratify(pid: str, plan: dict[str, Any], digest: str, *, consent: bool = False, **kw: Any):
    return partition_service.ratify_partition(
        pid,
        partition_json=json.dumps(plan),
        outward_consent=consent,
        approver=kw.pop("approver", "alice"),
        expected_digest=kw.pop("expected_digest", digest),
        **kw,
    )


def _assert_nothing_dispatched(pid: str) -> None:
    """A refusal must leave the partition exactly as it was: still
    `proposed`, with zero journal rows -- nothing dispatched."""
    row = partition_service.get_partition(pid)
    assert row is not None
    assert row["status"] == "proposed"
    assert row["ratified_json"] is None
    assert row["ratified_by"] is None
    assert partition_service.list_partition_tasks(pid) == []


# ---------------------------------------------------------------------------
# Step 1 -- parse + model validation
# ---------------------------------------------------------------------------


class TestStep1Parse:
    def test_malformed_json_denies_and_dispatches_nothing(self, config) -> None:
        pid, _, digest = _propose()
        with pytest.raises(partition_service.MalformedPlanError):
            partition_service.ratify_partition(
                pid,
                partition_json="{ this is not json",
                outward_consent=False,
                approver="alice",
                expected_digest=digest,
            )
        _assert_nothing_dispatched(pid)

    def test_a_dag_cycle_introduced_by_an_edit_denies(self, config) -> None:
        pid, _, digest = _propose(_task("a"), _task("b"))
        cyclic = _plan(_task("a", depends_on=["b"]), _task("b", depends_on=["a"]))
        with pytest.raises(partition_service.MalformedPlanError):
            _ratify(pid, cyclic, digest)
        _assert_nothing_dispatched(pid)

    def test_malformed_beats_every_later_check(self, config) -> None:
        """Step 1 runs first: a plan that is BOTH malformed and stale must
        report the malformation, never the digest mismatch."""
        pid, _, _ = _propose()
        with pytest.raises(partition_service.MalformedPlanError):
            partition_service.ratify_partition(
                pid,
                partition_json="[]",
                outward_consent=False,
                approver="alice",
                expected_digest="sha256:completely-wrong",
            )


# ---------------------------------------------------------------------------
# Step 2 -- referential (live config)
# ---------------------------------------------------------------------------


class TestStep2Referential:
    def test_edited_json_naming_an_unknown_pipeline_denies(self, config) -> None:
        """The headline acceptance criterion: the proposal was valid, the
        EDIT names a pipeline that does not exist in live config."""
        pid, _, digest = _propose(_task("t1", pipeline="bugfix"))
        edited = _plan(_task("t1", pipeline="totally-made-up"))
        with pytest.raises(partition_service.ReferentialError) as exc:
            _ratify(pid, edited, digest)
        assert "totally-made-up" in str(exc.value)
        _assert_nothing_dispatched(pid)

    def test_unknown_project_denies(self, config) -> None:
        pid, _, digest = _propose()
        with pytest.raises(partition_service.ReferentialError) as exc:
            _ratify(pid, _plan(_task("t1", project="ghost-project")), digest)
        assert "ghost-project" in str(exc.value)
        _assert_nothing_dispatched(pid)

    def test_unknown_module_of_a_known_project_denies(self, config) -> None:
        pid, _, digest = _propose()
        with pytest.raises(partition_service.ReferentialError):
            _ratify(pid, _plan(_task("t1", project="acme-api/no-such-module")), digest)
        _assert_nothing_dispatched(pid)

    def test_a_valid_module_target_resolves(self, config) -> None:
        pid, _, digest = _propose()
        outcome = _ratify(pid, _plan(_task("t1", project="acme-api/core")), digest)
        assert outcome.status == "ratified"

    def test_an_over_long_prompt_is_rejected_by_the_shared_validator(self, config) -> None:
        """The SAME `_validate_extra_prompt` a `POST /v1/runs` prompt goes
        through -- never a weaker reimplementation."""
        from hivepilot.services.api_service import MAX_PROMPT_LEN

        pid, _, digest = _propose()
        huge = _plan(_task("t1", prompt="x" * (MAX_PROMPT_LEN + 1)))
        with pytest.raises(partition_service.ReferentialError) as exc:
            _ratify(pid, huge, digest)
        assert "prompt rejected" in str(exc.value)
        _assert_nothing_dispatched(pid)

    def test_unreadable_pipelines_config_denies(self, config, monkeypatch) -> None:
        def boom() -> Any:
            raise RuntimeError("pipelines.yaml is corrupt")

        from hivepilot.services import project_service

        pid, _, digest = _propose()
        monkeypatch.setattr(project_service, "load_pipelines", boom)
        with pytest.raises(partition_service.ReferentialError):
            _ratify(pid, _plan(), digest)
        _assert_nothing_dispatched(pid)


# ---------------------------------------------------------------------------
# Step 3 -- policy, against LIVE config
# ---------------------------------------------------------------------------


class TestStep3MergePrIsRefusedUnconditionally:
    def test_a_merge_pr_pipeline_denies_even_with_consent(self, config) -> None:
        pid, _, digest = _propose()
        with pytest.raises(partition_service.PolicyDeniedError) as exc:
            _ratify(pid, _plan(_task("t1", pipeline="auto-merge")), digest, consent=True)
        assert "merge" in str(exc.value).lower()
        _assert_nothing_dispatched(pid)

    def test_a_merge_pr_pipeline_denies_even_when_forge_merge_is_allowlisted(self, config) -> None:
        """UNCONDITIONAL means unconditional: no allowlist entry and no
        ticked checkbox can ever authorize an auto-merge in a partition."""
        config(
            _policies_yaml(
                acme_api="""
      outward_actions:
        - git_push
        - forge_pr
        - forge_merge
      budget_daily_usd: 100.0
      max_partition_cost_usd: 10.0
      max_task_wall_clock_seconds: 3600"""
            )
        )
        pid, _, digest = _propose()
        with pytest.raises(partition_service.PolicyDeniedError):
            _ratify(pid, _plan(_task("t1", pipeline="auto-merge")), digest, consent=True)
        _assert_nothing_dispatched(pid)


class TestStep3OutwardAllowlist:
    def test_an_outward_pipeline_within_the_allowlist_is_admitted(self, config) -> None:
        pid, _, digest = _propose()
        outcome = _ratify(pid, _plan(_task("t1", pipeline="ship-it")), digest, consent=True)
        assert outcome.status == "ratified"
        assert outcome.outward_actions == ("forge_pr", "git_push")

    def test_empty_outward_actions_denies_every_consent(self, config) -> None:
        """`outward_actions: []` must mean "nothing outward", NEVER "no
        constraint" -- the documented fail-open bug class."""
        config(
            _policies_yaml(
                acme_api="""
      outward_actions: []
      budget_daily_usd: 100.0
      max_partition_cost_usd: 10.0
      max_task_wall_clock_seconds: 3600"""
            )
        )
        pid, _, digest = _propose()
        with pytest.raises(partition_service.PolicyDeniedError) as exc:
            _ratify(pid, _plan(_task("t1", pipeline="ship-it")), digest, consent=True)
        assert "allowlist" in str(exc.value)
        _assert_nothing_dispatched(pid)

    def test_absent_outward_actions_denies_every_consent(self, config) -> None:
        config(
            _policies_yaml(
                acme_api="""
      budget_daily_usd: 100.0
      max_partition_cost_usd: 10.0
      max_task_wall_clock_seconds: 3600"""
            )
        )
        pid, _, digest = _propose()
        with pytest.raises(partition_service.PolicyDeniedError):
            _ratify(pid, _plan(_task("t1", pipeline="ship-it")), digest, consent=True)
        _assert_nothing_dispatched(pid)

    def test_a_partially_covering_allowlist_denies_the_uncovered_action(self, config) -> None:
        config(
            _policies_yaml(
                acme_api="""
      outward_actions:
        - git_push
      budget_daily_usd: 100.0
      max_partition_cost_usd: 10.0
      max_task_wall_clock_seconds: 3600"""
            )
        )
        pid, _, digest = _propose()
        with pytest.raises(partition_service.PolicyDeniedError) as exc:
            _ratify(pid, _plan(_task("t1", pipeline="ship-it")), digest, consent=True)
        assert "forge_pr" in str(exc.value)

    def test_an_unknown_token_in_the_allowlist_authorizes_nothing(self, config) -> None:
        """A typo in `policies.yaml` must never widen anything."""
        config(
            _policies_yaml(
                acme_api="""
      outward_actions:
        - git_pushh
        - forge_prs
      budget_daily_usd: 100.0
      max_partition_cost_usd: 10.0
      max_task_wall_clock_seconds: 3600"""
            )
        )
        pid, _, digest = _propose()
        with pytest.raises(partition_service.PolicyDeniedError):
            _ratify(pid, _plan(_task("t1", pipeline="ship-it")), digest, consent=True)

    def test_the_proposals_own_outward_flag_is_never_trusted(self, config) -> None:
        """`outward: false` on a pipeline that actually pushes must not let
        the plan through -- the flag lives in the operator-editable JSON box
        the gate exists to police."""
        config(
            _policies_yaml(
                acme_api="""
      outward_actions: []
      budget_daily_usd: 100.0
      max_partition_cost_usd: 10.0
      max_task_wall_clock_seconds: 3600"""
            )
        )
        pid, _, digest = _propose()
        lying = _plan(_task("t1", pipeline="ship-it", outward=False))
        with pytest.raises(partition_service.PolicyDeniedError):
            _ratify(pid, lying, digest, consent=True)

    def test_the_proposals_outward_flag_cannot_invent_an_action_either(self, config) -> None:
        """The inverse direction, which proves the flag is not read AT ALL
        rather than merely OR-ed in: `outward: true` on a pipeline that does
        nothing outward must not manufacture a consent requirement."""
        pid, _, digest = _propose()
        boastful = _plan(_task("t1", pipeline="bugfix", outward=True))
        outcome = _ratify(pid, boastful, digest, consent=False)
        assert outcome.status == "ratified"
        assert outcome.outward_actions == ()

    def test_unresolvable_pipeline_config_yields_the_full_outward_set(self, config) -> None:
        """A pipeline that EXISTS but whose task is missing from tasks.yaml
        cannot be statically resolved -- "I cannot tell" must resolve to
        "everything", so it denies (here on the unconditional `forge_merge`
        member of that full set)."""
        assert autopilot_queue.pipeline_outward_actions("broken") == (
            autopilot_queue.OUTWARD_ACTIONS
        )
        pid, _, digest = _propose()
        with pytest.raises(partition_service.PolicyDeniedError):
            _ratify(pid, _plan(_task("t1", pipeline="broken")), digest, consent=True)
        _assert_nothing_dispatched(pid)

    def test_a_second_project_with_its_own_empty_allowlist_denies(self, config) -> None:
        """The allowlist is resolved PER TASK PROJECT, so one permissive
        project can never authorize outward action in another."""
        pid, _, digest = _propose()
        mixed = _plan(
            _task("a", project="acme-api", pipeline="ship-it"),
            _task("b", project="acme-web", pipeline="ship-it"),
        )
        with pytest.raises(partition_service.PolicyDeniedError) as exc:
            _ratify(pid, mixed, digest, consent=True)
        assert "acme-web" in str(exc.value)


class TestStep3Ceilings:
    def test_cost_sum_over_max_partition_cost_denies(self, config) -> None:
        pid, _, digest = _propose()
        expensive = _plan(
            _task("a", budget={"wall_clock_seconds": 600, "cost_usd": 6.0}),
            _task("b", budget={"wall_clock_seconds": 600, "cost_usd": 6.0}),
        )
        with pytest.raises(partition_service.PolicyDeniedError) as exc:
            _ratify(pid, expensive, digest)
        assert "12.00" in str(exc.value)
        _assert_nothing_dispatched(pid)

    def test_cost_sum_over_the_remaining_daily_budget_denies(self, config, monkeypatch) -> None:
        """`min(max_partition_cost_usd, budget_daily_usd - spent_today)` --
        the daily budget can be the binding constraint even when the
        per-partition ceiling is not."""
        config(
            _policies_yaml(
                acme_api="""
      outward_actions: []
      budget_daily_usd: 10.0
      max_partition_cost_usd: 100.0
      max_task_wall_clock_seconds: 3600"""
            )
        )
        monkeypatch.setattr(autopilot_queue, "spent_today_usd", lambda *, tenant="default": 9.5)
        pid, _, digest = _propose()
        plan = _plan(_task("a", budget={"wall_clock_seconds": 600, "cost_usd": 2.0}))
        with pytest.raises(partition_service.PolicyDeniedError) as exc:
            _ratify(pid, plan, digest)
        assert "spent_today" in str(exc.value)

    def test_a_cost_sum_exactly_at_the_ceiling_is_admitted(self, config) -> None:
        """The ceiling is inclusive -- `<=`, not `<`. Pinned so a future
        refactor cannot quietly turn a documented boundary into an
        off-by-one refusal."""
        pid, _, digest = _propose()
        plan = _plan(_task("a", budget={"wall_clock_seconds": 600, "cost_usd": 10.0}))
        assert _ratify(pid, plan, digest).status == "ratified"

    def test_absent_max_partition_cost_denies(self, config) -> None:
        config(
            _policies_yaml(
                acme_api="""
      outward_actions: []
      budget_daily_usd: 100.0
      max_task_wall_clock_seconds: 3600"""
            )
        )
        pid, _, digest = _propose()
        with pytest.raises(partition_service.PolicyDeniedError) as exc:
            _ratify(pid, _plan(), digest)
        assert "max_partition_cost_usd" in str(exc.value)

    def test_zero_max_partition_cost_denies_rather_than_meaning_unlimited(self, config) -> None:
        config(
            _policies_yaml(
                acme_api="""
      outward_actions: []
      budget_daily_usd: 100.0
      max_partition_cost_usd: 0
      max_task_wall_clock_seconds: 3600"""
            )
        )
        pid, _, digest = _propose()
        with pytest.raises(partition_service.PolicyDeniedError):
            _ratify(pid, _plan(), digest)

    def test_absent_daily_budget_denies(self, config) -> None:
        config(
            _policies_yaml(
                acme_api="""
      outward_actions: []
      max_partition_cost_usd: 10.0
      max_task_wall_clock_seconds: 3600"""
            )
        )
        pid, _, digest = _propose()
        with pytest.raises(partition_service.PolicyDeniedError) as exc:
            _ratify(pid, _plan(), digest)
        assert "budget_daily_usd" in str(exc.value)

    def test_absent_wall_clock_ceiling_denies(self, config) -> None:
        config(
            _policies_yaml(
                acme_api="""
      outward_actions: []
      budget_daily_usd: 100.0
      max_partition_cost_usd: 10.0"""
            )
        )
        pid, _, digest = _propose()
        with pytest.raises(partition_service.PolicyDeniedError) as exc:
            _ratify(pid, _plan(), digest)
        assert "max_task_wall_clock_seconds" in str(exc.value)

    def test_a_task_over_the_wall_clock_ceiling_denies(self, config) -> None:
        pid, _, digest = _propose()
        plan = _plan(_task("a", budget={"wall_clock_seconds": 3601, "cost_usd": 1.0}))
        with pytest.raises(partition_service.PolicyDeniedError) as exc:
            _ratify(pid, plan, digest)
        assert "3601" in str(exc.value)

    def test_an_unavailable_spend_figure_denies(self, config, monkeypatch) -> None:
        """An unknown budget is not an unlimited budget."""

        def boom(*, tenant: str = "default") -> float:
            raise RuntimeError("analytics unavailable")

        monkeypatch.setattr(autopilot_queue, "spent_today_usd", boom)
        pid, _, digest = _propose()
        with pytest.raises(partition_service.PolicyDeniedError) as exc:
            _ratify(pid, _plan(), digest)
        assert "unknown budget" in str(exc.value)

    def test_a_project_with_no_policy_block_at_all_denies(self, config) -> None:
        config(
            """
policies:
  default:
    require_approval: true
"""
        )
        pid, _, digest = _propose()
        with pytest.raises(partition_service.PolicyDeniedError):
            _ratify(pid, _plan(), digest)


class TestPolicyIsReadLiveNotFromTheProposal:
    def test_a_proposal_valid_at_propose_time_is_refused_after_policy_tightens(
        self, config
    ) -> None:
        """Config can change between propose and ratify. The gate must read
        the policy that is live NOW, never one cached at propose time."""
        from hivepilot.partition import load_partition

        pid, proposed_json, digest = _propose(_task("t1", pipeline="ship-it"))
        # It would have passed a moment ago...
        assert partition_service.assess_outward(load_partition(proposed_json)).actions == frozenset(
            {"git_push", "forge_pr"}
        )
        config(
            _policies_yaml(
                acme_api="""
      outward_actions: []
      budget_daily_usd: 100.0
      max_partition_cost_usd: 10.0
      max_task_wall_clock_seconds: 3600"""
            )
        )
        with pytest.raises(partition_service.PolicyDeniedError):
            _ratify(pid, _plan(_task("t1", pipeline="ship-it")), digest, consent=True)


# ---------------------------------------------------------------------------
# Step 4 -- consent
# ---------------------------------------------------------------------------


class TestStep4Consent:
    def test_consent_false_on_a_pushing_pipeline_denies_naming_the_actions(self, config) -> None:
        pid, _, digest = _propose()
        with pytest.raises(partition_service.ConsentRequiredError) as exc:
            _ratify(pid, _plan(_task("t1", pipeline="ship-it")), digest, consent=False)
        # NAMED, exactly -- never an unnamed "something outward".
        assert exc.value.actions == ("forge_pr", "git_push")
        assert "forge_pr" in str(exc.value) and "git_push" in str(exc.value)
        _assert_nothing_dispatched(pid)

    def test_consent_true_admits_the_same_plan(self, config) -> None:
        pid, _, digest = _propose()
        outcome = _ratify(pid, _plan(_task("t1", pipeline="ship-it")), digest, consent=True)
        assert outcome.status == "ratified"
        assert outcome.outward_consent is True
        row = partition_service.get_partition(pid)
        assert row is not None and row["outward_consent"] == 1

    def test_an_inward_only_plan_needs_no_consent(self, config) -> None:
        pid, _, digest = _propose()
        outcome = _ratify(pid, _plan(_task("t1", pipeline="bugfix")), digest, consent=False)
        assert outcome.status == "ratified"
        assert outcome.outward_actions == ()

    def test_policy_is_checked_before_consent(self, config) -> None:
        """Ordering matters: an out-of-policy outward action must be refused
        as a POLICY denial, so the operator is never invited to "just tick
        the box" for something policy would refuse anyway."""
        config(
            _policies_yaml(
                acme_api="""
      outward_actions: []
      budget_daily_usd: 100.0
      max_partition_cost_usd: 10.0
      max_task_wall_clock_seconds: 3600"""
            )
        )
        pid, _, digest = _propose()
        with pytest.raises(partition_service.PolicyDeniedError):
            _ratify(pid, _plan(_task("t1", pipeline="ship-it")), digest, consent=False)


# ---------------------------------------------------------------------------
# Step 5 -- concurrency (stale tab)
# ---------------------------------------------------------------------------


class TestStep5Digest:
    def test_a_stale_expected_digest_is_a_409(self, config) -> None:
        pid, _, _ = _propose()
        with pytest.raises(partition_service.DigestMismatchError) as exc:
            _ratify(pid, _plan(), "sha256:stale-from-an-old-tab")
        assert exc.value.status_code == 409
        _assert_nothing_dispatched(pid)

    def test_a_missing_expected_digest_is_a_409_not_a_bypass(self, config) -> None:
        """An absent digest must not be read as "no concurrency check"."""
        pid, _, _ = _propose()
        with pytest.raises(partition_service.DigestMismatchError):
            partition_service.ratify_partition(
                pid,
                partition_json=json.dumps(_plan()),
                outward_consent=False,
                approver="alice",
                expected_digest=None,
            )
        _assert_nothing_dispatched(pid)

    def test_an_empty_expected_digest_is_a_409_not_a_bypass(self, config) -> None:
        pid, _, _ = _propose()
        with pytest.raises(partition_service.DigestMismatchError):
            _ratify(pid, _plan(), "")

    def test_policy_is_checked_before_the_digest(self, config) -> None:
        """A stale tab carrying an OUT-OF-POLICY edit is refused on the
        merits (403), not merely on staleness (409) -- so the operator is
        told the real reason rather than being invited to reload and retry
        something policy will refuse anyway."""
        pid, _, _ = _propose()
        with pytest.raises(partition_service.PolicyDeniedError):
            _ratify(pid, _plan(_task("t1", pipeline="auto-merge")), "sha256:stale", consent=True)


# ---------------------------------------------------------------------------
# Step 6 -- idempotency
# ---------------------------------------------------------------------------


class TestStep6Idempotency:
    def test_the_second_ratify_changes_nothing(self, config) -> None:
        pid, proposed_json, digest = _propose()
        first = _ratify(pid, _plan(), digest)
        second = _ratify(pid, _plan(_task("t1", title="a sneaky retitle")), digest, approver="mal")
        assert first.idempotent is False
        assert second.idempotent is True
        row = partition_service.get_partition(pid)
        assert row is not None
        assert row["ratified_by"] == "alice"
        assert "sneaky" not in (row["ratified_json"] or "")

    def test_a_ratified_partition_is_immutable(self, config) -> None:
        """Wanting a different plan means a NEW partition (propose ->
        ratify), never an in-place mutation of a ratified one."""
        pid, _, digest = _propose(_task("a"))
        _ratify(pid, _plan(_task("a")), digest)
        _ratify(pid, _plan(_task("a"), _task("b")), digest)
        assert [r["task_id"] for r in partition_service.list_partition_tasks(pid)] == ["a"]


# ---------------------------------------------------------------------------
# Cross-cutting
# ---------------------------------------------------------------------------


class TestErrorContract:
    def test_each_refusal_carries_the_http_status_the_api_must_return(self) -> None:
        assert partition_service.MalformedPlanError.status_code == 400
        assert partition_service.ReferentialError.status_code == 400
        assert partition_service.PolicyDeniedError.status_code == 403
        assert partition_service.ConsentRequiredError.status_code == 403
        assert partition_service.DigestMismatchError.status_code == 409
        assert partition_service.PartitionNotFoundError.status_code == 404

    def test_an_unknown_partition_is_a_404(self, config) -> None:
        with pytest.raises(partition_service.PartitionNotFoundError):
            partition_service.ratify_partition(
                "no-such-partition",
                partition_json=json.dumps(_plan()),
                outward_consent=False,
                approver="alice",
                expected_digest="sha256:whatever",
            )

    def test_a_cross_tenant_partition_is_indistinguishable_from_a_missing_one(self, config) -> None:
        plan_json = json.dumps(_plan())
        pid = partition_service.create_partition(plan_json=plan_json, tenant="tenant-a")
        row = partition_service.get_partition(pid, tenant="tenant-a")
        assert row is not None
        with pytest.raises(partition_service.PartitionNotFoundError):
            partition_service.ratify_partition(
                pid,
                partition_json=plan_json,
                outward_consent=False,
                approver="mallory",
                expected_digest=str(row["proposed_digest"]),
                tenant="tenant-b",
            )
        after = partition_service.get_partition(pid, tenant="tenant-a")
        assert after is not None
        assert after["status"] == "proposed"
        assert partition_service.list_partition_tasks(pid) == []


class TestDenialsAreAudited:
    @pytest.mark.parametrize(
        ("pipeline", "consent", "error"),
        [
            ("auto-merge", True, partition_service.PolicyDeniedError),
            ("ship-it", False, partition_service.ConsentRequiredError),
        ],
    )
    def test_a_refused_ratification_is_recorded(self, config, pipeline, consent, error) -> None:
        pid, _, digest = _propose()
        with pytest.raises(error):
            _ratify(pid, _plan(_task("t1", pipeline=pipeline)), digest, consent=consent)
        entry = next(
            i
            for i in state_service.list_recent_interactions(limit=20)
            if i["action"] == "partition.ratify_denied"
        )
        assert entry["actor"] == "alice"
        assert entry["target"] == pid
        assert json.loads(entry["metadata"])["outward_consent"] is consent

    def test_a_denial_never_writes_a_ratify_interaction(self, config) -> None:
        pid, _, digest = _propose()
        with pytest.raises(partition_service.ConsentRequiredError):
            _ratify(pid, _plan(_task("t1", pipeline="ship-it")), digest, consent=False)
        actions = [i["action"] for i in state_service.list_recent_interactions(limit=20)]
        assert "partition.ratify" not in actions


class TestValidateRatificationIsTheSameRules:
    def test_the_dry_run_helper_raises_the_same_refusals(self, config) -> None:
        """A UI preview must never be a second, drifting copy of the gate."""
        from hivepilot.partition import load_partition

        plan = load_partition(json.dumps(_plan(_task("t1", pipeline="ship-it"))))
        with pytest.raises(partition_service.ConsentRequiredError):
            partition_service.validate_ratification(plan, outward_consent=False)
        assessment = partition_service.validate_ratification(plan, outward_consent=True)
        assert assessment.actions == frozenset({"git_push", "forge_pr"})
        assert assessment.total_cost_usd == pytest.approx(1.5)
