"""Tests for `POST /v1/partitions/{id}/preview` — the ratify UI's dry run
(propose -> ratify -> dispatch PRD, Sprint 4).

Why this endpoint exists at all: Pollen's ratification view has to tell the
operator, BEFORE they commit, (a) which outward-visible actions the plan they
are currently looking at would perform and (b) whether the gate would accept
it. Both answers are already defined exactly once, in
`partition_service.validate_ratification` / `assess_outward` /
`effective_parallelism`. A browser cannot call those, and re-deriving any of
them in TypeScript would create a second, drifting copy of the most
fail-closed module in this repo.

So the endpoint is a pure translation layer over the SAME functions the real
gate runs, and the test that matters most here is
`TestPreviewMatchesTheRealGate` — it asserts the preview verdict and the real
`ratify_partition` outcome agree, which is the only thing that makes "the UI
did not reimplement the rules" a property rather than a claim.

The fixtures mirror `tests/test_partition_dispatch.py`'s live-config shape
deliberately: the gate reads LIVE config, so mocking the loader would test
the opposite of the property under test.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import yaml

from hivepilot.services import autopilot_queue, partition_service

PROJECTS_YAML = """
projects:
  acme-api:
    path: /tmp/acme-api
    modules:
      core: apps/core
"""

PIPELINES_YAML = """
pipelines:
  bugfix:
    description: fix a bug
    stages:
      - name: fix
        task: implement
  ship-it:
    description: ship a change
    stages:
      - name: ship
        task: ship
"""

TASKS_YAML = """
tasks:
  implement:
    description: implement something
    role: developer
  ship:
    description: push a branch and open a PR
    role: developer
    git:
      push: true
      create_pr: true
"""

POLICIES_YAML = """
policies:
  default:
    require_approval: true
  projects:
    acme-api:
      outward_actions:
        - git_push
        - forge_pr
      budget_daily_usd: 100.0
      max_partition_cost_usd: 50.0
      max_task_wall_clock_seconds: 3600
