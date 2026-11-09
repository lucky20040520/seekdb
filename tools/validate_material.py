"""Check published mechanical material limits; does not evaluate learning quality."""
import json
import sys
from pathlib import Path


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError(f"non-standard JSON number: {value}")


def validate(path):
    raw = path.read_bytes()
    if len(raw) > 128 * 1024:
        raise ValueError(f"material exceeds 128 KiB: {len(raw)} bytes")
    data = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object,
                      parse_constant=reject_constant)
    if not isinstance(data, dict):
        raise ValueError("top level must be an object")
    return len(raw)


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / "agent" / "conversation.json"
    try:
        size = validate(target)
    except (OSError, ValueError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        sys.exit(1)
    print(f"PASS: UTF-8 JSON object, unique keys, standard constants, {size}/131072 bytes")
    print("Content authenticity, completeness, secrets, and official acceptance require separate review.")
