"""Per-brain runner parsing for dual-model debates (step D).

A role's debate brains may each pin their own runner via "runner:model", so a
single role can debate across different CLIs (e.g. opencode + claude).
"""

from __future__ import annotations

from hivepilot.orchestrator import _parse_brain


def test_runner_prefixed_brain_splits() -> None:
    assert _parse_brain("claude:claude-sonnet-4-6", "opencode") == ("claude", "claude-sonnet-4-6")


def test_bare_model_uses_default_runner() -> None:
    assert _parse_brain("opencode-go/kimi-k2.7-code", "opencode") == (
        "opencode",
        "opencode-go/kimi-k2.7-code",
    )


def test_unknown_prefix_stays_a_plain_model() -> None:
    # a colon whose prefix isn't a RunnerKind must not be treated as a runner
    assert _parse_brain("foo:bar", "opencode") == ("opencode", "foo:bar")


def test_vibe_runner_prefix() -> None:
    assert _parse_brain("vibe:mistral-large", "opencode") == ("vibe", "mistral-large")
