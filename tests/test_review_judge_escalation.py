"""The CISO summons the judge — role-driven escalation on the review facet.

The operator's wish (#32): "le juge tiers doit être automatiquement poussé par
le CSO s'il y a besoin et s'il en trouve l'utilité." The static half shipped as
config (the release debate block carries cursor/gpt-5.6-sol-high). This is the
dynamic half: a REVIEWER decides, in its verdict, that this review deserves
third-vendor arbitration — and only then is the judge dispatched.

Two constraints shape everything here:

    the verdict parser is SACRED (it has been wrongly blamed before, and
    loosening it fabricates verdicts). Escalation is therefore a SIBLING line
    in the same fail-closed grammar — `escalate: JUDGE` — parsed by its own
    function, never a change to `_parse_reviewer_verdict` or `_parse_verdict`.

    fail CLOSED both ways: prose merely mentioning the word never triggers
    (line-anchored, exact token), and an escalation on a pipeline with NO
    judge configured refuses loudly — a warning naming the gap, never a
    silently dropped request and never a fabricated verdict.
"""

from __future__ import annotations

import pytest

from hivepilot.orchestrator import Orchestrator, _parse_review_escalation


class TestTheEscalationLine:
    """Same grammar family as `status:` — fail-closed, line-anchored."""

    def test_the_exact_line_triggers(self):
        assert _parse_review_escalation("status: PASS\nescalate: JUDGE\n") is True

    def test_a_leading_bullet_is_tolerated_like_status_lines_are(self):
        assert _parse_review_escalation("- escalate: JUDGE") is True

    def test_case_of_the_token_is_strict(self):
        """`JUDGE` is the token, exactly — a fail-closed grammar does not
        guess that `judge` meant the same thing."""
        assert _parse_review_escalation("escalate: judge") is False

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "   ",
            "status: PASS",
            "I would escalate to a judge if unsure.",  # prose, not a line
            "the escalate: JUDGE convention is documented here",  # mid-line
            "escalate: ARBITER",  # unknown target
            "escalate:",  # no token
        ],
    )
    def test_everything_else_is_not_an_escalation(self, text):
        assert _parse_review_escalation(text) is False

    def test_it_never_touches_the_verdict_parsers(self):
        """The sacred-parser constraint, pinned: the escalation grammar lives
        in its own function and the two verdict parsers are unchanged by this
        feature (no mention of escalation inside either)."""
        import inspect

        from hivepilot import orchestrator

        assert "escalate" not in inspect.getsource(orchestrator._parse_reviewer_verdict)
        assert "escalate" not in inspect.getsource(orchestrator._parse_verdict)


class _Effective:
    def __init__(self, enable_judge=True, runner="cursor", model="gpt-5.6-sol-high"):
        self.enable_judge = enable_judge
        self.runner = runner
        self.model = model
        self.confidence_threshold = 0.7


