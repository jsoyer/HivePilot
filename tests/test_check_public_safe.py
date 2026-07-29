"""
Tests for scripts/check_public_safe.py — the public-safe content guard.

`scripts/` is not an importable package (no __init__.py), so we load the
module directly from its file path via importlib. This lets us exercise the
internal functions (pattern loading, scanning) as well as the CLI entry
point (`main`) without needing a subprocess for every test.

Fixtures use a SYNTHETIC marker (`ACME-INTERNAL-DOC`) rather than a real
denylist entry. These tests must prove the MECHANISM catches a planted
marker; pinning them to a real forbidden string would both re-print that
string in the repo and make the tests fail whenever the denylist is edited.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "check_public_safe.py"
_spec = importlib.util.spec_from_file_location("check_public_safe", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
check_public_safe = importlib.util.module_from_spec(_spec)
sys.modules["check_public_safe"] = check_public_safe
_spec.loader.exec_module(check_public_safe)

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Stand-in for "a customer-specific marker". Not a real denylist entry.
MARKER = "ACME-INTERNAL-DOC"


def _patterns(*raw: str) -> list:
    return check_public_safe.compile_patterns(list(raw))


@pytest.fixture
def tmp_denylist(tmp_path: Path) -> Path:
    denylist = tmp_path / "denylist.txt"
    denylist.write_text(
        "\n".join(
            [
                "# a comment line",
                "",
                MARKER,
                "widget-cli",
            ]
        )
        + "\n"
    )
    return denylist


# ---------------------------------------------------------------------------
# Denylist parsing
# ---------------------------------------------------------------------------


def test_parse_denylist_file_skips_comments_and_blanks(tmp_denylist: Path) -> None:
    entries = check_public_safe.parse_denylist_file(tmp_denylist)
    assert entries == [
        (check_public_safe.SCOPE_ANY, MARKER),
        (check_public_safe.SCOPE_ANY, "widget-cli"),
    ]


def test_parse_denylist_file_missing_returns_empty(tmp_path: Path) -> None:
    assert check_public_safe.parse_denylist_file(tmp_path / "does-not-exist.txt") == []


def test_parse_denylist_file_strips_trailing_inline_comment(tmp_path: Path) -> None:
    """REGRESSION: the parser used to strip only WHOLE-line `#` comments.

    An entry written as `MARKER          # why it is forbidden` therefore
    compiled into a regex demanding the spaces and the comment text as well,
    so it matched nothing anywhere — the guard's most important pattern was
    silently dead while the check reported "passed". Against the unfixed
    parser this test gets the whole line back including the comment.
    """
    denylist = tmp_path / "denylist.txt"
    denylist.write_text(f"{MARKER}          # internal doc name\n")

    entries = check_public_safe.parse_denylist_file(denylist)

    assert entries == [(check_public_safe.SCOPE_ANY, MARKER)]


def test_inline_comment_stripping_keeps_a_hash_that_is_part_of_the_regex(
    tmp_path: Path,
) -> None:
    """Only whitespace-then-`#` starts a comment, so `issue#\\d+` survives."""
    denylist = tmp_path / "denylist.txt"
    denylist.write_text("issue#[0-9]+\n")
    assert check_public_safe.parse_denylist_file(denylist) == [
        (check_public_safe.SCOPE_ANY, "issue#[0-9]+")
    ]


def test_dead_inline_comment_pattern_would_not_have_matched(tmp_path: Path) -> None:
    """Nails WHY the parser bug mattered: the un-stripped pattern is inert.

    Compiling the raw line (what the old parser produced) against a realistic
    source line finds nothing; compiling the stripped pattern finds the marker.
    """
    raw_line = f"{MARKER}          # internal doc name"
    source_line = f'DOC = f"{{root}}/{MARKER}.md"'

    assert not _patterns(raw_line)[0].regex.search(source_line)
    assert _patterns(check_public_safe.strip_inline_comment(raw_line).strip())[0].regex.search(
        source_line
    )


def test_parse_denylist_file_rejects_an_unknown_scope(tmp_path: Path) -> None:
    """An unknown `[scope]` must raise, not silently degrade to 'no scope'."""
    denylist = tmp_path / "denylist.txt"
    denylist.write_text(f"[nonsense]\n{MARKER}\n")

    with pytest.raises(check_public_safe.DenylistError):
        check_public_safe.parse_denylist_file(denylist)


def test_compile_patterns_raises_on_an_invalid_regex() -> None:
    """An uncompilable pattern must be fatal, never skipped: a dropped pattern
    leaves a maintainer believing a marker is guarded when it is not."""
    with pytest.raises(check_public_safe.DenylistError):
        check_public_safe.compile_patterns(["unclosed ( group"])


def test_compile_patterns_case_sensitive() -> None:
    compiled = _patterns("widget-cli")
    assert len(compiled) == 1
    assert compiled[0].regex.search("we used widget-cli here")
    # Case-sensitive: structural patterns (e.g. {OBSIDIAN_VAULT}/[A-Z]...) must not
    # match generic lowercase segments, so compile_patterns does NOT fold case.
    assert not compiled[0].regex.search("we used WIDGET-CLI here")


def test_load_all_patterns_merges_extra_env(
    tmp_path: Path, tmp_denylist: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    extra = tmp_path / "extra.txt"
    extra.write_text("Real Person Name\n")
    monkeypatch.setenv("PUBLIC_DENYLIST_EXTRA", str(extra))

    patterns = check_public_safe.load_all_patterns(tmp_denylist)

    assert len(patterns) == 3
    assert any(p.regex.search("Real Person Name") for p in patterns)


def test_private_extra_patterns_are_always_scope_any(
    tmp_path: Path, tmp_denylist: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A private pattern exists because the string must not appear ANYWHERE.

    Even if the private file carries a narrowing `[prompts]` header, it must
    not silently reduce that pattern's reach — that would be a fail-open in
    the one file the maintainer cannot review in a public diff.
    """
    extra = tmp_path / "extra.txt"
    extra.write_text("[prompts]\nReal Person Name\n")
    monkeypatch.setenv("PUBLIC_DENYLIST_EXTRA", str(extra))

    patterns = check_public_safe.load_all_patterns(tmp_denylist)
    private = [p for p in patterns if p.regex.pattern == "Real Person Name"]

    assert len(private) == 1
    assert private[0].scope == check_public_safe.SCOPE_ANY


