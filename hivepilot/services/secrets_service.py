from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict

from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)


class SecretResolver:
    def __init__(self) -> None:
        self.resolvers = {
            "env": self._from_env,
            "file": self._from_file,
        }

    def resolve(self, config: Dict[str, Any]) -> dict[str, str]:
        secrets = {}
        for name, spec in config.items():
            source = spec.get("source", "env")
            resolver = self.resolvers.get(source)
            if not resolver:
                raise ValueError(f"Unknown secret source: {source}")
            secrets[name] = resolver(spec)
        return secrets

    def _from_env(self, spec: Dict[str, Any]) -> str:
        key = spec["key"]
        value = os.environ.get(key)
        if not value:
            raise RuntimeError(f"Environment variable {key} not set for secret")
        return value

    def _from_file(self, spec: Dict[str, Any]) -> str:
        path = Path(spec["path"])
        return path.read_text(encoding="utf-8").strip()


secret_resolver = SecretResolver()
