from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

import boto3  # type: ignore

from hivepilot.config import settings
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)


class ArtifactManager:
    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.artifacts_dir = run_dir / "artifacts"
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)

    def write_file(self, name: str, content: str) -> Path:
        path = self.artifacts_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        logger.info("artifact.write", path=str(path))
        return path

    def write_json(self, name: str, data: Any) -> Path:
        return self.write_file(name, json.dumps(data, indent=2))

    def export(self, exporters: Iterable[dict[str, Any]]) -> None:
        for exporter in exporters:
            target = exporter.get("target")
            if target == "s3":
                self._export_s3(exporter)
            elif target == "local":
                # Already local
                continue
            else:
                logger.warning("artifact.unknown_exporter", target=target)

    def _export_s3(self, config: dict[str, Any]) -> None:
        bucket = config["bucket"]
        prefix = config.get("prefix", f"runs/{self.run_dir.name}")
        session = boto3.session.Session()
        client = session.client(
            "s3",
            aws_access_key_id=config.get("aws_access_key_id"),
            aws_secret_access_key=config.get("aws_secret_access_key"),
            region_name=config.get("aws_region"),
        )
        for file in self.artifacts_dir.rglob("*"):
            if file.is_file():
                key = f"{prefix}/{file.relative_to(self.artifacts_dir)}"
                client.upload_file(str(file), bucket, key)
                logger.info("artifact.export.s3", bucket=bucket, key=key)
