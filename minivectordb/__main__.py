"""JSON Lines process adapter for the MiniVectorDB contest protocol."""
import argparse
import json
import os
import sys

from .sql import execute
from .storage import Database, DatabaseError


def send(message):
    print(json.dumps(message, ensure_ascii=False, allow_nan=False, separators=(",", ":")), flush=True)


def send_result(ident, result):
    if not result.columns:
        send({"id": ident, "ok": True, "affected_rows": result.affected})
        return
    # The response is one JSON line, but stream its rows to avoid retaining
    # the entire result set in memory. The final newline and flush complete it.
    encode = lambda value: json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    sys.stdout.write('{"id":' + encode(ident) + ',"ok":true,"columns":' + encode(result.columns) + ',"rows":[')
    first = True
    for row in result.rows:
        if not first:
            sys.stdout.write(",")
        sys.stdout.write(encode(row))
        first = False
    sys.stdout.write("]}\n")
    sys.stdout.flush()


def main():
    parser = argparse.ArgumentParser(description="MiniVectorDB JSONL protocol")
    parser.add_argument("--data", default=os.environ.get("MINIVECTORDB_DATA", "/data"))
    args = parser.parse_args()
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
        sys.stdout.reconfigure(encoding="utf-8")
    db = Database(args.data)
    closed = False
    try:
        send({"type": "ready"})
        for line in sys.stdin:
            ident = None
            try:
                request = json.loads(line)
                if not isinstance(request, dict):
                    raise DatabaseError("request must be an object")
                ident = request.get("id")
                operation = request.get("op")
                if operation == "shutdown":
                    db.close()
                    closed = True
                    send({"id": ident, "ok": True})
                    return
                if operation != "execute" or not isinstance(request.get("sql"), str):
                    raise DatabaseError("invalid request")
                result = execute(db, request["sql"], stream=True, search_mode=request.get("search_mode", "exact"))
                send_result(ident, result)
            except (DatabaseError, json.JSONDecodeError) as exc:
                send({"id": ident, "ok": False, "error": {
                    "code": exc.code if isinstance(exc, DatabaseError) else "invalid_request",
                    "message": str(exc)}})
    finally:
        if not closed:
            db.close()


if __name__ == "__main__":
    main()
