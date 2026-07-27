"""Tests for hivepilot.services.cost_backfill (usage-capture-modelusage fix).

`_isolate_state_db` (conftest.py, autouse) redirects state.db to a per-test
tmp file, so these tests never touch a real database. Steps are seeded via
direct SQL for full control over exactly the pre-fix shapes this backfill
targets (NULL/zero cost, bare-alias models, missing cache columns).
"""

from __future__ import annotations

from hivepilot.services import cost_backfill, db, state_service


def _seed_run() -> int:
    state_service.init_db()
    with db.connect() as conn:
        return db.insert_returning_id(
            conn,
            "INSERT INTO runs (project, task, status, tenant) VALUES (?, ?, ?, ?)",
            ("proj", "task", "success", "default"),
        )


def _seed_step(
    run_id: int,
    step: str,
    *,
    model: str | None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cost_usd: float | None = None,
) -> int:
    state_service.init_db()
    with db.connect() as conn:
        return db.insert_returning_id(
            conn,
            db.ph(
                "INSERT INTO steps (run_id, step, status, provider, model, "
                "input_tokens, output_tokens, cost_usd) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            ),
            (run_id, step, "success", "claude", model, input_tokens, output_tokens, cost_usd),
        )


def _get_cost(step_id: int) -> float | None:
    with db.connect() as conn:
        row = conn.execute(
            db.ph("SELECT cost_usd FROM steps WHERE id=?"), (step_id,)
        ).fetchone()
    return row["cost_usd"]


class TestBackfillUnpricedCosts:
    def test_dry_run_does_not_write(self) -> None:
        run_id = _seed_run()
        step_id = _seed_step(
            run_id, "s1", model="claude-sonnet-4-6", input_tokens=1_000_000, output_tokens=500_000
        )

        result = cost_backfill.backfill_unpriced_costs(apply=False)

        assert result.updated == 1
        assert step_id in result.updated_ids
        assert _get_cost(step_id) is None, "dry-run must never write"

    def test_apply_writes_computed_cost(self) -> None:
        run_id = _seed_run()
        step_id = _seed_step(
            run_id, "s1", model="claude-sonnet-4-6", input_tokens=1_000_000, output_tokens=500_000
        )

        result = cost_backfill.backfill_unpriced_costs(apply=True)

        assert result.updated == 1
        assert _get_cost(step_id) == 10.5

    def test_leaves_genuinely_unpriced_rows_untouched(self) -> None:
        """A model absent from the price map even after the expanded table
        must be left honestly unpriced -- never fabricated."""
        run_id = _seed_run()
        step_id = _seed_step(
            run_id, "s1", model="some-model-nobody-priced", input_tokens=100, output_tokens=100
        )

        result = cost_backfill.backfill_unpriced_costs(apply=True)

        assert result.still_unpriced == 1
        assert result.updated == 0
        assert _get_cost(step_id) is None

    def test_skips_rows_with_no_model(self) -> None:
        run_id = _seed_run()
        _seed_step(run_id, "s1", model=None, input_tokens=100, output_tokens=100)

        result = cost_backfill.backfill_unpriced_costs(apply=True)

        assert result.scanned == 0

    def test_skips_rows_with_no_tokens_at_all(self) -> None:
        run_id = _seed_run()
        _seed_step(run_id, "s1", model="claude-sonnet-4-6", input_tokens=None, output_tokens=None)

        result = cost_backfill.backfill_unpriced_costs(apply=True)

        assert result.scanned == 0

    def test_does_not_touch_rows_with_a_real_priced_cost(self) -> None:
        run_id = _seed_run()
        step_id = _seed_step(
            run_id,
            "s1",
            model="claude-sonnet-4-6",
            input_tokens=1_000_000,
            output_tokens=500_000,
            cost_usd=1.23,
        )

        result = cost_backfill.backfill_unpriced_costs(apply=True)

        assert result.scanned == 0
        assert _get_cost(step_id) == 1.23, "a real self-reported cost must never be overwritten"

    def test_recovers_bare_alias_rows(self) -> None:
        """The real production symptom: historical steps.model stored the
        bare CLI alias ('sonnet'), not a canonical id -- the expanded price
        map now prices the alias itself."""
        run_id = _seed_run()
        step_id = _seed_step(
            run_id, "s1", model="sonnet", input_tokens=1_000_000, output_tokens=500_000
        )

        result = cost_backfill.backfill_unpriced_costs(apply=True)

        assert result.updated == 1
        assert _get_cost(step_id) == 10.5

    def test_zero_cost_with_nonzero_tokens_is_backfilled(self) -> None:
        """The other real production symptom: cost_usd recorded as exactly
        0.0 alongside real, nonzero token counts -- a paid model cannot
        genuinely cost $0.0 for a million tokens, so this is treated as the
        same understated-cost bug, not a legitimate free step."""
        run_id = _seed_run()
        step_id = _seed_step(
            run_id,
            "s1",
            model="claude-sonnet-4-6",
            input_tokens=1_000_000,
            output_tokens=500_000,
            cost_usd=0.0,
        )

        result = cost_backfill.backfill_unpriced_costs(apply=True)

        assert result.updated == 1
        assert _get_cost(step_id) == 10.5

    def test_result_reports_scanned_total(self) -> None:
        run_id = _seed_run()
        _seed_step(run_id, "s1", model="claude-sonnet-4-6", input_tokens=1, output_tokens=1)
        _seed_step(run_id, "s2", model="some-model-nobody-priced", input_tokens=1, output_tokens=1)

        result = cost_backfill.backfill_unpriced_costs(apply=False)

        assert result.scanned == 2
        assert result.updated == 1
        assert result.still_unpriced == 1
