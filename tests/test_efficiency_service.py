"""Tests for `hivepilot.services.efficiency_service` (Pollen data endpoints
sprint) -- composes two independent, best-effort "savings" sources for the
Pollen Efficiency panel:

- **headroom**: a real, always-present, zero-safe delegate to
  `hivepilot.services.headroom_metrics.efficiency_summary` (a genuinely
  queryable SQLite aggregate -- see that module's own docstring). NEVER
  `None`.
- **rtk**: a best-effort shell-out to `rtk gain -a -f json` (a companion
  dev-tool, not hivepilot data) -- `None` on ANY failure (binary absent,
  non-zero exit, timeout, bad JSON, unexpected shape), never raises, never
  fabricates a number.

The `_isolate_state_db` fixture (autouse, `conftest.py`) isolates
headroom_metrics' underlying SQLite table so these tests never touch a real
``state.db``.
"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

from hivepilot.services import efficiency_service, headroom_metrics

# ---------------------------------------------------------------------------
# headroom_summary -- real, zero-safe, tenant-scoped, never null
# ---------------------------------------------------------------------------


class TestHeadroomSummary:
    def test_empty_is_real_zero_dict_not_null(self) -> None:
        result = efficiency_service.headroom_summary(tenant="default")
        assert result == {
            "total_compressions": 0,
            "chars_saved": 0,
            "avg_ratio": 0.0,
            "p95_ratio": 0.0,
            "est_tokens_saved": 0.0,
            "total_skipped": 0,
            "skip_reasons": {},
            "total_attempts": 0,
        }

    def test_reflects_recorded_compressions(self) -> None:
        headroom_metrics.record_compression(
            tenant="default", step="s1", chars_before=1000, chars_after=250, ratio=0.25
        )
        result = efficiency_service.headroom_summary(tenant="default")
        assert result["total_compressions"] == 1
        assert result["chars_saved"] == 750

    def test_tenant_scoped(self) -> None:
        headroom_metrics.record_compression(
            tenant="acme", step="s1", chars_before=1000, chars_after=250, ratio=0.25
        )
        assert efficiency_service.headroom_summary(tenant="default")["total_compressions"] == 0
        assert efficiency_service.headroom_summary(tenant="acme")["total_compressions"] == 1

    def test_none_tenant_is_unscoped_all_tenants(self) -> None:
        headroom_metrics.record_compression(
            tenant="acme", step="s1", chars_before=1000, chars_after=250, ratio=0.25
        )
        headroom_metrics.record_compression(
            tenant="other", step="s1", chars_before=1000, chars_after=250, ratio=0.25
        )
        assert efficiency_service.headroom_summary(tenant=None)["total_compressions"] == 2


# ---------------------------------------------------------------------------
# rtk_summary -- best-effort shell-out
# ---------------------------------------------------------------------------

_SAMPLE_RTK_JSON = json.dumps(
    {
        "summary": {
            "total_commands": 100,
            "total_input": 1000,
            "total_output": 200,
            "total_saved": 800,
            "avg_savings_pct": 80.0,
        },
        "daily": [
            {"date": "2026-07-20", "commands": 10, "saved_tokens": 80},
            {"date": "2026-07-21", "commands": 10, "saved_tokens": 90},
        ],
    }
)


class TestRtkSummaryAbsentBinary:
    def test_returns_none_when_rtk_not_on_path(self) -> None:
        with patch.object(efficiency_service.shutil, "which", return_value=None):
            assert efficiency_service.rtk_summary() is None


class TestRtkSummaryHappyPath:
    def test_parses_summary_and_series(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["rtk", "gain", "-a", "-f", "json"],
            returncode=0,
            stdout=_SAMPLE_RTK_JSON,
            stderr="",
        )
        with (
            patch.object(efficiency_service.shutil, "which", return_value="/usr/bin/rtk"),
            patch.object(efficiency_service.subprocess, "run", return_value=completed) as mock_run,
        ):
            result = efficiency_service.rtk_summary()

        assert result is not None
        assert result["gain_pct"] == 80.0
        assert result["tokens_saved"] == 800
        assert result["total_commands"] == 100
        assert result["saved_series"] == [
            {"date": "2026-07-20", "saved_tokens": 80},
            {"date": "2026-07-21", "saved_tokens": 90},
        ]
        # rtk's JSON output has no per-command breakdown -- must never be
        # fabricated from the (text-only) "By Command" table.
        assert result["top_commands"] is None

        args, kwargs = mock_run.call_args
        assert args[0] == ["rtk", "gain", "-a", "-f", "json"]
        assert kwargs.get("shell", False) is False
        assert "timeout" in kwargs

    def test_days_truncates_saved_series_to_most_recent(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["rtk", "gain", "-a", "-f", "json"],
            returncode=0,
            stdout=_SAMPLE_RTK_JSON,
            stderr="",
        )
        with (
            patch.object(efficiency_service.shutil, "which", return_value="/usr/bin/rtk"),
            patch.object(efficiency_service.subprocess, "run", return_value=completed),
        ):
            result = efficiency_service.rtk_summary(days=1)
        assert result is not None
        assert result["saved_series"] == [{"date": "2026-07-21", "saved_tokens": 90}]

    def test_days_none_returns_full_series(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["rtk", "gain", "-a", "-f", "json"],
            returncode=0,
            stdout=_SAMPLE_RTK_JSON,
            stderr="",
        )
        with (
            patch.object(efficiency_service.shutil, "which", return_value="/usr/bin/rtk"),
            patch.object(efficiency_service.subprocess, "run", return_value=completed),
        ):
            result = efficiency_service.rtk_summary(days=None)
        assert result is not None
        assert len(result["saved_series"]) == 2


class TestRtkSummaryFailureModesNeverRaiseOrFabricate:
    def test_nonzero_exit_returns_none(self) -> None:
        completed = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="boom")
        with (
            patch.object(efficiency_service.shutil, "which", return_value="/usr/bin/rtk"),
            patch.object(efficiency_service.subprocess, "run", return_value=completed),
        ):
            assert efficiency_service.rtk_summary() is None

    def test_timeout_returns_none(self) -> None:
        with (
            patch.object(efficiency_service.shutil, "which", return_value="/usr/bin/rtk"),
            patch.object(
                efficiency_service.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired(cmd="rtk", timeout=10),
            ),
        ):
            assert efficiency_service.rtk_summary() is None

    def test_oserror_returns_none(self) -> None:
        with (
            patch.object(efficiency_service.shutil, "which", return_value="/usr/bin/rtk"),
            patch.object(efficiency_service.subprocess, "run", side_effect=OSError("no such file")),
        ):
            assert efficiency_service.rtk_summary() is None

    def test_invalid_json_returns_none(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="not json{{{", stderr=""
        )
        with (
            patch.object(efficiency_service.shutil, "which", return_value="/usr/bin/rtk"),
            patch.object(efficiency_service.subprocess, "run", return_value=completed),
        ):
            assert efficiency_service.rtk_summary() is None

    def test_unexpected_json_shape_missing_summary_returns_none(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps({"unexpected": "shape"}), stderr=""
        )
        with (
            patch.object(efficiency_service.shutil, "which", return_value="/usr/bin/rtk"),
            patch.object(efficiency_service.subprocess, "run", return_value=completed),
        ):
            assert efficiency_service.rtk_summary() is None

    def test_non_dict_json_returns_none(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps([1, 2, 3]), stderr=""
        )
        with (
            patch.object(efficiency_service.shutil, "which", return_value="/usr/bin/rtk"),
            patch.object(efficiency_service.subprocess, "run", return_value=completed),
        ):
            assert efficiency_service.rtk_summary() is None


# ---------------------------------------------------------------------------
# efficiency_summary -- composition
# ---------------------------------------------------------------------------


class TestEfficiencySummary:
    def test_composes_headroom_real_and_rtk_null(self) -> None:
        with patch.object(efficiency_service.shutil, "which", return_value=None):
            result = efficiency_service.efficiency_summary(tenant="default")
        assert result["headroom"]["total_compressions"] == 0
        assert result["rtk"] is None

    def test_composes_both_present(self) -> None:
        headroom_metrics.record_compression(
            tenant="default", step="s1", chars_before=1000, chars_after=250, ratio=0.25
        )
        completed = subprocess.CompletedProcess(
            args=["rtk", "gain", "-a", "-f", "json"],
            returncode=0,
            stdout=_SAMPLE_RTK_JSON,
            stderr="",
        )
        with (
            patch.object(efficiency_service.shutil, "which", return_value="/usr/bin/rtk"),
            patch.object(efficiency_service.subprocess, "run", return_value=completed),
        ):
            result = efficiency_service.efficiency_summary(tenant="default")
        assert result["headroom"]["total_compressions"] == 1
        assert result["rtk"]["gain_pct"] == 80.0


class TestHeadroomSkipsAreVisible:
    """Zero compressions has two completely different meanings.

    Until skips were persisted, a plugin running correctly and finding
    nothing worth rewriting was indistinguishable from one that never ran —
    and the dashboard said "not reporting yet" for weeks while it was
    working.
    """

    def test_a_skip_is_recorded_and_counted(self) -> None:
        headroom_metrics.record_skip(tenant="default", step="s1", reason="non_shrinking", chars=4)

        result = efficiency_service.headroom_summary(tenant="default")
        assert result["total_compressions"] == 0
        assert result["total_skipped"] == 1
        assert result["total_attempts"] == 1
        assert result["skip_reasons"] == {"non_shrinking": 1}

    def test_skips_do_not_pollute_the_compression_aggregates(self) -> None:
        headroom_metrics.record_compression(
            tenant="default", step="s1", chars_before=1000, chars_after=250, ratio=0.25
        )
        headroom_metrics.record_skip(tenant="default", step="s2", reason="non_shrinking", chars=4)

        result = efficiency_service.headroom_summary(tenant="default")
        assert result["total_compressions"] == 1
        assert result["chars_saved"] == 750
        assert result["avg_ratio"] == 0.25
        assert result["total_attempts"] == 2

    def test_reasons_are_grouped_not_listed(self) -> None:
        for _ in range(3):
            headroom_metrics.record_skip(tenant="default", step="s", reason="non_shrinking")
        headroom_metrics.record_skip(tenant="default", step="s", reason="already_compressed")

        assert efficiency_service.headroom_summary(tenant="default")["skip_reasons"] == {
            "non_shrinking": 3,
            "already_compressed": 1,
        }

    def test_a_skip_is_tenant_scoped(self) -> None:
        headroom_metrics.record_skip(tenant="other", step="s", reason="non_shrinking")

        assert efficiency_service.headroom_summary(tenant="default")["total_skipped"] == 0
