"""HP-117: local package-tree taxonomy (logical, tenant-scoped, no cloud)."""

from __future__ import annotations

from pathlib import Path

import yaml

from hivepilot import skill_taxonomy as taxonomy_module
from hivepilot.pass_store import inbox
from hivepilot.skill_catalog import SKILL_ID_SIDECAR, logical_skill_id, scan_catalog
from hivepilot.skill_taxonomy import (
    ASSIGNED,
    DEFAULT_CONFIDENCE_THRESHOLD,
    NEEDS_REVIEW,
    TAXONOMY_REVIEW_ACTION,
    UNPLACED,
    Classification,
    SkillTaxonomyError,
    assign,
    confidence_threshold,
    get,
    interpret_classifier,
    list_placements,
    normalize_category_path,
    place,
    reclassify,
    tree,
)

_SOURCE = Path(__file__).resolve().parents[1] / "hivepilot" / "skill_taxonomy.py"

_FORBIDDEN = (
    "cloud_browse_skills",
    "cloud_auth_flow",
    "cloud_package",
    "openspace-mcp",
    "upload_skill",
    "import_skill",
    "OPENSPACE_CLOUD",
    "def materialize_skill_category_tree",
    "urllib.request",
    "httpx",
    "import pickle",
)


def _write_skill_dir(
    skills_root: Path,
    name: str,
    *,
    body: str = "# Skill\n",
) -> Path:
    skill_dir = skills_root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    frontmatter = {"description": f"{name} skill"}
    text = "---\n" + yaml.safe_dump(frontmatter) + "---\n" + body
    (skill_dir / "SKILL.md").write_text(text, encoding="utf-8")
    return skill_dir


def _tree_signature(root: Path) -> dict[str, tuple[int, int]]:
    signature: dict[str, tuple[int, int]] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        stat = path.stat()
        signature[path.relative_to(root).as_posix()] = (stat.st_mtime_ns, stat.st_size)
    return signature


class TestSurfaceIsLocal:
    def test_source_omits_cloud_and_disk_materialize(self) -> None:
        source = _SOURCE.read_text(encoding="utf-8")
        for token in _FORBIDDEN:
            assert token not in source
        assert not hasattr(taxonomy_module, "materialize_skill_category_tree")

    def test_statuses_and_threshold(self) -> None:
        assert (UNPLACED, ASSIGNED, NEEDS_REVIEW) == ("unplaced", "assigned", "needs_review")
        assert confidence_threshold() == DEFAULT_CONFIDENCE_THRESHOLD


class TestReclassifyDoesNotMoveFiles:
    def test_mapping_changes_without_touching_skill_dirs(self, tmp_path: Path) -> None:
        skills_root = tmp_path / "skills"
        _write_skill_dir(skills_root, "code-review", body="# review\n")
        catalog = scan_catalog(base_dir=tmp_path)
        skill = catalog.get_by_name("code-review")
        assert skill is not None
        before = _tree_signature(skills_root)

        first = assign("code-review", "technology/computing/review")
        second = reclassify("code-review", "technology/computing/lint")

        assert first.logical_id == second.logical_id == logical_skill_id("code-review")
        assert first.category_path == "technology/computing/review"
        assert second.category_path == "technology/computing/lint"
        assert second.status == ASSIGNED
        assert second.assigned is True
        assert _tree_signature(skills_root) == before
        assert (skills_root / "code-review" / "SKILL.md").is_file()
        assert not (skills_root / "technology").exists()
        assert not (tmp_path / "technology").exists()
        assert not list(tmp_path.rglob(SKILL_ID_SIDECAR))


class TestAmbiguousNeedsReview:
    def test_multiple_paths_do_not_assign(self) -> None:
        decision = place(
            "docs",
            {
                "paths": [
                    "writing/docs",
                    "technology/computing/docs",
                ]
            },
        )
        assert decision.action == "review"
        assert decision.placement.status == NEEDS_REVIEW
        assert decision.placement.assigned is False
        assert decision.placement.category_path == ""
        assert decision.placement.candidates == (
            "writing/docs",
            "technology/computing/docs",
        )
        assert decision.classification is not None
        assert decision.classification.ambiguous is True
        assert decision.classification.reason == "multiple"
        pending = inbox(kind="skill_evolution", tenant="default")
        assert any(
            row.action == TAXONOMY_REVIEW_ACTION and row.id == decision.proposal_id
            for row in pending
        )

    def test_low_confidence_and_flag_need_review(self) -> None:
        low = interpret_classifier(
            {"path": "writing/docs", "confidence": 0.2},
        )
        flagged = interpret_classifier(
            {"path": "writing/docs", "ambiguous": True},
        )
        empty = interpret_classifier({"paths": []})
        invalid = interpret_classifier("../etc/passwd")
        assert low.ambiguous and low.reason == "low_confidence"
        assert flagged.ambiguous and flagged.reason == "flagged"
        assert empty.ambiguous and empty.reason == "empty"
        assert invalid.ambiguous and invalid.reason == "invalid"

        kept = assign("stable", "writing/docs")
        decision = place("stable", {"path": "writing/guides", "confidence": 0.1})
        assert decision.action == "review"
        assert decision.placement.status == NEEDS_REVIEW
        assert decision.placement.category_path == kept.category_path

    def test_unique_path_assigns(self) -> None:
        decision = place("lint", "Technology/Computing/lint/")
        assert decision.action == "assign"
        assert decision.placement.status == ASSIGNED
        assert decision.placement.category_path == "technology/computing/lint"
        assert get("lint").assigned is True


class TestTenantScope:
    def test_tenants_do_not_share_mappings(self) -> None:
        assign("shared", "writing/docs", tenant="alpha")
        assign("shared", "technology/computing", tenant="beta")
        alpha = get("shared", tenant="alpha")
        beta = get("shared", tenant="beta")
        assert alpha.category_path == "writing/docs"
        assert beta.category_path == "technology/computing"
        assert get("shared").known is False
        assert [row.tenant for row in list_placements(tenant="alpha")] == ["alpha"]


class TestLogicalTree:
    def test_tree_is_built_from_mappings(self) -> None:
        assign("docs", "writing/docs")
        assign("lint", "technology/computing/lint")
        assign("review", "technology/computing/review")
        root = tree()
        names = {child.name for child in root.children}
        assert names == {"technology", "writing"}
        computing = next(node for node in root.children if node.name == "technology").children[0]
        assert computing.path == "technology/computing"
        assert {row.skill_name for row in computing.children[0].placements} | {
            row.skill_name for row in computing.children[1].placements
        } == {"lint", "review"}

    def test_unknown_is_unplaced_and_not_persisted(self) -> None:
        ghost = get("missing-skill")
        assert ghost.known is False
        assert ghost.status == UNPLACED
        assert ghost.assigned is False
        assert list_placements() == ()


class TestPathValidation:
    def test_rejects_traversal_and_empty(self) -> None:
        for raw in ("", "/", "..", "a/../b", "a//b", "a/./b", "has space"):
            try:
                normalize_category_path(raw)
            except SkillTaxonomyError:
                continue
            raise AssertionError(f"expected SkillTaxonomyError for {raw!r}")

    def test_classification_dataclass_respects_ambiguity(self) -> None:
        classified = interpret_classifier(
            Classification(paths=("writing/docs", "ops/runbooks"), ambiguous=False)
        )
        assert classified.ambiguous is True
        assert classified.reason == "multiple"
