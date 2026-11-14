"""Calculate the competition efficiency indicator from measured data.

All time values are milliseconds; I/O and memory values are bytes.  This
module intentionally does not collect measurements or infer a baseline.
"""

import math
import statistics
from decimal import Decimal, ROUND_HALF_UP


_PHASES = ("write", "query")
_METRICS = {"time_ms": 1.0, "io_bytes": 1024.0 ** 2, "memory_bytes": 1024.0 ** 2}
_BOOL_CHECKS = ("write_correct", "query_correct", "resources_ok")
_RECALL_CHECKS = ("recall_unfiltered", "recall_filtered")


def rank_to_points(rank):
    """Convert a known competition ranking to points; never infer the rank."""
    if type(rank) is not int or rank < 1:
        raise ValueError("rank must be a positive integer")
    return max(40 - 0.5 * (rank - 1), 0)


def _round_efficiency(value):
    return float(Decimal(str(value)).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP))


def _number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite non-negative number")
    try:
        valid = math.isfinite(value) and value >= 0
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError(f"{name} must be a finite non-negative number")
    return value


def _metrics(obj, name):
    if not isinstance(obj, dict):
        raise ValueError(f"{name} must be an object")
    for metric in _METRICS:
        if metric not in obj:
            raise ValueError(f"{name}.{metric} is required")
        _number(obj[metric], f"{name}.{metric}")


def calculate_report(baseline: dict, rounds: list) -> dict:
    """Validate three runs, and calculate their median efficiency indicator.

    A failed correctness, resource or recall check disqualifies the report;
    disqualified runs have no indicator. The indicator is not a score or rank.
    """
    if not isinstance(baseline, dict):
        raise ValueError("baseline must be an object")
    for phase in _PHASES:
        _metrics(baseline.get(phase), f"baseline.{phase}")
    if not isinstance(rounds, list) or len(rounds) != 3:
        raise ValueError("exactly three rounds are required")

    failures = []
    for index, run in enumerate(rounds, 1):
        if not isinstance(run, dict):
            raise ValueError(f"round {index} must be an object")
        for phase in _PHASES:
            _metrics(run.get(phase), f"round {index}.{phase}")
        checks = run.get("checks")
        if not isinstance(checks, dict):
            raise ValueError(f"round {index}.checks must be an object")
        for key in _BOOL_CHECKS:
            if type(checks.get(key)) is not bool:
                raise ValueError(f"round {index}.checks.{key} must be a boolean")
            if not checks[key]:
                failures.append(f"round {index}: {key} failed")
        for key in _RECALL_CHECKS:
            recall = _number(checks.get(key), f"round {index}.checks.{key}")
            if recall > 1:
                raise ValueError(f"round {index}.checks.{key} must be at most 1")
            if recall < 0.95:
                failures.append(f"round {index}: {key} below 0.95")

    if failures:
        return {"eligible": False, "reasons": failures, "rounds": [], "median_efficiency": None}

    calculated = []
    for run in rounds:
        values = {}
        for phase in _PHASES:
            terms = {}
            for metric, epsilon in _METRICS.items():
                # Equivalent to R/(1+R), avoiding overflow for large R.
                base = baseline[phase][metric] + epsilon
                actual = run[phase][metric] + epsilon
                scale = max(base, actual)
                terms[metric] = (base / scale) / (base / scale + actual / scale)
            values[phase] = 0.6 * terms["time_ms"] + 0.2 * terms["io_bytes"] + 0.2 * terms["memory_bytes"]
        values["efficiency"] = (values["write"] + 2 * values["query"]) / 3
        calculated.append(values)
    return {
        "eligible": True,
        "reasons": [],
        "rounds": calculated,
        "median_efficiency": _round_efficiency(statistics.median(x["efficiency"] for x in calculated)),
    }
