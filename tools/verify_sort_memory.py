"""Diagnose equal JSON sizes and decoded-object memory in fresh local processes."""
import argparse
import gc
import json
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import threading
import tracemalloc
from contextlib import closing
from unittest.mock import patch

from minivector.engine import Database, encode
from minivector.segments import Reader
from minivector.sql import execute


def object_bytes(value):
    stack, seen, total = [value], set(), 0
    while stack:
        item = stack.pop()
        identity = id(item)
        if identity in seen:
            continue
        seen.add(identity)
        total += sys.getsizeof(item)
        if isinstance(item, (list, tuple)):
            stack.extend(item)
    return total


def worker(root, mode, tracing, row_count):
    import psutil
    process = psutil.Process()
    with closing(Database(root)) as db:
        table = db.table("t")
        gc.collect()
        initial_memory = process.memory_info()
        baseline = initial_memory.rss
        rss_samples = [baseline]
        stop = threading.Event()

        def sample():
            while not stop.wait(0.005):
                rss_samples.append(process.memory_info().rss)

        sampler = threading.Thread(target=sample)
        spill_count = []
        spill_events = []
        decoded_rows = 0
        read_row = Reader._row
        temporary_file = tempfile.NamedTemporaryFile

        def counted_read(reader):
            nonlocal decoded_rows
            # The streaming source requests the next row after spill() has
            # returned and external_sort has replaced its previous batch.
            if spill_events and spill_events[-1]["decoded_rows"] == decoded_rows:
                spill_events[-1]["rss_after_release_before_next_decode"] = process.memory_info().rss
            row = read_row(reader)
            decoded_rows += 1
            return row

        def temporary(*args, **kwargs):
            if kwargs.get("prefix") == "sort-":
                spill_count.append(1)
                memory = process.memory_info()
                rss_samples.append(memory.rss)
                spill_events.append({"decoded_rows": decoded_rows, "rss_at_file_creation": memory.rss,
                                     "process_peak_wset_at_file_creation": getattr(memory, "peak_wset", None)})
            return temporary_file(*args, **kwargs)

        sampler.start()
        if tracing:
            tracemalloc.start()
        phase = {}
        try:
            with patch("minivector.engine.tempfile.NamedTemporaryFile", temporary), \
                    patch.object(Reader, "_row", counted_read):
                if mode in ("decode", "phased"):
                    retained = list(db.rows(table))
                    rss_samples.append(process.memory_info().rss)
                    if mode == "phased":
                        start_current, decode_peak = tracemalloc.get_traced_memory() if tracing else (None, None)
                        if tracing:
                            # Python 3.8 has no reset_peak. A fresh tracer sees
                            # only new allocations while the decoded rows stay
                            # alive, which isolates the sorted() allocation step.
                            tracemalloc.stop()
                            tracemalloc.start()
                        sort_start_current = tracemalloc.get_traced_memory()[0] if tracing else None
                        phase_rss_start = process.memory_info().rss
                        rss_samples.append(phase_rss_start)
                        first_sample = len(rss_samples) - 1
                        ordered = sorted(retained, key=lambda row: (row[0], row[0]))
                        rss_samples.append(process.memory_info().rss)
                        end_current, sort_peak = tracemalloc.get_traced_memory() if tracing else (None, None)
                        phase = {"decode_traced_peak": decode_peak, "decode_traced_current": start_current,
                                 "sort_start_traced": sort_start_current,
                                 "sort_end_traced": end_current,
                                 "sort_peak_above_start": sort_peak - sort_start_current if tracing else None,
                                 "sort_sampled_rss_above_start": max(rss_samples[first_sample:]) - phase_rss_start,
                                 "same_row_objects": all(a is b for a, b in zip(retained, ordered)),
                                 "trace_peak_scope": "fresh tracer for sorting; decoded rows retained but untracked"}
                        actual = [row[0] for row in ordered]
                    else:
                        actual = [row[0] for row in retained]
                else:
                    columns, rows = execute(db, "SELECT id FROM t ORDER BY id;")
                    assert columns == ["id"]
                    actual = []
                    for row in rows:
                        actual.append(row[0])
                        if len(actual) == 1:
                            rss_samples.append(process.memory_info().rss)
                current, peak = tracemalloc.get_traced_memory() if tracing else (None, None)
                trace_metadata = tracemalloc.get_tracemalloc_memory() if tracing else None
                final_memory = process.memory_info()
                rss_samples.append(final_memory.rss)
        finally:
            if tracing:
                tracemalloc.stop()
            stop.set()
            sampler.join()
        expected = list(range(row_count))
        assert sorted(actual) == expected
        assert decoded_rows == row_count
        if mode in ("sort", "phased"):
            assert actual == expected
        # Object graph inspection runs after both measurement windows, so the
        # temporary identity set does not inflate the reported peaks.
        if mode == "sort":
            retained = list(db.rows(table))
        payload = retained[0][1]
        serialized_bytes = sum(len(encode(row).encode("utf-8")) for row in retained)
        first_spill_bytes = (sum(len(encode(row).encode("utf-8"))
                                for row in retained[:spill_events[0]["decoded_rows"]])
                             if spill_events else None)
        if mode == "sort":
            assert bool(spill_count) == (serialized_bytes >= 2 * 1024 * 1024)
        result = {"mode": mode, "tracing": tracing, "payload_type": type(payload).__name__,
                "element_type": type(payload[0]).__name__,
                "retained_object_bytes": object_bytes(retained),
                "serialized_bytes": serialized_bytes,
                "traced_current": current, "traced_peak": peak,
                "trace_metadata_bytes": trace_metadata,
                "rss_before": baseline, "rss_sampled_peak": max(rss_samples),
                "rss_sample_count": len(rss_samples), "spills": len(spill_count),
                "decoded_rows": decoded_rows, "spill_events": spill_events,
                "first_spill_serialized_bytes": first_spill_bytes,
                "rss_at_query_end": final_memory.rss,
                "process_peak_wset_before": getattr(initial_memory, "peak_wset", None),
                "process_peak_wset_after": getattr(final_memory, "peak_wset", None),
                "numpy_loaded": "numpy" in sys.modules, "rows_verified": len(actual),
                "temporary_files_remaining": len(list(Path(root).glob("sort-*.jsonl")))}
        if mode == "phased":
            assert phase["same_row_objects"]
            result.update(phase)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", choices=("decode", "sort", "phased"))
    parser.add_argument("--data-dir")
    parser.add_argument("--tracing", choices=("on", "off", "both"), default="on")
    parser.add_argument("--phased", action="store_true")
    parser.add_argument("--rows", type=int, default=3000)
    parser.add_argument("--shape", choices=("text", "vector", "both"), default="both")
    args = parser.parse_args()
    if args.rows < 1:
        parser.error("--rows must be positive")
    if args.worker:
        assert args.tracing != "both"
        print(json.dumps(worker(args.data_dir, args.worker, args.tracing == "on", args.rows)))
        return
    with tempfile.TemporaryDirectory() as root:
        paths = {}
        shapes = ("text", "vector") if args.shape == "both" else (args.shape,)
        for shape in shapes:
            path = Path(root) / shape
            with closing(Database(path)) as db:
                column = "VARCHAR(511)" if shape == "text" else "VECTOR(128)"
                execute(db, f"CREATE TABLE t(id BIGINT PRIMARY KEY,payload {column});")
                for start in range(0, args.rows, 100):
                    rows = [[key, "x" * 511 if shape == "text" else [0.0] * 128]
                            for key in range(start, min(start + 100, args.rows))]
                    db.insert("t", rows)
            paths[shape] = path
        totals = set()
        print(json.dumps({"system": platform.system(), "python": platform.python_version(),
                          "rows": args.rows, "serialized_spill_threshold": 2 * 1024 * 1024,
                          "scope": "Local diagnostic, RSS sampled every 5ms; not platform measurement"}))
        for round_number in range(1, 4):
            tracing_modes = ("off", "on") if args.tracing == "both" else (args.tracing,)
            modes = ("phased",) if args.phased else ("decode", "sort")
            cases = [(shape, mode, tracing) for shape in paths
                     for mode in modes for tracing in tracing_modes]
            if round_number == 2:
                cases.reverse()
            for shape, mode, tracing in cases:
                command = [sys.executable, "-m", "tools.verify_sort_memory", "--worker", mode,
                           "--data-dir", str(paths[shape]), "--tracing", tracing, "--rows", str(args.rows)]
                child = subprocess.run(command, capture_output=True, text=True, timeout=60)
                if child.returncode:
                    raise RuntimeError("diagnostic child failed: " + child.stderr)
                result = json.loads(child.stdout)
                assert result["rows_verified"] == args.rows and result["temporary_files_remaining"] == 0
                totals.add(result["serialized_bytes"])
                print(json.dumps(dict(result, shape=shape, round=round_number)), flush=True)
        assert len(totals) == 1


if __name__ == "__main__":
    main()
