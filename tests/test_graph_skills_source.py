"""Tests for the built-in `skills` graph source (Mirador Graph View PRD,
Sprint 2, decision D3) — `hivepilot/graph_sources/skills_source.py`.

Unlike every other graph source, `skills` reads a LOCAL FILESYSTEM path
(`settings.graph_skills_scan_path`), not tenant/config state — every test
here builds a real `tmp_path` directory tree rather than mocking a service
layer, so the path-traversal guard and size cap are exercised against real
`Path` objects, not stubs.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from hivepilot import graph as graph_module
from hivepilot.graph_sources.skills_source import (
    _MAX_READ_BYTES,
    SKILLS_GRAPH_SOURCE,
    _build_graph,
    _is_within_root,
    _node_detail,
    _read_capped,
    _scan_root,
)


def _write_skill(root, rel_dir: str, *, name: str, description: str, tags: list[str] | None = None):
    skill_dir = root / rel_dir
    skill_dir.mkdir(parents=True, exist_ok=True)
    front_matter: dict[str, Any] = {"name": name, "description": description}
    if tags is not None:
        front_matter["tags"] = tags
    content = "---\n" + yaml.safe_dump(front_matter) + "---\nbody\n"
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
    return skill_dir


@pytest.fixture()
def ctx():
    return graph_module.GraphContext(tenant="default", role="admin")


@pytest.fixture()
def scan_root(tmp_path, monkeypatch):
    from hivepilot.config import settings

    root = tmp_path / "skills-root"
    root.mkdir()
    monkeypatch.setattr(settings, "graph_skills_scan_path", root, raising=False)
    return root


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestSkillsGraphSourceRegistration:
    def test_registered_under_skills_name(self):
        import hivepilot.graph_sources  # noqa: F401 - side-effect import

        assert graph_module.get_graph_source("skills") is SKILLS_GRAPH_SOURCE

    def test_spec_shape_admin_gated(self):
        assert SKILLS_GRAPH_SOURCE.name == "skills"
        assert SKILLS_GRAPH_SOURCE.min_role == "admin"
        assert SKILLS_GRAPH_SOURCE.node_detail is not None


# ---------------------------------------------------------------------------
# _build_graph — no scan path / empty configuration
# ---------------------------------------------------------------------------


class TestBuildGraphEmptyConfig:
    def test_no_scan_path_configured_returns_empty_never_crashes(self, ctx, monkeypatch):
        from hivepilot.config import settings

        monkeypatch.setattr(settings, "graph_skills_scan_path", None, raising=False)
        data = _build_graph(ctx)
        assert data.nodes == ()
        assert data.edges == ()

    def test_scan_path_not_a_directory_returns_empty(self, ctx, tmp_path, monkeypatch):
        from hivepilot.config import settings

        monkeypatch.setattr(
            settings, "graph_skills_scan_path", tmp_path / "does-not-exist", raising=False
        )
        data = _build_graph(ctx)
        assert data.nodes == ()

    def test_empty_directory_returns_empty(self, ctx, scan_root):
        data = _build_graph(ctx)
        assert data.nodes == ()
        assert data.edges == ()


# ---------------------------------------------------------------------------
# _scan_root — nodes/edges/status
# ---------------------------------------------------------------------------


class TestScanRoot:
    def test_skill_node_present_with_front_matter(self, ctx, scan_root):
        _write_skill(
            scan_root, "skillA", name="Skill A", description="does a thing", tags=["x", "y"]
        )
        data = _build_graph(ctx)
        skill_nodes = [n for n in data.nodes if n.kind == "skill"]
        assert len(skill_nodes) == 1
        node = skill_nodes[0]
        assert node.label == "Skill A"
        assert node.status == "active"
        assert set(node.badges) == {"x", "y"}
        assert node.id == "skill:skillA/SKILL.md"

    def test_vendored_status_from_path(self, ctx, scan_root):
        _write_skill(scan_root, "vendored/skillV", name="Vendored", description="d")
        data = _build_graph(ctx)
        node = next(n for n in data.nodes if n.kind == "skill")
        assert node.status == "vendored"

    def test_hook_command_agent_nodes_and_membership_edges(self, ctx, scan_root):
        skill_dir = _write_skill(scan_root, "skillA", name="Skill A", description="d")
        (skill_dir / "hooks").mkdir()
        (skill_dir / "hooks" / "before.py").write_text("pass", encoding="utf-8")
        (skill_dir / "commands").mkdir()
        (skill_dir / "commands" / "run.sh").write_text("#!/bin/sh", encoding="utf-8")
        (skill_dir / "agents").mkdir()
        (skill_dir / "agents" / "agent.md").write_text("# agent", encoding="utf-8")

        data = _build_graph(ctx)
        kinds = {n.kind for n in data.nodes}
        assert {"skill", "hook", "command", "agent"} <= kinds

        skill_id = next(n.id for n in data.nodes if n.kind == "skill")
        member_edges = [e for e in data.edges if e.kind == "member" and e.source == skill_id]
        assert len(member_edges) == 3

    def test_orphan_hook_outside_any_skill_has_no_membership_edge(self, ctx, scan_root):
        (scan_root / "hooks").mkdir()
        (scan_root / "hooks" / "orphan.py").write_text("pass", encoding="utf-8")
        data = _build_graph(ctx)
        hook_nodes = [n for n in data.nodes if n.kind == "hook"]
        assert len(hook_nodes) == 1
        assert not [e for e in data.edges if e.kind == "member"]

    def test_no_duplicate_node_ids(self, ctx, scan_root):
        _write_skill(scan_root, "skillA", name="A", description="d")
        _write_skill(scan_root, "skillB", name="B", description="d")
        data = _build_graph(ctx)
        ids = [n.id for n in data.nodes]
        assert len(ids) == len(set(ids))


# ---------------------------------------------------------------------------
# Path-traversal guard
# ---------------------------------------------------------------------------


class TestPathTraversalGuard:
    def test_resolved_path_outside_root_is_rejected(self, tmp_path, scan_root):
        outside = tmp_path / "outside.txt"
        outside.write_text("nope", encoding="utf-8")
        assert _is_within_root(outside, scan_root.resolve()) is None

    def test_symlink_escaping_root_is_not_scanned(self, ctx, tmp_path, scan_root):
        outside_dir = tmp_path / "outside-secret"
        outside_dir.mkdir()
        (outside_dir / "SKILL.md").write_text(
            "---\nname: leaked\ndescription: OUTSIDE_MARKER\n---\nbody\n", encoding="utf-8"
        )
        symlink_path = scan_root / "escape"
        try:
            symlink_path.symlink_to(outside_dir, target_is_directory=True)
        except OSError:
            pytest.skip("symlinks not supported in this environment")

        data = _build_graph(ctx)
        assert "OUTSIDE_MARKER" not in str(data)
        assert not any(n.label == "leaked" for n in data.nodes)

    def test_node_detail_rejects_traversal_attempt_in_node_id(self, ctx, scan_root):
        _write_skill(scan_root, "skillA", name="A", description="d")
        assert _node_detail(ctx, "skill:../../../../etc/passwd") is None
        assert _node_detail(ctx, "skill:") is None

    def test_embedded_null_byte_in_node_id_never_raises(self, ctx, scan_root):
        """`Path.resolve()` raises `ValueError` (not `OSError`) on a path
        containing an embedded null byte. `_is_within_root` must catch that
        too -- a malformed/hostile node id degrades to "not within root"
        (rejected) right at the guard, never an uncaught exception."""
        _write_skill(scan_root, "skillA", name="A", description="d")
        assert _node_detail(ctx, "skill:\x00foo") is None
        assert _is_within_root(Path("skillA/\x00SKILL.md"), scan_root.resolve()) is None

    def test_embedded_null_byte_via_api_never_500(self, tmp_tokens_file, api_client, scan_root):
        from hivepilot.services.token_service import add_token

        _write_skill(scan_root, "skillA", name="A", description="d")
        raw, _ = add_token("admin")
        resp = api_client.get("/v1/graph/skills/node/skill:%00foo", headers=_auth(raw))
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Size cap
# ---------------------------------------------------------------------------


class TestSizeCap:
    def test_read_capped_never_reads_past_max_bytes(self, tmp_path):
        huge = tmp_path / "huge.md"
        huge.write_text("A" * (_MAX_READ_BYTES * 3), encoding="utf-8")
        content = _read_capped(huge)
        assert len(content.encode("utf-8", errors="replace")) <= _MAX_READ_BYTES

    def test_huge_skill_description_is_truncated_in_detail(self, ctx, scan_root):
        huge_description = "D" * 5000
        _write_skill(scan_root, "skillA", name="Big", description=huge_description)
        detail = _node_detail(ctx, "skill:skillA/SKILL.md")
        assert detail is not None
        text_section = next(s for s in detail.sections if s["kind"] == "text")
        assert len(text_section["content"]) < 5000


# ---------------------------------------------------------------------------
# _node_detail
# ---------------------------------------------------------------------------


class TestNodeDetail:
    def test_unknown_node_returns_none(self, ctx, scan_root):
        assert _node_detail(ctx, "totally-unknown-prefix") is None
        assert _node_detail(ctx, "skill:does/not/exist/SKILL.md") is None

    def test_skill_detail_has_text_and_table(self, ctx, scan_root):
        _write_skill(scan_root, "skillA", name="A", description="a real description")
        detail = _node_detail(ctx, "skill:skillA/SKILL.md")
        assert detail is not None
        assert detail.title == "skillA"
        assert "skill" in detail.tags
        kinds = [s["kind"] for s in detail.sections]
        assert "text" in kinds
        assert "table" in kinds

    def test_hook_detail_has_tags_and_table_no_text(self, ctx, scan_root):
        skill_dir = _write_skill(scan_root, "skillA", name="A", description="d")
        (skill_dir / "hooks").mkdir()
        (skill_dir / "hooks" / "before.py").write_text("pass", encoding="utf-8")
        detail = _node_detail(ctx, "hook:skillA/hooks/before.py")
        assert detail is not None
        assert "hook" in detail.tags
        kinds = [s["kind"] for s in detail.sections]
        assert kinds == ["table"]

    def test_no_scan_path_returns_none(self, ctx, monkeypatch):
        from hivepilot.config import settings

        monkeypatch.setattr(settings, "graph_skills_scan_path", None, raising=False)
        assert _node_detail(ctx, "skill:skillA/SKILL.md") is None


# ---------------------------------------------------------------------------
# Full API — role gating + no-crash
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_tokens_file(tmp_path, monkeypatch):
    tokens_file = tmp_path / "tokens.yaml"
    tokens_file.write_text(yaml.safe_dump({"tokens": []}), encoding="utf-8")
    from hivepilot.config import settings

    monkeypatch.setattr(settings, "tokens_file", tokens_file)
    return tokens_file


@pytest.fixture()
def api_client():
    from hivepilot.services.api_service import app

    return TestClient(app, raise_server_exceptions=True)


def _auth(raw_token: str) -> dict:
    return {"Authorization": f"Bearer {raw_token}"}


class TestSkillsGraphApi:
    def test_read_token_forbidden(self, api_client, tmp_tokens_file, scan_root):
        from hivepilot.services.token_service import add_token

        raw, _ = add_token("read")
        resp = api_client.get("/v1/graph/skills", headers=_auth(raw))
        assert resp.status_code == 403

    def test_run_token_forbidden(self, api_client, tmp_tokens_file, scan_root):
        from hivepilot.services.token_service import add_token

        raw, _ = add_token("run")
        resp = api_client.get("/v1/graph/skills", headers=_auth(raw))
        assert resp.status_code == 403

    def test_admin_token_ok(self, api_client, tmp_tokens_file, scan_root):
        from hivepilot.services.token_service import add_token

        _write_skill(scan_root, "skillA", name="A", description="d")
        raw, _ = add_token("admin")
        resp = api_client.get("/v1/graph/skills", headers=_auth(raw))
        assert resp.status_code == 200
        assert len(resp.json()["nodes"]) == 1

    def test_admin_no_scan_path_returns_empty_never_crashes(
        self, api_client, tmp_tokens_file, monkeypatch
    ):
        from hivepilot.config import settings
        from hivepilot.services.token_service import add_token

        monkeypatch.setattr(settings, "graph_skills_scan_path", None, raising=False)
        raw, _ = add_token("admin")
        resp = api_client.get("/v1/graph/skills", headers=_auth(raw))
        assert resp.status_code == 200
        assert resp.json()["nodes"] == []


# ---------------------------------------------------------------------------
# _scan_root helper direct coverage (non-directory root)
# ---------------------------------------------------------------------------


def test_scan_root_non_directory_returns_empty(tmp_path):
    nodes, edges = _scan_root(tmp_path / "missing")
    assert nodes == []
    assert edges == []


def test_scan_root_helper_used_directly(tmp_path):
    root = tmp_path / "root2"
    root.mkdir()
    _write_skill(root, "s", name="S", description=textwrap.dedent("desc"))
    nodes, edges = _scan_root(root)
    assert len(nodes) == 1
