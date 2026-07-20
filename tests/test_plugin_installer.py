"""Unit tests for `hivepilot.services.plugin_installer`.

Security-critical module: the ONLY network-fetch-of-plugin-code path in
HivePilot (see docs/PLUGINS.md "Trust model" -- everywhere else plugin code
reaches the process via local file or `pip install`, never a network fetch).
The single most important property under test is that `fetch_plugin` REJECTS
any name not in the curated `KNOWN_EXAMPLE_PLUGINS` registry -- no
arbitrary-URL/arbitrary-path fetch is ever possible, and the fetched content
is written to disk only, never imported/exec'd by this module.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests

from hivepilot.services import plugin_installer
from hivepilot.services.plugin_installer import (
    KNOWN_EXAMPLE_PLUGINS,
    ExamplePluginSpec,
    fetch_plugin,
    installed_plugins_dir,
    is_enabled,
    is_installed,
    persist_enabled,
)

# ---------------------------------------------------------------------------
# Registry sanity — every curated spec is well-formed, matches a real
# `plugins/*.py` stem shipped in this repo, and has a real `<stem>_enabled`
# Settings flag. Not a network call — a light guard against typos/fabrication.
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGINS_DIR = REPO_ROOT / "plugins"


def test_registry_is_non_empty() -> None:
    assert len(KNOWN_EXAMPLE_PLUGINS) > 0


@pytest.mark.parametrize("name", sorted(KNOWN_EXAMPLE_PLUGINS))
def test_every_spec_has_a_real_plugin_source_file(name: str) -> None:
    assert (PLUGINS_DIR / f"{name}.py").exists(), f"plugins/{name}.py does not exist"


@pytest.mark.parametrize("name", sorted(KNOWN_EXAMPLE_PLUGINS))
def test_every_spec_has_a_real_settings_enabled_flag(name: str) -> None:
    from hivepilot.config import Settings

    assert f"{name}_enabled" in Settings.model_fields


@pytest.mark.parametrize("name", sorted(KNOWN_EXAMPLE_PLUGINS))
def test_every_spec_is_well_formed(name: str) -> None:
    spec = KNOWN_EXAMPLE_PLUGINS[name]
    assert isinstance(spec, ExamplePluginSpec)
    assert spec.name == name
    assert spec.description
    assert spec.env_flag == f"HIVEPILOT_{name.upper()}_ENABLED"
    assert spec.prereq_kind in ("binary", "pip", "config", "none")
    assert spec.prereq_detail


@pytest.mark.parametrize("name", ["rtk", "herdr", "hugo", "gh", "tmux", "bitwarden", "vaultwarden"])
def test_binary_prereq_plugins_name_their_binary(name: str) -> None:
    spec = KNOWN_EXAMPLE_PLUGINS[name]
    assert spec.prereq_kind == "binary"


@pytest.mark.parametrize("name", ["mem0", "headroom"])
def test_pip_prereq_plugins_name_a_pip_package(name: str) -> None:
    spec = KNOWN_EXAMPLE_PLUGINS[name]
    assert spec.prereq_kind == "pip"
    assert "pip install" in spec.prereq_detail


def test_mem0_prereq_matches_source_derived_package_name() -> None:
    """mem0's plugin source lazily imports `mem0` (`pip install mem0ai`) --
    see plugins/mem0.py's own docstring/comment citing the exact pip name."""
    assert "mem0ai" in KNOWN_EXAMPLE_PLUGINS["mem0"].prereq_detail


def test_headroom_prereq_matches_source_derived_package_name() -> None:
    """headroom's plugin source lazily imports `headroom` (`pip install
    "headroom-ai[all]"`) -- see plugins/headroom.py's own docstring."""
    assert "headroom-ai" in KNOWN_EXAMPLE_PLUGINS["headroom"].prereq_detail


def test_bitwarden_and_vaultwarden_both_require_the_bw_cli() -> None:
    """Both are structural siblings driving the SAME official Bitwarden `bw`
    CLI (vaultwarden targets a self-hosted server) -- see plugins/bitwarden.py
    / plugins/vaultwarden.py's `_BW_BINARY = "bw"`."""
    assert "bw" in KNOWN_EXAMPLE_PLUGINS["bitwarden"].prereq_detail
    assert "bw" in KNOWN_EXAMPLE_PLUGINS["vaultwarden"].prereq_detail


def test_no_secret_or_sensitive_data_in_any_spec() -> None:
    # Deliberately word-boundary-anchored: "password" alone would false-positive
    # on the legitimate substrings "onepassword"/"1password" (a real plugin name).
    banned = ("api_key", "apikey", "token=", " password ", "secret=")
    for spec in KNOWN_EXAMPLE_PLUGINS.values():
        haystack = f" {' '.join([spec.name, spec.description, spec.prereq_detail])} ".lower()
        for token in banned:
            assert token not in haystack


