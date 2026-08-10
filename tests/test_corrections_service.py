"""Corrections: standing operator instructions, per role, read verbatim.

The lessons table already exists and is deliberately NOT this. Lessons are
distilled by an LLM, scored, and injected top-N by rank -- right for durable
cross-run learning, wrong for "stop doing X, I am telling you". Making an
agent stop doing something today currently costs a config PR, a merge, a
sync and a restart; this costs editing a file.

The engine's whole contribution is: "for role R, read `corrections/R.md`".
No role name appears anywhere in it, which is what keeps the engine generic
while the content stays tenant-owned in the config repo.

The tests that matter here are the negative ones. A corrections file that
silently fails to load is worse than no feature at all -- the operator writes
the instruction, believes it is in force, and the agent carries on. So:
absence is silent, a read error is loud, and oversized content is cut with
the cut DECLARED in the text the agent sees.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hivepilot.services import corrections_service as cs


@pytest.fixture
def corrections_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the config chain at a temp `prompts/corrections/`."""
    root = tmp_path / "prompts" / "corrections"
    root.mkdir(parents=True)
    monkeypatch.setattr(cs.settings, "base_dir", tmp_path)
    monkeypatch.setattr(cs.settings, "config_repo", None, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    return root


class TestNothingToSayIsSilent:
    def test_no_file_yields_no_text(self, corrections_dir: Path) -> None:
        """The overwhelmingly common case. It must add nothing at all -- not
        an empty header, which would cost tokens on every call forever."""
        assert cs.load_corrections("developer") == ""

    def test_an_empty_file_yields_no_text(self, corrections_dir: Path) -> None:
        (corrections_dir / "developer.md").write_text("   \n\n")

        assert cs.load_corrections("developer") == ""

    def test_no_role_yields_no_text(self, corrections_dir: Path) -> None:
        (corrections_dir / "_all.md").write_text("applies to everyone")

        assert cs.load_corrections(None) == ""
        assert cs.load_corrections("") == ""


class TestItReachesTheRoleThatAskedForIt:
    def test_the_role_file_is_read_verbatim(self, corrections_dir: Path) -> None:
        """Verbatim, not ranked or summarised. That is the entire difference
        from the lessons table."""
        (corrections_dir / "developer.md").write_text("Never force-push to main.")

        out = cs.load_corrections("developer")

        assert "Never force-push to main." in out

    def test_another_roles_file_is_not_read(self, corrections_dir: Path) -> None:
        """Per role, not one global file: a correction for the developer is
        noise in the CISO's prompt, paid for on every single call."""
        (corrections_dir / "ciso.md").write_text("CISO-only instruction.")

        assert cs.load_corrections("developer") == ""

    def test_all_applies_to_every_role(self, corrections_dir: Path) -> None:
        (corrections_dir / "_all.md").write_text("Answer in French.")

        assert "Answer in French." in cs.load_corrections("developer")
        assert "Answer in French." in cs.load_corrections("ciso")

    def test_both_files_are_included_with_all_first(self, corrections_dir: Path) -> None:
        """Cross-role first, role-specific second, so the specific one is
        nearest the task and wins a conflict by recency."""
        (corrections_dir / "_all.md").write_text("EVERYONE")
        (corrections_dir / "developer.md").write_text("DEVELOPER")

        out = cs.load_corrections("developer")

        assert out.index("EVERYONE") < out.index("DEVELOPER")

    def test_the_text_is_labelled_so_the_agent_knows_what_it_is(
        self, corrections_dir: Path
    ) -> None:
        """Unlabelled text dropped into a prompt reads as part of the task."""
        (corrections_dir / "developer.md").write_text("Never force-push.")

        assert "correction" in cs.load_corrections("developer").lower()


class TestItCannotBeUsedToReadArbitraryFiles:
    @pytest.mark.parametrize(
        "role", ["../../etc/passwd", "..", "a/b", "dev*", "dev eloper", ".hidden"]
    )
    def test_a_role_key_that_is_not_a_plain_identifier_is_refused(
        self, corrections_dir: Path, role: str
    ) -> None:
        """The role key becomes a filename. It comes from config today, but a
        path-traversing key would turn this into an arbitrary file read, and
        the agent would receive the contents as instructions."""
        assert cs.load_corrections(role) == ""


class TestOversizeIsCutAndSaysSo:
    def test_a_huge_file_is_bounded(self, corrections_dir: Path) -> None:
        """An unbounded operator-editable file is one paste away from the
        128 KB single-argv limit that has already broken a runner here."""
        (corrections_dir / "developer.md").write_text("x" * (cs._MAX_CORRECTION_CHARS * 3))

        out = cs.load_corrections("developer")

        assert len(out) < cs._MAX_CORRECTION_CHARS * 2

    def test_the_cut_is_declared_in_the_text_the_agent_sees(self, corrections_dir: Path) -> None:
        """A silent truncation makes the agent confidently act on half an
        instruction, with nothing anywhere saying the other half existed."""
        (corrections_dir / "developer.md").write_text("y" * (cs._MAX_CORRECTION_CHARS * 3))

        assert "truncated" in cs.load_corrections("developer").lower()

    def test_content_within_the_budget_is_untouched(self, corrections_dir: Path) -> None:
        body = "z" * 100
        (corrections_dir / "developer.md").write_text(body)

        assert body in cs.load_corrections("developer")


class TestAFailedReadIsLoud:
    def test_an_unreadable_file_does_not_break_the_run(
        self, corrections_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """It must never break a run -- but it must not pass for "no
        corrections" either, which is why the warning below is asserted."""
        (corrections_dir / "developer.md").write_text("something")

        def boom(*_a: object, **_k: object) -> str:
            raise OSError("permission denied")

        monkeypatch.setattr(Path, "read_text", boom)

        assert cs.load_corrections("developer") == ""

    def test_an_unreadable_file_is_logged(
        self,
        corrections_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        (corrections_dir / "developer.md").write_text("something")

        def boom(*_a: object, **_k: object) -> str:
            raise OSError("permission denied")

        monkeypatch.setattr(Path, "read_text", boom)

        with caplog.at_level("WARNING"):
            cs.load_corrections("developer")

        assert "corrections" in caplog.text


class TestTheRoleReachesTheRunnerUnconditionally:
    """`metadata["role"]` was set only when lesson distillation was enabled.

    That was correct when retrieval was its only consumer. Corrections became
    a second one, so turning distillation off silently disabled corrections
    too — two unrelated features coupled through one dict key, failing in the
    quiet direction.

    Found by a real pipeline run: zero `corrections.applied` in a run whose
    launch environment happened to omit the distillation flag, while a direct
    call to `load_corrections()` resolved 937 characters. The unit tests could
    not catch it because they pass `metadata={"role": …}` themselves.
    """

    def test_the_role_is_set_even_with_distillation_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hivepilot import orchestrator as orch

        seen: dict[str, object] = {}

        class _Cfg:
            enable_distillation = False

        monkeypatch.setattr(orch, "resolve_lessons_config", lambda **k: _Cfg())
        # The assertion is on the helper the orchestrator uses to decide, not
        # on a full pipeline run: the coupling lived in one branch.
        assert orch._role_metadata_value(_Cfg(), "developer") == "developer"
        assert seen == {}

    def test_a_roleless_task_still_yields_none(self) -> None:
        from hivepilot import orchestrator as orch

        class _Cfg:
            enable_distillation = True

        assert orch._role_metadata_value(_Cfg(), None) is None
