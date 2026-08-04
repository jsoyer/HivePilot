"""Every synthesized RunnerDefinition must carry its role's options.

`allowed_tools` is a **whitelist**. Its absence is therefore not "no grant"
but "no limit" — a dispatch built without options hands the agent an
unrestricted shell.

Measured on 2026-08-04. The review dispatch built its definition with
`name`, `kind`, `command`, `model`, `effort`, `host` and no `options=`. The
reviewer then ran 23 bare Bash commands — `ls -la /`, `find / -maxdepth 6`,
`cat …` — across the whole filesystem, and burned **1 099 726 cache tokens**
reviewing a 3.8 KB diff. It never once ran `gh pr diff`, because nothing
pointed it at the PR and nothing stopped it walking the disk.

Fourth occurrence of one pattern: a hand-built `RunnerDefinition` silently
dropping declared configuration. See `iac-cli-ignores-project-options` and
HivePilot #402/#403 for the previous three.
"""

from __future__ import annotations

from pathlib import Path

import hivepilot.orchestrator as orch


class TestRoleRunnerOptions:
    def test_a_role_with_a_permission_mode_carries_it(self) -> None:
        # `developer` is the one default role declaring a permission_mode.
        assert orch._role_runner_options("developer").get("permission_mode") == "bypassPermissions"

    def test_a_role_with_no_grants_yields_no_options(self) -> None:
        """Absent is absent — never a fabricated empty allowlist.

        An empty `allowed_tools` list would be a whitelist matching nothing,
        which would block every command rather than leave the role as it was.
        """
        options = orch._role_runner_options("reviewer")

        assert "allowed_tools" not in options or options["allowed_tools"]

    def test_an_unknown_role_degrades_rather_than_raising(self) -> None:
        """A dispatch must not die because a role name went stale.

        Returning no options leaves behaviour exactly as it was before this
        function existed, which is the safe direction for a lookup failure.
        """
        assert orch._role_runner_options("not-a-role") == {}


class TestEveryDispatchSiteThreadsThem:
    """Pinned by reading the source: reproducing each site needs a full
    dispatch, and the failure mode is silent — a definition without options
    works fine, it just leaves the agent unbounded."""

    # The windows below are generous on purpose: these constructions carry
    # multi-line comments explaining WHY the options are there, and a tight
    # slice would fail on the explanation rather than on the code.
    @staticmethod
    def _source() -> str:
        return Path(orch.__file__).read_text(encoding="utf-8")

    def test_the_review_dispatch_threads_them(self) -> None:
        # Anchored on `RunnerDefinition(` rather than the name alone:
        # `name=f"review:{role_name}"` also appears on the TaskStep built
        # just above, and matching that one tested nothing.
        source = self._source()
        block = next(
            b
            for b in source.split("RunnerDefinition(")[1:]
            if 'name=f"review:{role_name}"' in b[:120]
        )[:1200]

        assert "options=" in block, (
            "a reviewer without options gets an unrestricted shell — measured "
            "at 23 bare commands and 1.1M cache tokens on a 3.8 KB diff"
        )

    def test_the_debate_dispatch_threads_them(self) -> None:
        source = self._source()
        marker = 'name=f"debate:{role_name}:{brain_model}"'
        block = source[source.index(marker) : source.index(marker) + 1200]

        assert "options=" in block

    def test_no_synthesized_definition_is_left_without_options(self) -> None:
        """A guard against the fifth occurrence.

        Every `RunnerDefinition(` built in this module must name `options`,
        even to pass `{}` deliberately — the one legitimate case is the
        fallback where no role exists at all, and stating it explicitly is
        what distinguishes it from an oversight.
        """
        source = self._source()
        blocks = source.split("RunnerDefinition(")[1:]
        missing = [b[:120].replace("\n", " ") for b in blocks if "options=" not in b[:1200]]

        assert not missing, f"RunnerDefinition built without options: {missing}"
