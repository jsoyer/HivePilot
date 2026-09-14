"""HP-112: local host skills — discovery via BM25, delegate via HP APIs."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hivepilot.host_skills import (
    DELEGATE_MODES,
    HOST_SKILL_NAMES,
    DelegateReport,
    DiscoveryReport,
    HostSkillsError,
    delegate,
    disclose,
    discover,
    ingest_host_skills,
    pipeline_via_orchestrator,
    register_pipeline_runner,
    skill_specs,
)
from hivepilot.services import delegation
from hivepilot.skill_capabilities import build_capability_map, parse_skill_tool_tokens
from hivepilot.skill_catalog import SkillCatalog
from hivepilot.skill_ranker import SkillRanker, retrieve
from hivepilot.skill_trust import register_revision, set_enabled

_HOST_SOURCE = Path(__file__).resolve().parents[1] / "hivepilot" / "host_skills.py"
_PLUGIN_SOURCE = (
    Path(__file__).resolve().parents[1] / "hivepilot" / "bundled_plugins" / "host_skills.py"
)

_FORBIDDEN = (
    "execute_task",
    "auto-evolve",
    "auto_evolve",
    "cloud_browse_skills",
    "cloud_auth_flow",
    "openspace-mcp",
    "OPENSPACE_CLOUD",
    "import pickle",
    "urllib.request",
    "httpx",
    "openai",
)


def _record(
    catalog: SkillCatalog,
    name: str,
    description: str,
    body: str = "# Body\n",
    *,
    enabled: bool = True,
):
    revision = catalog.record(
        name=name,
        files={"SKILL.md": f"---\ndescription: {description}\n---\n{body}"},
        description=description,
    )
    register_revision(
        revision_id=revision.revision_id,
        skill_name=name,
        logical_id=revision.logical_id,
        enabled=enabled,
    )
    return revision


def _catalog() -> SkillCatalog:
    catalog = SkillCatalog()
    _record(
        catalog,
        "redaction-docs",
        "Rédiger la documentation utilisateur et les guides d'installation.",
        "# Docs\n\nInstaller l'application.\n",
    )
    _record(
        catalog,
        "revue-code",
        "Revue adversariale d'un diff de code.",
        "# Revue\n",
    )
    return catalog


class TestSurfaceIsLocal:
    def test_source_omits_remote_and_vendor_hooks(self) -> None:
        source = _HOST_SOURCE.read_text(encoding="utf-8") + _PLUGIN_SOURCE.read_text(
            encoding="utf-8"
        )
        for token in _FORBIDDEN:
            assert token not in source
        for spec in skill_specs():
            blob = spec["description"] + spec["files"]["SKILL.md"]
            for token in _FORBIDDEN:
                assert token not in blob

    def test_host_skill_names_and_modes(self) -> None:
        assert HOST_SKILL_NAMES == ("skill-discovery", "delegate-task")
        assert DELEGATE_MODES == ("subagent", "peer", "pipeline")
        assert {spec["name"] for spec in skill_specs()} == set(HOST_SKILL_NAMES)

    def test_specs_grant_no_tool_tokens(self) -> None:
        catalog = SkillCatalog()
        ingest_host_skills(catalog)
        for spec in skill_specs():
            assert parse_skill_tool_tokens(spec["files"]) == ()
        assert build_capability_map(catalog) == {}


class TestDiscoverUsesLocalRanker:
    def test_same_order_as_bm25_retrieve(self) -> None:
        catalog = _catalog()
        report = discover("rédiger la documentation utilisateur", catalog=catalog)
        hits = retrieve(catalog, "rédiger la documentation utilisateur")
        assert isinstance(report, DiscoveryReport)
        assert [hit.name for hit in report.hits] == [hit.name for hit in hits]
        assert report.hits[0].name == "redaction-docs"
        assert "body" not in report.hits[0].to_dict()

    def test_calls_ranker_retrieve_not_a_remote(self) -> None:
        catalog = _catalog()
        seen: dict[str, object] = {}

        def _retrieve(self, query, *, top_k=10, include_provisional=True):
            seen["query"] = query
            seen["top_k"] = top_k
            seen["include_provisional"] = include_provisional
            seen["catalog"] = self.catalog
            return ()

        with patch.object(SkillRanker, "retrieve", _retrieve):
            discover("installation", catalog=catalog, top_k=3, include_provisional=False)
        assert seen == {
            "query": "installation",
            "top_k": 3,
            "include_provisional": False,
            "catalog": catalog,
        }

    def test_disabled_rows_are_absent(self) -> None:
        catalog = _catalog()
        hidden, _ = _record(
            catalog,
            "masque",
            "terme unique masque pour le déploiement",
            enabled=True,
        )
        set_enabled(hidden.revision_id, False)
        report = discover("terme unique masque", catalog=catalog)
        assert report.hits == ()

    def test_progressive_disclosure(self) -> None:
        catalog = _catalog()
        report = discover("documentation utilisateur", catalog=catalog)
        card = disclose(catalog, report.hits[0].revision_id)
        assert card is not None
        assert "Installer l'application" in card.body
        combined = discover(
            "documentation utilisateur",
            catalog=catalog,
            disclose_revision_id=report.hits[0].revision_id,
        )
        assert combined.disclosure is not None
        assert combined.disclosure.body == card.body

    def test_does_not_touch_the_network(self) -> None:
        catalog = _catalog()

        def _boom(*_args, **_kwargs):  # pragma: no cover - must never run
            raise AssertionError("discover reached the network")

        with (
            patch("socket.create_connection", _boom),
            patch("urllib.request.urlopen", _boom),
        ):
            report = discover("revue adversariale", catalog=catalog)
        assert report.hits[0].name == "revue-code"

    def test_ingested_host_skills_rank_locally(self) -> None:
        catalog = SkillCatalog()
        ingest_host_skills(catalog)
        for spec in skill_specs():
            revision = catalog.active_revision(spec["name"])
            assert revision is not None
            register_revision(
                revision_id=revision.revision_id,
                skill_name=spec["name"],
                logical_id=revision.logical_id,
                enabled=True,
            )
        report = discover("deterministic BM25 catalog", catalog=catalog)
        assert report.hits[0].name == "skill-discovery"
        peer = discover("named pipeline subagent peer", catalog=catalog)
        assert peer.hits[0].name == "delegate-task"

    def test_scans_injected_plugin_skills_when_catalog_omitted(self) -> None:
        specs = [
            {
                "name": "local-only",
                "description": "Guide d'installation locale unique",
                "files": {"SKILL.md": "---\ndescription: install\n---\n# x\n"},
            }
        ]
        revision = SkillCatalog().ingest(specs[0])
        register_revision(
            revision_id=revision.revision_id,
            skill_name="local-only",
            logical_id=revision.logical_id,
            enabled=True,
        )
        report = discover("installation locale unique", plugin_skills=specs)
        assert [hit.name for hit in report.hits] == ["local-only"]


class TestDelegateUsesHivePilotApis:
    def setup_method(self) -> None:
        register_pipeline_runner(None)
        delegation.register_subagent_executor(None)
        delegation.register_peer_executor(None)

    def teardown_method(self) -> None:
        register_pipeline_runner(None)
        delegation.register_subagent_executor(None)
        delegation.register_peer_executor(None)

    def test_subagent_calls_run_subagent(self) -> None:
        seen: list[tuple[str, str]] = []
        delegation.register_subagent_executor(
            lambda role, prompt: seen.append((role, prompt)) or f"{role}:{prompt}"
        )
        report = delegate("subagent", role="ceo", prompt="résume")
        assert seen == [("ceo", "résume")]
        assert report == DelegateReport(
            mode="subagent",
            status="ok",
            role="ceo",
            output="ceo:résume",
        )

    def test_subagent_empty_without_executor(self) -> None:
        report = delegate("subagent", role="ceo", prompt="x")
        assert report.status == "empty"
        assert report.output is None

    def test_peer_calls_spawn_peer(self) -> None:
        with patch(
            "hivepilot.host_skills.spawn_peer", return_value=42
        ) as spawn:
            report = delegate("peer", project="api", task="docs", role="cto", tenant="acme")
        spawn.assert_called_once_with("api", "docs", "cto", tenant="acme")
        assert report.status == "spawned"
        assert report.run_id == 42
        assert report.project == "api"

    def test_pipeline_uses_registered_runner(self) -> None:
        seen: list[tuple[str, str, str, str]] = []

        def _runner(project, pipeline, prompt, tenant):
            seen.append((project, pipeline, prompt, tenant))
            return ["stage-ok"]

        register_pipeline_runner(_runner)
        report = delegate(
            "pipeline",
            project="api",
            pipeline="docs",
            prompt="ajoute un rollback",
            tenant="acme",
        )
        assert seen == [("api", "docs", "ajoute un rollback", "acme")]
        assert report.status == "ran"
        assert report.pipeline == "docs"
        assert "stage-ok" in (report.output or "")

    def test_pipeline_uses_orchestrator_run_pipeline(self) -> None:
        seen: dict[str, object] = {}

        def _run_pipeline(**kwargs):
            seen.update(kwargs)
            return "piped"

        orch = SimpleNamespace(run_pipeline=_run_pipeline)
        report = delegate(
            "pipeline",
            project="api",
            pipeline="ship",
            prompt="go",
            orchestrator=orch,
        )
        assert seen["project_names"] == ["api"]
        assert seen["pipeline_name"] == "ship"
        assert seen["extra_prompt"] == "go"
        assert seen["auto_git"] is False
        assert seen["dry_run"] is False
        assert report.status == "ran"
        assert report.output == "piped"

    def test_pipeline_via_orchestrator_helper(self) -> None:
        orch = SimpleNamespace(
            run_pipeline=lambda **kwargs: kwargs["pipeline_name"],
        )
        assert pipeline_via_orchestrator(orch, "p", "docs", "x") == "docs"

    def test_pipeline_without_runner_fails_closed(self) -> None:
        try:
            delegate("pipeline", project="api", pipeline="docs")
        except HostSkillsError as exc:
            assert "pipeline runner" in str(exc)
        else:
            raise AssertionError("expected HostSkillsError")

    def test_unknown_mode_and_missing_fields(self) -> None:
        try:
            delegate("remote-worker", role="ceo", prompt="x")
        except HostSkillsError as exc:
            assert "unknown delegate mode" in str(exc)
        else:
            raise AssertionError("expected HostSkillsError")
        try:
            delegate("subagent", role="", prompt="x")
        except HostSkillsError as exc:
            assert "role" in str(exc)
        else:
            raise AssertionError("expected HostSkillsError")

    def test_delegate_never_exposes_a_remote_task_entry(self) -> None:
        import hivepilot.host_skills as host_mod

        assert not hasattr(host_mod, "execute_task")
        delegation.register_subagent_executor(lambda role, prompt: "ok")
        report = delegate("subagent", role="ceo", prompt="x")
        assert report.status == "ok"
        assert report.mode == "subagent"


class TestBundledPlugin:
    def test_register_publishes_both_specs(self) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "hp_host_skills_plugin", _PLUGIN_SOURCE
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        payload = module.register()
        names = [row["name"] for row in payload["skills"]]
        assert names == list(HOST_SKILL_NAMES)
        assert all(row["provider"] == "host_skills" for row in payload["skills"])
