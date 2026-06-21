"""Unit tests for _parse_mention — no network, no Telegram imports needed."""
from __future__ import annotations

import pytest
from hivepilot.services.telegram_bot import _parse_mention, _AGENT_REGISTRY, _ALIAS_TO_ROLE


# Helpers — build the resolution tables used by _parse_mention
def _make_tables(extra_groups=None, extra_projects=None):
    """Return (groups, agents_known, projects_known) for test isolation."""
    groups = {"noxys": type("G", (), {"hub": "noxys", "components": ["noxys-api", "noxys-ui"]})()}
    if extra_groups:
        groups.update(extra_groups)
    agents_known = set(_ALIAS_TO_ROLE.keys())
    projects_known = {"noxys", "noxys-api", "noxys-ui"}
    if extra_projects:
        projects_known.update(extra_projects)
    return groups, agents_known, projects_known


class TestParseMention:
    """_parse_mention: pure parsing, no I/O."""

    def test_non_at_text_returns_none(self):
        groups, agents, projects = _make_tables()
        kind, name, rest = _parse_mention("hello world", groups=groups, agents_known=agents, projects_known=projects)
        assert kind == "none"

    def test_empty_string_returns_none(self):
        groups, agents, projects = _make_tables()
        kind, name, rest = _parse_mention("", groups=groups, agents_known=agents, projects_known=projects)
        assert kind == "none"

    def test_agent_mention_gustave(self):
        groups, agents, projects = _make_tables()
        kind, name, rest = _parse_mention("@gustave fix the auth bug", groups=groups, agents_known=agents, projects_known=projects)
        assert kind == "agent"
        assert name == "developer"
        assert rest == "fix the auth bug"

    def test_agent_mention_blaise_with_target(self):
        """@blaise @noxys-api review this — target stays in rest for _cmd_mention to parse."""
        groups, agents, projects = _make_tables()
        kind, name, rest = _parse_mention("@blaise @noxys-api review this", groups=groups, agents_known=agents, projects_known=projects)
        assert kind == "agent"
        assert name == "cto"
        assert rest == "@noxys-api review this"

    def test_group_mention(self):
        groups, agents, projects = _make_tables()
        kind, name, rest = _parse_mention("@noxys ship the device-fleet API", groups=groups, agents_known=agents, projects_known=projects)
        assert kind == "group"
        assert name == "noxys"
        assert rest == "ship the device-fleet API"

    def test_group_wins_over_project_collision(self):
        """When 'noxys' is both a group and a project name, group wins."""
        groups, agents, projects = _make_tables()
        kind, name, rest = _parse_mention("@noxys do something", groups=groups, agents_known=agents, projects_known=projects)
        assert kind == "group"

    def test_project_mention_when_not_a_group(self):
        """noxys-api is a project but not a group — should resolve to project."""
        groups, agents, projects = _make_tables()
        kind, name, rest = _parse_mention("@noxys-api review the endpoints", groups=groups, agents_known=agents, projects_known=projects)
        assert kind == "project"
        assert name == "noxys-api"
        assert rest == "review the endpoints"

    def test_unknown_token(self):
        groups, agents, projects = _make_tables()
        kind, name, rest = _parse_mention("@unknownbot do stuff", groups=groups, agents_known=agents, projects_known=projects)
        assert kind == "unknown"

    def test_accent_normalization(self):
        """Accented alias 'aliénor' resolves even with accent."""
        groups, agents, projects = _make_tables()
        kind, name, rest = _parse_mention("@alienor review this", groups=groups, agents_known=agents, projects_known=projects)
        assert kind == "agent"
        assert name == "ceo"

    def test_case_insensitive(self):
        groups, agents, projects = _make_tables()
        kind, name, rest = _parse_mention("@GUSTAVE fix tests", groups=groups, agents_known=agents, projects_known=projects)
        assert kind == "agent"
        assert name == "developer"

    def test_rest_stripped(self):
        groups, agents, projects = _make_tables()
        kind, name, rest = _parse_mention("@gustave   fix bug  ", groups=groups, agents_known=agents, projects_known=projects)
        assert rest == "fix bug"

    def test_mention_only_no_rest(self):
        """@gustave with no order → rest is empty string."""
        groups, agents, projects = _make_tables()
        kind, name, rest = _parse_mention("@gustave", groups=groups, agents_known=agents, projects_known=projects)
        assert kind == "agent"
        assert rest == ""

    def test_all_agent_aliases_resolve(self):
        """Every alias in _ALIAS_TO_ROLE should resolve to agent kind."""
        groups, agents, projects = _make_tables()
        for alias in _ALIAS_TO_ROLE:
            kind, name, rest = _parse_mention(f"@{alias} do it", groups=groups, agents_known=agents, projects_known=projects)
            assert kind == "agent", f"alias {alias!r} should resolve to agent"
