"""Compare duplicate-alias results across the Python/C++ cache capacities."""
import argparse
import json
from pathlib import Path

from run import Pair, ok


def query(table):
    return ("SELECT id,L2_DISTANCE(v,[0,0]) AS x,L2_DISTANCE(v,[10,0]) AS x "
            f"FROM {table} ORDER BY x LIMIT 1")


def row_budget(pair):
    pair.step("schema", "CREATE TABLE t(id BIGINT PRIMARY KEY,n BIGINT,v VECTOR(2))", ok())
    # 90,000 rows fit the reference's 16 MiB cache; 100,000 do not. Both fit
    # the compact C++ cache. Ordering must change exactly as in the reference.
    for begin in range(1, 100001, 10000):
        records = [f"({i},30,[{0 if i == 1 else 10 if i == 2 else 5},0])"
                   for i in range(begin, begin + 10000)]
        pair.step(f"insert-{begin}", "INSERT INTO t VALUES" + ",".join(records), ok(affected_rows=10000))
        if begin == 80001:
            pair.step("reference-cache-fits", query("t"), ok(columns=["id", "x", "x"], rows=[[1, 0.0, 10.0]]))
    pair.step("reference-cache-over-budget", query("t"), ok(rows=[[2, 10.0, 0.0]]))
    pair.step("index", "CREATE VECTOR INDEX vi ON t(v)", ok())
    pair.step("index-after-budget-fallback", query("t"), ok(rows=[[1, 0.0, 10.0]]))
    pair.restart()
    pair.step("index-after-restart", query("t"), ok(rows=[[1, 0.0, 10.0]]))


def unicode_budget(pair):
    pair.step("schema", "CREATE TABLE t(id BIGINT PRIMARY KEY,s VARCHAR(3000),v VECTOR(2))", ok())
    # Python stores Latin-1 characters in one byte; C++ stores their UTF-8 in
    # two. This table fits only the reference cache, so C++ must scan using the
    # first alias rather than change the result when its own cache is full.
    literal = "'" + "\u00e4" * 3000 + "'"
    for begin in range(1, 4001, 100):
        records = [f"({i},{literal},[{0 if i == 1 else 10 if i == 2 else 5},0])"
                   for i in range(begin, begin + 100)]
        pair.step(f"insert-{begin}", "INSERT INTO t VALUES" + ",".join(records), ok(affected_rows=100))
    pair.step("only-reference-cache-fits", query("t"), ok(columns=["id", "x", "x"], rows=[[1, 0.0, 10.0]]))
    pair.step("fallback-distance-projection", query("t").replace("LIMIT 1", "LIMIT 2"),
              ok(rows=[[1, 0.0, 10.0], [3, 5.0, 5.0]]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python-image", default="minivector-boundary:python-f1b9f4e")
    parser.add_argument("--cpp-image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    records, groups = [], []
    for name, test in [("row_budget", row_budget), ("unicode_budget", unicode_budget)]:
        print("START " + name, flush=True)
        pair = Pair({"python": args.python_image, "cpp": args.cpp_image}, name, records, 60)
        try:
            test(pair)
        finally:
            groups.append({"name": name, "cleanup_errors": pair.close()})
        print("DONE " + name, flush=True)
    failures = [r for r in records if r["failures"] or not r["equivalent"]]
    summary = {"images": {"python": args.python_image, "cpp": args.cpp_image},
               "steps": len(records), "groups": groups, "failures": failures}
    (args.output / "steps.json").write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    (args.output / "result.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"steps": len(records), "failures": len(failures)}), flush=True)
    return bool(failures or any(g["cleanup_errors"] for g in groups))


if __name__ == "__main__":
    raise SystemExit(main())
