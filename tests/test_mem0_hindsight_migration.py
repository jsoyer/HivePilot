"""HP-53 slice 1: mem0 → Hindsight retain, idempotent, no plugin deletion."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from typer.testing import CliRunner

from hivepilot.services import mem0_hindsight_migration as mig
from hivepilot.services import state_service


@dataclass
class _Mem0:
    pages: dict[str, list] = field(default_factory=dict)
    calls: list = field(default_factory=list)

    def get_all(self, **kwargs):
        self.calls.append(kwargs)
        user_id = (kwargs.get("filters") or {}).get("user_id") or kwargs.get("user_id")
        page = int(kwargs.get("page") or 1)
        batches = self.pages.get(user_id, [])
        if page - 1 < len(batches):
            return {"results": batches[page - 1]}
        return {"results": []}


@dataclass
class _Hindsight:
    retains: list = field(default_factory=list)
    fail_on: str | None = None

    def retain(self, *, bank_id, content):
        if self.fail_on and self.fail_on in content:
            raise RuntimeError("retain exploded")
        self.retains.append((bank_id, content))


class TestExtractAndIds:
    def test_results_dicts(self):
        items = mig.extract_memories(
            {
                "results": [
                    {"id": "m1", "memory": "The API is on :8888", "metadata": {"role": "docs"}}
                ]
            }
        )
        assert items[0]["id"] == "m1"
        assert "8888" in items[0]["text"]

    def test_hash_id_when_mem0_omits_id(self):
        item = {"id": None, "text": "fact", "metadata": {}}
        assert mig.memory_id(item, "atlas:docs").startswith("hash:")
        assert mig.memory_id(item, "atlas:docs") == mig.memory_id(item, "atlas:docs")


class TestListPagination:
    def test_walks_pages(self):
        client = _Mem0(
            pages={
                "atlas:docs:developer": [
                    [{"id": "a", "memory": "one"}],
                    [{"id": "b", "memory": "two"}],
                ]
            }
        )
        items = mig.list_memories_for_key(client, "atlas:docs:developer", page_size=1)
        assert [i["id"] for i in items] == ["a", "b"]
        assert client.calls[0]["filters"]["user_id"] == "atlas:docs:developer"


class TestMigrate:
    def test_dry_run_does_not_retain(self):
        mem0 = _Mem0(pages={"p:t:r": [[{"id": "m1", "memory": "kept"}]]})
        hs = _Hindsight()
        report = mig.migrate(
            dry_run=True,
            user_id="p:t:r",
            mem0_client=mem0,
            hindsight_client=hs,
        )
        assert report.dry_run is True
        assert report.memories_found == 1
        assert report.migrated == 1
        assert hs.retains == []
        assert state_service.mem0_already_migrated("m1", "p:t:r") is False

    def test_maps_user_id_to_same_bank_and_tags_content(self):
        mem0 = _Mem0(
            pages={"atlas:docs:developer": [[{"id": "m1", "memory": "The API lives here"}]]}
        )
        hs = _Hindsight()
        report = mig.migrate(
            user_id="atlas:docs:developer",
            mem0_client=mem0,
            hindsight_client=hs,
        )
        assert report.migrated == 1
        assert report.failed == 0
        bank, content = hs.retains[0]
        assert bank == "atlas:docs:developer"
        assert "migrated-from-mem0" in content
        assert "The API lives here" in content
        assert state_service.mem0_already_migrated("m1", "atlas:docs:developer") is True

    def test_second_run_is_idempotent(self):
        mem0 = _Mem0(pages={"p:t": [[{"id": "m1", "memory": "once"}]]})
        hs = _Hindsight()
        mig.migrate(user_id="p:t", mem0_client=mem0, hindsight_client=hs)
        mig.migrate(user_id="p:t", mem0_client=mem0, hindsight_client=hs)
        assert len(hs.retains) == 1

    def test_force_re_retains(self):
        mem0 = _Mem0(pages={"p:t": [[{"id": "m1", "memory": "again"}]]})
        hs = _Hindsight()
        mig.migrate(user_id="p:t", mem0_client=mem0, hindsight_client=hs)
        mig.migrate(user_id="p:t", mem0_client=mem0, hindsight_client=hs, force=True)
        assert len(hs.retains) == 2

    def test_one_retain_failure_continues(self):
        mem0 = _Mem0(
            pages={
                "p:t": [
                    [
                        {"id": "ok", "memory": "good"},
                        {"id": "bad", "memory": "boom-retain exploded"},
                    ]
                ]
            }
        )
        hs = _Hindsight(fail_on="boom")
        report = mig.migrate(user_id="p:t", mem0_client=mem0, hindsight_client=hs)
        assert report.migrated == 1
        assert report.failed == 1
        assert report.errors

    def test_missing_mem0_client_is_unavailable(self, monkeypatch):
        monkeypatch.setattr("hivepilot.config.settings.mem0_enabled", True, raising=False)
        monkeypatch.setattr(mig, "build_mem0_client", lambda: None)
        with pytest.raises(mig.MigrationUnavailable, match="mem0"):
            mig.migrate(user_id="p:t")

    def test_apply_requires_hindsight(self, monkeypatch):
        monkeypatch.setattr("hivepilot.config.settings.mem0_enabled", True, raising=False)
        monkeypatch.setattr("hivepilot.config.settings.hindsight_enabled", False, raising=False)
        with pytest.raises(mig.MigrationUnavailable, match="HINDSIGHT"):
            mig.migrate(user_id="p:t", mem0_client=_Mem0())


class TestCli:
    def test_dry_run_exits_zero(self, monkeypatch):
        def _fake(**kwargs):
            return mig.MigrationReport(keys_scanned=1, memories_found=2, migrated=2, dry_run=True)

        monkeypatch.setattr(mig, "migrate", _fake)
        from hivepilot.cli import app

        result = CliRunner().invoke(
            app, ["memory", "migrate-mem0", "--dry-run", "--user-id", "p:t"]
        )
        assert result.exit_code == 0, result.output
        assert "DRY-RUN" in result.output
        assert "migrated=2" in result.output

    def test_unavailable_exits_one(self, monkeypatch):
        def _boom(**kwargs):
            raise mig.MigrationUnavailable("HIVEPILOT_MEM0_ENABLED is off")

        monkeypatch.setattr(mig, "migrate", _boom)
        from hivepilot.cli import app

        result = CliRunner().invoke(app, ["memory", "migrate-mem0"])
        assert result.exit_code == 1
        assert "FAIL" in result.output
        assert "MEM0" in result.output
