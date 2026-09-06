#!/usr/bin/env python3
"""Write the curated HP-64 OpenAPI contract to `web/openapi.json`.

Usage:
    python scripts/export_openapi.py
    python scripts/export_openapi.py --check   # exit 1 if committed file is stale
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "web" / "openapi.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if web/openapi.json does not match the live FastAPI schema",
    )
    args = parser.parse_args()

    from hivepilot.services.openapi_contract import dumps_contract

    rendered = dumps_contract()
    if args.check:
        current = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""
        if current != rendered:
            print(
                "web/openapi.json is stale. Run `python scripts/export_openapi.py`.",
                file=sys.stderr,
            )
            return 1
        print("web/openapi.json matches the live contract")
        return 0

    TARGET.write_text(rendered, encoding="utf-8")
    print(f"wrote {TARGET.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
