from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from hivepilot.config import settings


def create_run_directory() -> Path:
    timestamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    run_dir = settings.resolve_path(settings.runs_dir / timestamp)
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def write_summary(run_dir: Path, summary: dict[str, Any]) -> None:
    path = run_dir / f"summary.{settings.output_format}"
    if settings.output_format == "json":
        path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    else:
        lines = [f"{key}: {value}" for key, value in summary.items()]
        path.write_text("\n".join(lines), encoding="utf-8")
