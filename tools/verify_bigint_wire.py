"""Compare the SQL digits sent on stdin with the integer digits returned after restart."""
import argparse
import json
from pathlib import Path
import tempfile

from protocol_check._assertions import same
from tools.verify_journal_commit_error import jsonl_session


A, B = 9007199254740992, 9007199254740993


def input_trial(root, rounded):
    sent_key = int(float(B)) if rounded else B
    trace = {}
    replies = jsonl_session(root, [
        ("ddl", "CREATE TABLE t(id BIGINT PRIMARY KEY, tag VARCHAR(1));"),
        ("seed", f"INSERT INTO t VALUES ({A},'a');"),
        ("insert", f"INSERT INTO t VALUES ({sent_key},'b');"),
    ], wire_trace=trace)
    assert replies["seed"]["affected_rows"] == 1, replies
    reply = replies["insert"]
    expected = [[A, "a"]]
    if rounded:
        assert reply["ok"] is False and reply["error"]["code"] == "DUPLICATE", reply
    else:
        assert reply["ok"] is True and reply["affected_rows"] == 1, reply
        expected.append([B, "b"])
    recovered = jsonl_session(root, [("read", "SELECT id,tag FROM t ORDER BY id;")], wire_trace=trace)
    assert recovered["read"]["rows"] == expected, recovered
    return {"mode": "binary64-first" if rounded else "decimal",
            "request": trace["insert"][0].decode(), "insert_reply": trace["insert"][1].decode(),
            "reopened_stdout": trace["read"][1].decode()}


def output_trial(root):
    values = ",".join(f"({key})" for key in (A - 1, A, B))
    jsonl_session(root, [("ddl", "CREATE TABLE t(id BIGINT PRIMARY KEY);"),
                         ("seed", f"INSERT INTO t VALUES {values};")])
    trace = {}
    jsonl_session(root, [("read", "SELECT id FROM t ORDER BY id;")], wire_trace=trace)
    line = trace["read"][1]
    tokens = [row[0] for row in json.loads(line, parse_int=str)["rows"]]
    floats = [row[0] for row in json.loads(line, parse_int=float)["rows"]]
    assert tokens == [str(A - 1), str(A), str(B)], tokens
    distinct = {}
    for label, pair in (("lower", slice(0, 2)), ("upper", slice(1, 3))):
        distinct[label] = {"decimal": len(set(tokens[pair])), "binary64": len(set(floats[pair]))}
    assert distinct == {"lower": {"decimal": 2, "binary64": 2},
                        "upper": {"decimal": 2, "binary64": 1}}, distinct
    # Deliberately compare the middle returned key to an expected B. Both
    # operands come from the same unmodified response in the float check.
    actual = json.loads(line)["rows"][1][0]
    wrong_pair = {"actual": actual, "expected": B, "decimal_equal": tokens[1] == str(B),
                  "binary64_equal": floats[1] == floats[2], "public_equal": same(actual, B)}
    assert not wrong_pair["decimal_equal"] and wrong_pair["binary64_equal"]
    assert not wrong_pair["public_equal"] and same(json.loads(line)["rows"][2][0], B)
    return {"stdout": line.decode(), "decimal_tokens": tokens,
            "binary64_as_integer": [int(value) for value in floats], "distinct": distinct,
            "wrong_pair": wrong_pair}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("input", "output"), default="input", nargs="?")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as temp:
        if args.stage == "output":
            print(json.dumps(output_trial(temp)))
        else:
            for name, rounded in (("exact", False), ("rounded", True)):
                print(json.dumps(input_trial(Path(temp) / name, rounded)))
