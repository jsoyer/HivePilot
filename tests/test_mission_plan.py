"""Tests for MissionPlan + decomposition and the orchestrator service (HP-49)."""

from __future__ import annotations

import pytest
import yaml
from fastapi.testclient import TestClient

from hivepilot.services import api_service, mission_plan, orchestrator_service, state_service
from hivepilot.services.mission_plan import MissionPlan, MissionTask
from hivepilot.services.token_service import add_token


@pytest.fixture(autouse=True)
def _reset_planner():
    mission_plan.register_planner(None)
    yield
    mission_plan.register_planner(None)


@pytest.fixture()
def tmp_tokens_file(tmp_path, monkeypatch):
    from hivepilot.config import settings

    tokens_file = tmp_path / "tokens.yaml"
    tokens_file.write_text(yaml.safe_dump({"tokens": []}), encoding="utf-8")
    monkeypatch.setattr(settings, "tokens_file", tokens_file)
    return tokens_file


@pytest.fixture()
def api_client():
    return TestClient(api_service.app, raise_server_exceptions=True)


def _auth(raw: str) -> dict:
    return {"Authorization": f"Bearer {raw}"}


class TestDecompose:
    def test_fallback_is_a_single_honest_task(self) -> None:
        plan = mission_plan.decompose("Ship the live board")
        assert len(plan.tasks) == 1
        assert plan.tasks[0].role == mission_plan.DEFAULT_TASK_ROLE
        assert plan.strategy == mission_plan.DEFAULT_STRATEGY
        assert plan.tasks[0].description == "Ship the live board"

    def test_uses_a_registered_planner(self) -> None:
        def _planner(goal, project):
            return MissionPlan(
                goal=goal,
                tasks=[
                    MissionTask(id="t1", title="API", role="developer"),
                    MissionTask(id="t2", title="UI", role="developer", depends_on=["t1"]),
                ],
                strategy="pipeline",
            )

        mission_plan.register_planner(_planner)
        plan = mission_plan.decompose("big feature", project="atlas")
        assert [t.id for t in plan.tasks] == ["t1", "t2"]
        assert plan.tasks[1].depends_on == ["t1"]

    def test_broken_planner_degrades_to_fallback(self) -> None:
        mission_plan.register_planner(
            lambda goal, project: (_ for _ in ()).throw(RuntimeError("x"))
        )
        plan = mission_plan.decompose("goal")
        assert len(plan.tasks) == 1  # fell back

    def test_planner_returning_no_tasks_degrades(self) -> None:
        mission_plan.register_planner(lambda goal, project: MissionPlan(goal=goal, tasks=[]))
        assert len(mission_plan.decompose("goal").tasks) == 1

    def test_invalid_strategy_falls_back_to_default(self) -> None:
        mission_plan.register_planner(
            lambda goal, project: MissionPlan(
                goal=goal,
                tasks=[MissionTask(id="t1", title="x", role="developer")],
                strategy="bogus",
            )
        )
        assert mission_plan.decompose("g").strategy == mission_plan.DEFAULT_STRATEGY

    def test_round_trips_through_dict(self) -> None:
        plan = MissionPlan(
            goal="g",
            tasks=[MissionTask(id="t1", title="x", role="qa", depends_on=["t0"])],
            strategy="code_only_self_merge",
            roles_config={"developer": {"model": "claude/opus", "repli": "grok/grok-4.6"}},
        )
        restored = MissionPlan.from_dict(plan.to_dict())
        assert restored.strategy == "code_only_self_merge"
        assert restored.tasks[0].depends_on == ["t0"]
        assert restored.roles_config["developer"]["repli"] == "grok/grok-4.6"


class TestOrchestratorService:
    def test_project_space_is_a_singleton(self) -> None:
        a = orchestrator_service.get_or_create_project_space("atlas")
        b = orchestrator_service.get_or_create_project_space("atlas")
        assert a == b  # same persistent space
        other = orchestrator_service.get_or_create_project_space("nimbus")
        assert other != a

    def test_decompose_feature_posts_summary_with_task_trace(self) -> None:
        mission_plan.register_planner(
            lambda goal, project: MissionPlan(
                goal=goal,
                tasks=[
                    MissionTask(id="t1", title="API", role="developer", description="the api"),
                    MissionTask(id="t2", title="UI", role="developer", description="the ui"),
                ],
            )
        )
        result = orchestrator_service.decompose_feature("build X", "atlas")
        assert len(result["plan"]["tasks"]) == 2

        msgs = state_service.list_space_messages(result["space_id"])
        assert msgs[-1]["sender_type"] == "system"
        assert "2 task" in msgs[-1]["body"]
        # the per-task breakdown rides as a collapsible action trace
        labels = [a["label"] for a in msgs[-1]["actions"]]
        assert labels == ["t1 · API", "t2 · UI"]


class TestDecomposeEndpoint:
    def test_requires_run(self, api_client, tmp_tokens_file):
        assert api_client.post("/v1/orchestrator/decompose", json={"goal": "x"}).status_code == 401
        raw, _ = add_token("read")
        assert (
            api_client.post(
                "/v1/orchestrator/decompose", json={"goal": "x"}, headers=_auth(raw)
            ).status_code
            == 403
        )

    def test_empty_goal_is_400(self, api_client, tmp_tokens_file):
        raw, _ = add_token("run")
        assert (
            api_client.post(
                "/v1/orchestrator/decompose", json={"goal": "   "}, headers=_auth(raw)
            ).status_code
            == 400
        )

    def test_returns_plan_and_space(self, api_client, tmp_tokens_file):
        raw, _ = add_token("run", tenant="acme")
        resp = api_client.post(
            "/v1/orchestrator/decompose",
            json={"goal": "ship the board", "project": "atlas"},
            headers=_auth(raw),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["plan"]["goal"] == "ship the board"
        assert body["plan"]["tasks"]  # at least the fallback task
        assert isinstance(body["space_id"], int)
