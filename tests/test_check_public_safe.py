"""
Tests for scripts/check_public_safe.py — the public-safe content guard.

`scripts/` is not an importable package (no __init__.py), so we load the
module directly from its file path via importlib. This lets us exercise the
internal functions (pattern loading, scanning) as well as the CLI entry
point (`main`) without needing a subprocess for every test.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "check_public_safe.py"
_spec = importlib.util.spec_from_file_location("check_public_safe", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
check_public_safe = importlib.util.module_from_spec(_spec)
sys.modules["check_public_safe"] = check_public_safe
_spec.loader.exec_module(check_public_safe)


@pytest.fixture
def tmp_denylist(tmp_path: Path) -> Path:
    denylist = tmp_path / "denylist.txt"
    denylist.write_text(
        "\n".join(
            [
                "# a comment line",
                "",
                "AGENT-[A-Z][A-Z-]+",
                "rtk proxy",
            ]
        )
        + "\n"
    )
    return denylist


def test_parse_denylist_file_skips_comments_and_blanks(tmp_denylist: Path) -> None:
    patterns = check_public_safe.parse_denylist_file(tmp_denylist)
    assert patterns == ["AGENT-[A-Z][A-Z-]+", "rtk proxy"]


def test_parse_denylist_file_missing_returns_empty(tmp_path: Path) -> None:
    patterns = check_public_safe.parse_denylist_file(tmp_path / "does-not-exist.txt")
    assert patterns == []


def test_compile_patterns_case_sensitive() -> None:
    compiled = check_public_safe.compile_patterns(["rtk proxy"])
    assert len(compiled) == 1
    assert compiled[0].search("we used rtk proxy here")
    # Case-sensitive: structural patterns (e.g. {OBSIDIAN_VAULT}/[A-Z]...) must not
    # match generic lowercase segments, so compile_patterns does NOT fold case.
    assert not compiled[0].search("we used RTK PROXY here")


def test_load_all_patterns_merges_extra_env(
    tmp_path: Path, tmp_denylist: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    extra = tmp_path / "extra.txt"
    extra.write_text("Real Person Name\n")
    monkeypatch.setenv("PUBLIC_DENYLIST_EXTRA", str(extra))

    patterns = check_public_safe.load_all_patterns(tmp_denylist)

    assert len(patterns) == 3
    assert any(p.search("Real Person Name") for p in patterns)


def test_load_all_patterns_ignores_missing_extra_env(
    tmp_denylist: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PUBLIC_DENYLIST_EXTRA", str(tmp_path / "nope.txt"))
    patterns = check_public_safe.load_all_patterns(tmp_denylist)
    assert len(patterns) == 2


def test_load_all_patterns_both_absent_returns_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PUBLIC_DENYLIST_EXTRA", raising=False)
    patterns = check_public_safe.load_all_patterns(tmp_path / "missing.txt")
    assert patterns == []


def test_scan_file_finds_matches(tmp_path: Path) -> None:
    target = tmp_path / "leak.md"
    target.write_text("line one\nsee AGENT-DETECTION-FABRIC and rtk proxy obs\nclean line\n")
    patterns = [
        re.compile(r"AGENT-[A-Z][A-Z-]+", re.IGNORECASE),
        re.compile(r"rtk proxy", re.IGNORECASE),
    ]

    findings = check_public_safe.scan_file(target, patterns)

    assert len(findings) == 2
    linenos = {f.lineno for f in findings}
    assert linenos == {2}


def test_scan_file_no_matches(tmp_path: Path) -> None:
    target = tmp_path / "clean.md"
    target.write_text("nothing to see here\n")
    patterns = [re.compile(r"AGENT-[A-Z][A-Z-]+", re.IGNORECASE)]

    findings = check_public_safe.scan_file(target, patterns)

    assert findings == []


def test_main_exits_zero_on_clean_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clean = tmp_path / "clean.md"
    clean.write_text("nothing sensitive here\n")
    denylist = tmp_path / "denylist.txt"
    denylist.write_text("AGENT-[A-Z][A-Z-]+\n")
    monkeypatch.setattr(check_public_safe, "DEFAULT_DENYLIST_PATH", denylist)
    monkeypatch.delenv("PUBLIC_DENYLIST_EXTRA", raising=False)

    exit_code = check_public_safe.main([str(clean)])

    assert exit_code == 0


def test_main_exits_one_on_forbidden_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    leaky = tmp_path / "leak.md"
    leaky.write_text("see AGENT-DETECTION-FABRIC and rtk proxy obs\n")
    denylist = tmp_path / "denylist.txt"
    denylist.write_text("AGENT-[A-Z][A-Z-]+\nrtk proxy\n")
    monkeypatch.setattr(check_public_safe, "DEFAULT_DENYLIST_PATH", denylist)
    monkeypatch.delenv("PUBLIC_DENYLIST_EXTRA", raising=False)

    exit_code = check_public_safe.main([str(leaky)])
    out = capsys.readouterr().out

    assert exit_code == 1
    assert "Public-safe check FAILED" in out
    assert "AGENT-DETECTION-FABRIC" in out


def test_main_warns_and_exits_zero_when_no_denylist_at_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    clean = tmp_path / "clean.md"
    clean.write_text("hello\n")
    monkeypatch.setattr(check_public_safe, "DEFAULT_DENYLIST_PATH", tmp_path / "missing.txt")
    monkeypatch.delenv("PUBLIC_DENYLIST_EXTRA", raising=False)

    exit_code = check_public_safe.main([str(clean)])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "warning" in out.lower()
