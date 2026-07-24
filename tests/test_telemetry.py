"""Process-memory telemetry must report the live working set and the lifetime
peak as distinct, correctly-labeled readings (POSIX ru_maxrss is the peak, not
the current RSS)."""
from app.telemetry import (
    memory_snapshot,
    process_peak_rss_bytes,
    process_rss_bytes,
)


def test_current_and_peak_rss_are_positive_and_correctly_ordered():
    current = process_rss_bytes()      # live working set
    peak = process_peak_rss_bytes()    # read after current, so peak >= current holds
    # Readable on the CI (Linux) and dev (Windows) targets.
    assert isinstance(current, int) and current > 0
    assert isinstance(peak, int) and peak > 0
    # Peak is the lifetime maximum, so it is at least the current working set.
    assert peak >= current


def test_memory_snapshot_labels_current_and_peak_rss_separately():
    snapshot = memory_snapshot()
    assert "rss_mib" in snapshot and "rss_peak_mib" in snapshot
    assert snapshot["rss_peak_mib"] >= snapshot["rss_mib"]
