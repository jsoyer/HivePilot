"""HP-77: declarative, shareable plugin packs.

A pack is YAML (plugins + config refs + credential *names* + host/capability
guards). Installing one is a loop over the existing curated
``plugin_installer.fetch_plugin`` / ``persist_enabled`` path — never an
arbitrary URL, never plugin code from the hub.

The optional hub (``HIVEPILOT_PLUGIN_PACKS_INDEX_URL``) is metadata only,
same trust model as ``plugin_index``.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import requests
import yaml

from hivepilot import __version__ as HIVEPILOT_VERSION
from hivepilot.config import settings
from hivepilot.plugin_capabilities import PLUGIN_CAPABILITIES
from hivepilot.services import plugin_installer
from hivepilot.utils.terminal import strip_control_chars

_SAFE_NAME = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
_ENV_REF = re.compile(r"^\$\{env:[A-Za-z_][A-Za-z0-9_]*\}$")
_SECRET_REF = re.compile(r"^\$\{secret:[A-Za-z0-9_.\-]+\}$")
_SECRET_KEY = re.compile(r"(token|secret|password|api_key|apikey)", re.IGNORECASE)
_HIVEPILOT_ENV = re.compile(r"^HIVEPILOT_[A-Z][A-Z0-9_]*$")
MAX_PACK_BYTES = 64 * 1024
MAX_INDEX_BYTES = 512 * 1024
_STREAM_CHUNK = 65536


class PackError(ValueError):
    """The pack manifest is invalid or cannot be applied."""


@dataclass
class PackPlugin:
    name: str
    enable: bool = True


@dataclass
class PluginPack:
    name: str
    version: str
    description: str
    plugins: list[PackPlugin]
    min_hivepilot: str | None = None
    platforms: list[str] = field(default_factory=list)
    config: dict[str, str] = field(default_factory=dict)
    credentials: list[str] = field(default_factory=list)
    capabilities: list[str] = field(default_factory=list)
    source: str = "bundled"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def bundled_packs_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "bundled_packs"


def imported_packs_dir() -> Path:
    return settings.xdg_data_home / "plugin_packs"


def parse_pack(text: str, *, source: str = "import") -> PluginPack:
    blob = (text or "").strip()
    if not blob:
        raise PackError("empty pack manifest")
    if len(blob.encode("utf-8")) > MAX_PACK_BYTES:
        raise PackError(f"pack exceeds {MAX_PACK_BYTES} bytes")
    try:
        data = yaml.safe_load(blob)
    except yaml.YAMLError as exc:
        raise PackError(f"invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise PackError("pack must be a YAML/JSON object")
    return _pack_from_mapping(data, source=source)


def _pack_from_mapping(data: dict[str, Any], *, source: str) -> PluginPack:
    raw_name = data.get("name") or (data.get("metadata") or {}).get("name")
    name = strip_control_chars(str(raw_name or "")).strip().lower()
    if not _SAFE_NAME.fullmatch(name):
        raise PackError("pack name must be a lowercase slug (a-z, digits, ._-)")
    version = strip_control_chars(str(data.get("version") or "0.0.0"))
    description = strip_control_chars(str(data.get("description") or ""))
    host = data.get("host") if isinstance(data.get("host"), dict) else {}
    min_hp = host.get("min_hivepilot") or data.get("min_hivepilot")
    platforms_raw = host.get("platforms") or host.get("platform") or data.get("platforms") or []
    if isinstance(platforms_raw, str):
        platforms_raw = [platforms_raw]
    platforms = [strip_control_chars(str(p)).lower() for p in platforms_raw if str(p).strip()]
    plugins = _parse_plugins(data.get("plugins"))
    config = _parse_config(data.get("config") or data.get("env") or {})
    credentials = _parse_credentials(data.get("credentials"))
    capabilities = _parse_capabilities(data.get("capabilities"))
    return PluginPack(
        name=name,
        version=version,
        description=description,
        plugins=plugins,
        min_hivepilot=str(min_hp) if min_hp else None,
        platforms=platforms,
        config=config,
        credentials=credentials,
        capabilities=capabilities,
        source=source,
    )


def _parse_plugins(raw: Any) -> list[PackPlugin]:
    if not isinstance(raw, list) or not raw:
        raise PackError("pack must list at least one plugin")
    out: list[PackPlugin] = []
    seen: set[str] = set()
    for item in raw:
        if isinstance(item, str):
            name, enable = item, True
        elif isinstance(item, dict) and item.get("name"):
            name, enable = str(item["name"]), bool(item.get("enable", True))
        else:
            raise PackError("each plugin must be a name or {name, enable}")
        name = strip_control_chars(name).strip()
        if name in plugin_installer.AGENT_CLI_PLUGINS:
            raise PackError(f"plugin {name!r} is an agent CLI — use `hivepilot agents install`")
        if name in plugin_installer.DEMO_PLUGINS:
            raise PackError(f"plugin {name!r} is a demo showcase, not installable")
        if name not in plugin_installer.KNOWN_EXAMPLE_PLUGINS:
            raise PackError(f"unknown curated plugin {name!r}")
        if name in seen:
            raise PackError(f"duplicate plugin {name!r}")
        seen.add(name)
        out.append(PackPlugin(name=name, enable=enable))
    return out


def _parse_config(raw: Any) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise PackError("config must be a map of env vars")
    out: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise PackError("config keys and values must be strings")
        key = strip_control_chars(key).strip()
        value = strip_control_chars(value).strip()
        if not _HIVEPILOT_ENV.fullmatch(key):
            raise PackError(f"config key {key!r} must be a HIVEPILOT_* env name")
        if _SECRET_KEY.search(key) and not (
            _ENV_REF.fullmatch(value) or _SECRET_REF.fullmatch(value)
        ):
            raise PackError(f"{key} must be a ${{env:}} or ${{secret:}} ref, not a literal")
        out[key] = value
    return out


def _parse_credentials(raw: Any) -> list[str]:
    if raw is None:
        return []
    names: list[Any]
    if isinstance(raw, dict) and isinstance(raw.get("refs"), list):
        names = raw["refs"]
    elif isinstance(raw, list):
        names = raw
    else:
        raise PackError("credentials must be a list of env names")
    out: list[str] = []
    for name in names:
        if not isinstance(name, str) or not _HIVEPILOT_ENV.fullmatch(name):
            raise PackError(f"credential ref {name!r} must be a HIVEPILOT_* name")
        out.append(name)
    return out


def _parse_capabilities(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, dict):
        raw = raw.get("required") or []
    if not isinstance(raw, list):
        raise PackError("capabilities must be a list")
    allowed = set(PLUGIN_CAPABILITIES)
    out: list[str] = []
    for item in raw:
        token = strip_control_chars(str(item)).strip()
        if token not in allowed:
            raise PackError(f"unknown capability {token!r}")
        if token not in out:
            out.append(token)
    return out


def load_pack_file(path: Path, *, source: str) -> PluginPack:
    return parse_pack(path.read_text(encoding="utf-8"), source=source)


def list_packs() -> list[PluginPack]:
    found: dict[str, PluginPack] = {}
    bundled = bundled_packs_dir()
    if bundled.is_dir():
        for path in sorted(bundled.glob("*.yaml")):
            pack = load_pack_file(path, source="bundled")
            found[pack.name] = pack
    imported = imported_packs_dir()
    if imported.is_dir():
        for path in sorted(imported.glob("*.yaml")):
            pack = load_pack_file(path, source="import")
            found[pack.name] = pack
    return [found[name] for name in sorted(found)]


def get_pack(name: str) -> PluginPack | None:
    slug = (name or "").strip().lower()
    return next((pack for pack in list_packs() if pack.name == slug), None)


def export_pack(name: str) -> str:
    pack = get_pack(name)
    if pack is None:
        raise PackError(f"unknown pack {name!r}")
    payload = {
        "name": pack.name,
        "version": pack.version,
        "description": pack.description,
        "host": {"min_hivepilot": pack.min_hivepilot, "platforms": pack.platforms},
        "plugins": [{"name": p.name, "enable": p.enable} for p in pack.plugins],
        "config": pack.config,
        "credentials": pack.credentials,
        "capabilities": pack.capabilities,
    }
    return yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)


def save_imported_pack(pack: PluginPack, text: str) -> Path:
    dest = imported_packs_dir()
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / f"{pack.name}.yaml"
    path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    return path


def _version_tuple(raw: str) -> tuple[int, ...]:
    parts: list[int] = []
    for token in re.split(r"[^0-9]+", (raw or "").strip()):
        if token:
            parts.append(int(token))
    return tuple(parts or (0,))


def preview_pack(pack: PluginPack) -> dict[str, Any]:
    """Host-compat + permission preview. Never writes."""
    blockers: list[str] = []
    warnings: list[str] = []
    if pack.min_hivepilot and _version_tuple(HIVEPILOT_VERSION) < _version_tuple(
        pack.min_hivepilot
    ):
        blockers.append(
            f"host HivePilot {HIVEPILOT_VERSION} is older than pack minimum {pack.min_hivepilot}"
        )
    if pack.platforms and sys.platform not in pack.platforms:
        blockers.append(f"host platform {sys.platform} is not in {pack.platforms}")
    policy = set(settings.plugins_capability_policy or [])
    blocked_caps = [cap for cap in pack.capabilities if cap not in policy]
    if blocked_caps:
        warnings.append("capability policy does not currently allow: " + ", ".join(blocked_caps))
    missing_creds = [name for name in pack.credentials if not (os.environ.get(name) or "").strip()]
    if missing_creds:
        warnings.append("credential env vars not set: " + ", ".join(missing_creds))
    plugins: list[dict[str, Any]] = []
    for item in pack.plugins:
        spec = plugin_installer.KNOWN_EXAMPLE_PLUGINS[item.name]
        plugins.append(
            {
                "name": item.name,
                "enable": item.enable,
                "installed": plugin_installer.is_installed(item.name),
                "enabled": plugin_installer.is_enabled(item.name),
                "prereq_kind": spec.prereq_kind,
                "prereq_detail": spec.prereq_detail,
            }
        )
    return {
        "pack": pack.to_dict(),
        "compatible": not blockers,
        "blockers": blockers,
        "warnings": warnings,
        "capability_blocked": blocked_caps,
        "missing_credentials": missing_creds,
        "plugins": plugins,
    }


def persist_config_line(key: str, value: str, *, env_path: Path | None = None) -> Path:
    """Upsert a non-secret config literal into the Settings dotenv file."""
    from hivepilot.services.plugin_installer import _default_env_path

    resolved = env_path if env_path is not None else _default_env_path()
    line = f"{key}={value}"
    lines: list[str] = []
    if resolved.exists():
        lines = resolved.read_text(encoding="utf-8").splitlines()
    for index, existing in enumerate(lines):
        if existing.startswith(f"{key}="):
            lines[index] = line
            break
    else:
        lines.append(line)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return resolved


def install_pack(
    name: str,
    *,
    consent: bool,
    dest_dir: Path | None = None,
    env_path: Path | None = None,
    fetch=None,
) -> dict[str, Any]:
    """Apply a pack. ``consent`` is the install-time permission signature."""
    if consent is not True:
        raise PackError("consent is required to install a plugin pack")
    pack = get_pack(name)
    if pack is None:
        raise PackError(f"unknown pack {name!r}")
    preview = preview_pack(pack)
    if not preview["compatible"]:
        raise PackError("; ".join(preview["blockers"]))
    do_fetch = fetch or plugin_installer.fetch_plugin
    installed: list[dict[str, Any]] = []
    for item in pack.plugins:
        path = do_fetch(item.name, dest_dir=dest_dir)
        if item.enable:
            plugin_installer.persist_enabled(item.name, env_path=env_path)
        spec = plugin_installer.KNOWN_EXAMPLE_PLUGINS[item.name]
        installed.append(
            {
                "name": item.name,
                "installed_to": str(path),
                "enabled": item.enable,
                "prereq_detail": spec.prereq_detail,
            }
        )
    for key, value in pack.config.items():
        if _ENV_REF.fullmatch(value) or _SECRET_REF.fullmatch(value):
            continue
        persist_config_line(key, value, env_path=env_path)
    return {
        "pack": pack.name,
        "version": pack.version,
        "installed": installed,
        "restart_required": True,
        "warnings": preview["warnings"],
        "capability_blocked": preview["capability_blocked"],
        "missing_credentials": preview["missing_credentials"],
    }


def fetch_hub_packs(*, url: str | None = None, timeout: int = 10) -> list[PluginPack]:
    """Metadata-only hub fetch. Never downloads plugin code."""
    target = (url if url is not None else settings.plugin_packs_index_url).strip()
    if not target:
        raise PackError("HIVEPILOT_PLUGIN_PACKS_INDEX_URL is not set")
    try:
        response = requests.get(target, timeout=timeout, stream=True)
    except requests.RequestException as exc:
        raise PackError(f"pack hub fetch failed: {type(exc).__name__}") from exc
    try:
        response.raise_for_status()
        body = bytearray()
        for chunk in response.iter_content(chunk_size=_STREAM_CHUNK):
            body.extend(chunk)
            if len(body) > MAX_INDEX_BYTES:
                raise PackError(f"pack hub index exceeds {MAX_INDEX_BYTES} bytes")
    except requests.HTTPError as exc:
        raise PackError(f"pack hub returned HTTP {response.status_code}") from exc
    finally:
        response.close()
    try:
        data = yaml.safe_load(bytes(body).decode("utf-8"))
    except (ValueError, yaml.YAMLError) as exc:
        raise PackError("pack hub index is not valid JSON/YAML") from exc
    raw_packs = data.get("packs") if isinstance(data, dict) else data
    if not isinstance(raw_packs, list):
        raise PackError("pack hub index must be a list or {packs: [...]}")
    packs: list[PluginPack] = []
    for item in raw_packs:
        if not isinstance(item, dict):
            continue
        manifest = item.get("manifest") if isinstance(item.get("manifest"), dict) else item
        try:
            packs.append(_pack_from_mapping(manifest, source="hub"))
        except PackError:
            continue
    return packs
