"""This-host resource snapshot for Pollen (HP-68 slice 1).

Reads Linux ``/proc`` plus ``shutil.disk_usage``. Never invents a fleet
or a "servers" count — those need a real inventory. Missing ``/proc``
(macOS, a container without procfs) returns ``available: false`` with a
reason, not a fabricated percentage.

CPU percent is a short two-sample ``/proc/stat`` window (default 80 ms),
not a 1-minute load average dressed up as utilization.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Any, Callable

from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

_PROC_MEMINFO = Path("/proc/meminfo")
_PROC_STAT = Path("/proc/stat")
_DISK_PATH = "/"
_CPU_SAMPLE_SECONDS = 0.08

_HOST_NOTE = (
    "Readings are from this HivePilot host, not a fleet. There is no server inventory here."
)
_UNAVAILABLE_NOTE = (
    "Host metrics need a readable /proc (Linux). This host has no snapshot — nothing is invented."
)


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.info("host_resources.read_failed", path=str(path), error=str(exc))
        return None


def parse_meminfo(text: str) -> dict[str, int] | None:
    """``used_bytes`` / ``total_bytes`` from a ``/proc/meminfo`` dump.

    Used is ``MemTotal - MemAvailable`` (the kernel's own "what is left
    for new work" figure), never ``MemTotal - MemFree`` which ignores
    reclaimable cache and would overstate pressure.
    """
    total_kb: int | None = None
    avail_kb: int | None = None
    for line in text.splitlines():
        if line.startswith("MemTotal:"):
            total_kb = int(line.split()[1])
        elif line.startswith("MemAvailable:"):
            avail_kb = int(line.split()[1])
        if total_kb is not None and avail_kb is not None:
            break
    if total_kb is None or avail_kb is None or total_kb <= 0:
        return None
    total = total_kb * 1024
    used = max(0, total - avail_kb * 1024)
    return {"used_bytes": used, "total_bytes": total}


def parse_cpu_times(text: str) -> tuple[int, int] | None:
    """Return ``(idle, total)`` jiffies from the aggregate ``cpu `` line."""
    for line in text.splitlines():
        if not line.startswith("cpu "):
            continue
        parts = line.split()
        if len(parts) < 5:
            return None
        nums = [int(p) for p in parts[1:]]
        idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
        total = sum(nums[:8]) if len(nums) >= 8 else sum(nums)
        if total <= 0:
            return None
        return idle, total
    return None


def cpu_used_pct(before: str, after: str) -> float | None:
    first = parse_cpu_times(before)
    second = parse_cpu_times(after)
    if first is None or second is None:
        return None
    idle_delta = second[0] - first[0]
    total_delta = second[1] - first[1]
    if total_delta <= 0:
        return None
    busy = 1.0 - (idle_delta / total_delta)
    return max(0.0, min(100.0, busy * 100.0))


def snapshot(
    *,
    meminfo_text: str | None = None,
    stat_samples: tuple[str, str] | None = None,
    disk_usage: tuple[int, int] | None = None,
    nproc: int | None = None,
    read_text: Callable[[Path], str | None] = _read_text,
    sleep: Callable[[float], None] = time.sleep,
    cpu_sample_seconds: float = _CPU_SAMPLE_SECONDS,
    disk_path: str = _DISK_PATH,
) -> dict[str, Any]:
    """Return a this-host snapshot. Injected args are for tests only."""
    ram = _ram_block(meminfo_text, read_text)
    cpu = _cpu_block(stat_samples, nproc, read_text, sleep, cpu_sample_seconds)
    disk = _disk_block(disk_usage, disk_path)
    available = ram is not None or cpu is not None or disk is not None
    return {
        "available": available,
        "source": "procfs" if available else None,
        "ram": ram,
        "cpu": cpu,
        "disk": disk,
        "note": _HOST_NOTE if available else _UNAVAILABLE_NOTE,
    }


def _ram_block(
    meminfo_text: str | None,
    read_text: Callable[[Path], str | None],
) -> dict[str, Any] | None:
    text = meminfo_text if meminfo_text is not None else read_text(_PROC_MEMINFO)
    if not text:
        return None
    parsed = parse_meminfo(text)
    if parsed is None:
        return None
    total = parsed["total_bytes"]
    used = parsed["used_bytes"]
    return {
        "used_bytes": used,
        "total_bytes": total,
        "used_pct": round(100.0 * used / total, 1) if total else None,
    }


def _cpu_block(
    stat_samples: tuple[str, str] | None,
    nproc: int | None,
    read_text: Callable[[Path], str | None],
    sleep: Callable[[float], None],
    cpu_sample_seconds: float,
) -> dict[str, Any] | None:
    cores = nproc if nproc is not None else os.cpu_count()
    if stat_samples is not None:
        before, after = stat_samples
    else:
        before = read_text(_PROC_STAT)
        if not before:
            return None
        if cpu_sample_seconds > 0:
            sleep(cpu_sample_seconds)
        after = read_text(_PROC_STAT)
        if not after:
            return None
    pct = cpu_used_pct(before, after)
    if pct is None:
        return None
    return {
        "used_pct": round(pct, 1),
        "nproc": cores,
    }


def _disk_block(
    disk_usage: tuple[int, int] | None,
    disk_path: str,
) -> dict[str, Any] | None:
    if disk_usage is not None:
        total, used = disk_usage
    else:
        try:
            usage = shutil.disk_usage(disk_path)
        except OSError as exc:
            logger.info("host_resources.disk_failed", path=disk_path, error=str(exc))
            return None
        total, used = usage.total, usage.used
    if total <= 0:
        return None
    return {
        "used_bytes": used,
        "total_bytes": total,
        "used_pct": round(100.0 * used / total, 1),
        "path": disk_path,
    }
