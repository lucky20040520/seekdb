"""Black-box validation of disabled/enabled engine profiling over JSONL."""
import argparse
import json
import subprocess

PREFIX = "MINIVECTOR_PROFILE "
SQL = [
    "CREATE TABLE t(id BIGINT PRIMARY KEY,tag VARCHAR(8),v VECTOR(2));",
    "INSERT INTO t VALUES (1,'a',[0,0]),(2,'a',[1,0]),(3,'a',[2,0]),(4,'b','[3,0]');",
    "SELECT id,L2_DISTANCE(v,'[0,0]') AS d FROM t WHERE tag='a' ORDER BY d LIMIT 1;",
    "CREATE VECTOR INDEX iv ON t(v);",
    "SELECT id,L2_DISTANCE(v,[0,0]) AS d FROM t WHERE tag='a' ORDER BY d LIMIT 1;",
    "UPDATE t SET tag='b' WHERE id=1;",
    "SELECT this is invalid;",
]


def run(image, enabled):
    command = ["docker", "run", "--rm", "-i", "--network=none"]
    if enabled:
        command.extend(["--env", "MINIVECTOR_PROFILE=1"])
    command.append(image)
    requests = [{"id": n + 1, "op": "execute", "sql": sql} for n, sql in enumerate(SQL)]
    requests.append({"id": len(requests) + 1, "op": "shutdown"})
    source = "".join(json.dumps(request) + "\n" for request in requests).encode()
    process = subprocess.run(command, input=source, capture_output=True, timeout=120, check=True)
    responses = [json.loads(line) for line in process.stdout.decode().splitlines()]
    assert responses[0] == {"type": "ready"} and len(responses) == len(requests) + 1
    assert all(r["ok"] for r in responses[1:-2]) and not responses[-2]["ok"] and responses[-1]["ok"]
    assert responses[3]["rows"] == [[1, 0.0]] and responses[5]["rows"] == [[1, 0.0]]
    lines = process.stderr.decode().splitlines()
    if not enabled:
        assert not lines, "profiling must be silent by default"
        return responses
    assert all(line.startswith(PREFIX) for line in lines), lines
    events = [json.loads(line[len(PREFIX):]) for line in lines]
    assert len(events) == len(requests) + 1
    assert events[0]["kind"] == "startup" and events[0]["sequence"] == 0
    assert [e["sequence"] for e in events[1:]] == list(range(1, len(requests) + 1))
    create, insert, cached, indexed, update, invalid = [events[i] for i in (1, 2, 3, 5, 6, 7)]
    for event in events:
        assert sum(p["ns"] for p in event["phases"].values()) <= event["wall_ns"], event
    for event in (create, insert, cached, indexed, update, invalid):
        assert event["phases"]["protocol_parse"]["calls"] == 1
        assert event["phases"]["sql_parse"]["calls"] == 1
    for name in ("file_sync", "directory_sync", "collect"):
        assert create["phases"][name]["calls"] > 0
        assert insert["phases"][name]["calls"] > 0
    assert insert["phases"]["vector_literal_parse"]["calls"] == 1
    assert cached["phases"]["vector_literal_parse"]["calls"] == 1
    for name in ("stored_row_decode", "filter_lists", "distance_exact", "distance_screen", "heap"):
        assert cached["phases"][name]["calls"] > 0, name
    assert cached["phases"]["filter"]["calls"] == 0, "the equality list already proves the only predicate"
    assert cached["phases"]["distance_exact"]["calls"] == 1, "the winning distance must be reused for projection"
    for name in ("stored_row_decode", "filter", "distance_exact", "heap"):
        assert indexed["phases"][name]["calls"] > 0, name
    assert invalid["phases"]["file_sync"]["calls"] == 0
    return responses


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    disabled, enabled = run(args.image, False), run(args.image, True)
    assert disabled == enabled, "profiling must not change responses"
    print("PASS disabled/enabled protocol, phase attribution, exact results and error handling")


if __name__ == "__main__":
    main()
