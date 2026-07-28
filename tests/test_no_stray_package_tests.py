"""Guard against `test_*.py` files landing inside the shipped `hivepilot`
package.

Root cause (fixed at the source, but the residue shipped for a while): a
TDD hook (`check-test-exists.sh`) resolved its project root to the wrong
checkout inside isolated git worktrees, so it falsely reported "no test
file exists" for legitimately test-first work. Agents worked around it by
creating same-directory companion `test_*.py` stubs next to the module
being edited -- which land inside the published package, since
`[tool.setuptools.packages.find]` has no per-file exclusion and simply
ships every `.py` file under an included package directory.

The hook is fixed now, so this should not recur -- but nothing else in the
build enforces it, and `pyproject.toml`'s `testpaths = ["tests"]` means a
regression here is invisible to a normal `pytest` run (the stray file is
silently never collected, so CI stays green while the wheel quietly grows
dead weight). This test is the belt-and-braces guard: it fails loudly, in
the real suite, the moment a `test_*.py` file reappears anywhere under
`hivepilot/`.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HIVEPILOT_PKG = REPO_ROOT / "hivepilot"


def test_no_test_files_inside_the_shipped_package() -> None:
    stray = sorted(str(p.relative_to(REPO_ROOT)) for p in HIVEPILOT_PKG.rglob("test_*.py"))
    assert stray == [], (
        "Found test_*.py file(s) inside the shipped hivepilot/ package "
        f"(they belong in tests/ instead): {stray}"
    )
