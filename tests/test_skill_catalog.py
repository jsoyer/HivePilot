"""HP-98: skill catalog + revision DAG (read-only scan)."""

from __future__ import annotations

from pathlib import Path

import yaml

from hivepilot.skill_catalog import (
    ORIGIN_VALUES,
    SKILL_ID_SIDECAR,
    SkillCatalog,
    SkillCatalogError,
    SkillOrigin,
    logical_skill_id,
    scan_catalog,
    snapshot_hash,
)


def _write_skill_dir(
    skills_root: Path,
    name: str,
    *,
    body: str = "# Skill\n",
    extra_files: dict[str, str] | None = None,
) -> Path:
    skill_dir = skills_root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    frontmatter = {"description": f"{name} skill"}
    text = "---\n" + yaml.safe_dump(frontmatter) + "---\n" + body
    (skill_dir / "SKILL.md").write_text(text, encoding="utf-8")
    for rel, content in (extra_files or {}).items():
        target = skill_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return skill_dir


def _tree_signature(root: Path) -> dict[str, tuple[int, int]]:
    """relpath → (mtime_ns, size) for every regular file under *root*."""
    signature: dict[str, tuple[int, int]] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        stat = path.stat()
        signature[path.relative_to(root).as_posix()] = (stat.st_mtime_ns, stat.st_size)
    return signature


def _plugin_spec(name: str, body: str = "# plugin\n") -> dict[str, str | dict[str, str]]:
    return {
        "name": name,
        "description": f"{name} from plugin",
        "provider": "test_plugin",
        "files": {"SKILL.md": body},
    }


class TestOriginEnum:
    def test_four_origins(self) -> None:
        assert tuple(origin.value for origin in SkillOrigin) == ORIGIN_VALUES
        assert {origin.name for origin in SkillOrigin} == {
            "IMPORTED",
            "FIXED",
            "DERIVED",
            "CAPTURED",
        }

    def test_unknown_origin_rejected(self) -> None:
        catalog = SkillCatalog()
        try:
            catalog.record(name="x", files={"SKILL.md": "a"}, origin="cloud")
        except SkillCatalogError as exc:
            assert "unknown origin" in str(exc)
        else:
            raise AssertionError("expected SkillCatalogError")


class TestScanWritesNothing:
    def test_directory_scan_does_not_write_skill_id(self, tmp_path: Path) -> None:
        skills_root = tmp_path / "skills"
        _write_skill_dir(skills_root, "code-review", body="# review\n")
        before = _tree_signature(tmp_path)

        catalog = scan_catalog(base_dir=tmp_path)

        assert len(catalog) == 1
        after = _tree_signature(tmp_path)
        assert after == before
        assert not list(tmp_path.rglob(SKILL_ID_SIDECAR))

    def test_plugin_scan_touches_no_files(self, tmp_path: Path) -> None:
        before = _tree_signature(tmp_path)
        catalog = scan_catalog(plugin_skills=[_plugin_spec("sample-skill")])
        assert catalog.get_by_name("sample-skill") is not None
        assert _tree_signature(tmp_path) == before
        assert not list(tmp_path.rglob(SKILL_ID_SIDECAR))


class TestOneActiveRevision:
    def test_scan_marks_imported_revision_active(self, tmp_path: Path) -> None:
        _write_skill_dir(tmp_path / "skills", "docs")
        catalog = scan_catalog(base_dir=tmp_path)
        skill = catalog.get_by_name("docs")
        assert skill is not None
        assert skill.active_revision.origin is SkillOrigin.IMPORTED
        assert sum(1 for rev in skill.revisions if rev.is_active) == 1

    def test_content_change_leaves_exactly_one_active(self) -> None:
        catalog = SkillCatalog()
        first = catalog.record(name="lint", files={"SKILL.md": "v1"}, origin=SkillOrigin.IMPORTED)
        second = catalog.record(name="lint", files={"SKILL.md": "v2"}, origin=SkillOrigin.IMPORTED)
        skill = catalog.get_by_name("lint")
        assert skill is not None
        active = [rev for rev in skill.revisions if rev.is_active]
        assert len(active) == 1
        assert active[0].revision_id == second.revision_id
        assert first.is_active is True  # frozen snapshot; catalog holds the deactivated copy
        assert skill.revisions[0].is_active is False


