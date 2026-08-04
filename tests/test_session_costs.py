"""Per-session cost, split by what was actually billed.

A total answers "how much"; it cannot answer "where did it go". On this
workload the intuitive reading of the raw numbers is the wrong one — one
review dispatch recorded **516 982 cache-read tokens** against 3 040 fresh
input and 20 455 output. Read as volume that says the reviewers read far too
much. Read as cost it says they *write* a lot and the reading is cached and
cheap. Only the second is true, and only the split shows it.

That mattered concretely: the volume reading sent an optimisation effort at
"bound what the reviewers read" before anyone had weighted it.
"""

from __future__ import annotations

import pytest

from hivepilot.services import analytics_service, pricing


@pytest.fixture
def priced(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rates at distinct magnitudes so a component mix-up cannot pass."""
    monkeypatch.setattr(
        pricing,
        "_effective_price_map",
        lambda: {
            "m": {"input": 10.0, "output": 100.0, "cache_read": 1.0, "cache_write": 12.5},
            "no-cache-rate": {"input": 10.0, "output": 100.0},
        },
    )


def _seed(run_id_project: str, task: str, steps: list[dict]) -> int:
    from tests.test_analytics_service import _seed_run, _seed_step_with_usage

    run_id = _seed_run(project=run_id_project, task=task, status="success")
    for step in steps:
        _seed_step_with_usage(run_id, step.pop("name", "s"), "success", **step)
    return run_id


class TestSessionCosts:
    def test_it_splits_a_session_by_component(self, priced: None) -> None:
        _seed(
            "noxys",
            "review",
            [
                {
                    "model": "m",
                    "provider": "claude",
                    "input_tokens": 1_000_000,
                    "output_tokens": 1_000_000,
                    "cache_read_tokens": 1_000_000,
                    "cache_creation_tokens": 1_000_000,
                }
            ],
        )

        session = analytics_service.session_costs(days=None)["sessions"][0]

        assert session["by_component"] == {
            "input": 10.0,
            "output": 100.0,
            "cache_read": 1.0,
            "cache_write": 12.5,
        }

    def test_the_components_reconcile_with_the_total(self, priced: None) -> None:
        """Two contradictory figures on one dashboard is worse than one."""
        _seed(
            "noxys",
            "review",
            [
                {
                    "model": "m",
                    "provider": "claude",
                    "input_tokens": 3_040,
                    "output_tokens": 20_455,
                    "cache_read_tokens": 516_982,
                    "cache_creation_tokens": 34_873,
                }
            ],
        )

        session = analytics_service.session_costs(days=None)["sessions"][0]

        assert sum(session["by_component"].values()) == pytest.approx(session["cost_usd"], rel=1e-6)

    def test_the_measured_shape_puts_spend_on_output_not_cache(self, priced: None) -> None:
        """The finding the whole split exists to make visible."""
        _seed(
            "noxys",
            "review",
            [
                {
                    "model": "m",
                    "provider": "claude",
                    "input_tokens": 3_040,
                    "output_tokens": 20_455,
                    "cache_read_tokens": 516_982,
                    "cache_creation_tokens": 34_873,
                }
            ],
        )

        session = analytics_service.session_costs(days=None)["sessions"][0]

        assert session["cache_read_tokens"] > 20 * session["output_tokens"], "volume: cache wins"
        assert session["by_component"]["output"] > session["by_component"]["cache_read"], (
            "cost: output wins — the opposite of what the volume suggests"
        )

    def test_an_unpriceable_step_is_counted_not_hidden(self, priced: None) -> None:
        """A partly-unpriceable session must not look cheap.

        Contributing zero for a step nobody can price understates the bill
        while looking like a complete answer.
        """
        _seed(
            "noxys",
            "review",
            [
                {"model": "m", "provider": "claude", "input_tokens": 1_000, "output_tokens": 1_000},
                {
                    "name": "gap",
                    "model": "unknown-model",
                    "provider": "claude",
                    "input_tokens": 9_000,
                    "output_tokens": 9_000,
                },
            ],
        )

        session = analytics_service.session_costs(days=None)["sessions"][0]

        assert session["unpriced_steps"] == 1
        assert session["input_tokens"] == 10_000, "tokens still counted even when unpriceable"

    def test_a_tokenless_step_is_not_a_pricing_gap(self, priced: None) -> None:
        """A shell step has no model and no tokens. It is not unpriced work."""
        _seed("noxys", "groomer", [{"name": "signals", "provider": "shell"}])

        session = analytics_service.session_costs(days=None)["sessions"][0]

        assert session["unpriced_steps"] == 0

    def test_sessions_are_ranked_by_cost(self, priced: None) -> None:
        _seed(
            "noxys",
            "cheap",
            [{"model": "m", "provider": "claude", "input_tokens": 1, "output_tokens": 1}],
        )
        _seed(
            "noxys",
            "dear",
            [{"model": "m", "provider": "claude", "input_tokens": 1, "output_tokens": 1_000_000}],
        )

        sessions = analytics_service.session_costs(days=None)["sessions"]

        assert sessions[0]["task"] == "dear"

    def test_tenant_isolation(self, priced: None) -> None:
        from tests.test_analytics_service import _seed_run, _seed_step_with_usage

        mine = _seed_run(tenant="acme", project="p", task="t")
        theirs = _seed_run(tenant="other", project="p", task="t")
        for run in (mine, theirs):
            _seed_step_with_usage(
                run, "s", "success", model="m", provider="claude", input_tokens=1, output_tokens=1
            )

        result = analytics_service.session_costs(tenant="acme", days=None)

        assert result["total_sessions"] == 1
