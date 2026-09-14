"""HP-113: behavior-memory eval is opt-in — gating + no keyword routing."""

from __future__ import annotations

import inspect

from hivepilot.eval_behavior_memory import (
    BEHAVIOR_MEMORY_EVAL_ENV,
    SCENARIOS,
    BehaviorCase,
    BehaviorMemoryEvalError,
    default_cases,
    enabled,
    run_case,
    uses_keyword_routing,
)
from hivepilot.eval_contract import ROUTE_FIELDS


def test_opt_in_flag_defaults_off() -> None:
    assert enabled({}) is False
    assert enabled({BEHAVIOR_MEMORY_EVAL_ENV: ""}) is False
    assert enabled({BEHAVIOR_MEMORY_EVAL_ENV: "0"}) is False
    assert enabled({BEHAVIOR_MEMORY_EVAL_ENV: "true"}) is False
    assert enabled({BEHAVIOR_MEMORY_EVAL_ENV: "1"}) is True


def test_harness_has_no_keyword_router() -> None:
    assert uses_keyword_routing() is False
    fields = set(BehaviorCase.__dataclass_fields__)
    assert fields.isdisjoint(ROUTE_FIELDS)
    params = set(inspect.signature(run_case).parameters)
    assert params.isdisjoint(ROUTE_FIELDS)


def test_default_cases_are_explicit_named_scenarios() -> None:
    names = [item.scenario for item in default_cases()]
    assert names == list(SCENARIOS)
    for case in default_cases():
        assert case.key
        assert case.scenario.isidentifier()


def test_unknown_scenario_is_refused() -> None:
    try:
        run_case(BehaviorCase(name="bad", scenario="keyword-route", key="prefs"))
    except BehaviorMemoryEvalError as exc:
        assert "scenario" in str(exc)
    else:
        raise AssertionError("expected BehaviorMemoryEvalError")
