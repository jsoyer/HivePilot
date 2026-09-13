"""HP-95: tool catalog risk × policy × volatile × idempotency."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from hivepilot.outward import OUTWARD_ACTIONS
from hivepilot.plugin_capabilities import PLUGIN_CAPABILITIES
from hivepilot.services import policy_service
from hivepilot.services.api_service import TypedToolOut
from hivepilot.services.typed_tools import TypedTool
from hivepilot.tool_catalog import (
    APPROVAL_KINDS,
    HIGH_RISKS,
    POLICIES,
    RISK_LEVELS,
    TOOL_APPROVAL_KIND,
    ToolCatalogError,
    ToolDecision,
    approval_payload,
    axes_are_orthogonal,
    lint_catalog,
    load_catalog,
    reload_catalog,
    resolve,
    resolve_typed_tool,
)


def _write_catalog(tmp_path: Path, tools: list[dict]) -> Path:
    path = tmp_path / "tool_catalog.yaml"
    path.write_text(yaml.safe_dump({"version": 1, "tools": tools}), encoding="utf-8")
    return path


def _load(tmp_path: Path, tools: list[dict]):
    return load_catalog(_write_catalog(tmp_path, tools), force=True)


SAMPLE = [
    {
        "token": "Read",
        "risk": "low",
        "default_policy": "allow",
        "volatile": False,
        "idempotent": True,
    },
    {
        "token": "Bash",
        "risk": "high",
        "default_policy": "require_approval",
        "volatile": True,
        "idempotent": False,
    },
    {
        "token": "mcp__github__create_*",
        "risk": "high",
        "default_policy": "require_approval",
        "volatile": False,
        "idempotent": False,
    },
    {
        "token": "openapi__demo__ping",
        "risk": "low",
        "default_policy": "allow",
        "volatile": False,
        "idempotent": True,
        "match": {"source_kind": "openapi", "qualified_name": "openapi__demo__ping"},
    },
]


@pytest.fixture()
def catalog(tmp_path: Path):
    return _load(tmp_path, SAMPLE)


def test_unknown_token_is_denied(catalog) -> None:
    decision = resolve("not-a-real-tool", catalog=catalog)
    assert decision.known is False
    assert decision.policy == "deny"
    assert decision.denied is True
    assert decision.allowed is False
    assert decision.risk == ""


def test_empty_token_is_denied(catalog) -> None:
    decision = resolve("   ", catalog=catalog)
    assert decision.denied is True
    assert decision.known is False


def test_known_low_risk_resolves_allow(catalog) -> None:
    decision = resolve("Read", catalog=catalog)
    assert decision.known is True
    assert decision.risk == "low"
    assert decision.policy == "allow"
    assert decision.allowed is True
    assert decision.volatile is False
    assert decision.idempotent is True
    assert decision.approval_kind == TOOL_APPROVAL_KIND


def test_known_high_risk_resolves_require_approval(catalog) -> None:
    decision = resolve("Bash", catalog=catalog)
    assert decision.risk == "high"
    assert decision.policy == "require_approval"
    assert decision.needs_approval is True
    assert decision.volatile is True
    assert decision.idempotent is False


def test_glob_token_matches_typed_tool_name(catalog) -> None:
    decision = resolve("mcp__github__create_issue", catalog=catalog)
    assert decision.known is True
    assert decision.risk == "high"
    assert decision.policy == "require_approval"


def test_shipped_yaml_resolves_known_and_denies_unknown() -> None:
    root = Path(__file__).resolve().parents[1] / "tool_catalog.yaml"
    shipped = load_catalog(root, force=True)
    assert shipped.entries
    assert resolve("Read", catalog=shipped).allowed
    assert resolve("Bash", catalog=shipped).needs_approval
    assert resolve("totally-unknown-zz", catalog=shipped).denied


def test_load_from_yaml_roundtrip(tmp_path: Path) -> None:
    path = _write_catalog(tmp_path, SAMPLE)
    loaded = load_catalog(path, force=True)
    assert loaded.path == path
    tokens = {entry.token for entry in loaded.entries}
    assert "Read" in tokens
    assert "Bash" in tokens


def test_missing_yaml_is_deny_all(tmp_path: Path) -> None:
    missing = tmp_path / "absent.yaml"
    loaded = load_catalog(missing, force=True)
    assert loaded.entries == ()
    assert resolve("Read", catalog=loaded).denied is True


def test_policy_override_does_not_mutate_risk(catalog) -> None:
    before = resolve("Bash", catalog=catalog)
    after = resolve(
        "Bash",
        catalog=catalog,
        policy_overrides={"Bash": "deny"},
    )
    assert before.risk == after.risk == "high"
    assert after.policy == "deny"
    assert after.default_policy == "require_approval"
    assert after.overridden is True


def test_high_override_to_allow_stays_gated_without_mechanical(catalog) -> None:
    decision = resolve(
        "Bash",
        catalog=catalog,
        policy_overrides={"Bash": "allow"},
        change_class="",
    )
    assert decision.risk == "high"
    assert decision.policy == "require_approval"
    assert decision.allowed is False


def test_high_override_to_allow_requires_hp86_mechanical(catalog) -> None:
    denied = resolve(
        "Bash",
        catalog=catalog,
        policy_overrides={"Bash": "allow"},
        change_class="product_fork",
    )
    allowed = resolve(
        "Bash",
        catalog=catalog,
        policy_overrides={"Bash": "allow"},
        change_class="mechanical",
    )
    assert denied.policy == "require_approval"
    assert denied.risk == "high"
    assert allowed.policy == "allow"
    assert allowed.risk == "high"


def test_tighten_high_to_deny_is_always_allowed(catalog) -> None:
    decision = resolve(
        "Bash",
        catalog=catalog,
        policy_overrides={"Bash": "deny"},
        change_class="mechanical",
    )
    assert decision.policy == "deny"
    assert decision.risk == "high"


def test_orthogonality_with_plugin_caps_and_outward_tokens() -> None:
    catalog_tokens = set(RISK_LEVELS) | set(POLICIES) | set(HIGH_RISKS)
    assert catalog_tokens.isdisjoint(PLUGIN_CAPABILITIES)
    assert catalog_tokens.isdisjoint(OUTWARD_ACTIONS)
    assert axes_are_orthogonal() is True
    assert "network" not in RISK_LEVELS
    assert "external_api" not in POLICIES
    assert "secrets_access" not in APPROVAL_KINDS


def test_resolve_ignores_outward_and_plugin_cap_tokens(catalog) -> None:
    """A capability / outward token is not a tool token and stays denied."""
    for token in ("network", "filesystem", "external_api", "notify"):
        decision = resolve(token, catalog=catalog)
        assert decision.denied is True
        assert decision.known is False


def test_approval_kinds_include_tool_and_not_hp61_classes() -> None:
    assert TOOL_APPROVAL_KIND in APPROVAL_KINDS
    assert APPROVAL_KINDS == frozenset({"partition", "tool", "memory", "skill_evolution"})
    assert "mechanical" not in APPROVAL_KINDS


def test_approval_payload_is_kind_tool(catalog) -> None:
    payload = approval_payload(resolve("Bash", catalog=catalog))
    assert payload["kind"] == "tool"
    assert payload["risk"] == "high"
    assert payload["policy"] == "require_approval"
    assert "change_class" not in payload


def test_typed_tool_resolution_uses_qualified_name(catalog) -> None:
    tool = TypedTool(
        qualified_name="openapi__demo__ping",
        local_name="ping",
        source_kind="openapi",
        source_id="demo",
        description="",
        input_schema={},
    )
    decision = resolve_typed_tool(tool, catalog=catalog)
    assert decision.known is True
    assert decision.risk == "low"
    assert decision.policy == "allow"
    # TypedTool schema is untouched.
    assert set(tool.to_dict()) == {
        "qualified_name",
        "local_name",
        "source_kind",
        "source_id",
        "description",
        "input_schema",
        "id",
    }


def test_v1_tools_response_schema_is_unchanged() -> None:
    """HP-95 must not fold catalog axes into GET /v1/tools."""
    assert set(TypedToolOut.model_fields) == {
        "id",
        "qualified_name",
        "local_name",
        "source_kind",
        "source_id",
        "description",
        "input_schema",
    }
    dumped = TypedToolOut(
        qualified_name="openapi__demo__ping",
        local_name="ping",
        source_kind="openapi",
        source_id="demo",
    ).model_dump()
    assert "risk" not in dumped
    assert "policy" not in dumped
    assert "volatile" not in dumped
    assert "idempotent" not in dumped
    with pytest.raises(ValidationError):
        TypedToolOut(risk="high")  # type: ignore[call-arg]


def test_invalid_catalog_risk_is_rejected(tmp_path: Path) -> None:
    path = _write_catalog(
        tmp_path,
        [{"token": "X", "risk": "network", "default_policy": "allow"}],
    )
    with pytest.raises(ToolCatalogError, match="risk"):
        load_catalog(path, force=True)


def test_lint_catalog_reports_bad_yaml(tmp_path: Path) -> None:
    path = tmp_path / "tool_catalog.yaml"
    path.write_text("tools: [unclosed\n", encoding="utf-8")
    problems = lint_catalog(path)
    assert problems
    reload_catalog()


def test_project_tool_policies_override_policy_only(
    catalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    parsed = yaml.safe_load(
        """
