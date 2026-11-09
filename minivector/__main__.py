"""Local TSV diagnostic adapter, NOT the still-unpublished official protocol."""
import argparse
import json
import sys
from .engine import Database, DBError
from .sql import execute


def statements(stream):
    buffer, state = [], "normal"
    # Incremental lexer ensures quoted semicolons and comment semicolons are data.
    for line in stream:
        i = 0
        while i < len(line):
            char = line[i]
            following = line[i + 1] if i + 1 < len(line) else ""
            buffer.append(char)
            if state == "line":
                if char == "\n": state = "normal"
            elif state == "block":
                if char == "*" and following == "/":
                    buffer.append(following)
                    i += 1
                    state = "normal"
            elif state in ("'", "`"):
                if char == state:
                    if following == state:
                        buffer.append(following)
                        i += 1
                    else:
                        state = "normal"
            elif char in ("'", "`"):
                state = char
            elif char == "-" and following == "-":
                buffer.append(following)
                i += 1
                state = "line"
            elif char == "/" and following == "*":
                buffer.append(following)
                i += 1
                state = "block"
            elif char == ";":
                yield "".join(buffer)
                buffer = []
            i += 1
    if "".join(buffer).strip():
        yield "".join(buffer)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default="/data")
    parser.add_argument("--mode", choices=("exact", "ann"), default="exact")
    args = parser.parse_args()
    db = Database(args.data_dir)
    print("LOCAL_READY", flush=True)
    try:
        for sql in statements(sys.stdin):
            try:
                result = execute(db, sql, args.mode)
                if isinstance(result, tuple):
                    headers, rows = result
                    print("\t".join(headers))
                    for row in rows:
                        print("\t".join(json.dumps(v, ensure_ascii=False, allow_nan=False) for v in row))
                    print("END", flush=True)
                else:
                    print("OK " + str(result if result is not None else 0), flush=True)
            except DBError as error:
                print("ERROR " + error.code + " " + str(error), flush=True)
    finally:
        db.close()


if __name__ == "__main__":
    main()
