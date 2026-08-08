"""Say out loud which tools an agent was refused.

Claude Code reports every refused tool call in its result envelope, on every
dispatch. HivePilot threw the field away: `permission_denials` appeared
nowhere in the codebase.

Run 356's CTO stage is what that costs. The agent asked for a cheap
directory listing —

    rtk fd . surfaces/agent -t d -d 3     DENIED

— was refused, explored the monorepo file by file instead, reached 84 969
input tokens and died on "Prompt is too long". $1.17 spent, the stage
failed, eleven stages skipped. The cause was one missing line in a role's
`allowed_tools`, and the only way to find it was reading a 4 000-character
failure blob by hand.

**The failing case is the lucky one.** An agent that is refused a tool and
still finishes looks like a success: it simply did the expensive thing
instead, and will keep doing it on every run until somebody happens to
look. So denials are reported whether the dispatch succeeded or not.

This is what makes per-command `allowed_tools` sustainable. The alternative
— granting `Bash(rtk:*)` — is not a wider whitelist but the absence of one:
`rtk proxy <cmd>` runs anything unfiltered, so that pattern is a full shell
grant wearing a narrow name. Keeping the grants narrow costs one line per
new tool; the price is only unacceptable while the need for that line is
invisible.
"""

from __future__ import annotations

import json

from hivepilot.runners.claude_runner import extract_permission_denials

_DENIAL = {
    "tool_name": "Bash",
    "tool_use_id": "toolu_01",
    "tool_input": {"command": "rtk fd . surfaces/agent -t d -d 3", "description": "list dirs"},
}


class TestItFindsWhatWasRefused:
    def test_a_denied_command_is_extracted(self) -> None:
        envelope = json.dumps({"result": "ok", "permission_denials": [_DENIAL]})

        assert extract_permission_denials(envelope) == ["rtk fd . surfaces/agent -t d -d 3"]

    def test_several_denials_are_all_reported(self) -> None:
        envelope = json.dumps({"result": "ok", "permission_denials": [_DENIAL, _DENIAL]})

        assert len(extract_permission_denials(envelope)) == 2

    def test_a_non_bash_tool_is_named_by_its_tool(self) -> None:
        """Not every denial carries a command. Naming the tool still tells an
        operator which grant is missing."""
        envelope = json.dumps(
            {"result": "ok", "permission_denials": [{"tool_name": "WebFetch", "tool_input": {}}]}
        )

        assert extract_permission_denials(envelope) == ["WebFetch"]


class TestItIsQuietWhenThereIsNothingToSay:
    def test_no_denials_means_an_empty_list(self) -> None:
        assert extract_permission_denials(json.dumps({"result": "ok"})) == []

    def test_an_empty_list_stays_empty(self) -> None:
        assert (
            extract_permission_denials(json.dumps({"result": "x", "permission_denials": []})) == []
        )


class TestItNeverBreaksADispatch:
    def test_unparseable_output_yields_nothing(self) -> None:
        """A dispatch must not fail because its diagnostics could not be
        read. This is reporting, not control flow."""
        assert extract_permission_denials("not json at all") == []

    def test_a_malformed_denial_entry_is_skipped(self) -> None:
        envelope = json.dumps({"result": "x", "permission_denials": ["a bare string", 42, None]})

        assert extract_permission_denials(envelope) == []

    def test_a_non_list_field_is_survived(self) -> None:
        envelope = json.dumps({"result": "x", "permission_denials": {"tool_name": "Bash"}})

        assert extract_permission_denials(envelope) == []

    def test_empty_input_is_survived(self) -> None:
        assert extract_permission_denials("") == []
