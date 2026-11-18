"""Probe VARCHAR validation and comparison through the production JSONL entry."""
import argparse
import json
from pathlib import Path
import tempfile

from tools.verify_journal_commit_error import jsonl_session


COMPOSED = "\u00e9"
DECOMPOSED = "e\u0301"
EMOJI = "\U0001f600"
INPUTS = (("precomposed", COMPOSED), ("decomposed", DECOMPOSED), ("emoji", EMOJI))
X, Y = "\ue000", "\U00010000"


def setup(width):
    statements = [("create", f"CREATE TABLE t(id BIGINT PRIMARY KEY, tag VARCHAR({width}));")]
    for key, (name, value) in enumerate(INPUTS, 1):
        statements.append((name, f"INSERT INTO t VALUES ({key},'{value}');"))
    return statements


def length_probe(root):
    statements = setup(1)
    statements.append(("filter", f"SELECT id FROM t WHERE tag='{DECOMPOSED}' ORDER BY id;"))
    replies = jsonl_session(root, statements)
    assert replies["create"]["ok"] is True, replies
    for name in ("precomposed", "emoji"):
        assert replies[name]["ok"] is True and replies[name]["affected_rows"] == 1, replies
    invalid = replies["decomposed"]
    assert invalid["ok"] is False and invalid["error"]["code"] == "TYPE", invalid
    assert replies["filter"]["ok"] is True and replies["filter"]["rows"] == [], replies["filter"]
    reopened = jsonl_session(root, [("stored", "SELECT id,tag FROM t ORDER BY id;")])
    rows = reopened["stored"]["rows"]
    assert rows == [[1, COMPOSED], [3, EMOJI]], rows
    return {"writes": [replies[name] for name, _ in INPUTS], "comparison": replies["filter"],
            "reopened": [[key, [ord(c) for c in tag]] for key, tag in rows]}


def equality_probe(root):
    searches = [("find_1", f"SELECT id FROM t WHERE tag='{COMPOSED}' ORDER BY id;"),
                ("find_2", f"SELECT id FROM t WHERE tag='{DECOMPOSED}' ORDER BY id;")]
    first = jsonl_session(root, setup(2) + searches)
    assert all(first[name]["ok"] and first[name]["affected_rows"] == 1 for name, _ in INPUTS), first
    reopened = jsonl_session(root, searches + [("stored", "SELECT id,tag FROM t ORDER BY id;")])
    for session in (first, reopened):
        assert session["find_1"]["rows"] == [[1]] and session["find_2"]["rows"] == [[2]], session
    rows = reopened["stored"]["rows"]
    codes = [[key, [ord(c) for c in tag]] for key, tag in rows]
    assert codes == [[1, [233]], [2, [101, 769]], [3, [128512]]], codes
    return {"affected_rows": [first[name]["affected_rows"] for name, _ in INPUTS],
            "before": [first[name]["rows"] for name, _ in searches],
            "after": [reopened[name]["rows"] for name, _ in searches], "stored": codes}


def ordering_probe(root):
    data = [(7, X), (9, Y), (3, Y)]
    statements = [("create", "CREATE TABLE t(id BIGINT PRIMARY KEY, tag VARCHAR(1));"),
                  ("insert", f"INSERT INTO t VALUES (7,'{X}'),(9,'{Y}'),(3,'{Y}');")]
    queries = [("asc", "SELECT id FROM t ORDER BY tag ASC;"),
               ("desc1", "SELECT id FROM t ORDER BY tag DESC LIMIT 1;"),
               ("range", f"SELECT id FROM t WHERE tag>'{X}' ORDER BY id;")]
    expected = {"asc": [7, 3, 9], "desc1": [3], "range": [3, 9]}
    orders = {}
    # Big endian preserves unsigned code-unit order in the UTF-16 comparison.
    for encoding in ("utf-8", "utf-16-be"):
        reference = sorted(data, key=lambda pair: (pair[1].encode(encoding), pair[0]))
        orders[encoding] = [row[0] for row in reference]

    results = []
    for mode, other_mode, escaped in (("literal", "escaped", False), ("escaped", "literal", True)):
        directory = Path(root) / mode
        first = jsonl_session(directory, statements + queries, ensure_ascii=escaped)
        assert first["insert"]["ok"] and first["insert"]["affected_rows"] == len(data), first
        second = jsonl_session(directory, queries, ensure_ascii=not escaped)
        for phase, read_mode, replies in (("original", mode, first), ("reopen", other_mode, second)):
            ids = {name: [row[0] for row in replies[name]["rows"]] for name, _ in queries}
            assert ids == expected, ids
            results.append({"written": mode, "read": read_mode, "phase": phase, "ids": ids})
    return {"references_asc": orders, "sessions": results}


def tie_probe(root):
    cases = []
    for lead, tail in ((9, 3), (3, 9)):
        statements = [
            ("create", "CREATE TABLE t(id BIGINT PRIMARY KEY, tag VARCHAR(1));"),
            ("insert", f"INSERT INTO t VALUES (7,'{X}'),({lead},'{Y}'),({tail},'{Y}');"),
            ("asc", "SELECT id FROM t ORDER BY tag ASC;"),
            ("desc", "SELECT id FROM t ORDER BY tag DESC;"),
            ("first", "SELECT id FROM t ORDER BY tag DESC LIMIT 1;"),
        ]
        replies = jsonl_session(Path(root) / str(lead), statements)
        assert replies["insert"]["affected_rows"] == 3, replies
        orders = {name: [row[0] for row in replies[name]["rows"]] for name in ("asc", "desc", "first")}
        assert orders == {"asc": [7, 3, 9], "desc": [3, 9, 7], "first": [3]}, orders
        cases.append({"inserted": [7, lead, tail], "observed": orders})
    return {"cases": cases}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    probes = {"length": length_probe, "equality": equality_probe, "order": ordering_probe, "ties": tie_probe}
    parser.add_argument("probe", choices=probes, nargs="?", default="length")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as directory:
        print(json.dumps(probes[args.probe](directory), ensure_ascii=True))
