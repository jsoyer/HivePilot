"""Tests for MissionPlan + decomposition and the orchestrator service (HP-49)."""

from __future__ import annotations

import pytest
import yaml
from fastapi.testclient import TestClient

from hivepilot.services import (
    api_service,
    async_run_service,
    delegation,
    mission_plan,
    orchestrator_service,
    spaces_responder,
    state_service,
)
from hivepilot.services.mission_plan import MissionPlan, MissionTask
from hivepilot.services.token_service import add_token


@pytest.fixture(autouse=True)
def _reset_planner():
    mission_plan.register_planner(None)
    delegation.register_peer_executor(None)
    spaces_responder.register_reply_generator(None)
    yield
    mission_plan.register_planner(None)
    delegation.register_peer_executor(None)
    spaces_responder.register_reply_generator(None)


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


class TestStrategyPresets:
    def test_every_strategy_has_a_preset(self) -> None:
        assert set(mission_plan.STRATEGY_PRESETS) == set(mission_plan.STRATEGIES)
        for name, preset in mission_plan.STRATEGY_PRESETS.items():
            assert preset.name == name
            assert preset.dispatch in ("sequential", "parallel")
            assert preset.merge in mission_plan.MERGE_POLICIES

    def test_preset_encodes_the_mockup_modes(self) -> None:
        pipeline = mission_plan.resolve_strategy("pipeline")
        assert pipeline.stages == ("code", "review", "merge")
        assert pipeline.dispatch == "parallel" and pipeline.merge == "per_task"

        self_merge = mission_plan.resolve_strategy("code_only_self_merge")
        assert self_merge.stages == ("code",) and self_merge.merge == "per_branch"

        final_merge = mission_plan.resolve_strategy("code_only_final_merge")
        assert final_merge.merge == "final"

        assert mission_plan.resolve_strategy("new_mission").new_mission is True
        assert mission_plan.resolve_strategy("sequential").dispatch == "sequential"

    def test_resolve_unknown_falls_back_to_default(self) -> None:
        assert mission_plan.resolve_strategy("bogus").name == mission_plan.DEFAULT_STRATEGY
        assert mission_plan.resolve_strategy(None).name == mission_plan.DEFAULT_STRATEGY

    def test_to_dict_exposes_strategy_detail_for_the_ui(self) -> None:
        detail = MissionPlan(goal="g", strategy="pipeline").to_dict()["strategy_detail"]
        assert detail["dispatch"] == "parallel"
        assert detail["merge"] == "per_task"
        assert detail["guarantee"].startswith("strategy.guarantee.")


class TestSpawnOrdering:
    def test_sequential_dispatch_honors_depends_on(self) -> None:
        preset = mission_plan.resolve_strategy("sequential")
        tasks = [
            MissionTask(id="c", title="C", role="developer", depends_on=["b"]),
            MissionTask(id="a", title="A", role="developer"),
            MissionTask(id="b", title="B", role="developer", depends_on=["a"]),
        ]
        ordered = [t.id for t in orchestrator_service._ordered_tasks(tasks, preset)]
        assert ordered == ["a", "b", "c"]

    def test_parallel_dispatch_keeps_plan_order(self) -> None:
        preset = mission_plan.resolve_strategy("pipeline")
        tasks = [
            MissionTask(id="c", title="C", role="developer", depends_on=["b"]),
            MissionTask(id="a", title="A", role="developer"),
        ]
        assert [t.id for t in orchestrator_service._ordered_tasks(tasks, preset)] == ["c", "a"]

    def test_cyclic_dependencies_do_not_drop_tasks(self) -> None:
        preset = mission_plan.resolve_strategy("sequential")
        tasks = [
            MissionTask(id="x", title="X", role="developer", depends_on=["y"]),
            MissionTask(id="y", title="Y", role="developer", depends_on=["x"]),
        ]
        ordered = [t.id for t in orchestrator_service._ordered_tasks(tasks, preset)]
        assert sorted(ordered) == ["x", "y"]  # both present despite the cycle

    def test_spawn_plan_orders_and_annotates_merge_policy(self) -> None:
        space_id = orchestrator_service.get_or_create_project_space("atlas")
        plan = MissionPlan(
            goal="g",
            strategy="sequential",
            tasks=[
                MissionTask(id="t2", title="second", role="developer", depends_on=["t1"]),
                MissionTask(id="t1", title="first", role="developer"),
            ],
        )
        runs = orchestrator_service.spawn_plan(plan, "atlas", space_id)
        assert set(runs) == {"t1", "t2"}

        traces = [
            m["body"]
            for m in state_service.list_space_messages(space_id)
            if m["body"].startswith("→")
        ]
        assert traces[0].startswith("→ t1") and traces[1].startswith("→ t2")  # dependency order
        assert "merge final groupé" in traces[0]  # sequential → final merge policy


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


