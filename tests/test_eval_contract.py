"""HP-113: 0-model eval contract — denied / traversal / pending / approve×2."""

from __future__ import annotations

import inspect

from hivepilot.eval_contract import (
    APPROVE_TWICE,
    DENIED,
    MEMORY,
    PENDING,
    ROUTE_FIELDS,
    SKILL_EVOLUTION,
    TOOL,
    TRAVERSAL,
    TRAVERSAL_PATH,
    UNKNOWN_TOKEN,
    ContractCase,
    ContractError,
    check_suite,
    check_verdict,
    default_cases,
    default_catalog,
    expected_effects,
    run_case,
    run_suite,
    uses_keyword_routing,
)
from hivepilot.pass_store import REJECTED
from hivepilot.skill_evolution_validator import REJECT
from hivepilot.tool_catalog import resolve


def test_harness_has_no_keyword_router() -> None:
    assert uses_keyword_routing() is False
    fields = set(ContractCase.__dataclass_fields__)
    assert fields.isdisjoint(ROUTE_FIELDS)
    params = set(inspect.signature(run_case).parameters)
    assert params.isdisjoint(ROUTE_FIELDS)
    source = inspect.getsource(run_case)
    assert "keyword" not in source.lower()


def test_unknown_and_denied_tokens_resolve_denied() -> None:
    catalog = default_catalog()
    unknown = resolve(UNKNOWN_TOKEN, catalog=catalog)
    assert unknown.denied is True
    denied = resolve("DeniedTool", catalog=catalog)
    assert denied.denied is True
    assert denied.known is True


def test_denied_tool_has_zero_side_effects() -> None:
    for token in (UNKNOWN_TOKEN, "DeniedTool"):
        verdict = run_case(
            ContractCase(
                name=f"denied-{token}",
                gate=DENIED,
                surface=TOOL,
                token=token,
            )
        )
        check_verdict(verdict)
        assert verdict.effect_count == 0
        assert verdict.mutated is False


def test_pending_tool_has_zero_side_effects() -> None:
    verdict = run_case(
        ContractCase(name="pending-write", gate=PENDING, surface=TOOL, token="Write")
    )
    check_verdict(verdict)
    assert verdict.effect_count == 0
    assert verdict.mutated is False
    assert verdict.status == "pending_approval"


def test_approve_twice_is_one_tool_effect() -> None:
    verdict = run_case(
        ContractCase(
            name="approve-write",
            gate=APPROVE_TWICE,
            surface=TOOL,
            token="Write",
        )
    )
    check_verdict(verdict)
    assert verdict.effect_count == 1
    assert verdict.cached_second is True


def test_denied_and_pending_memory_leave_corpus() -> None:
    denied = run_case(ContractCase(name="mem-deny", gate=DENIED, surface=MEMORY, path="prefs"))
    pending = run_case(ContractCase(name="mem-pending", gate=PENDING, surface=MEMORY, path="prefs"))
    check_verdict(denied)
    check_verdict(pending)
    assert denied.status == REJECTED
    assert pending.effect_count == 0
    assert pending.mutated is False


def test_approve_twice_is_one_memory_effect() -> None:
    verdict = run_case(
        ContractCase(
            name="mem-approve",
            gate=APPROVE_TWICE,
            surface=MEMORY,
            path="prefs",
        )
    )
    check_verdict(verdict)
    assert verdict.effect_count == 1
    assert verdict.cached_second is True


def test_traversal_has_zero_side_effects() -> None:
    verdict = run_case(
        ContractCase(
            name="trav",
            gate=TRAVERSAL,
            surface=SKILL_EVOLUTION,
            path=TRAVERSAL_PATH,
        )
    )
    check_verdict(verdict)
    assert verdict.effect_count == 0
    assert verdict.mutated is False
    assert verdict.status == REJECT


def test_default_suite_meets_contract() -> None:
    verdicts = check_suite()
    names = [item.name for item in verdicts]
    assert names == [item.name for item in default_cases()]
    by_gate = {item.gate for item in verdicts}
    assert by_gate == {DENIED, TRAVERSAL, PENDING, APPROVE_TWICE}
    for verdict in verdicts:
        assert verdict.effect_count == expected_effects(verdict.gate)


def test_run_suite_matches_check_suite() -> None:
    raw = run_suite()
    assert len(raw) == len(default_cases())
    for verdict in raw:
        check_verdict(verdict)


def test_unknown_gate_is_refused() -> None:
    try:
        run_case(ContractCase(name="bad", gate="auto", surface=TOOL, token="Write"))
    except ContractError as exc:
        assert "gate" in str(exc)
    else:
        raise AssertionError("expected ContractError")
