"""HP-54: mission 'où elle en est' via Hindsight reflect()."""

from __future__ import annotations

from dataclasses import dataclass, field

from hivepilot.services import hindsight_reflect as reflect
from hivepilot.services import orchestrator_service, state_service


@dataclass
class _RecordingReflect:
    calls: list = field(default_factory=list)
    answer: str = "Deux tâches en cours ; l'API attend le reviewer."

    def reflect(self, **kwargs):
        self.calls.append(kwargs)
        return {"answer": self.answer}


def _status(*, succeeded=1, failed=0, pending=1, done=False, tasks=None) -> dict:
    return {
        "total": succeeded + failed + pending,
        "succeeded": succeeded,
        "failed": failed,
        "pending": pending,
        "done": done,
        "tasks": tasks
        or {
            "t1": {"run_id": 1, "status": "success"},
            "t2": {"run_id": 2, "status": "running"},
        },
    }


class TestExtractAndFingerprint:
    def test_answer_field(self):
        assert reflect.extract_reflect_text({"answer": "  ici.  "}) == "ici."

    def test_object_text(self):
        class _R:
            text = "prose"

        assert reflect.extract_reflect_text(_R()) == "prose"

    def test_fingerprint_is_stable(self):
        a = reflect.status_fingerprint(_status())
        b = reflect.status_fingerprint(_status())
        assert a == b
        assert a != reflect.status_fingerprint(_status(pending=0, done=True, failed=0, succeeded=2))


class TestExperienceBanks:
    def test_project_task_and_role_from_steps(self) -> None:
        r1 = state_service.record_run_start("atlas", "API")
        state_service.record_step(r1, "write", "ok", role="developer")
        r2 = state_service.record_run_start("atlas", "UI")
        banks = reflect.experience_banks_for_mission(
            {"project": "atlas", "runs": {"t1": r1, "t2": r2}}
        )
        assert banks[0] == "atlas:API:developer"
        assert banks[1] == "atlas:UI"


class TestReflectCall:
    def test_disabled_makes_zero_calls(self):
        client = _RecordingReflect()
        result = reflect.reflect_mission_progress(
            {"project": "atlas", "goal": "ship", "runs": {"t1": 1}},
            _status(),
            client=client,
            enabled=False,
        )
        assert result.text is None
        assert client.calls == []

    def test_cached_fingerprint_skips_client(self):
        status = _status()
        fp = reflect.status_fingerprint(status)
        client = _RecordingReflect()
        result = reflect.reflect_mission_progress(
            {
                "project": "atlas",
                "goal": "ship",
                "runs": {"t1": 1},
                "narrative": "déjà dit",
                "narrative_fingerprint": fp,
            },
            status,
            client=client,
            enabled=True,
        )
        assert result.text == "déjà dit"
        assert result.cached is True
        assert client.calls == []

    def test_reflect_uses_experience_facts_and_never_disposition(self) -> None:
        r1 = state_service.record_run_start("atlas", "API")
        state_service.record_step(r1, "write", "ok", role="developer")
        client = _RecordingReflect()
        result = reflect.reflect_mission_progress(
            {"project": "atlas", "goal": "ship the API", "runs": {"t1": r1}},
            _status(tasks={"t1": {"run_id": r1, "status": "running"}}),
            client=client,
            enabled=True,
        )
        assert result.text == client.answer
        assert result.bank_id == "atlas:API:developer"
        assert result.cached is False
        kwargs = client.calls[0]
        assert kwargs["bank_id"] == "atlas:API:developer"
        assert kwargs["fact_types"] == ["experience"]
        assert kwargs["budget"] == "low"
        assert "ship the API" in kwargs["context"]
        assert "disposition" not in kwargs
        assert not any(key.startswith("disposition") for key in kwargs)

    def test_raising_client_returns_none(self) -> None:
        r1 = state_service.record_run_start("atlas", "API")

        class _Boom:
            def reflect(self, **kwargs):
                raise RuntimeError("hindsight down")

        result = reflect.reflect_mission_progress(
            {"project": "atlas", "goal": "g", "runs": {"t1": r1}},
            _status(tasks={"t1": {"run_id": r1, "status": "running"}}),
            client=_Boom(),
            enabled=True,
        )
        assert result.text is None


class TestCheckMissionWiresReflect:
    def test_check_mission_returns_and_caches_narrative(self, monkeypatch) -> None:
        sid = orchestrator_service.get_or_create_project_space("atlas")
        r1 = state_service.record_run_start("atlas", "t1")
        mid = state_service.create_mission(sid, "atlas", "goal", {"t1": r1})

        calls = {"n": 0}

        def _fake(mission, status, **kwargs):
            calls["n"] += 1
            return reflect.MissionNarrative(
                text="On avance sur t1.",
                bank_id="atlas:t1",
                fingerprint=reflect.status_fingerprint(status),
                cached=False,
            )

        monkeypatch.setattr(reflect, "reflect_mission_progress", _fake)
        # check_mission imports the function from the module at call time —
        # patch the name it imports.
        monkeypatch.setattr(
            "hivepilot.services.hindsight_reflect.reflect_mission_progress",
            _fake,
        )

        res = orchestrator_service.check_mission(mid)
        assert res["narrative"] == "On avance sur t1."
        row = state_service.get_mission(mid)
        assert row["narrative"] == "On avance sur t1."
        assert row["narrative_fingerprint"]

        # Second poll: stored fingerprint matches → helper reports cached.
        def _cached(mission, status, **kwargs):
            calls["n"] += 1
            return reflect.MissionNarrative(
                text=mission.get("narrative"),
                bank_id=None,
                fingerprint=mission.get("narrative_fingerprint") or "",
                cached=True,
            )

        monkeypatch.setattr(
            "hivepilot.services.hindsight_reflect.reflect_mission_progress",
            _cached,
        )
        res2 = orchestrator_service.check_mission(mid)
        assert res2["narrative"] == "On avance sur t1."

    def test_disabled_narrative_is_none_and_existing_synthesis_still_runs(self) -> None:
        sid = orchestrator_service.get_or_create_project_space("atlas")
        r1 = state_service.record_run_start("atlas", "t1")
        state_service.complete_run(r1, "success")
        mid = state_service.create_mission(sid, "atlas", "goal", {"t1": r1})
        res = orchestrator_service.check_mission(mid)
        assert res["synthesized"] is True
        assert res["narrative"] is None
        assert res["status"]["done"] is True
