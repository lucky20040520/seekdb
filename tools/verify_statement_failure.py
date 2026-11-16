"""Check statement atomicity and transaction visibility via JSONL."""
import json
from pathlib import Path
import tempfile

from tools.verify_commit_cut import Session


def trial(outcome):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        with Session(root) as session:
            session.request(1, "CREATE TABLE t(id BIGINT PRIMARY KEY, label VARCHAR(16));")
            session.request(2, "INSERT INTO t VALUES (1,'old');")
            session.request(3, "BEGIN;")
            session.request(4, "UPDATE t SET label='edited' WHERE id=1;")
            session.send(5, "INSERT INTO t VALUES (2,'new'),(1,'duplicate');")
            error = session.receive()
            assert error["id"] == 5 and error["ok"] is False
            assert error["error"]["code"] == "DUPLICATE", error
            inside = session.request(6, "SELECT * FROM t ORDER BY id;")["rows"]
            assert inside == [[1, "edited"]], inside
            end = session.request(7, outcome + ";")
        with Session(root) as reopened:
            rows = reopened.request(1, "SELECT * FROM t ORDER BY id;")["rows"]
        assert rows == [[1, "edited" if outcome == "COMMIT" else "old"]], rows
        print(json.dumps({"outcome": outcome, "failed_statement": error,
                          "inside": inside, "transaction_end": end, "reopened": rows}))


if __name__ == "__main__":
    for outcome in ("COMMIT", "ROLLBACK"):
        trial(outcome)
