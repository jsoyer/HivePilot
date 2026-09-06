"""HP-79: skill usage + workshop (propose / accept / reject, never auto-apply)."""

from __future__ import annotations

from pathlib import Path

import pytest

from hivepilot.services import skill_workshop_service as sws
from hivepilot.skill_dirs import SKILL_MANIFEST


class _Lookup:
    def __init__(self, specs: dict[str, dict]) -> None:
        self.specs = specs

    def get_skill(self, name: str) -> dict | None:
        return self.specs.get(name)


def _dir_skill(tmp_path: Path, name: str = "demo", body: str = "# Demo\n\nDo the thing.\n") -> Path:
    root = tmp_path / "skills" / name
    root.mkdir(parents=True)
    (root / SKILL_MANIFEST).write_text(body, encoding="utf-8")
    return root


def test_record_usage_and_list(tmp_path: Path):
    sws.record_usage(["demo"], run_id=3, step="impl", runner_kind="claude")
    rows = sws.list_usage(skill_name="demo")
    assert len(rows) == 1
    assert rows[0]["step"] == "impl"
    assert rows[0]["outcome"] == "applied"


def test_propose_patch_never_writes_disk(tmp_path: Path):
    root = _dir_skill(tmp_path)
    original = (root / SKILL_MANIFEST).read_text(encoding="utf-8")
    lookup = _Lookup(
        {
            "demo": {
                "name": "demo",
                "provider": f"directory:{root.resolve()}",
                "files": {SKILL_MANIFEST: original},
            }
        }
    )
    row = sws.propose_patch(
        "demo",
        {SKILL_MANIFEST: original + "\nBe concise.\n"},
        lookup=lookup,
        rationale="tighten the prompt",
    )
    assert row["status"] == "proposed"
    assert "Be concise" in row["diff_text"]
    assert (root / SKILL_MANIFEST).read_text(encoding="utf-8") == original


def test_second_propose_returns_pending(tmp_path: Path):
    root = _dir_skill(tmp_path)
    files = {SKILL_MANIFEST: (root / SKILL_MANIFEST).read_text(encoding="utf-8")}
    lookup = _Lookup(
        {"demo": {"name": "demo", "provider": f"directory:{root.resolve()}", "files": files}}
    )
    first = sws.propose_patch(
        "demo", {SKILL_MANIFEST: files[SKILL_MANIFEST] + "\nA\n"}, lookup=lookup, rationale="a"
    )
    second = sws.propose_patch(
        "demo", {SKILL_MANIFEST: files[SKILL_MANIFEST] + "\nB\n"}, lookup=lookup, rationale="b"
    )
    assert first["id"] == second["id"]


def test_reject_leaves_files_untouched(tmp_path: Path):
    root = _dir_skill(tmp_path)
    original = (root / SKILL_MANIFEST).read_text(encoding="utf-8")
    lookup = _Lookup(
        {
            "demo": {
                "name": "demo",
                "provider": f"directory:{root.resolve()}",
                "files": {SKILL_MANIFEST: original},
            }
        }
    )
    row = sws.propose_patch(
        "demo", {SKILL_MANIFEST: original + "\nX\n"}, lookup=lookup, rationale="x"
    )
    decided = sws.decide_proposal(
        row["id"], accept=False, actor="jerome", lookup=lookup, tenant="default"
    )
    assert decided["status"] == "rejected"
    assert (root / SKILL_MANIFEST).read_text(encoding="utf-8") == original


def test_accept_writes_directory_skill(tmp_path: Path, monkeypatch):
    root = _dir_skill(tmp_path)
    original = (root / SKILL_MANIFEST).read_text(encoding="utf-8")
    lookup = _Lookup(
        {
            "demo": {
                "name": "demo",
                "provider": f"directory:{root.resolve()}",
                "files": {SKILL_MANIFEST: original},
            }
        }
    )
    monkeypatch.setattr(sws, "skill_scan_dirs", lambda: [tmp_path / "skills"])
    row = sws.propose_patch(
        "demo",
        {SKILL_MANIFEST: original + "\nAlways run tests.\n"},
        lookup=lookup,
        rationale="tdd",
    )
    decided = sws.decide_proposal(
        row["id"], accept=True, actor="jerome", lookup=lookup, tenant="default"
    )
    assert decided["status"] == "accepted"
    assert "Always run tests" in (root / SKILL_MANIFEST).read_text(encoding="utf-8")


def test_accept_refuses_plugin_skill(tmp_path: Path):
    lookup = _Lookup(
        {
            "improve": {
                "name": "improve",
                "provider": "plugin:improve",
                "files": {SKILL_MANIFEST: "# Improve\n"},
            }
        }
    )
    row = sws.propose_patch(
        "improve",
        {SKILL_MANIFEST: "# Improve\n\nMore.\n"},
        lookup=lookup,
        rationale="note",
    )
    with pytest.raises(sws.SkillWorkshopError, match="directory"):
        sws.decide_proposal(row["id"], accept=True, actor="x", lookup=lookup, tenant="default")


def test_unsafe_path_rejected():
    with pytest.raises(sws.SkillWorkshopError, match="unsafe"):
        sws.merge_patch({SKILL_MANIFEST: "x"}, {"../etc/passwd": "no"})


def test_propose_from_failure_appends_note(tmp_path: Path):
    root = _dir_skill(tmp_path)
    original = (root / SKILL_MANIFEST).read_text(encoding="utf-8")
    lookup = _Lookup(
        {
            "demo": {
                "name": "demo",
                "provider": f"directory:{root.resolve()}",
                "files": {SKILL_MANIFEST: original},
            }
        }
    )
    created = sws.propose_from_failure(
        ["demo"],
        lookup=lookup,
        detail="claude exited 1: tests failed",
        run_id=9,
        step="impl",
    )
    assert len(created) == 1
    assert "Observed failure" in created[0]["diff_text"]
    assert (root / SKILL_MANIFEST).read_text(encoding="utf-8") == original