class TestTheEscalatedJudgeDispatch:
    @staticmethod
    def _orch(monkeypatch, capture_result='{"decision": "approve", "confidence": 0.9}'):
        orch = Orchestrator.__new__(Orchestrator)

        captured: dict = {}

        class _Registry:
            def capture_definition(self, definition, payload):
                captured["definition"] = definition
                captured["payload"] = payload
                return capture_result

        orch.registry = _Registry()
        orch._pipeline_run_id = 41

        registered: list = []
        monkeypatch.setattr(
            Orchestrator,
            "_register_verdict",
            lambda self, v, confidence_threshold=None: registered.append(v),
            raising=True,
        )
        return orch, captured, registered

    def test_it_dispatches_with_the_debate_blocks_runner_and_model(self, monkeypatch):
        from hivepilot.services import state_service

        rows: list = []
        monkeypatch.setattr(state_service, "record_verdict", lambda **kw: rows.append(kw))
        orch, captured, registered = self._orch(monkeypatch)

        verdict = orch._run_escalated_judge(
            escalated_by=["ciso"],
            effective=_Effective(),
            reviewer_summaries=["ciso: NEEDS_HUMAN"],
            run_id=7,
            project_name="noxys",
        )

        assert captured["definition"].kind == "cursor"
        assert captured["definition"].model == "gpt-5.6-sol-high"
        assert verdict is not None and verdict.decision == "approve"
        assert registered and registered[0].decision == "approve"

    def test_the_judges_verdict_is_recorded_with_who_summoned_it(self, monkeypatch):
        """The row must say this verdict exists BECAUSE the ciso asked — an
        escalated verdict indistinguishable from a routine one would make the
        mechanism unmeasurable."""
        from hivepilot.services import state_service

        rows: list = []
        monkeypatch.setattr(state_service, "record_verdict", lambda **kw: rows.append(kw))
        orch, _captured, _registered = self._orch(monkeypatch)

        orch._run_escalated_judge(
            escalated_by=["ciso", "qa"],
            effective=_Effective(),
            reviewer_summaries=[],
            run_id=7,
            project_name="noxys",
        )

        assert rows and rows[0]["kind"] == "review-escalation"
        assert "ciso" in rows[0]["summary"] and "qa" in rows[0]["summary"]

    def test_no_judge_configured_refuses_loudly_and_dispatches_nothing(self, monkeypatch, caplog):
        """The fail-closed half. A dropped request is the house defect; a
        fabricated verdict would be worse. Loud, named, and None."""
        import logging

        from hivepilot.services import state_service

        rows: list = []
        monkeypatch.setattr(state_service, "record_verdict", lambda **kw: rows.append(kw))
        orch, captured, registered = self._orch(monkeypatch)

        with caplog.at_level(logging.WARNING):
            verdict = orch._run_escalated_judge(
                escalated_by=["ciso"],
                effective=_Effective(enable_judge=False),
                reviewer_summaries=[],
                run_id=7,
                project_name="noxys",
            )

        assert verdict is None
        assert "definition" not in captured, "dispatched a judge nobody configured"
        assert registered == [] and rows == []
        assert any("ciso" in r.message and "judge" in r.message.lower() for r in caplog.records)

    def test_a_judge_that_answers_garbage_registers_no_fabricated_verdict(self, monkeypatch):
        """`_parse_verdict`'s contract carries through: an unparseable judge
        answer is a None-decision verdict, recorded as such — never invented."""
        from hivepilot.services import state_service

        rows: list = []
        monkeypatch.setattr(state_service, "record_verdict", lambda **kw: rows.append(kw))
        orch, _captured, registered = self._orch(monkeypatch, capture_result="not json at all")

        verdict = orch._run_escalated_judge(
            escalated_by=["ciso"],
            effective=_Effective(),
            reviewer_summaries=[],
            run_id=7,
            project_name="noxys",
        )

        assert verdict is not None and verdict.decision is None
        assert rows[0]["decision"] is None

    def test_a_crashing_judge_does_not_fail_the_review(self, monkeypatch, caplog):
        """Escalation is an EXTRA opinion, not a new failure mode for the
        run: the reviewers' own verdicts already gate promotion."""
        import logging

        orch, _c, _r = self._orch(monkeypatch)
        orch.registry = type(
            "R",
            (),
            {"capture_definition": lambda self, d, p: (_ for _ in ()).throw(RuntimeError("boom"))},
        )()

        with caplog.at_level(logging.WARNING):
            verdict = orch._run_escalated_judge(
                escalated_by=["ciso"],
                effective=_Effective(),
                reviewer_summaries=[],
                run_id=7,
                project_name="noxys",
            )

        assert verdict is None


class TestTheLoopIsWired:
    def test_the_review_loop_collects_escalations_and_calls_the_judge(self):
        """Source pin on the wiring (the loop needs a full pipeline run to
        drive; the helper above carries the behaviour). Both halves must be
        present: collection during iteration, dispatch after registration."""
        import inspect

        from hivepilot import orchestrator

        src = inspect.getsource(orchestrator.Orchestrator)

        assert "_parse_review_escalation(output or" in src
        assert "_run_escalated_judge(" in src
