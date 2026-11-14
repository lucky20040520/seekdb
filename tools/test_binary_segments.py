"""Black-box legacy recovery and bounded binary rewrite regression."""
import argparse
import json
from pathlib import Path
import time
from types import SimpleNamespace

from test_cpp import Target


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--image", required=True)
    p.add_argument("--python-image", required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    target = Target(SimpleNamespace(docker_image=args.python_image, command=None,
                                   profile=False, screen_block=None, timeout=60))
    result = {"image": args.image, "legacy_image": args.python_image, "passed": False, "checks": []}
    start = time.perf_counter()
    try:
        s = target.start()
        s.sql("CREATE TABLE t(id BIGINT PRIMARY KEY,tag VARCHAR(16),v VECTOR(3))")
        s.sql("INSERT INTO t VALUES(-9223372036854775808,'旧😀',[1e-45,3.4028234e38,-0.0]),(2,'b',[1,2,3])")
        s.sql("CREATE VECTOR INDEX ix ON t(v)")
        before = s.sql("SELECT * FROM t ORDER BY id")["rows"]
        s.stop()
        target.args.docker_image = args.image
        s = target.start()
        s.sql("SELECT * FROM t ORDER BY id", rows=before)
        s.sql("INSERT INTO t VALUES(3,'new',[0,0,0])", affected=1)
        s.sql("INSERT INTO t VALUES(2,'duplicate',[0,0,0])", fail=True)
        s.sql("SELECT id FROM t ORDER BY L2_DISTANCE(v,[0,0,0]) LIMIT 3", rows=[[3],[2],[-9223372036854775808]])
        s.sql("BEGIN")
        s.sql("UPDATE t SET tag='rollback',v=[0,0,0] WHERE id=2", affected=1)
        s.sql("ROLLBACK")
        s.sql("SELECT * FROM t WHERE id<=2 ORDER BY id", rows=before)
        s.sql("UPDATE t SET tag='binary' WHERE id=2", affected=1)
        s.sql("DELETE FROM t WHERE id=3", affected=1)
        expected = [list(row) for row in before]
        expected[1][1] = "binary"
        s.stop()
        s = target.start()
        s.sql("SELECT * FROM t ORDER BY id", rows=expected)
        s.sql("SELECT id FROM t WHERE tag='binary' ORDER BY L2_DISTANCE(v,[0,0,0]) LIMIT 10", rows=[[2]])
        result["checks"].append("legacy JSONL/index recovery, mixed append, duplicate keys, rollback, binary rewrite, restart")

        # More than 256 KiB of (key, offset) entries in a single rewritten segment.
        s.sql("CREATE TABLE large(id BIGINT PRIMARY KEY,n BIGINT)")
        s.sql("BEGIN")
        for begin in range(0, 17000, 1000):
            s.sql("INSERT INTO large VALUES" + ",".join("(%d,%d)" % (i, i % 7) for i in range(begin, begin + 1000)), affected=1000)
        s.sql("COMMIT")
        s.sql("UPDATE large SET n=42", affected=17000)
        truth = [[i, 42] for i in range(17000)]
        s.sql("SELECT * FROM large ORDER BY id", rows=truth)
        s.sql("INSERT INTO large VALUES(16999,9)", fail=True)
        s.stop()
        s = target.start()
        s.sql("SELECT * FROM large ORDER BY id", rows=truth)
        s.sql("BEGIN")
        s.sql("DELETE FROM large", affected=17000)
        s.sql("ROLLBACK")
        s.sql("SELECT * FROM large ORDER BY id", rows=truth)
        s.sql("DELETE FROM large", affected=17000)
        s.sql("SELECT * FROM large", rows=[])
        s.sql("INSERT INTO large VALUES(16999,-9223372036854775808)", affected=1)
        s.stop()
        s = target.start()
        s.sql("SELECT * FROM large", rows=[[16999,-9223372036854775808]])
        s.stop()
        result["checks"].append("17000-row footer spill, exact full contents, restart, duplicate keys, empty segment and reinsertion")
        result["passed"] = True
    except BaseException as exc:
        result["error"] = repr(exc)
        raise
    finally:
        target.cleanup()
        result.update(seconds=time.perf_counter()-start, requests=target.requests)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=True), flush=True)


if __name__ == "__main__":
    main()