# ---------------------------------------------------------------------------
# fetch_plugin — curated-name-only guard (THE key security test)
# ---------------------------------------------------------------------------


def test_fetch_plugin_rejects_unknown_name(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown"):
        fetch_plugin("not-a-real-plugin", dest_dir=tmp_path)


def test_fetch_plugin_unknown_name_lists_available(tmp_path: Path) -> None:
    with pytest.raises(ValueError) as excinfo:
        fetch_plugin("not-a-real-plugin", dest_dir=tmp_path)
    for name in KNOWN_EXAMPLE_PLUGINS:
        assert name in str(excinfo.value)


def test_fetch_plugin_never_makes_a_network_call_for_unknown_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mock_get = MagicMock()
    monkeypatch.setattr(requests, "get", mock_get)
    with pytest.raises(ValueError):
        fetch_plugin("not-a-real-plugin", dest_dir=tmp_path)
    mock_get.assert_not_called()


# ---------------------------------------------------------------------------
# fetch_plugin — happy path
# ---------------------------------------------------------------------------


def test_fetch_plugin_writes_response_body_to_dest_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = "def register():\n    return {}\n"
    mock_response.raise_for_status = MagicMock()
    mock_get = MagicMock(return_value=mock_response)
    monkeypatch.setattr(requests, "get", mock_get)

    path = fetch_plugin("rtk", repo="https://example.com/x", ref="main", dest_dir=tmp_path)

    assert path == tmp_path / "rtk.py"
    assert path.read_text(encoding="utf-8") == mock_response.text
    mock_get.assert_called_once()
    args, kwargs = mock_get.call_args
    assert args[0] == "https://example.com/x/main/plugins/rtk.py"
    assert kwargs.get("timeout") is not None


def test_fetch_plugin_uses_configured_repo_and_ref_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from hivepilot.config import settings

    monkeypatch.setattr(settings, "plugins_source_repo", "https://example.com/fork", raising=False)
    monkeypatch.setattr(settings, "plugins_source_ref", "v9", raising=False)

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = "def register():\n    return {}\n"
    mock_response.raise_for_status = MagicMock()
    mock_get = MagicMock(return_value=mock_response)
    monkeypatch.setattr(requests, "get", mock_get)

    fetch_plugin("rtk", dest_dir=tmp_path)

    args, _kwargs = mock_get.call_args
    assert args[0] == "https://example.com/fork/v9/plugins/rtk.py"


def test_fetch_plugin_is_idempotent_overwrites_existing_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "rtk.py").write_text("stale content", encoding="utf-8")

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = "fresh content"
    mock_response.raise_for_status = MagicMock()
    monkeypatch.setattr(requests, "get", MagicMock(return_value=mock_response))

    path = fetch_plugin("rtk", dest_dir=tmp_path)

    assert path.read_text(encoding="utf-8") == "fresh content"


def test_fetch_plugin_creates_dest_dir_if_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dest = tmp_path / "nested" / "plugins"
    assert not dest.exists()

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = "def register():\n    return {}\n"
    mock_response.raise_for_status = MagicMock()
    monkeypatch.setattr(requests, "get", MagicMock(return_value=mock_response))

    path = fetch_plugin("rtk", dest_dir=dest)

    assert path.exists()


def test_fetch_plugin_defaults_to_the_managed_installed_plugins_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from hivepilot.config import settings

    monkeypatch.setattr(type(settings), "xdg_data_home", property(lambda self: tmp_path))

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = "def register():\n    return {}\n"
    mock_response.raise_for_status = MagicMock()
    monkeypatch.setattr(requests, "get", MagicMock(return_value=mock_response))

    path = fetch_plugin("rtk")

    assert path == tmp_path / "plugins" / "rtk.py"


# ---------------------------------------------------------------------------
# fetch_plugin — network/HTTP failure handling
# ---------------------------------------------------------------------------


def test_fetch_plugin_timeout_raises_runtime_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(requests, "get", MagicMock(side_effect=requests.Timeout()))
    with pytest.raises(RuntimeError, match="timed out"):
        fetch_plugin("rtk", dest_dir=tmp_path)


def test_fetch_plugin_connection_error_raises_runtime_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(requests, "get", MagicMock(side_effect=requests.ConnectionError("boom")))
    with pytest.raises(RuntimeError):
        fetch_plugin("rtk", dest_dir=tmp_path)


def test_fetch_plugin_http_error_status_raises_runtime_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mock_response = MagicMock()
    mock_response.status_code = 404
    mock_response.raise_for_status = MagicMock(side_effect=requests.HTTPError("404 Client Error"))
    monkeypatch.setattr(requests, "get", MagicMock(return_value=mock_response))
    with pytest.raises(RuntimeError):
        fetch_plugin("rtk", dest_dir=tmp_path)


