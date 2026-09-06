"""HP-68 slice 1 — this-host RAM/CPU/disk. No fleet, no invented quotas."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from hivepilot.services import host_resources
from hivepilot.services.token_service import add_token

MEMINFO = """\
MemTotal:       16777216 kB
MemFree:         1048576 kB
MemAvailable:    5242880 kB
"""

# total delta 600, idle delta 200 → 66.7% busy
STAT_IDLE = "cpu  100 0 100 800 0 0 0 0 0 0\n"
STAT_BUSY = "cpu  300 0 300 1000 0 0 0 0 0 0\n"


def test_parse_meminfo_uses_available_not_free() -> None:
    parsed = host_resources.parse_meminfo(MEMINFO)
    assert parsed is not None
    total = 16777216 * 1024
    used = total - 5242880 * 1024
    assert parsed["total_bytes"] == total
    assert parsed["used_bytes"] == used


def test_cpu_used_pct_from_two_samples() -> None:
    pct = host_resources.cpu_used_pct(STAT_IDLE, STAT_BUSY)
    assert pct == pytest.approx(66.66666666666667)


def test_snapshot_injected_never_invents_servers() -> None:
    snap = host_resources.snapshot(
        meminfo_text=MEMINFO,
        stat_samples=(STAT_IDLE, STAT_BUSY),
        disk_usage=(1_000_000_000, 50_000_000),
        nproc=8,
        sleep=lambda _s: None,
    )
    assert snap["available"] is True
    assert snap["source"] == "procfs"
    assert "server" not in snap
    assert "servers" not in snap
    assert snap["ram"]["used_pct"] == pytest.approx(68.8, abs=0.1)
    assert snap["cpu"]["used_pct"] == pytest.approx(66.7)
    assert snap["cpu"]["nproc"] == 8
    assert snap["disk"]["used_pct"] == 5.0
    assert snap["disk"]["path"] == "/"
    assert "fleet" in snap["note"]


def test_snapshot_unavailable_when_nothing_readable() -> None:
    snap = host_resources.snapshot(
        meminfo_text="",
        stat_samples=("", ""),
        disk_usage=(0, 0),
        nproc=None,
        read_text=lambda _p: None,
        sleep=lambda _s: None,
    )
    assert snap["available"] is False
    assert snap["ram"] is None
    assert snap["cpu"] is None
    assert snap["disk"] is None
    assert snap["source"] is None
    assert "invented" in snap["note"]


def test_live_linux_snapshot_has_expected_keys() -> None:
    if not Path("/proc/meminfo").exists():
        pytest.skip("no /proc")
    snap = host_resources.snapshot(cpu_sample_seconds=0.02)
    assert set(snap.keys()) == {"available", "source", "ram", "cpu", "disk", "note"}
    if snap["ram"] is not None:
        assert snap["ram"]["total_bytes"] > 0
        assert 0 <= snap["ram"]["used_bytes"] <= snap["ram"]["total_bytes"]


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


def test_host_resources_endpoint_read_role(tmp_tokens_file, api_client, monkeypatch) -> None:
    raw, _ = add_token("read")
    monkeypatch.setattr(
        host_resources,
        "snapshot",
        lambda: {
            "available": True,
            "source": "procfs",
            "ram": {"used_bytes": 1, "total_bytes": 2, "used_pct": 50.0},
            "cpu": {"used_pct": 10.0, "nproc": 2},
            "disk": {"used_bytes": 1, "total_bytes": 20, "used_pct": 5.0, "path": "/"},
            "note": "this host",
        },
    )

    resp = api_client.get("/v1/host/resources", headers=_auth(raw))
    assert resp.status_code == 200
    data = resp.json()
    assert data["available"] is True
    assert "servers" not in data
    assert set(data.keys()) == {"available", "source", "ram", "cpu", "disk", "note"}


def test_host_resources_endpoint_rejects_anonymous(api_client) -> None:
    resp = api_client.get("/v1/host/resources")
    assert resp.status_code in {401, 403}
