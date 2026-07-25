"""Unit tests for the direct agent order feature.

Tests _resolve_agent and _parse_ask_args as pure functions — no Telegram
connection required. Uses the module-level helpers defined in telegram_bot.py.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import hivepilot.services.telegram_bot as bot
from hivepilot.roles import Role

# ---------------------------------------------------------------------------
# _resolve_agent
# ---------------------------------------------------------------------------


class TestResolveAgent:
    """Resolve tokens to role keys."""

    def test_role_key_direct(self):
        assert bot._resolve_agent("ceo") == "ceo"

    def test_role_key_developer(self):
        assert bot._resolve_agent("developer") == "developer"

    def test_role_key_chief_of_staff(self):
        assert bot._resolve_agent("chief_of_staff") == "chief_of_staff"

    def test_alias_ceo(self):
        assert bot._resolve_agent("ceo") == "ceo"

    def test_alias_alienor(self):
        assert bot._resolve_agent("alienor") == "ceo"

    def test_alias_jules(self):
        assert bot._resolve_agent("jules") == "chief_of_staff"

    def test_alias_cos(self):
        assert bot._resolve_agent("cos") == "chief_of_staff"

    def test_alias_blaise(self):
        assert bot._resolve_agent("blaise") == "cto"

    def test_alias_gustave(self):
        assert bot._resolve_agent("gustave") == "developer"

    def test_alias_dev(self):
        assert bot._resolve_agent("dev") == "developer"

    def test_alias_victor(self):
        assert bot._resolve_agent("victor") == "reviewer"

    def test_alias_hugo(self):
        assert bot._resolve_agent("hugo") == "ciso"

    def test_alias_marie(self):
        assert bot._resolve_agent("marie") == "qa"

    def test_alias_theo(self):
        # ascii alias (no accent)
        assert bot._resolve_agent("theo") == "documentation"

    def test_alias_docs(self):
        assert bot._resolve_agent("docs") == "documentation"

    def test_alias_audit(self):
        assert bot._resolve_agent("audit") == "auditor"

    def test_alias_henri(self):
        assert bot._resolve_agent("henri") == "auditor"

    def test_case_insensitive_upper(self):
        assert bot._resolve_agent("CEO") == "ceo"

    def test_case_insensitive_mixed(self):
        assert bot._resolve_agent("Gustave") == "developer"

    def test_accent_insensitive_alienor(self):
        # Accented form should resolve same as ascii alias
        assert bot._resolve_agent("aliénor") == "ceo"

    def test_accent_insensitive_theo(self):
        assert bot._resolve_agent("théo") == "documentation"

    def test_accent_insensitive_alienor_uppercase(self):
        assert bot._resolve_agent("Aliénor") == "ceo"

    def test_unknown_returns_none(self):
        assert bot._resolve_agent("unknown_agent") is None

    def test_empty_string_returns_none(self):
        assert bot._resolve_agent("") is None

    def test_gibberish_returns_none(self):
        assert bot._resolve_agent("xyzzy42") is None


# ---------------------------------------------------------------------------
# _parse_ask_args
# ---------------------------------------------------------------------------

DEFAULT = "acme"


class TestParseAskArgs:
    """Parse /ask argument lists into (role_key_or_None, target, order)."""

    def test_empty_args(self):
        role, target, order = bot._parse_ask_args([], DEFAULT)
        assert role is None
        assert target == DEFAULT
        assert order == ""

    def test_agent_and_order(self):
        role, target, order = bot._parse_ask_args(["gustave", "add", "tests"], DEFAULT)
        assert role == "developer"
        assert target == DEFAULT
        assert order == "add tests"

    def test_agent_with_at_target_and_order(self):
        role, target, order = bot._parse_ask_args(
            ["cto", "@acme-api", "review", "the", "schema"], DEFAULT
        )
        assert role == "cto"
        assert target == "acme-api"
        assert order == "review the schema"

    def test_at_target_strips_at_sign(self):
        _, target, _ = bot._parse_ask_args(["jules", "@myproject", "plan"], DEFAULT)
        assert target == "myproject"

    def test_no_at_target_uses_default(self):
        _, target, _ = bot._parse_ask_args(["jules", "plan", "things"], DEFAULT)
        assert target == DEFAULT

    def test_unknown_agent_returns_none_role(self):
        role, target, order = bot._parse_ask_args(["nobody", "do", "stuff"], DEFAULT)
        assert role is None
        assert order == "do stuff"

    def test_unknown_agent_with_at_target(self):
        role, target, order = bot._parse_ask_args(["nobody", "@proj", "do", "stuff"], DEFAULT)
        assert role is None
        assert target == "proj"
        assert order == "do stuff"

    def test_empty_order_when_only_agent(self):
        role, target, order = bot._parse_ask_args(["ceo"], DEFAULT)
        assert role == "ceo"
        assert order == ""

    def test_empty_order_when_agent_and_target_only(self):
        role, target, order = bot._parse_ask_args(["ceo", "@proj"], DEFAULT)
        assert role == "ceo"
        assert target == "proj"
        assert order == ""

    def test_accent_agent_resolved(self):
        role, _, _ = bot._parse_ask_args(["aliénor", "kickoff"], DEFAULT)
        assert role == "ceo"

    def test_order_with_multiple_spaces_joined(self):
        role, target, order = bot._parse_ask_args(["marie", "run", "all", "qa", "suites"], DEFAULT)
        assert role == "qa"
        assert order == "run all qa suites"

    def test_auditor_resolves(self):
        role, _, _ = bot._parse_ask_args(["henri", "deep", "audit"], DEFAULT)
        assert role == "auditor"


# ---------------------------------------------------------------------------
# Registry integrity checks
# ---------------------------------------------------------------------------


class TestRegistryIntegrity:
    """Sanity checks on the agent registry itself."""

    def test_all_roles_have_aliases(self):
        for role_key, entry in bot._AGENT_REGISTRY.items():
            assert entry["aliases"], f"{role_key} has no aliases"

    def test_all_aliases_ascii_lowercase(self):
        import string

        allowed = set(string.ascii_lowercase + string.digits + "_")
        for role_key, entry in bot._AGENT_REGISTRY.items():
            for alias in entry["aliases"]:
                bad = set(alias) - allowed
                assert not bad, f"{role_key} alias {alias!r} has non-ascii chars: {bad}"

    def test_alias_to_role_reverse_lookup_complete(self):
        for role_key, entry in bot._AGENT_REGISTRY.items():
            for alias in entry["aliases"]:
                assert bot._ALIAS_TO_ROLE.get(alias) == role_key

    def test_auditor_task_is_none(self):
        assert bot._AGENT_REGISTRY["auditor"]["task"] is None

    def test_all_non_auditor_tasks_are_strings(self):
        for role_key, entry in bot._AGENT_REGISTRY.items():
            if role_key != "auditor":
                assert isinstance(entry["task"], str), f"{role_key} task should be a str"


# ---------------------------------------------------------------------------
# _ALIAS_HANDLERS factory
# ---------------------------------------------------------------------------


class TestAliasHandlers:
    """Verify that the factory produced the right handlers."""

    def test_all_aliases_have_handlers(self):
        for role_key, entry in bot._AGENT_REGISTRY.items():
            for alias in entry["aliases"]:
                assert alias in bot._ALIAS_HANDLERS, f"alias {alias!r} missing from _ALIAS_HANDLERS"

    def test_handlers_are_coroutine_functions(self):

        for alias, handler in bot._ALIAS_HANDLERS.items():
            assert asyncio.iscoroutinefunction(handler), f"handler for {alias!r} is not a coroutine"


# ---------------------------------------------------------------------------
# _build_application registration check
# ---------------------------------------------------------------------------


class TestBuildApplicationAskCommands:
    """_build_application must register /ask and all aliases."""

    def test_ask_registered(self):
        import inspect

        src = inspect.getsource(bot._build_application)
        assert '"ask"' in src

    def test_curated_aliases_registered(self):
        """Only the CURATED aliases (_CURATED_ALIASES) get a dedicated
        Telegram slash command wired into _build_application -- since
        _build_agent_registry() was generalised to iterate every loaded
        ROLES entry (not just the original 9), _AGENT_REGISTRY can now hold
        many more aliases (pm, cfo, release_manager, first names, ...) that
        are addressable via /ask and @mention but do NOT get their own
        dedicated /command. This replaces the old test_all_aliases_registered
        (which asserted ALL registry aliases were literally registered here
        -- an invariant that assumed a small, fully-enumerated registry and
        is no longer the intended architecture)."""
        import inspect

        src = inspect.getsource(bot._build_application)
        for role_key, aliases in bot._CURATED_ALIASES.items():
            for alias in aliases:
                assert alias in src, f"curated alias {alias!r} not registered in _build_application"

    def test_help_mentions_ask(self):
        import inspect

        src = inspect.getsource(bot._cmd_help)
        assert "ask" in src

    def test_help_mentions_agent_aliases(self):
        import inspect

        src = inspect.getsource(bot._cmd_help)
        # A sample of aliases should appear in the help text
        for alias in ("gustave", "jules", "theo", "henri"):
            assert alias in src, f"alias {alias!r} missing from _cmd_help"


# ---------------------------------------------------------------------------
# _build_agent_registry generalization — every loaded role with a
# command_task must become addressable, not just the original curated 9
# (bug: pm/cfo/release_manager/designer_* etc. were unresolvable even though
# hivepilot.roles.ROLES had them loaded).
# ---------------------------------------------------------------------------


def _make_role(
    name: str, title: str, *, display_name: str | None = None, command_task: str | None = None
):
    return Role(
        name=name,
        title=title,
        prompt_file=Path("dummy.md"),
        model_profile="automation",
        inputs=[],
        outputs=[],
        can_block=False,
        order=1,
        display_name=display_name,
        command_task=command_task,
    )


# A fixture roster deliberately mixing: two of the ORIGINAL curated roles
# (ceo, chief_of_staff) so regression on curated aliases is exercised
# together with the fix; three NEW business roles (pm/cfo/release_manager)
# representative of the deployment's actually-loaded-but-unaddressable
# roles from the bug report; and one role with NO command_task, to prove
# the "not configured" degrade path still works instead of crashing.
_FIXTURE_ROLES = {
    "ceo": _make_role("ceo", "CEO", display_name="Aliénor", command_task="ceo-intake"),
    "chief_of_staff": _make_role(
        "chief_of_staff", "Chief of Staff", display_name="Jules", command_task="cos-synthesis"
    ),
    "pm": _make_role("pm", "Product Manager", display_name="Margaux", command_task="noxys-pm-spec"),
    "cfo": _make_role("cfo", "CFO", display_name="Henriette", command_task="noxys-cfo-report"),
    "release_manager": _make_role(
        "release_manager", "Release Manager", display_name="Paul", command_task="release-notes"
    ),
    "ghost": _make_role("ghost", "Ghost Role", display_name=None, command_task=None),
}


def _fixture_registry() -> dict:
    with patch("hivepilot.roles.ROLES", _FIXTURE_ROLES):
        return bot._build_agent_registry()


class TestBuildAgentRegistryGeneralization:
    """_build_agent_registry() must iterate ALL loaded ROLES, not a fixed table."""

    def test_pm_registered_with_correct_task(self):
        registry = _fixture_registry()
        assert registry["pm"]["task"] == "noxys-pm-spec"

    def test_pm_own_name_is_an_alias(self):
        registry = _fixture_registry()
        assert "pm" in registry["pm"]["aliases"]

    def test_cfo_registered_with_correct_task_and_alias(self):
        registry = _fixture_registry()
        assert registry["cfo"]["task"] == "noxys-cfo-report"
        assert "cfo" in registry["cfo"]["aliases"]

    def test_release_manager_registered_with_separator_free_variant(self):
        registry = _fixture_registry()
        assert "release_manager" in registry["release_manager"]["aliases"]
        assert "releasemanager" in registry["release_manager"]["aliases"]

    def test_first_name_alias_derived_for_pm(self):
        registry = _fixture_registry()
        assert "margaux" in registry["pm"]["aliases"]

    def test_role_without_command_task_is_registered_with_none_task(self):
        registry = _fixture_registry()
        assert "ghost" in registry
        assert registry["ghost"]["task"] is None

    def test_role_without_command_task_degrades_gracefully_no_crash(self):
        """_run_agent_order must not crash for a resolvable role_key whose
        task is None -- it should reply with the graceful 'not configured'
        message, exactly like before this fix for an alias-only entry."""
        update = MagicMock()
        update.message.reply_text = AsyncMock()
        registry = _fixture_registry()
        with patch.object(bot, "_AGENT_REGISTRY", registry):
            asyncio.run(bot._run_agent_order(update, "ghost", "acme", "do stuff"))
        update.message.reply_text.assert_awaited_once()
        text = update.message.reply_text.call_args[0][0]
        assert "not configured" in text

    def test_curated_alias_cos_still_resolves(self):
        registry = _fixture_registry()
        assert "cos" in registry["chief_of_staff"]["aliases"]
        assert "jules" in registry["chief_of_staff"]["aliases"]

    def test_curated_alias_ceo_regression(self):
        registry = _fixture_registry()
        assert "ceo" in registry["ceo"]["aliases"]
        assert "alienor" in registry["ceo"]["aliases"]

    def test_no_alias_points_to_the_wrong_role(self):
        """Every alias in the built registry must reverse-resolve to the
        role that actually claims it -- no cross-role alias pointing."""
        registry = _fixture_registry()
        alias_to_role: dict[str, str] = {}
        for role_key, entry in registry.items():
            for alias in entry["aliases"]:
                assert alias not in alias_to_role, (
                    f"alias {alias!r} claimed by both {alias_to_role.get(alias)!r} and {role_key!r}"
                )
                alias_to_role[alias] = role_key

    def test_alias_collision_role_name_always_wins(self):
        """A new role literally NAMED 'jules' must win the 'jules' alias
        over chief_of_staff's curated first-name alias -- the explicit role
        name always wins, and the loser's alias is dropped (never silently
        repointed to the wrong role)."""
        roles = dict(_FIXTURE_ROLES)
        roles["jules"] = _make_role("jules", "Some Other Role", command_task="jules-task")
        with patch("hivepilot.roles.ROLES", roles):
            registry = bot._build_agent_registry()

        assert registry["jules"]["task"] == "jules-task"
        assert "jules" in registry["jules"]["aliases"]
        # chief_of_staff must have LOST the "jules" alias to the role that
        # actually owns that name -- never silently keep pointing "jules" at
        # chief_of_staff once another role is explicitly named "jules".
        assert "jules" not in registry["chief_of_staff"]["aliases"]
        # chief_of_staff keeps its other curated alias untouched.
        assert "cos" in registry["chief_of_staff"]["aliases"]

    def test_deterministic_across_repeated_calls(self):
        """Building the registry twice from the same ROLES must yield the
        identical alias assignment -- no order-dependence."""
        registry_a = _fixture_registry()
        registry_b = _fixture_registry()
        assert registry_a == registry_b

    def test_auditor_meta_agent_preserved_when_not_a_real_role(self):
        """auditor stays a special-cased meta-agent (task None, curated
        audit/henri aliases only) when no real 'auditor' Role is loaded --
        unchanged from before this fix."""
        registry = _fixture_registry()
        assert registry["auditor"]["task"] is None
        assert set(registry["auditor"]["aliases"]) == {"audit", "henri"}

    def test_resolve_agent_pm_end_to_end(self):
        """_resolve_agent must resolve 'pm' once the module-level
        _AGENT_REGISTRY / _ALIAS_TO_ROLE reflect a deployment that loaded a
        pm role -- reproduces the live bug end to end."""
        registry = _fixture_registry()
        alias_to_role = {
            alias: role_key for role_key, entry in registry.items() for alias in entry["aliases"]
        }
        with (
            patch.object(bot, "_AGENT_REGISTRY", registry),
            patch.object(bot, "_ALIAS_TO_ROLE", alias_to_role),
        ):
            assert bot._resolve_agent("pm") == "pm"
            assert bot._resolve_agent("cfo") == "cfo"
            assert bot._resolve_agent("margaux") == "pm"
            assert bot._resolve_agent("cos") == "chief_of_staff"

    def test_execute_concierge_decision_pm_no_longer_unconfigured(self):
        """Reproduces the exact live symptom: the concierge NL path's
        '{role} is not configured on this deployment' branch must NOT fire
        for a role_key that IS loaded (pm) once the registry reflects it."""
        registry = _fixture_registry()

        class _FakeDecision:
            kind = "route"
            role_key = "pm"
            target = "acme"
            order = "spec the new feature"

        update_like = MagicMock()
        update_like.message.reply_text = AsyncMock()
        with (
            patch.object(bot, "_AGENT_REGISTRY", registry),
            patch.object(bot, "_run_agent_order", new=AsyncMock()) as run_agent_order,
        ):
            asyncio.run(bot._execute_concierge_decision(update_like, _FakeDecision()))

        # Must NOT have replied with the "not configured" message.
        if update_like.message.reply_text.await_args_list:
            for call in update_like.message.reply_text.await_args_list:
                assert "not configured" not in call.args[0]
        run_agent_order.assert_awaited_once()
        assert run_agent_order.call_args.args[1] == "pm"