class TestContentChangeKeepsLogicalId:
    def test_rescan_after_edit_is_fixed_same_logical_id(self, tmp_path: Path) -> None:
        skill_dir = _write_skill_dir(tmp_path / "skills", "code-review", body="# v1\n")
        catalog = scan_catalog(base_dir=tmp_path)
        first = catalog.active_revision("code-review")
        assert first is not None
        logical_id = first.logical_id
        assert logical_id == logical_skill_id("code-review")

        (skill_dir / "SKILL.md").write_text(
            (skill_dir / "SKILL.md").read_text(encoding="utf-8").replace("# v1", "# v2"),
            encoding="utf-8",
        )
        scan_catalog(base_dir=tmp_path, catalog=catalog)
        second = catalog.active_revision("code-review")
        assert second is not None
        assert second.logical_id == logical_id
        assert second.revision_id != first.revision_id
        assert second.origin is SkillOrigin.FIXED
        assert second.parent_revision_ids == (first.revision_id,)
        assert second.generation == first.generation + 1
        assert len(catalog.revisions("code-review")) == 2

    def test_identical_rescan_is_noop(self, tmp_path: Path) -> None:
        _write_skill_dir(tmp_path / "skills", "stable", body="# same\n")
        catalog = scan_catalog(base_dir=tmp_path)
        first = catalog.active_revision("stable")
        scan_catalog(base_dir=tmp_path, catalog=catalog)
        again = catalog.active_revision("stable")
        assert first is not None and again is not None
        assert again.revision_id == first.revision_id
        assert len(catalog.revisions("stable")) == 1


class TestDerivedAndCaptured:
    def test_captured_is_root(self) -> None:
        catalog = SkillCatalog()
        rev = catalog.record(
            name="captured-flow",
            files={"SKILL.md": "# captured\n"},
            origin=SkillOrigin.CAPTURED,
        )
        assert rev.origin is SkillOrigin.CAPTURED
        assert rev.parent_revision_ids == ()
        assert rev.generation == 0
        assert rev.logical_id == logical_skill_id("captured-flow")

    def test_derived_new_logical_skill_keeps_parent(self) -> None:
        catalog = SkillCatalog()
        parent = catalog.record(
            name="base",
            files={"SKILL.md": "# base\n"},
            origin=SkillOrigin.IMPORTED,
        )
        child = catalog.record(
            name="specialized",
            files={"SKILL.md": "# specialized\n"},
            origin=SkillOrigin.DERIVED,
            parent_logical_ids=[parent.logical_id],
        )
        assert child.origin is SkillOrigin.DERIVED
        assert child.logical_id != parent.logical_id
        assert child.parent_revision_ids == (parent.revision_id,)
        assert child.generation == 1
        assert catalog.active_revision("base") is not None
        assert catalog.active_revision("specialized") is not None

    def test_derived_without_parents_rejected(self) -> None:
        catalog = SkillCatalog()
        try:
            catalog.record(
                name="orphan",
                files={"SKILL.md": "x"},
                origin=SkillOrigin.DERIVED,
            )
        except SkillCatalogError as exc:
            assert "parent" in str(exc)
        else:
            raise AssertionError("expected SkillCatalogError")


class TestPluginWins:
    def test_plugin_name_shadows_directory(self, tmp_path: Path) -> None:
        _write_skill_dir(tmp_path / "skills", "shared", body="# directory\n")
        catalog = scan_catalog(
            base_dir=tmp_path,
            plugin_skills=[_plugin_spec("shared", "# plugin\n")],
        )
        active = catalog.active_revision("shared")
        assert active is not None
        assert active.files["SKILL.md"] == "# plugin\n"
        assert active.provider == "test_plugin"
        assert len(catalog) == 1


class TestSnapshotHash:
    def test_hash_is_order_independent(self) -> None:
        left = snapshot_hash({"b.md": "2", "a.md": "1"})
        right = snapshot_hash({"a.md": "1", "b.md": "2"})
        assert left == right
        assert left != snapshot_hash({"a.md": "1", "b.md": "changed"})
