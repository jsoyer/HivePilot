"""Unit tests for the direct agent order feature.

Tests _resolve_agent and _parse_ask_args as pure functions — no Telegram
connection required. Uses the module-level helpers defined in telegram_bot.py.
"""

from __future__ import annotations

import hivepilot.services.telegram_bot as bot

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
        import asyncio

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

    def test_all_aliases_registered(self):
        import inspect

        src = inspect.getsource(bot._build_application)
        for role_key, entry in bot._AGENT_REGISTRY.items():
            for alias in entry["aliases"]:
                assert alias in src, f"alias {alias!r} not registered in _build_application"

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
