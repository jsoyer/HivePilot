"""Tests for `hivepilot.partition_sources` — the `PartitionSource`
abstraction (propose -> ratify -> dispatch PRD, Sprint 1, spec section 4).

Mirrors `tests/test_provider.py` (`hivepilot.forges.provider`): a
process-global name -> instance map plus a fail-closed `resolve_source(name)`
lookup that must NEVER silently fall back to a default source.
"""

from __future__ import annotations

import pytest

from hivepilot.partition_sources import (
    SOURCE_MAP,
    PartitionSourceRegistry,
    SourceCollisionError,
    SourceDocument,
    UnknownSourceError,
    resolve_source,
)
from hivepilot.partition_sources.drift_source import DriftSource, DriftSourceError
from hivepilot.partition_sources.run_source import RunSource, RunSourceError
from hivepilot.partition_sources.text_source import TextSource, TextSourceError
from hivepilot.partition_sources.verdict_source import VerdictSource, VerdictSourceError
from hivepilot.services import state_service

# ---------------------------------------------------------------------------
# Registry / resolve_source — mirrors test_provider.py's resolve_forge tests
# ---------------------------------------------------------------------------


def test_all_four_builtins_are_registered() -> None:
    assert {"run", "verdict", "drift", "text"} <= set(SOURCE_MAP)


@pytest.mark.parametrize(
    ("name", "cls"),
    [("run", RunSource), ("verdict", VerdictSource), ("drift", DriftSource), ("text", TextSource)],
)
def test_resolve_source_returns_registered_builtin(name: str, cls: type) -> None:
    source = resolve_source(name)
    assert isinstance(source, cls)
    assert source.name == name


def test_resolve_source_fails_closed_on_unknown_name() -> None:
    with pytest.raises(UnknownSourceError, match="nope"):
        resolve_source("nope")


def test_resolve_source_never_falls_back_to_a_default() -> None:
    """FAIL-CLOSED: removing a registered source must make resolve_source
    raise for that name -- NEVER silently substitute a different source."""
    original = SOURCE_MAP.pop("run")
    try:
        with pytest.raises(UnknownSourceError, match="run"):
            resolve_source("run")
    finally:
        SOURCE_MAP["run"] = original


def test_resolve_source_rejects_near_miss_names() -> None:
    """A plausible typo/plural must not be treated as its singular sibling —
    proves there is no fuzzy/prefix matching hiding a silent fallback."""
    with pytest.raises(UnknownSourceError):
        resolve_source("runs")
    with pytest.raises(UnknownSourceError):
        resolve_source("Run")


def test_source_registry_rejects_silent_collision() -> None:
    class _FakeSource:
        name = "run"

        def fetch(self, ref: str) -> SourceDocument:
            raise NotImplementedError

    with pytest.raises(SourceCollisionError):
        PartitionSourceRegistry.register("run", _FakeSource())  # type: ignore[arg-type]


def test_source_registry_allows_explicit_override() -> None:
    original = SOURCE_MAP["run"]

    class _FakeSource:
        name = "run"

        def fetch(self, ref: str) -> SourceDocument:
            raise NotImplementedError

    fake = _FakeSource()
    try:
        PartitionSourceRegistry.register("run", fake, override=True)  # type: ignore[arg-type]
        assert SOURCE_MAP["run"] is fake
    finally:
        PartitionSourceRegistry.register("run", original, override=True)


def test_known_kinds_includes_all_builtins() -> None:
    assert {"run", "verdict", "drift", "text"} <= PartitionSourceRegistry.known_kinds()


# ---------------------------------------------------------------------------
# SourceDocument shape
# ---------------------------------------------------------------------------


def test_source_document_digest_is_sha256_prefixed() -> None:
    doc = TextSource().fetch("hello world")
    assert doc.digest.startswith("sha256:")
    assert len(doc.digest) == len("sha256:") + 64


# ---------------------------------------------------------------------------
# text source — the universal escape hatch: literal string or file path
# ---------------------------------------------------------------------------


