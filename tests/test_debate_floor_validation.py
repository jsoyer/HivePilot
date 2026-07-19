"""Fail-closed validation of the debate config *floor* and override string fields.

Two guards, both closing an empty/degenerate-value fail-open on the debate
surface (see the recurring "security gate empty-value fail-open" bug class):

1. `Settings.judge_confidence_threshold` (env HIVEPILOT_JUDGE_CONFIDENCE_THRESHOLD)
   -- the global *floor* threshold -- must be a finite number in ``(0, 1]``. A
   floor of ``0`` would make ``git_service.is_blocking(verdict, 0)`` approve any
   finite-confidence ACCEPT, i.e. a fail-OPEN PR gate. This mirrors the
   per-pipeline ``DebateConfig.confidence_threshold`` guard that already exists.
2. `DebateConfig.runner` / `.model` -- a present-but-blank override ("" or
   whitespace) is falsy and would silently fall through to the floor in
   ``resolve_debate_config``, hiding a config typo. Reject it at load.
"""

from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from hivepilot.config import Settings
from hivepilot.models import DebateConfig

# --------------------------------------------------------------------------- #
# Floor threshold: Settings.judge_confidence_threshold in (0, 1]               #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("bad", [0, 0.0, -0.1, -1, 1.5, 2, math.inf, -math.inf, math.nan])
def test_floor_threshold_rejects_out_of_range(bad: float) -> None:
    with pytest.raises(ValidationError):
        Settings(judge_confidence_threshold=bad)


@pytest.mark.parametrize("ok", [0.01, 0.5, 0.7, 1.0])
def test_floor_threshold_accepts_in_range(ok: float) -> None:
    assert Settings(judge_confidence_threshold=ok).judge_confidence_threshold == ok


def test_floor_threshold_default_is_valid() -> None:
    # The 0.5 default must itself satisfy the (0, 1] contract.
    assert Settings().judge_confidence_threshold == 0.5


def test_floor_threshold_zero_via_env_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    # The real attack vector: an operator setting the env var to 0. Loading
    # Settings must raise rather than silently disabling the verdict->PR gate.
    monkeypatch.setenv("HIVEPILOT_JUDGE_CONFIDENCE_THRESHOLD", "0")
    with pytest.raises(ValidationError):
        Settings()


# --------------------------------------------------------------------------- #
# Override strings: DebateConfig.runner / .model must be non-blank when set    #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("field", ["runner", "model"])
@pytest.mark.parametrize("blank", ["", "   ", "\t", "\n"])
def test_debate_override_rejects_blank_string(field: str, blank: str) -> None:
    with pytest.raises(ValidationError):
        DebateConfig.model_validate({field: blank})


@pytest.mark.parametrize("field", ["runner", "model"])
def test_debate_override_accepts_real_value(field: str) -> None:
    cfg = DebateConfig.model_validate({field: "claude"})
    assert getattr(cfg, field) == "claude"


@pytest.mark.parametrize("field", ["runner", "model"])
def test_debate_override_none_inherits(field: str) -> None:
    # None is the "inherit the pipeline/floor value" sentinel -- always valid.
    cfg = DebateConfig.model_validate({field: None})
    assert getattr(cfg, field) is None


def test_debate_config_all_absent_is_valid() -> None:
    cfg = DebateConfig()
    assert cfg.runner is None
    assert cfg.model is None
    assert cfg.confidence_threshold is None
    assert cfg.enable_judge is None
    assert cfg.enable_arbiter is None
