from __future__ import annotations

import gc
import os
import sys
import tracemalloc
from collections import Counter
from threading import Lock


def start_memory_trace() -> None:
    """Begin per-allocation tracing. Off by default (tracemalloc adds per-alloc
    overhead); the caller enables it behind a flag so allocation attribution is
    available without paying for it in steady state."""
    if not tracemalloc.is_tracing():
        tracemalloc.start()


def _windows_memory_counters():
    """The Win32 PROCESS_MEMORY_COUNTERS for this process, or None. Exposes both
    the current (WorkingSetSize) and peak (PeakWorkingSetSize) working set."""
    try:
        import ctypes
        from ctypes import wintypes

        class _PMC(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        kernel32 = ctypes.windll.kernel32
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        get_info = ctypes.windll.psapi.GetProcessMemoryInfo
        # Without explicit types the 64-bit HANDLE is truncated to a 32-bit int
        # and the call fails; pin the signature so it succeeds.
        get_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PMC), wintypes.DWORD]
        get_info.restype = wintypes.BOOL
        counters = _PMC()
        counters.cb = ctypes.sizeof(_PMC)
        if get_info(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            return counters
    except Exception:
        return None
    return None


def process_rss_bytes() -> int | None:
    """CURRENT resident set size of this process in bytes, or None when it can't
    be read cheaply here. Dependency-free: /proc on Linux (the Render deploy
    target), the Win32 working-set counter on Windows. This is the live working
    set, NOT the lifetime peak -- see :func:`process_peak_rss_bytes`. (POSIX
    ``ru_maxrss`` is deliberately not used here: it is the peak, not the current.)"""
    try:
        with open("/proc/self/statm", "r", encoding="ascii") as handle:
            resident_pages = int(handle.read().split()[1])
        return resident_pages * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError, AttributeError):
        pass
    if sys.platform == "win32":
        counters = _windows_memory_counters()
        if counters is not None:
            return int(counters.WorkingSetSize)
    return None


def process_peak_rss_bytes() -> int | None:
    """PEAK (lifetime maximum) resident set size in bytes, or None. POSIX
    ``ru_maxrss`` (KiB on Linux, bytes on macOS) or the Win32 peak working set."""
    try:
        import resource
    except ImportError:
        resource = None
    if resource is not None:
        ru = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # Linux reports ru_maxrss in KiB, macOS in bytes.
        return ru if sys.platform == "darwin" else ru * 1024
    if sys.platform == "win32":
        counters = _windows_memory_counters()
        if counters is not None:
            return int(counters.PeakWorkingSetSize)
    return None


def memory_snapshot() -> dict:
    """Lightweight process-memory readout for ``/api/runtime``. Cheap enough to
    call per request: current RSS (``rss_mib``) and lifetime peak RSS
    (``rss_peak_mib``) reported separately, GC generation counts, and (only when
    tracing is on) the tracked Python-heap current/peak."""
    snapshot: dict[str, object] = {
        "gc_counts": list(gc.get_count()),
        "gc_collections": [stat.get("collections", 0) for stat in gc.get_stats()],
        "tracing": tracemalloc.is_tracing(),
    }
    rss = process_rss_bytes()
    if rss is not None:
        snapshot["rss_mib"] = round(rss / (1024 * 1024), 1)
    peak_rss = process_peak_rss_bytes()
    if peak_rss is not None:
        snapshot["rss_peak_mib"] = round(peak_rss / (1024 * 1024), 1)
    if tracemalloc.is_tracing():
        current, peak = tracemalloc.get_traced_memory()
        snapshot["python_heap_current_mib"] = round(current / (1024 * 1024), 1)
        snapshot["python_heap_peak_mib"] = round(peak / (1024 * 1024), 1)
    return snapshot


class RuntimeTelemetry:
    def __init__(self):
        self._counters: Counter[str] = Counter()
        self._lock = Lock()

    def increment(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._counters[name] += amount

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(sorted(self._counters.items()))


runtime_telemetry = RuntimeTelemetry()
