"""HP-77: declarative plugin packs — parse, host-compat, consent, install."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from hivepilot.services import plugin_pack_service as packs
from hivepilot.services.token_service import add_token


@pytest.fixture()
def pack_home(tmp_path):
    return tmp_path


@pytest.fixture()
def tmp_tokens_file(tmp_path, monkeypatch):
    from hivepilot.config import settings

    tokens_file = tmp_path / "tokens.yaml"
    tokens_file.write_text(yaml.safe_dump({"tokens": []}), encoding="utf-8")
    monkeypatch.setattr(settings, "tokens_file", tokens_file)
    return tokens_file


@pytest.fixture()
def api_client():
    from hivepilot.services.api_service import app

    return TestClient(app, raise_server_exceptions=True)


def _auth(raw: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw}"}


def test_bundled_skills_kit_parses():
    pack = packs.get_pack("skills-kit")
    assert pack is not None
    assert [p.name for p in pack.plugins] == ["improve", "shadcn"]
    assert pack.source == "bundled"


def test_parse_rejects_unknown_and_agent_cli():
    with pytest.raises(packs.PackError, match="unknown curated plugin"):
        packs.parse_pack("name: x\nplugins: [not-a-plugin]\n")
    with pytest.raises(packs.PackError, match="agent CLI"):
        packs.parse_pack("name: x\nplugins: [codex]\n")
    with pytest.raises(packs.PackError, match="demo showcase"):
        packs.parse_pack("name: x\nplugins: [sample]\n")


def test_parse_rejects_literal_secrets():
    with pytest.raises(packs.PackError, match="ref"):
        packs.parse_pack(
            "\n".join(
                [
                    "name: leaky",
                    "plugins: [improve]",
                    "config:",
                    "  HIVEPILOT_INFISICAL_TOKEN: sk-literal",
                ]
            )
        )


def test_parse_keeps_env_refs():
    pack = packs.parse_pack(
        "\n".join(
            [
                "name: ok-refs",
                "plugins: [obsidian]",
                "config:",
                "  HIVEPILOT_OBSIDIAN_VAULT: ${env:HIVEPILOT_OBSIDIAN_VAULT}",
                "credentials:",
                "  - HIVEPILOT_HINDSIGHT_API_KEY",
                "capabilities: [filesystem]",
            ]
        )
    )
    assert pack.config["HIVEPILOT_OBSIDIAN_VAULT"].startswith("${env:")
    assert pack.credentials == ["HIVEPILOT_HINDSIGHT_API_KEY"]


def test_preview_reports_capability_and_missing_creds(monkeypatch):
    from hivepilot.config import settings

    monkeypatch.setattr(settings, "plugins_capability_policy", [])
    pack = packs.get_pack("obsidian-memory")
    assert pack is not None
    preview = packs.preview_pack(pack)
    assert preview["compatible"] is True
    assert "filesystem" in preview["capability_blocked"]
    assert "HIVEPILOT_HINDSIGHT_API_KEY" in preview["missing_credentials"]


def test_install_requires_consent():
    with pytest.raises(packs.PackError, match="consent"):
        packs.install_pack("skills-kit", consent=False)


def test_install_fetches_each_plugin(pack_home, monkeypatch):
    dest = pack_home / "plugins"
    dest.mkdir()
    env_path = pack_home / ".env"
    fetched: list[str] = []

    def _fetch(name, dest_dir=None, **_k):
        fetched.append(name)
        path = (dest_dir or dest) / f"{name}.py"
        path.write_text("# stub\n", encoding="utf-8")
        return path

    result = packs.install_pack(
        "skills-kit",
        consent=True,
        dest_dir=dest,
        env_path=env_path,
        fetch=_fetch,
    )
    assert fetched == ["improve", "shadcn"]
    assert result["restart_required"] is True
    text = env_path.read_text(encoding="utf-8")
    assert "HIVEPILOT_IMPROVE_ENABLED=true" in text
    assert "HIVEPILOT_SHADCN_ENABLED=true" in text


def test_import_and_export_roundtrip(pack_home):
    text = packs.export_pack("skills-kit")
    parsed = packs.parse_pack(text, source="import")
    path = packs.save_imported_pack(parsed, text)
    assert path.exists()
    listed = {p.name: p.source for p in packs.list_packs()}
    assert listed["skills-kit"] == "import"


def test_hub_skips_invalid_entries(monkeypatch):
    class _Resp:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def iter_content(self, chunk_size: int):
            yield b'{"packs":[{"name":"skills-kit","plugins":["improve","shadcn"]},{"name":"bad"}]}'

        def close(self) -> None:
            return None

    monkeypatch.setattr(packs.requests, "get", lambda *_a, **_k: _Resp())
    hub = packs.fetch_hub_packs(url="https://example.test/packs.json")
    names = {p.name for p in hub}
    assert "skills-kit" in names
    assert "bad" not in names


def test_api_list_and_install_consent(api_client, tmp_tokens_file, pack_home, monkeypatch):
    admin, _ = add_token("admin")
    read, _ = add_token("read")
    listed = api_client.get("/v1/plugin-packs", headers=_auth(read))
    assert listed.status_code == 200
    names = {row["pack"]["name"] for row in listed.json()["packs"]}
    assert "skills-kit" in names
    assert "obsidian-memory" in names

    denied = api_client.post(
        "/v1/plugin-packs/skills-kit/install",
        headers=_auth(admin),
        json={"consent": False},
    )
    assert denied.status_code == 400

    dest = pack_home / "plugins"
    dest.mkdir()

    def _fetch(name, dest_dir=None, **_k):
        path = Path(dest_dir or dest) / f"{name}.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# stub\n", encoding="utf-8")
        return path

    monkeypatch.setattr(packs.plugin_installer, "fetch_plugin", _fetch)
    monkeypatch.setattr(packs.plugin_installer, "_default_env_path", lambda: pack_home / ".env")
    installed = api_client.post(
        "/v1/plugin-packs/skills-kit/install",
        headers=_auth(admin),
        json={"consent": True},
    )
    assert installed.status_code == 200, installed.text
    assert installed.json()["restart_required"] is True
    assert {row["name"] for row in installed.json()["installed"]} == {"improve", "shadcn"}


def test_api_import_rejects_openapi_secret_literal(api_client, tmp_tokens_file, pack_home):
    admin, _ = add_token("admin")
    resp = api_client.post(
        "/v1/plugin-packs/import",
        headers=_auth(admin),
        json={"text": "name: leak\nplugins: [improve]\nconfig:\n  HIVEPILOT_KMS_TOKEN: abc\n"},
    )
    assert resp.status_code == 400
    assert "ref" in resp.json()["detail"]