"""


@pytest.fixture
def live_config(tmp_path, monkeypatch):
    from hivepilot.config import settings
    from hivepilot.services import policy_service

    (tmp_path / "projects.yaml").write_text(PROJECTS_YAML, encoding="utf-8")
    (tmp_path / "pipelines.yaml").write_text(PIPELINES_YAML, encoding="utf-8")
    (tmp_path / "tasks.yaml").write_text(TASKS_YAML, encoding="utf-8")
    (tmp_path / "policies.yaml").write_text(POLICIES_YAML, encoding="utf-8")
    monkeypatch.setattr(settings, "config_repo", str(tmp_path), raising=False)
    policy_service.reload_policies()
    yield tmp_path
    policy_service.reload_policies()


@pytest.fixture(autouse=True)
def _zero_spend(monkeypatch):
    monkeypatch.setattr(autopilot_queue, "spent_today_usd", lambda *, tenant="default": 0.0)


@pytest.fixture()
def tmp_tokens_file(tmp_path, monkeypatch):
    tokens_file = tmp_path / "tokens.yaml"
    tokens_file.write_text(yaml.safe_dump({"tokens": []}), encoding="utf-8")
    from hivepilot.config import settings

    monkeypatch.setattr(settings, "tokens_file", tokens_file)
    return tokens_file


@pytest.fixture()
def api_client():
    from fastapi.testclient import TestClient

    from hivepilot.services.api_service import app as api_app

    return TestClient(api_app, raise_server_exceptions=True)


def _auth(raw_token: str) -> dict:
    return {"Authorization": f"Bearer {raw_token}"}


def _task(task_id: str, **overrides: Any) -> dict[str, Any]:
    task: dict[str, Any] = {
        "id": task_id,
        "title": f"Task {task_id}",
        "project": "acme-api",
        "pipeline": "bugfix",
        "prompt": f"do the {task_id} work",
        "depends_on": [],
        "budget": {"wall_clock_seconds": 30, "cost_usd": 1.0},
        "done_when": ["a repro test passes"],
        "outward": False,
    }
    task.update(overrides)
    return task


def _plan(*tasks: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    plan: dict[str, Any] = {
        "partition_version": 1,
        "source": {"kind": "text", "ref": "docs/bug-1234.md", "digest": "sha256:aaa"},
        "proposer": {"role": "partitioner", "pipeline": "propose-partition", "run_id": 4711},
        "policy": {"max_parallel": 3, "on_task_failure": "continue"},
        "tasks": list(tasks) or [_task("t1")],
    }
    plan.update(overrides)
    return plan


def _preview(api_client, raw: str, partition_id: str, plan_json: str, *, consent: bool = False):
    return api_client.post(
        f"/v1/partitions/{partition_id}/preview",
        headers=_auth(raw),
        json={"partition_json": plan_json, "outward_consent": consent},
    )


class TestPreviewRbacAndTenancy:
    def test_preview_requires_the_approve_rank(
        self, live_config, tmp_tokens_file, api_client
    ) -> None:
        """A dry run reveals the exact policy verdict a ratification would
        produce, so it must not be a lower-privilege oracle than the
        ratification itself."""
        from hivepilot.services.token_service import add_token

        plan_json = json.dumps(_plan(_task("a")))
        partition_id = partition_service.create_partition(plan_json=plan_json)

        raw_run, _ = add_token("run")
        assert _preview(api_client, raw_run, partition_id, plan_json).status_code == 403

        raw_approve, _ = add_token("approve")
        assert _preview(api_client, raw_approve, partition_id, plan_json).status_code == 200

    def test_preview_of_a_cross_tenant_partition_is_403(
        self, live_config, tmp_tokens_file, api_client
    ) -> None:
        """Mirrors the ratify endpoint exactly: the caller already holds
        `approve`, so "wrong tenant" is actionable and reported as 403."""
        from hivepilot.services.token_service import add_token

        plan_json = json.dumps(_plan(_task("a")))
        theirs = partition_service.create_partition(plan_json=plan_json, tenant="other-co")
        mine = partition_service.create_partition(plan_json=plan_json, tenant="acme")
        raw, _ = add_token("approve", tenant="acme")

        assert _preview(api_client, raw, mine, plan_json).status_code == 200
        assert _preview(api_client, raw, theirs, plan_json).status_code == 403

    def test_preview_of_an_unknown_partition_is_404(
        self, live_config, tmp_tokens_file, api_client
    ) -> None:
        """The known-id 200 is asserted in the SAME test on purpose: on its
        own, "404 for an unknown id" would also pass against a build where
        the route does not exist at all — a false green."""
        from hivepilot.services.token_service import add_token

        plan_json = json.dumps(_plan(_task("a")))
        known = partition_service.create_partition(plan_json=plan_json)
        raw, _ = add_token("approve")

        assert _preview(api_client, raw, known, plan_json).status_code == 200
        assert _preview(api_client, raw, "does-not-exist", plan_json).status_code == 404


class TestPreviewIsADryRun:
    def test_preview_changes_nothing(self, live_config, tmp_tokens_file, api_client) -> None:
        """The whole point of a dry run. A preview that ratified, wrote
        journal rows, or logged a denial would turn every keystroke in the
        JSON box into an audit-log entry (or worse, a dispatch)."""
        from hivepilot.services import state_service
        from hivepilot.services.token_service import add_token

        plan_json = json.dumps(_plan(_task("a")))
        partition_id = partition_service.create_partition(plan_json=plan_json)
        raw, _ = add_token("approve")
        before = len(state_service.list_recent_interactions(limit=500))

        # Both an accepted and a refused preview, so neither path can write.
        assert _preview(api_client, raw, partition_id, plan_json).status_code == 200
        assert _preview(api_client, raw, partition_id, "{ not json").status_code == 200

        row = partition_service.get_partition(partition_id)
        assert row is not None
        assert row["status"] == "proposed"
        assert row["ratified_json"] is None
        assert partition_service.list_partition_tasks(partition_id) == []
        assert len(state_service.list_recent_interactions(limit=500)) == before


class TestPreviewVerdict:
    def test_a_malformed_plan_is_a_200_verdict_not_an_http_error(
        self, live_config, tmp_tokens_file, api_client
    ) -> None:
        """A preview of an invalid plan is a SUCCESSFUL preview. Returning
        4xx would make a refusal indistinguishable from a network failure in
        the browser, and would throw away the structured verdict the UI needs
        to render. The gate's own `status_code` still travels, as data."""
        from hivepilot.services.token_service import add_token

        partition_id = partition_service.create_partition(plan_json=json.dumps(_plan(_task("a"))))
        raw, _ = add_token("approve")

        body = _preview(api_client, raw, partition_id, '{"partition_version": 1}').json()

        assert body["ok"] is False
        assert body["code"] == "malformed"
        assert body["status_code"] == 400
        assert body["detail"]

    def test_outward_actions_are_named_before_consent_is_given(
        self, live_config, tmp_tokens_file, api_client
    ) -> None:
        """The consent warning has to NAME what it is asking consent for, and
        it must do so while the checkbox is still unticked — otherwise the
        operator is asked to consent to an unnamed "something outward"."""
        from hivepilot.services.token_service import add_token

        plan_json = json.dumps(_plan(_task("a", pipeline="ship-it")))
        partition_id = partition_service.create_partition(plan_json=plan_json)
        raw, _ = add_token("approve")

        body = _preview(api_client, raw, partition_id, plan_json, consent=False).json()

        assert sorted(body["outward_actions"]) == ["forge_pr", "git_push"]
        assert body["ok"] is False
        assert body["code"] == "consent_required"

    def test_granting_consent_flips_the_same_plan_to_ok(
        self, live_config, tmp_tokens_file, api_client
    ) -> None:
        from hivepilot.services.token_service import add_token

        plan_json = json.dumps(_plan(_task("a", pipeline="ship-it")))
        partition_id = partition_service.create_partition(plan_json=plan_json)
        raw, _ = add_token("approve")

        body = _preview(api_client, raw, partition_id, plan_json, consent=True).json()

        assert body["ok"] is True
        assert body["code"] is None
        assert sorted(body["outward_actions"]) == ["forge_pr", "git_push"]

    def test_an_edited_plan_naming_an_unknown_pipeline_is_refused(
        self, live_config, tmp_tokens_file, api_client
    ) -> None:
        """The JSON box is an edit surface, so the preview must validate the
        SUBMITTED text against live config — not the stored proposal."""
        from hivepilot.services.token_service import add_token

        partition_id = partition_service.create_partition(plan_json=json.dumps(_plan(_task("a"))))
        raw, _ = add_token("approve")
        edited = json.dumps(_plan(_task("a", pipeline="no-such-pipeline")))

        body = _preview(api_client, raw, partition_id, edited, consent=True).json()

        assert body["ok"] is False
        assert body["code"] == "referential"
        assert body["status_code"] == 400
        assert "no-such-pipeline" in body["detail"]

    def test_effective_parallelism_is_reported_below_the_requested_number(
        self, live_config, tmp_tokens_file, api_client, monkeypatch
    ) -> None:
        """`claude_max_concurrency` defaults to 1, so a `max_parallel: 3`
        plan is one agent three times. The UI cannot show `requested` alone
        without lying, so the preview must carry the computed figure."""
        from hivepilot.config import settings
        from hivepilot.services.token_service import add_token

        monkeypatch.setattr(settings, "claude_max_concurrency", 1, raising=False)
        monkeypatch.setattr(settings, "concurrency_limit", 8, raising=False)
        plan_json = json.dumps(_plan(_task("a"), _task("b"), _task("c")))
        partition_id = partition_service.create_partition(plan_json=plan_json)
        raw, _ = add_token("approve")

        body = _preview(api_client, raw, partition_id, plan_json).json()

        assert body["parallelism"]["requested"] == 3
        assert body["parallelism"]["effective"] == 1
        assert body["parallelism"]["notes"]

    def test_waves_and_cost_sum_come_from_the_service(
        self, live_config, tmp_tokens_file, api_client
    ) -> None:
        from hivepilot.services.token_service import add_token

        plan_json = json.dumps(_plan(_task("a"), _task("b", depends_on=["a"])))
        partition_id = partition_service.create_partition(plan_json=plan_json)
        raw, _ = add_token("approve")

        body = _preview(api_client, raw, partition_id, plan_json).json()

        assert body["waves"] == [["a"], ["b"]]
        assert body["total_cost_usd"] == pytest.approx(2.0)
        assert body["task_ids"] == ["a", "b"]


