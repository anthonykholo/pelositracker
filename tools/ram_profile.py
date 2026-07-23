"""Ad-hoc RAM profiler for the memory-reduction changes.

Run: ``.\.venv\Scripts\python.exe -m tools.ram_profile``

This is a developer measurement aid, not a test and not part of the service. It
quantifies the three shipped live-memory reductions on synthetic workloads so
the wins can be re-checked after future changes:

1. bounded ``deque`` live buffers vs. the old ``list[-N:]`` trim (allocation
   churn under sustained ingest);
2. the engine's freshest-quote reduction (persisted request/snapshot size when
   the buffer holds a long tail of superseded quotes);
3. streaming JSONL loading vs. materializing every parsed row.

Numbers are directional and depend on the synthetic sizes below; the point is
the ratio between the "before" and "after" shapes, not absolute bytes.
"""

from __future__ import annotations

import json
import tempfile
import time
import tracemalloc
from collections import deque
from pathlib import Path


def _peak_mib(fn) -> float:
    tracemalloc.start()
    tracemalloc.reset_peak()
    fn()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return peak / (1024 * 1024)


def _elapsed_s(fn) -> float:
    start = time.perf_counter()
    fn()
    return time.perf_counter() - start


def bounded_buffer(events: int = 20, quotes_per_event: int = 20_000,
                   maxlen: int = 2_000) -> None:
    """Sustained ingest: bounded deque vs. the old list-slice trim.

    Both keep only the last ``maxlen`` items per event and end at the same
    bounded footprint, so *peak* memory is identical -- the deque win is the
    per-append allocation churn the slice paid (rebuilding an up-to-``maxlen``
    list on every append, i.e. O(n*maxlen) copies). That churn shows up as time
    and GC pressure, so we report elapsed time here rather than peak bytes.
    """
    def with_deque() -> None:
        buffers: dict[int, deque] = {event: deque(maxlen=maxlen) for event in range(events)}
        for i in range(events * quotes_per_event):
            buffers[i % events].append((i, i * 0.5, "quote-payload-stand-in"))

    def with_list_slice() -> None:
        buffers: dict[int, list] = {event: [] for event in range(events)}
        for i in range(events * quotes_per_event):
            key = i % events
            buffers[key].append((i, i * 0.5, "quote-payload-stand-in"))
            buffers[key] = buffers[key][-maxlen:]

    deque_time = _elapsed_s(with_deque)
    slice_time = _elapsed_s(with_list_slice)
    print("1) Live buffer retention "
          f"({events} events x {quotes_per_event:,} quotes, maxlen={maxlen:,})")
    print(f"   deque(maxlen)      time: {deque_time*1000:8.1f} ms")
    print(f"   list[-N:] trim     time: {slice_time*1000:8.1f} ms")
    if deque_time > 0:
        print(f"   -> {slice_time / deque_time:5.1f}x less append/trim work "
              "(same peak memory)\n")


def _payload(selection: int, observation: int) -> dict:
    """Stand-in for SignalEngine._quote_payload output (same keys the reducer
    and the JSON snapshot use)."""
    market = f"moneyline:{selection}"
    outcome = "home" if selection % 2 else "away"
    return {
        "market": market, "outcome": outcome,
        "comparison_market": market, "comparison_outcome": outcome,
        "comparison_source": "polymarket",
        "probability": 0.5 + (selection % 10) * 0.01,
        "source": "Polymarket", "observed_at": float(observation),
        "provider_timestamp": float(observation), "received_at": float(observation),
        "processed_at": float(observation), "timestamp_trusted": True,
        "identity_valid": True, "bid": 0.49, "ask": 0.51, "source_weight": 1.0,
        "is_exchange": True, "decimal_odds": None, "liquidity": 1000.0,
        "ask_size": 500.0, "depth_complete": True, "fee_metadata_known": True,
        "accepting_orders": True, "requested_cash": 100.0, "filled_cash": 100.0,
        "filled_shares": 196.0, "execution_fee": 0.0, "execution_vwap": 0.51,
        "execution_complete": True, "token_id": f"tok-{selection}",
        "book_hash": "deadbeef", "point": None, "side": None,
    }


def snapshot_reduction(selections: int = 30, observations: int = 400) -> None:
    """Freshest-quote reduction of the persisted request/snapshot."""
    from app.engine import SignalEngine

    payloads = [_payload(s, o) for o in range(observations) for s in range(selections)]
    reduced = SignalEngine._freshest_valid_payloads(payloads)
    before = len(json.dumps(payloads, separators=(",", ":")))
    after = len(json.dumps(reduced, separators=(",", ":")))
    print("2) Engine request / input_snapshot_json size "
          f"({selections} selections x {observations} observations)")
    print(f"   full buffer        : {len(payloads):>7,} payloads, {before/1024:8.1f} KiB")
    print(f"   freshest-per-sel.  : {len(reduced):>7,} payloads, {after/1024:8.1f} KiB")
    if after > 0:
        print(f"   -> {before / after:5.1f}x smaller persisted snapshot\n")


def jsonl_streaming(rows: int = 200_000) -> None:
    """Streaming iteration vs. materializing the whole parsed list."""
    from app.model_training import iter_observations_jsonl, load_observations_jsonl

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "observations.jsonl"
        with path.open("w", encoding="utf-8") as handle:
            for i in range(rows):
                handle.write(json.dumps({
                    "event_id": f"e{i % 5000}",
                    "observed_at": "2026-01-01T00:00:00+00:00",
                    "sport": "tennis", "league": "atp", "market": "moneyline",
                    "outcome": float(i % 2),
                    "candidate_probabilities": {"equal_family_logit": 0.5},
                    "executable_cost": 0.5, "execution_cost_error": 0.0,
                }) + "\n")

        def streamed() -> None:
            total = 0.0
            for observation in iter_observations_jsonl(path):
                total += observation.outcome  # single pass, one row live at a time

        def materialized() -> None:
            total = sum(o.outcome for o in load_observations_jsonl(path))

        stream_peak = _peak_mib(streamed)
        list_peak = _peak_mib(materialized)
    print(f"3) Offline JSONL load ({rows:,} observations)")
    print(f"   iter_observations_jsonl (stream) peak: {stream_peak:8.2f} MiB")
    print(f"   load_observations_jsonl  (list)  peak: {list_peak:8.2f} MiB")
    if stream_peak > 0:
        print(f"   -> {list_peak / stream_peak:5.1f}x lower peak for a single pass\n")


def main() -> int:
    bounded_buffer()
    snapshot_reduction()
    jsonl_streaming()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
