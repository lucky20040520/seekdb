"""Local stdin/stdout adapter; official contest framing is not yet available."""
import argparse
import json
import os
import sys

from .sql import execute
from .storage import Database, DatabaseError


def statements(source):
    buffer = []
    quote = None
    comment = False
    while True:
        char = source.read(1)
        if not char:
            if "".join(buffer).strip():
                yield "".join(buffer)
            return
        buffer.append(char)
        if comment:
            if char == "\n":
                comment = False
                if "".join(buffer).strip().lower() in ("--mode ann", "--mode exact"):
                    yield "".join(buffer).strip().lower()
                    buffer.clear()
            continue
        if quote:
            if char == quote:
                quote = None
            continue
        if char in ("'", "`"):
            quote = char
        elif char == "-" and len(buffer) >= 2 and buffer[-2] == "-":
            comment = True
        elif char == ";":
            yield "".join(buffer)
            buffer.clear()


def main():
    parser = argparse.ArgumentParser(description="MiniVectorDB local protocol")
    parser.add_argument("--data", default=os.environ.get("MINIVECTORDB_DATA", "/data"))
    parser.add_argument("--search-mode", choices=("exact", "ann"), default="exact")
    args = parser.parse_args()
    db = Database(args.data)
    try:
        print("READY", flush=True)
        for statement in statements(sys.stdin):
            if statement in ("--mode ann", "--mode exact"):
                args.search_mode = statement.split()[1]
                print("MODE\t" + args.search_mode, flush=True)
                continue
            try:
                result = execute(db, statement, stream=True, search_mode=args.search_mode)
                if result.columns:
                    print("COLUMNS\t" + "\t".join(result.columns))
                    for row in result.rows:
                        print("ROW\t" + "\t".join(json.dumps(value, ensure_ascii=False, separators=(",", ":")) for value in row))
                    print("END", flush=True)
                else:
                    print("OK\t" + str(result.affected), flush=True)
            except DatabaseError as exc:
                print("ERROR\t" + exc.code + "\t" + str(exc).replace("\n", " "), flush=True)
    finally:
        db.close()


if __name__ == "__main__":
    main()
