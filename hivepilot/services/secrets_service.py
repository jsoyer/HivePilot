from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Dict

import yaml

from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)


class SecretResolver:
    def __init__(self) -> None:
        self.resolvers: dict[str, Any] = {
            "env": self._from_env,
            "file": self._from_file,
            "vault": self._from_vault,
            "sops": self._from_sops,
        }

    def resolve(self, config: Dict[str, Any]) -> dict[str, str]:
        resolved: dict[str, str] = {}
        for name, spec in config.items():
            source = spec.get("source", "env")
            resolver = self.resolvers.get(source)
            if not resolver:
                raise ValueError(f"Unknown secret source: {source}")
            resolved[name] = resolver(spec)
        return resolved

    def _from_env(self, spec: Dict[str, Any]) -> str:
        key = spec["key"]
        value = os.environ.get(key)
        if not value:
            raise RuntimeError(f"Environment variable {key} not set for secret")
        return value

    def _from_file(self, spec: Dict[str, Any]) -> str:
        path = Path(spec["path"])
        return path.read_text(encoding="utf-8").strip()

    def _from_vault(self, spec: Dict[str, Any]) -> str:
        """Resolve a secret from HashiCorp Vault (KV v2).

        spec must contain:
          path: KV v2 path, e.g. "secret/data/myapp"
          key:  key within the data dict
        """
        try:
            import hvac  # type: ignore[import-untyped]
        except ImportError as exc:
            raise ImportError("hvac not installed; run: pip install hvac") from exc

        from hivepilot.config import settings

        vault_addr = settings.vault_addr or os.environ.get("HIVEPILOT_VAULT_ADDR")
        vault_token = settings.vault_token or os.environ.get("HIVEPILOT_VAULT_TOKEN")

        if not vault_addr or not vault_token:
            raise ValueError(
                "Vault is not configured: set HIVEPILOT_VAULT_ADDR and "
                "HIVEPILOT_VAULT_TOKEN environment variables (or vault_addr / "
                "vault_token in settings)."
            )

        client = hvac.Client(url=vault_addr, token=vault_token)
        path: str = spec["path"]
        key: str = spec["key"]

        response = client.secrets.kv.v2.read_secret_version(path=path)
        data: dict[str, Any] = response["data"]["data"]
        return data[key]

    def _from_sops(self, spec: Dict[str, Any]) -> str:
        """Resolve a secret by decrypting a SOPS-encrypted file.

        spec must contain:
          file: path to the SOPS-encrypted YAML or JSON file
          key:  top-level key to extract from the decrypted content
        """
        import shutil

        if not shutil.which("sops"):
            raise RuntimeError(
                "sops binary not found in PATH; install it from https://github.com/getsops/sops"
            )

        file_path = spec["file"]
        key: str = spec["key"]

        result = subprocess.run(
            ["sops", "-d", file_path],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"sops decryption failed for {file_path!r}: {result.stderr.strip()}")

        # Try YAML first, then JSON
        try:
            data: dict[str, Any] = yaml.safe_load(result.stdout)
        except yaml.YAMLError:
            data = json.loads(result.stdout)

        if key not in data:
            raise KeyError(f"Key {key!r} not found in decrypted sops file {file_path!r}")

        return str(data[key])


secret_resolver = SecretResolver()
