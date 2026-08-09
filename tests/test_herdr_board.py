"""The layout that turns a set of roles into a standing board.

herdr applies a JSON layout. This builds one: a pane per role, each running
`hivepilot watch --role <name>`.

The single rule that matters comes straight from the API's own semantics.
A `pane` node may legally omit `command` — but on `layout.apply` herdr then
launches the default shell. **Absent `command` means "start a shell", not
"a pane with nothing in it".** A board generated without commands would
apply cleanly and produce a wall of empty prompts, which looks like a
working board that has nothing to report.

So every pane carries its command, and a test says so directly. This is
also why nothing here builds an id: workspace/tab/pane ids are opaque and
are parsed from herdr's output, never constructed.
"""

from __future__ import annotations

import json

import pytest

from hivepilot.services.herdr_board import build_board_layout


class TestEveryPaneRuns:
    def test_a_pane_without_a_command_is_never_emitted(self) -> None:
        """The whole failure mode in one assertion: a command-less pane is a
        shell prompt, not a viewer."""
        layout = build_board_layout(["reviewer", "ciso", "qa"])

        panes = _panes(layout)
        assert panes
        for pane in panes:
            assert pane.get("command"), f"pane {pane.get('label')} would start a shell"

    def test_each_command_follows_exactly_one_role(self) -> None:
        layout = build_board_layout(["reviewer", "ciso"])

        commands = [p["command"] for p in _panes(layout)]
        assert any("--role reviewer" in c for c in commands)
        assert any("--role ciso" in c for c in commands)

    def test_one_pane_per_role(self) -> None:
        layout = build_board_layout(["a", "b", "c", "d"])

        assert len(_panes(layout)) == 4

    def test_panes_are_labelled_by_role(self) -> None:
        layout = build_board_layout(["ciso"])

        assert _panes(layout)[0]["label"] == "ciso"


class TestRemoteBoards:
    def test_a_remote_host_pipes_over_ssh(self) -> None:
        """The board runs where the operator is; the services run on the box.
        `--stdin` keeps that working with no new network surface."""
        layout = build_board_layout(["ciso"], remote="noxysdevbot", log_path="/runs/logs/x.log")

        command = _panes(layout)[0]["command"]
        assert command.startswith("ssh ")
        assert "noxysdevbot" in command
        assert "--stdin" in command
        assert "/runs/logs/x.log" in command

    def test_a_local_board_reads_the_file_directly(self) -> None:
        command = _panes(build_board_layout(["ciso"]))[0]["command"]

        assert "ssh" not in command
        assert "--stdin" not in command

    def test_a_remote_board_without_a_log_path_is_refused(self) -> None:
        """Guessing the path is the one thing this must not do — `logs_dir`
        is cwd-relative and the box carries five candidate files, four of
        them stale. A wrong guess produces a silent, plausible-looking board."""
        with pytest.raises(ValueError, match="log_path"):
            build_board_layout(["ciso"], remote="noxysdevbot")


class TestInputIsValidated:
    def test_no_roles_is_refused(self) -> None:
        with pytest.raises(ValueError, match="role"):
            build_board_layout([])

    def test_a_role_name_that_could_reshape_the_command_is_refused(self) -> None:
        """The role name lands inside a shell command. Anything outside a
        plain identifier is rejected rather than escaped: roles come from
        config, so a strange one is a mistake to surface, not to accommodate."""
        for hostile in ("ciso; rm -rf /", "ciso && curl evil", "$(whoami)", "a b", "`id`"):
            with pytest.raises(ValueError):
                build_board_layout([hostile])

    def test_ordinary_role_names_are_accepted(self) -> None:
        for ok in ("ciso", "qa", "tech_lead", "reviewer-2"):
            assert build_board_layout([ok])

    def test_duplicate_roles_collapse(self) -> None:
        """Two panes following the same role is never what was meant."""
        assert len(_panes(build_board_layout(["ciso", "ciso"]))) == 1


class TestItIsValidJson:
    def test_the_layout_serialises(self) -> None:
        json.dumps(build_board_layout(["ciso", "qa"]))

    def test_no_identifier_is_invented(self) -> None:
        """herdr ids are opaque and come from its output. A layout that
        hand-builds `w1:p1` is guessing at another process's state."""
        rendered = json.dumps(build_board_layout(["ciso", "qa"]))

        for invented in ('"w1"', '"w1:t1"', '"w1:p1"', '"pane_id"'):
            assert invented not in rendered


def _panes(node) -> list[dict]:
    if isinstance(node, dict):
        if node.get("type") == "pane":
            return [node]
        found: list[dict] = []
        for value in node.values():
            found.extend(_panes(value))
        return found
    if isinstance(node, list):
        return [p for item in node for p in _panes(item)]
    return []


class TestRolesDefaultsToTheConfiguredRoster:
    """`--roles` was required and hand-typed.

    Nobody enumerates twenty roles, which is exactly why no full board ever
    existed on this deployment: a feature already paid for, unreachable
    through its own ergonomics.
    """

    def _invoke(self, monkeypatch: pytest.MonkeyPatch, roster: dict[str, object], *args: str):
        from typer.testing import CliRunner

        import hivepilot.roles as roles_mod
        from hivepilot.cli import app

        monkeypatch.setattr(roles_mod, "load_roles", lambda *a, **k: roster)
        return CliRunner().invoke(app, ["herdr", "board", *args])

    def test_no_flag_builds_a_pane_per_configured_role(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = self._invoke(monkeypatch, {"ceo": object(), "cto": object(), "qa": object()})

        assert result.exit_code == 0, result.output
        assert [p["label"] for p in _panes(json.loads(result.output))] == ["ceo", "cto", "qa"]

    def test_file_order_is_preserved_not_sorted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """roles.yaml order is the operator's own ordering -- usually the
        order the pipeline runs in. Sorting would discard that for nothing."""
        result = self._invoke(monkeypatch, {"zulu": object(), "alpha": object()})

        assert [p["label"] for p in _panes(json.loads(result.output))] == ["zulu", "alpha"]

    def test_every_configured_role_appears_even_if_it_has_never_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Deliberately NOT filtered to roles with run history.

        Only 13 of this deployment's 20 roles have ever been dispatched. A
        role missing from the board because it never ran is indistinguishable
        from a role that does not exist -- a silent omission, which is the
        failure mode a standing board exists to remove.
        """
        result = self._invoke(monkeypatch, {f"role_{i}": object() for i in range(20)})

        assert len(_panes(json.loads(result.output))) == 20

    def test_an_explicit_flag_still_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        result = self._invoke(monkeypatch, {"ceo": object(), "cto": object()}, "--roles", "qa")

        assert [p["label"] for p in _panes(json.loads(result.output))] == ["qa"]

    def test_an_empty_roster_says_so_instead_of_emitting_an_empty_board(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An empty layout would apply cleanly and show nothing -- the same
        silent-plausible failure the module docstring warns about for
        command-less panes."""
        result = self._invoke(monkeypatch, {})

        assert result.exit_code != 0
        assert "no roles configured" in result.output
