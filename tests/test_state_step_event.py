"""A step must announce itself on the event stream, carrying its role.

`record_step` writes to SQLite and bumps a Prometheus counter, and that is
all: there is no `state.step` event. The stream carries `state.run_start`,
`state.verdict` and `state.run_complete` — a run's endpoints, never its
middle. Anything watching the stream sees a run begin, then silence for ten
minutes, then a verdict.

That is the gap between "a pipeline ran" and "watch the roles work", and it
is what a live role board needs closed. The database already knows: it has a
`role` column per step, filled by the same call.

The role on the event must be the *same* role the database gets, including
the correction that strips attribution from steps no model performed. Two
places computing "which agent did this" independently is how they drift, and
a board that credits an agent with `bash` work is worse than one that says
nothing.
"""

from __future__ import annotations

import json

import pytest
import structlog

from hivepilot.services import state_service
from hivepilot.utils import logging as hp_logging

_MARKER = "sk-live-BXQ2f8ZzNotARealSecret"


@pytest.fixture
def events(capsys: pytest.CaptureFixture[str]):
    """Render structlog through the real redaction processor, as production does.

    Snapshots and restores the caller's configuration. The obvious teardown,
    `structlog.reset_defaults()`, resets to *structlog's* defaults rather
    than HivePilot's — which silently detaches structlog from stdlib logging
    for every test that runs afterwards. That broke 15 tests in
    `test_swarm_service` and `test_telegram_bot`, all asserting on `caplog`,
    with the tell-tale signature of an empty capture while stdout plainly
    showed the events. They passed in isolation, which is exactly how this
    kind of damage hides.
    """
    previous = structlog.get_config()
    structlog.configure(
        processors=[hp_logging._redact_secret_values, structlog.processors.JSONRenderer()],
        logger_factory=structlog.PrintLoggerFactory(),
    )

    def _read() -> list[dict]:
        out = []
        for line in capsys.readouterr().out.splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    yield _read
    structlog.configure(**previous)


def _steps(events) -> list[dict]:
    return [e for e in events() if e.get("event") == "state.step"]


class TestTheStreamSeesTheStep:
    def test_recording_a_step_emits_one(self, events, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("HIVEPILOT_STATE_DB", str(tmp_path / "s.db"))
        run = state_service.record_run_start("p", "t")
        events()  # drain run_start

        state_service.record_step(run, "review:ciso", "success", role="ciso")

        assert len(_steps(events)) == 1

    def test_it_carries_what_a_watcher_needs(self, events, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("HIVEPILOT_STATE_DB", str(tmp_path / "s.db"))
        run = state_service.record_run_start("p", "t")
        events()

        state_service.record_step(run, "review:ciso", "success", role="ciso")
        (event,) = _steps(events)

        assert event["run_id"] == run
        assert event["step"] == "review:ciso"
        assert event["status"] == "success"
        assert event["role"] == "ciso"


class TestTheEventAgreesWithTheDatabase:
    def test_shell_work_is_not_credited_to_an_agent(self, events, tmp_path, monkeypatch) -> None:
        """The DB strips the role for providers known not to invoke a model.
        The event must strip it the same way, from the same computation --
        a board that shows `ciso` running `bash` is actively misleading."""
        monkeypatch.setenv("HIVEPILOT_STATE_DB", str(tmp_path / "s.db"))
        run = state_service.record_run_start("p", "t")
        events()

        state_service.record_step(run, "build", "success", provider="shell", role="ciso")
        (event,) = _steps(events)

        assert event["role"] is None

    def test_an_unknown_provider_keeps_its_role(self, events, tmp_path, monkeypatch) -> None:
        """A NULL provider is a telemetry gap, not proof no agent ran."""
        monkeypatch.setenv("HIVEPILOT_STATE_DB", str(tmp_path / "s.db"))
        run = state_service.record_run_start("p", "t")
        events()

        state_service.record_step(run, "propose", "success", role="architect")
        (event,) = _steps(events)

        assert event["role"] == "architect"


class TestNothingSensitiveRidesAlong:
    def test_a_secret_in_detail_never_reaches_the_stream(
        self, events, tmp_path, monkeypatch
    ) -> None:
        """`detail` carries `str(exc)`, which is where an agent's echoed
        secret lands. The stream is about to be rendered into a terminal
        pane, so this is the last place to be casual about it."""
        from hivepilot.services import config_provenance

        config_provenance.register_secret_value(_MARKER)
        monkeypatch.setenv("HIVEPILOT_STATE_DB", str(tmp_path / "s.db"))
        run = state_service.record_run_start("p", "t")
        events()

        state_service.record_step(
            run, "deploy", "failure", detail=f"boom: {_MARKER}", role="devops"
        )
        rendered = json.dumps(_steps(events))

        assert _MARKER not in rendered
