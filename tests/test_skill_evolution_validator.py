"""HP-110: deterministic validator + safety load, no mutation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from hivepilot.evidence import ingest_ref
from hivepilot.pass_store import inbox
from hivepilot.services.config_provenance import clear_secret_values, register_secret_value
from hivepilot.skill_catalog import SKILL_ID_SIDECAR, SkillCatalog
from hivepilot.skill_dirs import MAX_SKILL_FILE_BYTES
from hivepilot.skill_evolution import preview_accept, propose
from hivepilot.skill_evolution_validator import (
    APPROVE,
    NEEDS_HUMAN_REVIEW,
    REJECT,
    SkillEvolutionValidatorError,
    validate,
    validate_proposal,
)

SAFE_BODY = "# safe skill\n"


def _skill(
    body: str = SAFE_BODY,
    front: Mapping[str, str | Sequence[str]] | None = None,
    **extra: str,
) -> dict[str, str]:
    merged: dict[str, str | Sequence[str]] = dict(front or {})
    merged.update(extra)
    if not merged:
        return {"SKILL.md": body}
    lines = ["---"]
    for key, value in merged.items():
        if isinstance(value, list):
            lines.append(f"{key}:")
            lines.extend(f"  - {item}" for item in value)
        else:
            lines.append(f"{key}: {value}")
    lines.append("---")
    lines.append(body)
    return {"SKILL.md": "\n".join(lines) + "\n"}


def _codes(result) -> set[str]:
    return {item.code for item in result.findings}


class TestTraversalAndSymlink:
    def test_relative_escape_is_rejected(self) -> None:
        result = validate(files={"../../etc/passwd": "x"})
        assert result.result == REJECT
        assert result.mutated is False
        assert "path_traversal" in _codes(result)

    def test_absolute_path_is_rejected(self) -> None:
        result = validate(files={"/etc/passwd": "x"})
        assert result.result == REJECT
        assert "path_absolute" in _codes(result)

    def test_url_encoded_traversal_is_rejected(self) -> None:
        result = validate(files={"%2e%2e/secret": "x"})
        assert result.result == REJECT
        assert "path_traversal" in _codes(result)

    def test_escaping_symlink_is_rejected(self, tmp_path: Path) -> None:
        outside = tmp_path / "outside.txt"
        outside.write_text("host secret", encoding="utf-8")
        skill = tmp_path / "skills" / "leaky"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(SAFE_BODY, encoding="utf-8")
        (skill / "leak.md").symlink_to(outside)
        result = validate(skill_root=skill)
        assert result.result == REJECT
        assert "symlink_escape" in _codes(result)


class TestSizeUtf8Frontmatter:
    def test_oversized_file_is_rejected(self) -> None:
        result = validate(files={"SKILL.md": "x" * (MAX_SKILL_FILE_BYTES + 1)})
        assert result.result == REJECT
        assert "oversized_file" in _codes(result)

    def test_invalid_utf8_is_rejected(self) -> None:
        result = validate(files={"SKILL.md": b"\xff\xfe not utf-8"})
        assert result.result == REJECT
        assert "invalid_utf8" in _codes(result)

    def test_pickle_payload_is_rejected(self) -> None:
        result = validate(files={"model.pkl": b"\x80\x04payload"})
        assert result.result == REJECT
        assert "pickle_forbidden" in _codes(result)

    def test_malformed_frontmatter_is_rejected(self) -> None:
        result = validate(files={"SKILL.md": "---\n: [bad\n"})
        assert result.result == REJECT
        assert "frontmatter_malformed" in _codes(result)

    def test_name_mismatch_is_rejected(self) -> None:
        result = validate(files=_skill(name="other"), name="expected")
        assert result.result == REJECT
        assert "frontmatter_name_mismatch" in _codes(result)

    def test_invalid_min_role_is_rejected(self) -> None:
        result = validate(files=_skill(min_role="wizard"), name="gated")
        assert result.result == REJECT
        assert "frontmatter_invalid_min_role" in _codes(result)


class TestSecrets:
    def test_registered_secret_is_rejected(self) -> None:
        register_secret_value("super-secret-value-xyz")
        try:
            result = validate(files=_skill("token=super-secret-value-xyz\n"))
            assert result.result == REJECT
            assert "secret_registered" in _codes(result)
        finally:
            clear_secret_values()

    def test_secret_frontmatter_key_is_rejected(self) -> None:
        result = validate(files=_skill(api_key="not-a-grant"), name="leaky")
        assert result.result == REJECT
        assert "secret_key" in _codes(result)

    def test_pem_private_key_is_rejected(self) -> None:
        body = "-----BEGIN PRIVATE KEY-----\nMIIB\n-----END PRIVATE KEY-----\n"
        result = validate(files=_skill(body))
        assert result.result == REJECT
        assert "secret_material" in _codes(result)

    def test_secret_filename_is_rejected(self) -> None:
        result = validate(files={"id_rsa": "-----BEGIN OPENSSH PRIVATE KEY-----\n"})
        assert result.result == REJECT
        assert "secret_filename" in _codes(result) or "secret_material" in _codes(result)


class TestPrivilegeApprovals:
    def test_new_bash_without_approval_needs_review(self) -> None:
        result = validate(files=_skill(front={"allowed-tools": ["Bash"]}), name="wide")
        assert result.result == NEEDS_HUMAN_REVIEW
        assert result.mutated is False
        assert any(code.startswith("privilege_") for code in _codes(result))
        assert "allowed-tools:Bash" in result.privilege_extensions
        assert "shell" in result.privilege_extensions

    def test_generic_approval_is_not_specific(self) -> None:
        result = validate(
            files=_skill(front={"allowed-tools": ["Bash"]}),
            name="wide",
            specific_approvals=["approve", "*", "all"],
        )
        assert result.result == NEEDS_HUMAN_REVIEW

    def test_specific_approval_allows_named_extension(self) -> None:
        result = validate(
            files=_skill(front={"allowed-tools": ["Bash"]}),
            name="wide",
            specific_approvals=["allowed-tools:Bash", "shell"],
        )
        assert result.result == APPROVE
        assert result.mutated is False

    def test_hooks_and_permissions_need_named_approval(self) -> None:
        result = validate(
            files={
                **_skill(front={"permission_mode": "bypassPermissions", "hooks": ["PreToolUse"]}),
                "hooks/pre.sh": "#!/bin/sh\necho hi\n",
            },
            name="elevated",
        )
        assert result.result == NEEDS_HUMAN_REVIEW
        codes = _codes(result)
        assert "privilege_hooks" in codes
        assert "privilege_permissions" in codes
        assert "privilege_shell" in codes

    def test_baseline_keeps_existing_tools(self) -> None:
        baseline = _skill(front={"allowed-tools": ["Read", "Bash"]})
        proposed = _skill(front={"allowed-tools": ["Read", "Bash"]})
        result = validate(files=proposed, name="stable", baseline_files=baseline)
        assert result.result == APPROVE
        assert result.privilege_extensions == ()


class TestNoMutation:
    def test_validate_does_not_write_or_persist(self, tmp_path: Path) -> None:
        skill = tmp_path / "skills" / "drafted"
        skill.mkdir(parents=True)
        target = skill / "SKILL.md"
        target.write_text(SAFE_BODY, encoding="utf-8")
        before = target.read_text(encoding="utf-8")
        mtime = target.stat().st_mtime_ns
        result = validate(
            files={"SKILL.md": "# mutated\n", "../../escape.txt": "nope"},
            name="drafted",
            skill_root=skill,
        )
        assert result.result == REJECT
        assert result.mutated is False
        assert target.read_text(encoding="utf-8") == before
        assert target.stat().st_mtime_ns == mtime
        assert not (skill / SKILL_ID_SIDECAR).exists()
        assert inbox(kind="skill_evolution") == []
        assert len(SkillCatalog()) == 0

    def test_safe_skill_is_approved_without_writes(self, tmp_path: Path) -> None:
        skill = tmp_path / "skills" / "ok"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(SAFE_BODY, encoding="utf-8")
        result = validate(files=_skill(), name="ok", skill_root=skill)
        assert result.result == APPROVE
        assert result.mutated is False
        assert (skill / "SKILL.md").read_text(encoding="utf-8") == SAFE_BODY
        assert inbox(kind="skill_evolution") == []


class TestProposeGate:
    def test_reject_does_not_persist_draft(self) -> None:
        ingest_ref(ref_id="val-ev", preview="ok")
        draft = propose(
            evolution_type="fix",
            name="faulty",
            revision_id="rev-fix",
            causal_event_id="evt-1",
            representative_result="result-1",
            evidence_refs=["val-ev"],
            files={"../escape.md": "# bad\n"},
        )
        assert draft.persisted is False
        assert draft.status == "blocked"
        assert inbox(kind="skill_evolution") == []
        assert draft.payload is not None
        assert draft.payload["validation"]["result"] == REJECT

    def test_privilege_widening_persists_for_human_review(self) -> None:
        ingest_ref(ref_id="priv-ev", preview="ok")
        draft = propose(
            evolution_type="fix",
            name="faulty",
            revision_id="rev-fix",
            causal_event_id="evt-1",
            representative_result="result-1",
            evidence_refs=["priv-ev"],
            files=_skill(front={"allowed-tools": ["Bash"]}),
        )
        assert draft.persisted is True
        assert draft.payload is not None
        assert draft.payload["validation"]["result"] == NEEDS_HUMAN_REVIEW
        card = inbox(kind="skill_evolution")[0]
        assert card.payload["validation"]["result"] == NEEDS_HUMAN_REVIEW

    def test_hp111_hook_validates_without_applying(self) -> None:
        ingest_ref(ref_id="hook-ev", preview="ok")
        draft = propose(
            evolution_type="fix",
            name="faulty",
            revision_id="rev-fix",
            causal_event_id="evt-1",
            representative_result="result-1",
            evidence_refs=["hook-ev"],
            files=_skill(),
        )
        verdict = validate_proposal(draft.proposal_id)
        assert verdict.result == APPROVE
        assert verdict.mutated is False
        preview = preview_accept(draft.proposal_id)
        assert preview["would_mutate"] is False
        assert preview["validation"]["result"] == APPROVE
        assert inbox(kind="skill_evolution")[0].status == "PENDING"


def test_missing_proposal_is_rejected() -> None:
    with pytest.raises(SkillEvolutionValidatorError, match="proposal_id"):
        validate_proposal("")
    missing = validate_proposal("does-not-exist")
    assert missing.result == REJECT
    assert missing.reason == "proposal_not_found"
    assert missing.mutated is False