def test_fetch_plugin_never_execs_the_fetched_content(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The fetched body is malicious-looking Python -- must be written
    verbatim to disk and NEVER imported/exec'd by fetch_plugin itself."""
    malicious = "import os\nos.environ['PWNED'] = '1'\n"
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = malicious
    mock_response.raise_for_status = MagicMock()
    monkeypatch.setattr(requests, "get", MagicMock(return_value=mock_response))

    path = fetch_plugin("rtk", dest_dir=tmp_path)

    assert path.read_text(encoding="utf-8") == malicious
    import os as _os

    assert "PWNED" not in _os.environ


# ---------------------------------------------------------------------------
# installed_plugins_dir / is_installed
# ---------------------------------------------------------------------------


def test_installed_plugins_dir_matches_xdg_data_home(monkeypatch: pytest.MonkeyPatch) -> None:
    from hivepilot.config import settings

    monkeypatch.setattr(
        type(settings), "xdg_data_home", property(lambda self: Path("/tmp/x-data-home"))
    )
    assert installed_plugins_dir() == Path("/tmp/x-data-home/plugins")


def test_is_installed_false_when_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from hivepilot.config import settings

    monkeypatch.setattr(type(settings), "xdg_data_home", property(lambda self: tmp_path))
    assert is_installed("rtk") is False


def test_is_installed_true_when_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from hivepilot.config import settings

    monkeypatch.setattr(type(settings), "xdg_data_home", property(lambda self: tmp_path))
    (tmp_path / "plugins").mkdir()
    (tmp_path / "plugins" / "rtk.py").write_text("x", encoding="utf-8")
    assert is_installed("rtk") is True


# ---------------------------------------------------------------------------
# is_enabled
# ---------------------------------------------------------------------------


def test_is_enabled_reflects_settings_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    from hivepilot.config import settings

    monkeypatch.setattr(settings, "rtk_enabled", False, raising=False)
    assert is_enabled("rtk") is False
    monkeypatch.setattr(settings, "rtk_enabled", True, raising=False)
    assert is_enabled("rtk") is True


# ---------------------------------------------------------------------------
# persist_enabled — reuses the SAME .env upsert mechanism as
# `hivepilot.ui.plugin_persist.persist_plugins_disabled`
# ---------------------------------------------------------------------------


def test_persist_enabled_appends_new_line(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    result_path = persist_enabled("rtk", env_path=env_path)

    assert result_path == env_path
    content = env_path.read_text(encoding="utf-8")
    assert "HIVEPILOT_RTK_ENABLED=true" in content


def test_persist_enabled_preserves_other_lines(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("HIVEPILOT_OTHER_SETTING=xyz\n", encoding="utf-8")

    persist_enabled("rtk", env_path=env_path)

    content = env_path.read_text(encoding="utf-8")
    assert "HIVEPILOT_OTHER_SETTING=xyz" in content
    assert "HIVEPILOT_RTK_ENABLED=true" in content


def test_persist_enabled_upserts_existing_line_in_place(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "HIVEPILOT_A=1\nHIVEPILOT_RTK_ENABLED=false\nHIVEPILOT_B=2\n", encoding="utf-8"
    )

    persist_enabled("rtk", env_path=env_path)

    lines = env_path.read_text(encoding="utf-8").splitlines()
    assert lines.count("HIVEPILOT_RTK_ENABLED=true") == 1
    assert lines[0] == "HIVEPILOT_A=1"
    assert lines[-1] == "HIVEPILOT_B=2"


def test_persist_enabled_creates_parent_dirs(tmp_path: Path) -> None:
    env_path = tmp_path / "nested" / ".env"
    persist_enabled("rtk", env_path=env_path)
    assert env_path.exists()


def test_persist_enabled_rejects_unknown_name(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown"):
        persist_enabled("not-a-real-plugin", env_path=tmp_path / ".env")


def test_persist_enabled_default_env_path_matches_settings_env_file() -> None:
    """Mirrors `hivepilot.ui.plugin_persist.persist_plugins_disabled`'s
    default env_path resolution exactly: the SAME dotenv file `Settings`
    itself reads overrides from."""
    from hivepilot.config import Settings

    expected = Path(str(Settings.model_config.get("env_file") or ".env"))
    assert plugin_installer._default_env_path() == expected


def test_persist_enabled_json_disabled_flag_untouched(tmp_path: Path) -> None:
    """`persist_enabled` must only ever touch its own `<STEM>_ENABLED` line
    -- never the unrelated `HIVEPILOT_PLUGINS_DISABLED` JSON-list line the
    TUI plugin manager owns."""
    env_path = tmp_path / ".env"
    env_path.write_text(f"HIVEPILOT_PLUGINS_DISABLED={json.dumps(['other'])}\n", encoding="utf-8")

    persist_enabled("rtk", env_path=env_path)

    content = env_path.read_text(encoding="utf-8")
    assert f"HIVEPILOT_PLUGINS_DISABLED={json.dumps(['other'])}" in content
