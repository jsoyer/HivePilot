"""Sandbox utilities for the autonomous developer runner.

Provides two building blocks:
- scrub_env: strip the inherited process environment down to an explicit
  allowlist before passing it to an elevated-permission subprocess.
- wrap_bwrap: prefix an argv with bubblewrap (bwrap) confinement so the
  subprocess can only write to the worktree (and a few cache dirs).

Both functions are pure — they build argument lists / dicts without
executing anything. sandbox application happens in claude_runner.py.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import shutil

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default environment allowlist
# ---------------------------------------------------------------------------

# Exact names and glob-style patterns.
# Any env var whose key matches at least one entry is kept; the rest are
# dropped.  Entries without wildcards are treated as exact matches for speed;
# entries with '*' are matched with fnmatch.
DEFAULT_ALLOWLIST: list[str] = [
    # Shell / locale basics
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "SHELL",
    "TMPDIR",
    # XDG
    "XDG_CONFIG_HOME",
    "XDG_CACHE_HOME",
    "XDG_DATA_HOME",
    # LLM API keys
    "ANTHROPIC_API_KEY",
    "CLAUDE_*",
    "OPENAI_API_KEY",
    "GOOGLE_API_KEY",
    "MISTRAL_API_KEY",
    # Node / Go build caches (needed inside the worktree)
    "NODE_*",
    "GOPATH",
    "GOCACHE",
]


def scrub_env(env: dict[str, str], allowlist: list[str] | None = None) -> dict[str, str]:
    """Return a copy of *env* keeping only keys that match the *allowlist*.

    Matching rules (evaluated left-to-right, first match wins):
    - entries containing ``*`` → fnmatch glob on the key
    - all other entries → case-sensitive exact equality

    Args:
        env: source environment (usually ``os.environ.copy()``).
        allowlist: list of exact names and/or ``fnmatch`` patterns.  Defaults
            to :data:`DEFAULT_ALLOWLIST`.

    Returns:
        A new ``dict[str, str]`` containing only the allowed keys.
    """
    if allowlist is None:
        allowlist = DEFAULT_ALLOWLIST

    # Split into exact names and patterns once so the inner loop stays O(n).
    exact: set[str] = set()
    patterns: list[str] = []
    for entry in allowlist:
        if "*" in entry:
            patterns.append(entry)
        else:
            exact.add(entry)

    result: dict[str, str] = {}
    for key, value in env.items():
        if key in exact:
            result[key] = value
            continue
        for pat in patterns:
            if fnmatch.fnmatchcase(key, pat):
                result[key] = value
                break

    return result


# ---------------------------------------------------------------------------
# bwrap wrapper
# ---------------------------------------------------------------------------


def wrap_bwrap(argv: list[str], *, workdir: str) -> list[str]:
    """Prefix *argv* with a bubblewrap (bwrap) confinement command.

    The sandbox is intentionally permissive on the network (no
    ``--unshare-net``) because the subprocess needs to reach LLM API
    endpoints.  Filesystem confinement is:

    - whole FS read-only via ``--ro-bind / /``
    - worktree read-write via ``--bind <workdir> <workdir>``
    - /tmp, ~/.cache, ~/.claude read-write (build caches + claude state)
    - ~/.ssh, ~/.aws, ~/.gnupg masked with a fresh tmpfs (not readable)

    If ``bwrap`` is not on PATH, returns *argv* unchanged and logs a warning.

    Args:
        argv: the original subprocess argv to wrap.
        workdir: absolute path to the git worktree.  This is the ONLY
            directory where the subprocess may write to the real FS.

    Returns:
        A new list starting with ``["bwrap", ...]`` + *argv*, or the
        original *argv* if bwrap is unavailable.
    """
    if shutil.which("bwrap") is None:
        logger.warning(
            "sandbox.bwrap_unavailable: bwrap not found on PATH — "
            "running WITHOUT filesystem confinement"
        )
        return argv

    home = os.path.expanduser("~")

    # Ensure writable dirs exist before binding them (bwrap fails if the target
    # path is missing on the host).
    cache_dir = os.path.join(home, ".cache")
    claude_dir = os.path.join(home, ".claude")
    os.makedirs(cache_dir, exist_ok=True)
    os.makedirs(claude_dir, exist_ok=True)

    bwrap_args: list[str] = [
        "bwrap",
        # Whole FS readable so toolchain binaries work unchanged.
        "--ro-bind",
        "/",
        "/",
        # Standard pseudo-filesystems.
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        # The worktree is the only broad writable area.
        "--bind",
        workdir,
        workdir,
        # /tmp writable (many tools write there).
        "--bind",
        "/tmp",
        "/tmp",
        # Build caches writable (npm, pip, cargo, …).
        "--bind",
        cache_dir,
        cache_dir,
        # Claude stores its local state (session files, etc.) here.
        "--bind",
        claude_dir,
        claude_dir,
        # Mask credential dirs — they become empty tmpfs inside the sandbox.
        "--tmpfs",
        os.path.join(home, ".ssh"),
        "--tmpfs",
        os.path.join(home, ".aws"),
        "--tmpfs",
        os.path.join(home, ".gnupg"),
        # Start inside the worktree.
        "--chdir",
        workdir,
    ]

    return bwrap_args + argv
