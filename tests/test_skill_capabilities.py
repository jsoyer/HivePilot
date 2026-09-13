"""HP-108: skill→tools gate for concierge/chat; stage-attach untouched."""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import patch

from hivepilot.cli import stage_attach_skill
from hivepilot.orchestrator import _role_runner_options
from hivepilot.services import concierge_service
from hivepilot.skill_capabilities import (
    build_capability_map,
    catalog_token,
    chat_surface_active,
    chat_tool_surface,
    gate_chat_allowed_tools,
    on_chat_surface,
    parse_skill_tool_tokens,
    resolve_chat_tools,
    skill_is_on,
)
from hivepilot.skill_catalog import SkillCatalog
from hivepilot.skill_trust import register_revision, set_enabled


def _record(
    catalog: SkillCatalog,
    name: str,
    *,
    description: str = "demo",
    allowed_tools: str | None = None,
    enabled: bool = True,
    extra_body: str = "# Body\n",
):
    if allowed_tools is None:
        front = f"---\ndescription: {description}\n---\n"
    else:
        front = f"---\ndescription: {description}\nallowed-tools: {allowed_tools}\n---\n"
    revision = catalog.record(
        name=name,
        files={"SKILL.md": front + extra_body},
        description=description,
    )
    register_revision(
        revision_id=revision.revision_id,
        skill_name=name,
        logical_id=revision.logical_id,
        enabled=enabled,
    )
    return revision


def _web_catalog(*, web_enabled: bool = True) -> SkillCatalog:
    catalog = SkillCatalog()
    _record(catalog, "web-research", allowed_tools="WebSearch, WebFetch", enabled=web_enabled)
    _record(catalog, "read-only", allowed_tools="Read Grep Glob", enabled=True)
    return catalog


class TestTokenAndFrontmatter:
    def test_catalog_token_strips_qualifier(self) -> None:
        assert catalog_token("Bash(gh:*)") == "Bash"
        assert catalog_token("Read") == "Read"
        assert catalog_token("  ") == ""

    def test_frontmatter_list_and_csv(self) -> None:
        assert parse_skill_tool_tokens(
            {"SKILL.md": "---\nallowed-tools:\n  - Read\n  - Grep\n---\n# x\n"}
        ) == ("Read", "Grep")
        assert parse_skill_tool_tokens(
            {"SKILL.md": "---\nallowed_tools: WebSearch, WebFetch\n---\n"}
        ) == ("WebSearch", "WebFetch")

    def test_missing_frontmatter_is_empty(self) -> None:
        assert parse_skill_tool_tokens({"SKILL.md": "# No meta\n"}) == ()


class TestSkillOnOff:
    def test_unknown_skill_is_off(self) -> None:
        catalog = SkillCatalog()
        assert skill_is_on("ghost", catalog=catalog) is False

    def test_unregistered_catalog_skill_stays_on(self) -> None:
        catalog = SkillCatalog()
        catalog.record(name="orphan", files={"SKILL.md": "# x\n"}, description="x")
        assert skill_is_on("orphan", catalog=catalog) is True

    def test_enabled_flag_is_the_off_switch(self) -> None:
        catalog = _web_catalog(web_enabled=True)
        rev = catalog.active_revision("web-research")
        assert rev is not None
        assert skill_is_on("web-research", catalog=catalog) is True
        set_enabled(rev.revision_id, False)
        assert skill_is_on("web-research", catalog=catalog) is False


class TestResolveOnOff:
    def test_skill_on_keeps_mapped_tools(self) -> None:
        catalog = _web_catalog(web_enabled=True)
        assert resolve_chat_tools(
            ["Read", "WebSearch", "WebFetch", "Bash"],
            catalog=catalog,
        ) == ["Read", "WebSearch", "WebFetch", "Bash"]

    def test_skill_off_drops_mapped_tools(self) -> None:
        catalog = _web_catalog(web_enabled=False)
        assert resolve_chat_tools(
            ["Read", "WebSearch", "WebFetch", "Bash"],
            catalog=catalog,
        ) == ["Read", "Bash"]

    def test_unmapped_tools_pass_through(self) -> None:
        catalog = _web_catalog(web_enabled=False)
        assert resolve_chat_tools(["Bash", "Edit"], catalog=catalog) == ["Bash", "Edit"]

    def test_qualified_bash_is_gated_by_bash_token(self) -> None:
        catalog = SkillCatalog()
        _record(catalog, "shell", allowed_tools="Bash", enabled=False)
        assert resolve_chat_tools(["Read", "Bash(gh:*)"], catalog=catalog) == ["Read"]

    def test_union_keeps_token_if_any_owner_is_on(self) -> None:
        catalog = SkillCatalog()
        _record(catalog, "web-a", allowed_tools="WebSearch", enabled=False)
        _record(catalog, "web-b", allowed_tools="WebSearch", enabled=True)
        assert resolve_chat_tools(["WebSearch"], catalog=catalog) == ["WebSearch"]

    def test_overlay_map_wins(self) -> None:
        catalog = SkillCatalog()
        _record(catalog, "custom", allowed_tools="Read", enabled=True)
        assert resolve_chat_tools(
            ["Read", "Edit"],
            catalog=catalog,
            capability_map={"custom": ("Edit",)},
        ) == ["Read", "Edit"]

    def test_empty_catalog_does_not_claim_improve_defaults(self) -> None:
        catalog = SkillCatalog()
        assert "improve" not in build_capability_map(catalog)
        assert resolve_chat_tools(["Read", "Grep", "Glob", "Bash"], catalog=catalog) == [
            "Read",
            "Grep",
            "Glob",
            "Bash",
        ]

    def test_cataloged_improve_without_frontmatter_uses_default(self) -> None:
        catalog = SkillCatalog()
        _record(catalog, "improve", allowed_tools=None, enabled=True)
        assert build_capability_map(catalog)["improve"] == ("Read", "Grep", "Glob")
        disabled = SkillCatalog()
        _record(disabled, "improve", allowed_tools=None, enabled=False)
        assert resolve_chat_tools(["Read", "Bash"], catalog=disabled) == ["Bash"]