def test_text_source_fetch_literal_string() -> None:
    doc = TextSource().fetch("just some literal text")
    assert doc.kind == "text"
    assert doc.content == "just some literal text"


def test_text_source_fetch_reads_existing_file(tmp_path) -> None:
    f = tmp_path / "bug-1234.md"
    f.write_text("# Bug 1234\nSomething is broken.\n", encoding="utf-8")
    doc = TextSource().fetch(str(f))
    assert "Bug 1234" in doc.content


@pytest.mark.parametrize("blank", ["", "   "])
def test_text_source_rejects_blank_ref(blank: str) -> None:
    with pytest.raises(TextSourceError):
        TextSource().fetch(blank)


def test_text_source_rejects_none_ref() -> None:
    with pytest.raises(TextSourceError):
        TextSource().fetch(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# run source — reads runs/steps via existing state_service accessors
# ---------------------------------------------------------------------------


def test_run_source_fetch_returns_run_and_steps() -> None:
    run_id = state_service.record_run_start("acme-api", "fix-bug", status="failed")
    state_service.record_step(run_id, "plan", "success", detail="planned the fix")
    state_service.record_step(run_id, "apply", "failed", detail="tests still red")

    doc = RunSource().fetch(str(run_id))

    assert doc.kind == "run"
    assert "acme-api" in doc.content
    assert "failed" in doc.content
    assert "planned the fix" in doc.content
    assert "tests still red" in doc.content
    assert doc.metadata["run_id"] == run_id


def test_run_source_unknown_run_id_raises_named_error() -> None:
    with pytest.raises(RunSourceError):
        RunSource().fetch("999999")


def test_run_source_non_integer_ref_raises_named_error() -> None:
    with pytest.raises(RunSourceError):
        RunSource().fetch("not-an-int")


def test_run_source_blank_ref_raises_named_error() -> None:
    with pytest.raises(RunSourceError):
        RunSource().fetch("   ")


# ---------------------------------------------------------------------------
# verdict source — reads a `verdicts` row by id
# ---------------------------------------------------------------------------


def test_verdict_source_fetch_returns_verdict() -> None:
    run_id = state_service.record_run_start("acme-api", "fix-bug")
    verdict_id = state_service.record_verdict(
        run_id=run_id,
        project="acme-api",
        task="fix-bug",
        role="reviewer",
        kind="debate",
        decision="reject",
        confidence=0.9,
        summary="Missing null check",
    )

    doc = VerdictSource().fetch(str(verdict_id))

    assert doc.kind == "verdict"
    assert "reject" in doc.content
    assert "Missing null check" in doc.content
    assert doc.metadata["verdict_id"] == verdict_id


def test_verdict_source_unknown_id_raises_named_error() -> None:
    with pytest.raises(VerdictSourceError):
        VerdictSource().fetch("999999")


def test_verdict_source_non_integer_ref_raises_named_error() -> None:
    with pytest.raises(VerdictSourceError):
        VerdictSource().fetch("nope")


# ---------------------------------------------------------------------------
# drift source — reads a `drift_scans` row by id
# ---------------------------------------------------------------------------


def test_drift_source_fetch_returns_drift_scan() -> None:
    from hivepilot.services.drift_service import DriftResult, DriftSummary

    result = DriftResult(
        project="acme-infra",
        runner="terraform",
        drifted=True,
        summary=DriftSummary(to_add=1, to_change=2, to_destroy=0),
    )
    drift_id = state_service.record_drift_scan(result)

    doc = DriftSource().fetch(str(drift_id))

    assert doc.kind == "drift"
    assert "acme-infra" in doc.content
    assert "terraform" in doc.content
    assert doc.metadata["drift_id"] == drift_id


def test_drift_source_unknown_id_raises_named_error() -> None:
    with pytest.raises(DriftSourceError):
        DriftSource().fetch("999999")


def test_drift_source_non_integer_ref_raises_named_error() -> None:
    with pytest.raises(DriftSourceError):
        DriftSource().fetch("nope")
