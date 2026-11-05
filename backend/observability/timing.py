import platform
import time
from pathlib import Path


def monotonic_now_ns() -> int:
    return time.monotonic_ns()


def elapsed_ms(start_ns: int) -> int:
    return max(0, (time.monotonic_ns() - start_ns) // 1_000_000)


def clock_domain_id() -> str:
    """Identify a kernel monotonic-clock domain for cross-process queue timing."""
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except OSError:
        return platform.node()


def queue_elapsed_ms(enqueued_ns: int, enqueued_domain: str, picked_up_ns: int) -> int | None:
    if enqueued_domain != clock_domain_id():
        return None
    return max(0, (picked_up_ns - enqueued_ns) // 1_000_000)