class TestSpawnPlan:
    def test_spawns_one_run_per_task_and_traces_them(self) -> None:
        seen: list[tuple[int, str]] = []
        delegation.register_peer_executor(lambda run_id, role: seen.append((run_id, role)))
        plan = MissionPlan(
            goal="g",
            tasks=[
                MissionTask(id="t1", title="API", role="developer"),
                MissionTask(id="t2", title="UI", role="qa"),
            ],
        )
        space_id = orchestrator_service.get_or_create_project_space("atlas")

        runs = orchestrator_service.spawn_plan(plan, "atlas", space_id)
        assert set(runs.keys()) == {"t1", "t2"}
        assert async_run_service.wait_until_idle(5.0)

        assert sorted(role for _, role in seen) == ["developer", "qa"]
        traced = [m for m in state_service.list_space_messages(space_id) if "run #" in m["body"]]
        assert len(traced) == 2


class TestMissionEndpoint:
    def test_launch_mission_spawns_and_returns_runs(self, api_client, tmp_tokens_file):
        seen: list[tuple[int, str]] = []
        delegation.register_peer_executor(lambda run_id, role: seen.append((run_id, role)))
        mission_plan.register_planner(
            lambda goal, project: MissionPlan(
                goal=goal,
                tasks=[
                    MissionTask(id="t1", title="API", role="developer"),
                    MissionTask(id="t2", title="UI", role="developer"),
                ],
            )
        )
        raw, _ = add_token("run")
        resp = api_client.post(
            "/v1/orchestrator/mission",
            json={"goal": "build X", "project": "atlas"},
            headers=_auth(raw),
        )
        assert resp.status_code == 200
        assert set(resp.json()["runs"].keys()) == {"t1", "t2"}
        assert async_run_service.wait_until_idle(5.0)
        assert len(seen) == 2

    def test_mission_requires_run(self, api_client, tmp_tokens_file):
        assert api_client.post("/v1/orchestrator/mission", json={"goal": "x"}).status_code == 401
        raw, _ = add_token("read")
        assert (
            api_client.post(
                "/v1/orchestrator/mission", json={"goal": "x"}, headers=_auth(raw)
            ).status_code
            == 403
        )


class TestMissionTracking:
    def test_mission_row_round_trips(self) -> None:
        sid = state_service.create_space([{"type": "human"}])
        mid = state_service.create_mission(sid, "atlas", "goal", {"t1": 5, "t2": 6})
        mission = state_service.get_mission(mid)
        assert mission is not None
        assert mission["runs"] == {"t1": 5, "t2": 6}
        assert mission["synthesized"] is False
        state_service.mark_mission_synthesized(mid)
        assert state_service.get_mission(mid)["synthesized"] is True

    def test_status_is_done_only_when_all_runs_settle(self) -> None:
        r1 = state_service.record_run_start("atlas", "t1")
        r2 = state_service.record_run_start("atlas", "t2")
        state_service.complete_run(r1, "success")
        st = orchestrator_service.mission_status({"runs": {"t1": r1, "t2": r2}})
        assert st["succeeded"] == 1 and st["pending"] == 1 and st["done"] is False
        state_service.complete_run(r2, "failed")
        st2 = orchestrator_service.mission_status({"runs": {"t1": r1, "t2": r2}})
        assert st2["done"] is True and st2["failed"] == 1

    def test_check_mission_synthesizes_once(self) -> None:
        sid = orchestrator_service.get_or_create_project_space("atlas")
        r1 = state_service.record_run_start("atlas", "t1")
        state_service.complete_run(r1, "success")
        mid = state_service.create_mission(sid, "atlas", "goal", {"t1": r1})

        res = orchestrator_service.check_mission(mid)
        assert res["status"]["done"] is True and res["synthesized"] is True
        synth = [
            m for m in state_service.list_space_messages(sid) if "Mission terminée" in m["body"]
        ]
        assert len(synth) == 1

        orchestrator_service.check_mission(mid)  # idempotent — no second synthesis
        synth2 = [
            m for m in state_service.list_space_messages(sid) if "Mission terminée" in m["body"]
        ]
        assert len(synth2) == 1

    def test_status_endpoint_tracks_to_completion(self, api_client, tmp_tokens_file):
        mission_plan.register_planner(
            lambda goal, project: MissionPlan(
                goal=goal, tasks=[MissionTask(id="t1", title="X", role="developer")]
            )
        )
        raw, _ = add_token("run", tenant="acme")
        launched = api_client.post(
            "/v1/orchestrator/mission", json={"goal": "g", "project": "atlas"}, headers=_auth(raw)
        ).json()
        mid, run_id = launched["mission_id"], launched["runs"]["t1"]

        st = api_client.get(f"/v1/orchestrator/missions/{mid}", headers=_auth(raw)).json()
        assert st["status"]["done"] is False  # run still running

        state_service.complete_run(run_id, "success")
        st2 = api_client.get(f"/v1/orchestrator/missions/{mid}", headers=_auth(raw)).json()
        assert st2["status"]["done"] is True and st2["synthesized"] is True

        assert (
            api_client.get("/v1/orchestrator/missions/999999", headers=_auth(raw)).status_code
            == 404
        )
