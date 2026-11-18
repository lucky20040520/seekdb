"""Compare journal error positions and replay a write against known histories."""
import argparse
import json
import os
import subprocess
import sys
import tempfile
from unittest.mock import patch

from minivector import journal
from minivector.engine import Database
from minivector.sql import execute


SELECT_KEYS = "SELECT id FROM t ORDER BY id;"


def jsonl_session(root, statements):
    """Use the production entry in a fresh process, with no fault hooks."""
    requests = [{"id": name, "op": "execute", "sql": sql} for name, sql in statements]
    requests.append({"id": "bye", "op": "shutdown"})
    result = subprocess.run(
        [sys.executable, "-m", "minivector", "--data-dir", root],
        input="".join(json.dumps(item) + "\n" for item in requests),
        capture_output=True, encoding="utf-8", timeout=30, check=True,
    )
    messages = [json.loads(line) for line in result.stdout.splitlines()]
    assert messages[0] == {"type": "ready"}, messages
    assert [item["id"] for item in messages[1:]] == [item["id"] for item in requests], messages
    assert messages[-1]["ok"] is True, messages[-1]
    return {item["id"]: item for item in messages[1:-1]}


def inject_commit_error(db, stage):
    append = db.journal._append
    sync = os.fsync
    log_fd = db.journal.source.fileno()
    events = []

    def append_frame(kind, writer):
        if kind == journal.COMMIT and stage == "before-frame":
            events.append("injected before COMMIT frame")
            raise OSError("before journal commit frame")
        return append(kind, writer)

    def sync_frame(fd):
        sync(fd)
        if fd == log_fd and stage == "after-sync":
            events.append("real journal fsync completed")
            raise OSError("simulated error after journal fsync")

    with patch.object(db.journal, "_append", append_frame), patch.object(journal.os, "fsync", sync_frame):
        try:
            execute(db, "COMMIT;")
        except OSError as error:
            assert len(events) == 1 and db.journal.poisoned, events
            return events, str(error)
    raise AssertionError("injected COMMIT unexpectedly succeeded")


def trial(stage, retry):
    with tempfile.TemporaryDirectory() as root:
        db = Database(root)
        execute(db, "CREATE TABLE t(id BIGINT PRIMARY KEY);")
        execute(db, "INSERT INTO t VALUES (1);")
        execute(db, "BEGIN;")
        execute(db, "INSERT INTO t VALUES (2);")
        events, commit_error = inject_commit_error(db, stage)
        poisoned = db.journal.poisoned
        try:
            execute(db, "INSERT INTO t VALUES (3);")
        except OSError as error:
            subsequent_error = str(error)
        else:
            raise AssertionError("write after uncertain COMMIT unexpectedly succeeded")
        db.close()

        statements = [("before", SELECT_KEYS)]
        if retry:
            statements.extend([("retry2", "INSERT INTO t VALUES (2);"),
                               ("after_retry", SELECT_KEYS),
                               ("fresh3", "INSERT INTO t VALUES (3);"),
                               ("final", SELECT_KEYS)])
        observed = jsonl_session(root, statements)
        rows = observed["before"]["rows"]
        before_frame = stage == "before-frame"
        assert subsequent_error == "journal requires restart", subsequent_error
        assert rows == ([[1]] if before_frame else [[1], [2]]), rows
        report = {"stage": stage, "events": events, "commit_error": commit_error,
                  "poisoned": poisoned, "subsequent_write_error": subsequent_error,
                  "rows_after_reopen": rows}
        if retry:
            replay = observed["retry2"]
            if before_frame:
                assert replay["ok"] is True and replay["affected_rows"] == 1, replay
            else:
                assert replay["ok"] is False and replay["error"]["code"] == "DUPLICATE", replay
            assert observed["after_retry"]["rows"] == [[1], [2]], observed
            fresh = observed["fresh3"]
            assert fresh["ok"] is True and fresh["affected_rows"] == 1, fresh
            final = observed["final"]["rows"]
            persisted = jsonl_session(root, [("persisted", SELECT_KEYS)])["persisted"]["rows"]
            assert final == persisted == [[1], [2], [3]], (final, persisted)
            report.update(retry2=replay, rows_after_retry=observed["after_retry"]["rows"],
                          fresh3=fresh, final_rows=final, second_reopen=persisted)
        print(json.dumps(report))


def payload_trial(history):
    with tempfile.TemporaryDirectory() as root:
        db = Database(root)
        execute(db, "CREATE TABLE t(id BIGINT PRIMARY KEY, label VARCHAR(16));")
        execute(db, "INSERT INTO t VALUES (1,'seed');")
        attempt = "INSERT INTO t VALUES (2,'mine');"
        if history == "after-sync":
            execute(db, "BEGIN;")
            execute(db, attempt)
            inject_commit_error(db, "after-sync")
        elif history == "earlier-other":
            execute(db, "INSERT INTO t VALUES (2,'other');")
        else:
            assert history == "earlier-mine"
            execute(db, attempt)
        db.close()

        read = "SELECT id,label FROM t WHERE id=2;"
        replies = jsonl_session(root, [("before", read), ("retry2", attempt), ("after", read)])
        retry = replies["retry2"]
        assert retry["ok"] is False and retry["error"]["code"] == "DUPLICATE", retry
        expected = [[2, "other" if history == "earlier-other" else "mine"]]
        assert replies["before"]["rows"] == replies["after"]["rows"] == expected, replies
        print(json.dumps({"history": history, "before": replies["before"]["rows"],
                          "retry2": retry, "after": replies["after"]["rows"]}))


def request_id_trial(after_sync):
    with tempfile.TemporaryDirectory() as root:
        db = Database(root)
        execute(db, "CREATE TABLE t(id BIGINT PRIMARY KEY, label VARCHAR(16), request_id VARCHAR(16));")
        attempt = "INSERT INTO t VALUES (2,'mine','req-42');"
        if after_sync:
            execute(db, "BEGIN;")
            execute(db, attempt)
            inject_commit_error(db, "after-sync")
        else:
            execute(db, "INSERT INTO t VALUES (2,'mine','req-17');")
        db.close()

        read = "SELECT id,label,request_id FROM t WHERE id=2;"
        replies = jsonl_session(root, [("req-42", attempt), ("stored", read)])
        retry = replies["req-42"]
        assert retry["ok"] is False and retry["error"]["code"] == "DUPLICATE", retry
        stored = replies["stored"]["rows"]
        assert stored == [[2, "mine", "req-42" if after_sync else "req-17"]], stored
        print(json.dumps({"history": "after-sync" if after_sync else "earlier-mine",
                          "retry": retry, "stored": stored}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--retry", action="store_true", help="replay key 2, write 3 and reopen again")
    modes.add_argument("--payload", action="store_true", help="compare three known histories of key 2")
    modes.add_argument("--request-id", action="store_true", help="read back an application-supplied request ID")
    args = parser.parse_args()
    if args.payload:
        for history in ("after-sync", "earlier-other", "earlier-mine"):
            payload_trial(history)
    elif args.request_id:
        for after_sync in (True, False):
            request_id_trial(after_sync)
    else:
        for stage in ("before-frame", "after-sync"):
            trial(stage, args.retry)