policies:
  default:
    tool_policies:
      Read: deny
  projects:
    acme-api:
      tool_policies:
        Read: require_approval
"""
    )
    monkeypatch.setattr(policy_service, "load_policies", lambda *a, **k: parsed)
    policy_service.reload_policies()
    try:
        default_scope = policy_service.get_policy("other")
        project_scope = policy_service.get_policy("acme-api")
        assert default_scope.tool_policies["Read"] == "deny"
        assert project_scope.tool_policies["Read"] == "require_approval"
        via_project = resolve("Read", catalog=catalog, project="acme-api")
        assert via_project.risk == "low"
        assert via_project.policy == "require_approval"
    finally:
        policy_service.reload_policies()


def test_invalid_tool_policy_override_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parsed = yaml.safe_load(
        """
policies:
  default:
    tool_policies:
      Read: maybe
  projects:
    p: {}
"""
    )
    monkeypatch.setattr(policy_service, "load_policies", lambda *a, **k: parsed)
    policy_service.reload_policies()
    try:
        with pytest.raises(ValueError, match="tool_policies"):
            policy_service.get_policy("p")
    finally:
        policy_service.reload_policies()


def test_decision_to_dict_exposes_flags(catalog) -> None:
    payload = resolve("Read", catalog=catalog).to_dict()
    assert payload["risk"] == "low"
    assert payload["policy"] == "allow"
    assert isinstance(payload, dict)
    assert ToolDecision(
        token="x",
        known=False,
        risk="",
        policy="deny",
        default_policy="deny",
        volatile=True,
        idempotent=False,
        overridden=False,
    ).denied
