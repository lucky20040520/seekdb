"""Observe real JSONL output after lazy failures before and after spool rollover."""
import io
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

import minivector.jsonl as wire
from minivector.engine import DBError


def trial(wide):
    events = []
    original = wire.tempfile.SpooledTemporaryFile

    def spool(*args, **kwargs):
        file = original(*args, **kwargs)
        rollover = file.rollover

        def tracked_rollover():
            rollover()
            events.append({"event": "rollover", "rolled": file._rolled, "fd_open": not file.closed})

        file.rollover = tracked_rollover
        return file

    def rows():
        yield ["x" * (300000 if wide else 1)]
        events.append("row_yielded")
        raise DBError("TEST_LAZY", "injected after first row")

    def execute(db, sql, mode):
        if sql == "FAIL":
            return ["payload"], rows()
        return ["value"], iter([[7]])

    class Database:
        def __init__(self, path):
            self.path = path

        def close(self):
            events.append("closed")

    requests = [
        {"id": "bad", "op": "execute", "sql": "FAIL"},
        {"id": "good", "op": "execute", "sql": "OK"},
        {"id": "bye", "op": "shutdown"},
    ]
    with tempfile.TemporaryDirectory() as root:
        stdout = io.StringIO()
        stdin = io.StringIO("".join(json.dumps(x) + "\n" for x in requests))
        with patch.object(wire, "execute", execute),              patch.object(wire.tempfile, "SpooledTemporaryFile", spool),              patch.object(wire.sys, "stdin", stdin), patch.object(wire.sys, "stdout", stdout):
            wire.serve(Database(root))
        assert stdout.getvalue().endswith("\n")
        lines = stdout.getvalue().splitlines()
        decoded = [json.loads(line) for line in lines]
        assert len(decoded) == 4
        assert decoded[0] == {"type": "ready"}
        assert decoded[1]["id"] == "bad" and decoded[1]["ok"] is False
        assert decoded[1]["error"]["code"] == "TEST_LAZY"
        assert decoded[2] == {"id": "good", "ok": True, "columns": ["value"], "rows": [[7]]}
        assert decoded[3] == {"id": "bye", "ok": True}
        assert any(isinstance(e, dict) and e["event"] == "rollover" and e["rolled"] and e["fd_open"] for e in events) == wide
        assert not list(Path(root).iterdir())
    print(json.dumps({"wide": wide, "events": events, "stdout": lines,
                      "data_temporary_files_after_close": 0, "passed": True}))


if __name__ == "__main__":
    import sys
    if "--run" not in sys.argv:
        raise SystemExit("Use --run after recording the student's prediction")
    for wide in (False, True):
        trial(wide)