class TestNoKeywordForceLoad:
    def test_resolve_does_not_accept_or_use_a_query(self) -> None:
        catalog = _web_catalog(web_enabled=True)
        assert "query" not in inspect.signature(resolve_chat_tools).parameters
        # A message that names tools must not add them.
        assert resolve_chat_tools(["Read"], catalog=catalog) == ["Read"]
        assert "WebSearch" not in resolve_chat_tools(["Read"], catalog=catalog)


class TestChatSurfaceGate:
    def test_inactive_surface_passes_role_allowlist_through(self) -> None:
        catalog = _web_catalog(web_enabled=False)
        assert chat_surface_active() is False
        assert gate_chat_allowed_tools(["Read", "WebSearch"], catalog=catalog) == [
            "Read",
            "WebSearch",
        ]

    def test_active_surface_drops_off_skill_tools(self) -> None:
        catalog = _web_catalog(web_enabled=False)
        with chat_tool_surface():
            assert chat_surface_active() is True
            assert gate_chat_allowed_tools(["Read", "WebSearch"], catalog=catalog) == ["Read"]
        assert chat_surface_active() is False

    def test_on_chat_surface_runs_callable_under_the_gate(self) -> None:
        catalog = _web_catalog(web_enabled=False)

        def _probe(**kwargs: object) -> list[str] | None:
            assert kwargs["ok"] is True
            return gate_chat_allowed_tools(["WebSearch"], catalog=catalog)

        assert on_chat_surface(_probe, ok=True) == []


class TestRoleRunnerOptionsSurface:
    def test_pipeline_path_does_not_strip_tools(self, monkeypatch) -> None:
        catalog = _web_catalog(web_enabled=False)
        role = SimpleNamespace(permission_mode=None, allowed_tools=["Read", "WebSearch"])
        monkeypatch.setattr("hivepilot.roles.get_role", lambda name: role)
        with patch(
            "hivepilot.skill_capabilities.default_catalog",
            return_value=catalog,
        ):
            opts = _role_runner_options("developer")
        assert opts["allowed_tools"] == ["Read", "WebSearch"]

    def test_chat_surface_strips_off_skill_tools(self, monkeypatch) -> None:
        catalog = _web_catalog(web_enabled=False)
        role = SimpleNamespace(permission_mode=None, allowed_tools=["Read", "WebSearch"])
        monkeypatch.setattr("hivepilot.roles.get_role", lambda name: role)
        with (
            patch("hivepilot.skill_capabilities.default_catalog", return_value=catalog),
            chat_tool_surface(),
        ):
            opts = _role_runner_options("developer")
        assert opts["allowed_tools"] == ["Read"]


class TestConciergeClassifierUntouched:
    def test_classifier_stays_no_tools(self) -> None:
        options = concierge_service._build_classifier_options("cli")
        assert options["tools"] == concierge_service._CLASSIFIER_NO_TOOLS

    def test_role_chat_helper_filters_allowlist(self, monkeypatch) -> None:
        catalog = _web_catalog(web_enabled=False)
        role = SimpleNamespace(allowed_tools=["Read", "WebSearch"])
        monkeypatch.setattr("hivepilot.roles.get_role", lambda name: role)
        assert concierge_service.resolve_role_chat_tools("developer", catalog=catalog) == ["Read"]


class TestStageAttachUntouched:
    def test_attach_skill_source_has_no_capability_gate(self) -> None:
        src = inspect.getsource(stage_attach_skill)
        assert "skill_capabilities" not in src
        assert "resolve_chat_tools" not in src
        assert "keyword" not in src.lower()

    def test_orchestrator_skill_name_loop_does_not_import_gate(self) -> None:
        from hivepilot import orchestrator as orch_mod

        src = inspect.getsource(orch_mod)
        # Stage/step names still come from PipelineStage.skills / TaskStep.skills.
        assert "stage_skills" in src
        assert "step.skills" in src
        # The gate is only applied via role allowed_tools + chat_surface.
        assert "gate_chat_allowed_tools" in src
        attach_block = inspect.getsource(stage_attach_skill)
        assert "gate_chat_allowed_tools" not in attach_block
