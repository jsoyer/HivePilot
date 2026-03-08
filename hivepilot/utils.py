from __future__ import annotations

import os
from pathlib import Path


def expand_path(path: Path) -> Path:
    return Path(os.path.expandvars(str(path))).expanduser().resolve()