def test_load_all_patterns_ignores_missing_extra_env(
    tmp_denylist: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PUBLIC_DENYLIST_EXTRA", str(tmp_path / "nope.txt"))
    assert len(check_public_safe.load_all_patterns(tmp_denylist)) == 2


def test_load_all_patterns_both_absent_returns_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PUBLIC_DENYLIST_EXTRA", raising=False)
    assert check_public_safe.load_all_patterns(tmp_path / "missing.txt") == []


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------


def test_scan_file_finds_matches(tmp_path: Path) -> None:
    target = tmp_path / "leak.md"
    target.write_text(f"line one\nsee {MARKER} and widget-cli here\nclean line\n")

    findings = check_public_safe.scan_file(target, _patterns(MARKER, "widget-cli"))

    assert len(findings) == 2
    assert {f.lineno for f in findings} == {2}


def test_scan_file_no_matches(tmp_path: Path) -> None:
    target = tmp_path / "clean.md"
    target.write_text("nothing to see here\n")
    assert check_public_safe.scan_file(target, _patterns(MARKER)) == []


def test_prompts_scoped_pattern_does_not_apply_outside_prompts(tmp_path: Path) -> None:
    """A `[prompts]`-scoped pattern must not fire on engine code.

    `rtk proxy` in a shipped prompt tells somebody else's agent to run the
    maintainer's CLI wrapper; the same string in `plugins/rtk.py` is the
    first-party runner that implements it.
    """
    outside = tmp_path / "runner.py"
    outside.write_text("# wraps the command with widget-cli\n")
    scoped = check_public_safe.compile_patterns([(check_public_safe.SCOPE_PROMPTS, "widget-cli")])

    assert check_public_safe.scan_file(outside, scoped) == []


def test_prompts_scoped_pattern_applies_inside_prompts() -> None:
    """The other half: inside `prompts/` the scoped pattern is live."""
    scoped = check_public_safe.compile_patterns([(check_public_safe.SCOPE_PROMPTS, "widget-cli")])
    prompt_file = REPO_ROOT / "prompts" / "agents" / "developer.md"

    assert prompt_file.is_file(), "fixture assumes a real shipped prompt exists"
    assert scoped[0].applies_to(prompt_file)


def test_any_scoped_pattern_applies_everywhere(tmp_path: Path) -> None:
    anywhere = check_public_safe.compile_patterns([(check_public_safe.SCOPE_ANY, MARKER)])
    assert anywhere[0].applies_to(tmp_path / "anything.py")


# ---------------------------------------------------------------------------
# CLI behaviour
# ---------------------------------------------------------------------------


@pytest.fixture
def denylist_with_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    denylist = tmp_path / "denylist.txt"
    denylist.write_text(f"{MARKER}\n")
    monkeypatch.setattr(check_public_safe, "DEFAULT_DENYLIST_PATH", denylist)
    monkeypatch.delenv("PUBLIC_DENYLIST_EXTRA", raising=False)
    return denylist


def test_main_exits_zero_on_clean_files(tmp_path: Path, denylist_with_marker: Path) -> None:
    clean = tmp_path / "clean.md"
    clean.write_text("nothing sensitive here\n")

    assert check_public_safe.main([str(clean)]) == 0


def test_main_exits_one_on_forbidden_content(
    tmp_path: Path, denylist_with_marker: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    leaky = tmp_path / "leak.md"
    leaky.write_text(f"see {MARKER} here\n")

    exit_code = check_public_safe.main([str(leaky)])
    out = capsys.readouterr().out

    assert exit_code == 1
    assert "Public-safe check FAILED" in out
    assert MARKER in out


def test_main_catches_a_planted_marker_in_engine_code(
    tmp_path: Path, denylist_with_marker: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The point of widening the scan: a customer-specific marker in a PYTHON
    module must fail the check.

    Before this change the default glob set was `prompts/agents/*.md` only, so
    a marker baked into a module constant — which is exactly how one customer's
    document names survived in `hivepilot/agent_rules.py` — was invisible to
    the guard. The fixture plants the marker rather than relying on the real
    tree, so the test keeps meaning after the real leak is fixed.
    """
    engine_dir = tmp_path / "hivepilot"
    engine_dir.mkdir()
    module = engine_dir / "leaky_module.py"
    module.write_text(f'VAULT_DOC = f"{{root}}/{MARKER}.md"\n')

    exit_code = check_public_safe.main([str(engine_dir / "*.py")])
    out = capsys.readouterr().out

    assert exit_code == 1
    assert "leaky_module.py" in out
    assert MARKER in out


def test_default_globs_cover_engine_code_and_prompts() -> None:
    """The declared scope must actually include engine code, not just prompts.

    Guards the intent rather than a file count: `hivepilot/**/*.py` being in
    DEFAULT_GLOBS is the whole deliverable, and the previous value
    (`["prompts/agents/*.md"]`) fails this outright.
    """
    globs = check_public_safe.DEFAULT_GLOBS

    assert "hivepilot/**/*.py" in globs
    assert any(g.startswith("prompts/") for g in globs)
    assert not any(g.startswith("tests/") for g in globs), (
        "tests/ is excluded by design; see the module docstring"
    )


def test_default_globs_scan_substantially_more_than_the_old_prompts_only_scope() -> None:
    """The old scope was 9 files. Anything near that means the widening broke."""
    files, empty = check_public_safe.expand_globs(check_public_safe.DEFAULT_GLOBS)

    assert not empty, f"DEFAULT_GLOBS entries matched no files: {empty}"
    assert len(files) > 100, f"expected the widened scan to cover the engine, got {len(files)}"
    assert any(f.name == "agent_rules.py" for f in files), (
        "agent_rules.py — the module this widening exists for — must be scanned"
    )


# ---------------------------------------------------------------------------
# Fail-closed: scanning nothing is an ERROR, never a pass
# ---------------------------------------------------------------------------


def test_main_errors_when_no_denylist_at_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """CHANGED BEHAVIOUR (was: warn + exit 0).

    A guard with zero patterns protects nothing. Reporting that as a pass is
    the same defect the rest of this change exists to close: an empty/absent
    value read as "no constraint". The old assertion encoded the fail-open, so
    it is replaced deliberately rather than relaxed.
    """
    clean = tmp_path / "clean.md"
    clean.write_text("hello\n")
    monkeypatch.setattr(check_public_safe, "DEFAULT_DENYLIST_PATH", tmp_path / "missing.txt")
    monkeypatch.delenv("PUBLIC_DENYLIST_EXTRA", raising=False)

    exit_code = check_public_safe.main([str(clean)])
    out = capsys.readouterr().out

    assert exit_code == 2
    assert "ERROR" in out
    assert "protects nothing" in out


def test_main_errors_when_a_glob_matches_no_files(
    tmp_path: Path, denylist_with_marker: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A renamed/removed directory must not report "passed" for scanning nothing.

    Same failure as `plugins audit` printing "No plugin source files found to
    audit" on a host with six plugins.
    """
    exit_code = check_public_safe.main([str(tmp_path / "no-such-dir" / "*.py")])
    out = capsys.readouterr().out

    assert exit_code == 2
    assert "matched no files" in out


def test_main_errors_when_one_of_several_globs_matches_no_files(
    tmp_path: Path, denylist_with_marker: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Partial coverage is still silent coverage loss: one dead glob among
    several must fail, not be masked by the globs that still match."""
    clean = tmp_path / "clean.md"
    clean.write_text("fine\n")

    exit_code = check_public_safe.main([str(clean), str(tmp_path / "gone" / "*.py")])
    out = capsys.readouterr().out

    assert exit_code == 2
    assert "gone" in out


def test_main_errors_on_an_unusable_denylist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    denylist = tmp_path / "denylist.txt"
    denylist.write_text("unclosed ( group\n")
    monkeypatch.setattr(check_public_safe, "DEFAULT_DENYLIST_PATH", denylist)
    monkeypatch.delenv("PUBLIC_DENYLIST_EXTRA", raising=False)
    clean = tmp_path / "clean.md"
    clean.write_text("hello\n")

    exit_code = check_public_safe.main([str(clean)])
    out = capsys.readouterr().out

    assert exit_code == 2
    assert "unusable denylist" in out


def test_denylist_never_scans_itself(denylist_with_marker: Path) -> None:
    """The committed denylist enumerates forbidden strings by definition, so
    scanning it would always "find" every one of them."""
    files, _ = check_public_safe.expand_globs(check_public_safe.DEFAULT_GLOBS)
    assert check_public_safe.DEFAULT_DENYLIST_PATH.resolve() not in {f.resolve() for f in files}


# ---------------------------------------------------------------------------
# The real repository must be clean under the real, widened configuration
# ---------------------------------------------------------------------------


def test_repository_is_public_safe_under_the_widened_scan() -> None:
    """End-to-end: the shipped denylist against the shipped glob set.

    NOT a "fails on unfixed main" test, deliberately — on unfixed `main` this
    passes vacuously, because the guard there scanned 9 prompt files with its
    most important pattern silently dead. That is precisely the state this
    change ends: from here on, a customer-specific string committed anywhere in
    the engine turns this green assertion red.
    """
    assert check_public_safe.main([]) == 0
