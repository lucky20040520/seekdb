"""Official JSONL entry, with an explicit legacy SQL diagnostic option."""
import argparse
import json
import sys
from .engine import Database, DBError
from .sql import execute


def statements(stream):
    buffer, state, delimiter = [], "normal", None
    # Read only the next character needed for framing. readline()/line iteration
    # waits for a newline even after the driver has flushed a complete SQL;.
    while True:
        char = stream.read(1)
        if not char:
            break
        buffer.append(char)
        if state == "line":
            if char == "\n":
                state = "normal"
            continue
        if state == "block":
            if char == "*":
                state = "block_star"
            continue
        if state == "block_star":
            if char == "/":
                state = "normal"
            elif char != "*":
                state = "block"
            continue
        if state == "quoted":
            if char == delimiter:
                state = "quote_end"
            continue
        if state == "quote_end":
            if char == delimiter:
                state = "quoted"
                continue
            state = "normal"
        elif state == "hyphen":
            if char == "-":
                state = "line"
                continue
            state = "normal"
        elif state == "slash":
            if char == "*":
                state = "block"
                continue
            state = "normal"
        if char in ("'", "`"):
            delimiter, state = char, "quoted"
        elif char == "-":
            state = "hyphen"
        elif char == "/":
            state = "slash"
        elif char == ";":
            yield "".join(buffer)
            buffer = []
    if "".join(buffer).strip():
        yield "".join(buffer)


def main():
    # Diagnostic wire bytes are UTF-8 regardless of host locale/environment.
    # The parent's Popen(encoding=...) does not configure the child's streams.
    sys.stdin.reconfigure(encoding="utf-8", errors="strict")
    sys.stdout.reconfigure(encoding="utf-8", errors="strict")
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default="/data")
    parser.add_argument("--mode", choices=("exact", "ann"), default="exact")
    parser.add_argument("--protocol", choices=("jsonl", "diagnostic"), default="jsonl")
    args = parser.parse_args()
    db = Database(args.data_dir)
    if args.protocol == "jsonl":
        from .jsonl import serve
        serve(db)
        return
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
