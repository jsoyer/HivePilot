"""HP-99: evidence refs, bounded packets, redaction, evolution admissibility."""

from __future__ import annotations

from hivepilot.evidence import (
    DEFAULT_MAX_PACKET_CHARS,
    EVIDENCE_EVENT_KIND,
    Admissibility,
    EvidenceError,
    assess_evolution_claim,
    build_packet,
    claim_ref_ids,
    get_ref,
    ingest_ref,
    list_refs,
)
from hivepilot.pass_store import PENDING, REJECTED, create_pending, get, submit
from hivepilot.services import events
from hivepilot.services.config_provenance import REDACTED, register_secret_value


def test_valid_refs_make_evolution_claim_admissible() -> None:
    ingest_ref(ref_id="trace-1", preview="skill applied", tenant="acme")
    ingest_ref(ref_id="trace-2", preview="tool failed", tenant="acme")
    result = assess_evolution_claim(
        {"evidence_refs": ["trace-1", "trace-2"]},
        tenant="acme",
    )
    assert isinstance(result, Admissibility)
    assert result.admissible is True
    assert result.missing_refs == ()
    assert result.checked_refs == ("trace-1", "trace-2")


def test_missing_refs_make_evolution_claim_not_admissible() -> None:
    ingest_ref(ref_id="only-here", preview="ok", tenant="acme")
    result = assess_evolution_claim(
        {"evidence_refs": ["only-here", "ghost"]},
        tenant="acme",
    )
    assert result.admissible is False
    assert result.missing_refs == ("ghost",)
    assert "not admissible" in result.reason
    assert "ghost" in result.reason


def test_empty_refs_are_not_admissible() -> None:
    result = assess_evolution_claim({"path": "skill.md"}, tenant="acme")
    assert result.admissible is False
    assert "no evidence refs" in result.reason


def test_foreign_tenant_ref_is_missing() -> None:
    ingest_ref(ref_id="shared-id", preview="alpha", tenant="alpha")
    assert get_ref("shared-id", tenant="beta") is None
    result = assess_evolution_claim({"refs": ["shared-id"]}, tenant="beta")
    assert result.admissible is False
    assert result.missing_refs == ("shared-id",)


def test_secrets_are_redacted_at_ingest() -> None:
    secret = "super-secret-token-value"
    register_secret_value(secret)
    stored = ingest_ref(
        ref_id="leaky",
        preview=f"stderr contained {secret}",
        metadata={"note": f"also {secret}", "api_key": "should-never-persist"},
        tenant="acme",
    )
    assert secret not in stored.preview
    assert REDACTED in stored.preview
    assert stored.metadata["note"] == f"also {REDACTED}"
    assert stored.metadata["api_key"] == REDACTED
    assert stored.contains_secret is True
    reloaded = get_ref("leaky", tenant="acme")
    assert reloaded is not None
    assert secret not in reloaded.preview
    assert secret not in str(reloaded.metadata)


def test_watermark_is_change_log_id() -> None:
    stored = ingest_ref(ref_id="wm-1", preview="bounded", tenant="acme")
    rows = [row for row in events.read_since(0) if row["kind"] == EVIDENCE_EVENT_KIND]
    assert rows
    last = rows[-1]
    assert stored.watermark == last["id"]
    assert stored.first_seen_watermark == last["id"]
    assert last["tenant"] == "acme"
    assert last["entity_type"] == "evidence_ref"
    assert last["entity_id"] == "wm-1"
    again = ingest_ref(ref_id="wm-1", preview="bounded again", tenant="acme")
    assert again.first_seen_watermark == stored.first_seen_watermark
    assert again.watermark > stored.watermark
    assert again.watermark == events.latest_change_id()


def test_bounded_packet_omits_overflow_and_lists_missing() -> None:
    ingest_ref(ref_id="a", preview="aaaa", tenant="acme")
    ingest_ref(ref_id="b", preview="bbbb", tenant="acme")
    ingest_ref(ref_id="c", preview="cccc", tenant="acme")
    packet = build_packet(
        ["a", "missing", "b", "c"],
        tenant="acme",
        max_chars=6,
        max_refs=2,
        snippet_chars=4,
    )
    assert packet.build_status == "missing_refs"
    assert packet.missing_refs == ("missing",)
    assert [ref.ref_id for ref in packet.refs] == ["a", "b"]
    assert packet.budget.max_chars == 6
    assert packet.budget.used_chars <= 6
    assert "c" in packet.budget.omitted_refs
    assert packet.redaction_status == "redacted"
    assert packet.watermark >= packet.refs[-1].watermark
    assert DEFAULT_MAX_PACKET_CHARS >= 8000


def test_submit_skill_evolution_rejects_missing_refs() -> None:
    proposal = submit(
        kind="skill_evolution",
        tenant="acme",
        payload={"path": "skills/x/SKILL.md", "evidence_refs": ["nope"]},
    )
    assert proposal.status == REJECTED
    assert proposal.reason.startswith("not admissible")
    stored = get(proposal.id)
    assert stored is not None
    assert stored.status == REJECTED


def test_submit_skill_evolution_keeps_valid_refs_pending() -> None:
    ingest_ref(ref_id="ok-1", preview="applied", tenant="acme")
    proposal = submit(
        kind="skill_evolution",
        tenant="acme",
        payload={"path": "skills/x/SKILL.md", "evidence_refs": ["ok-1"]},
    )
    assert proposal.status == PENDING
    assert get(proposal.id).status == PENDING


def test_create_pending_still_persists_without_refs() -> None:
    proposal = create_pending(kind="skill_evolution", payload={"path": "skill.md"})
    assert proposal.status == PENDING


def test_claim_ref_ids_reads_nested_claims() -> None:
    assert claim_ref_ids(
        {
            "evidence_refs": ["top"],
            "claims": [{"refs": ["nested", "top"]}, "ignore"],
        }
    ) == ("top", "nested")


def test_unknown_ref_type_is_rejected() -> None:
    try:
        ingest_ref(ref_id="bad", ref_type="cloud_upload")
    except EvidenceError as exc:
        assert "ref_type" in str(exc)
    else:
        raise AssertionError("expected EvidenceError")


def test_list_refs_is_tenant_scoped() -> None:
    ingest_ref(ref_id="one", preview="a", tenant="acme")
    ingest_ref(ref_id="two", preview="b", tenant="other")
    assert [ref.ref_id for ref in list_refs(tenant="acme")] == ["one"]
