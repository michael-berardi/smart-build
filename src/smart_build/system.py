"""What the machine can take: CPU count, total and available memory, and the defaults derived from them."""

from __future__ import annotations

import os
import re
import subprocess
import sys

GIB = 1024**3

# A heavy build (a Rust release build, a Next or Astro production build, a big TypeScript check) keeps about four
# cores and about 8 GiB of memory busy. The default slot count is how many of those the machine holds at once.
CORES_PER_SLOT = 4
GIB_PER_SLOT = 8
MAX_AUTO_SLOTS = 8


def cpu_count() -> int:
    """CPUs this process may use (its affinity mask where the platform reports one)."""
    counter = getattr(os, "process_cpu_count", None)
    if counter is not None:
        count = counter()
    elif hasattr(os, "sched_getaffinity"):
        count = len(os.sched_getaffinity(0))
    else:
        count = os.cpu_count()
    return max(1, count or 1)


def total_memory() -> int | None:
    """Physical memory in bytes, or None when the platform does not say."""
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError, AttributeError):
        pass
    if sys.platform == "darwin":
        value = _sysctl("hw.memsize")
        return int(value) if value and value.isdigit() else None
    return None


def available_memory() -> int | None:
    """Memory a new build could use without pushing the machine into swap, in bytes; None when unknown."""
    if sys.platform.startswith("linux"):
        try:
            with open("/proc/meminfo", encoding="ascii") as handle:
                for line in handle:
                    if line.startswith("MemAvailable:"):
                        return int(line.split()[1]) * 1024
        except (OSError, ValueError, IndexError):
            return None
        return None
    if sys.platform == "darwin":
        return _darwin_available_memory()
    return None


def _darwin_available_memory() -> int | None:
    # Free, inactive, speculative and purgeable pages can all be handed to a new process without swapping.
    try:
        output = subprocess.run(
            ["vm_stat"], capture_output=True, text=True, timeout=5, check=False
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    page = re.search(r"page size of (\d+) bytes", output)
    if not page:
        return None
    counts = {}
    for name in ("Pages free", "Pages inactive", "Pages speculative", "Pages purgeable"):
        match = re.search(rf"^{name}:\s+(\d+)\.", output, re.M)
        if match is None:
            return None
        counts[name] = int(match.group(1))
    return sum(counts.values()) * int(page.group(1))


def _sysctl(name: str) -> str | None:
    try:
        result = subprocess.run(
            ["sysctl", "-n", name], capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def auto_slots() -> int:
    """Heavy builds this machine runs at once without thrashing: one per 4 CPUs and per 8 GiB, between 1 and 8."""
    by_cpu = cpu_count() // CORES_PER_SLOT
    memory = total_memory()
    by_memory = memory // (GIB_PER_SLOT * GIB) if memory else by_cpu
    return max(1, min(by_cpu, by_memory, MAX_AUTO_SLOTS))


def auto_memory_floor() -> int:
    """Memory that must stay available before another build starts: 10% of RAM, at most 4 GiB."""
    memory = total_memory()
    if not memory:
        return 2 * GIB
    return min(memory // 10, 4 * GIB)


def jobs_share(slots: int, weight: int) -> int:
    """This build's fair share of the CPUs when every slot is busy."""
    return max(1, cpu_count() * weight // max(1, slots))