class TestPreviewMatchesTheRealGate:
    """The anti-Goodhart test for this whole endpoint.

    A preview that merely *looks* right is worthless: what has to hold is
    that the dry run and the real ratification are the SAME gate. Each case
    below runs the preview and then the real `ratify_partition` on the same
    input and asserts they agree — so a future re-derivation of any rule in
    the preview path (or a drift in either direction) fails here.
    """

    @pytest.mark.parametrize(
        "pipeline,consent,expected_code",
        [
            ("bugfix", False, None),  # nothing outward: accepted without consent
            ("ship-it", False, "consent_required"),
            ("ship-it", True, None),
            ("no-such-pipeline", True, "referential"),
        ],
    )
    def test_preview_verdict_equals_the_ratify_outcome(
        self,
        live_config,
        tmp_tokens_file,
        api_client,
        monkeypatch,
        pipeline: str,
        consent: bool,
        expected_code: str | None,
    ) -> None:
        from hivepilot.services.token_service import add_token

        monkeypatch.setattr(
            partition_service,
            "dispatch_partition_background",
            lambda partition_id, **kwargs: None,
        )
        stored = json.dumps(_plan(_task("a")))
        partition_id = partition_service.create_partition(plan_json=stored)
        row = partition_service.get_partition(partition_id)
        assert row is not None
        raw, _ = add_token("approve")
        submitted = json.dumps(_plan(_task("a", pipeline=pipeline)))

        preview = _preview(api_client, raw, partition_id, submitted, consent=consent).json()

        if expected_code is None:
            assert preview["ok"] is True
            partition_service.ratify_partition(
                partition_id,
                partition_json=submitted,
                outward_consent=consent,
                approver="operator",
                expected_digest=str(row["proposed_digest"]),
            )
            assert partition_service.get_partition(partition_id)["status"] == "ratified"
        else:
            assert preview["ok"] is False
            assert preview["code"] == expected_code
            with pytest.raises(partition_service.RatificationError) as excinfo:
                partition_service.ratify_partition(
                    partition_id,
                    partition_json=submitted,
                    outward_consent=consent,
                    approver="operator",
                    expected_digest=str(row["proposed_digest"]),
                )
            assert excinfo.value.code == expected_code
            assert excinfo.value.status_code == preview["status_code"]
            assert partition_service.get_partition(partition_id)["status"] == "proposed"
