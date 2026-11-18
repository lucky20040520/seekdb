"""Validate the material file and optionally its local evidence index and mirror."""
import argparse
import json
import re
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


def read_material(path):
    raw = path.read_bytes()
    if len(raw) > 128 * 1024:
        raise ValueError(f"material exceeds 128 KiB: {len(raw)} bytes")
    data = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object,
                      parse_constant=reject_constant)
    if not isinstance(data, dict):
        raise ValueError("top level must be an object")
    return raw, data


def validate(path):
    raw, _ = read_material(path)
    return len(raw)


def resolve(document, pointer):
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise ValueError(f"expected a pointer to a string field: {pointer!r}")
    value = document
    for token in pointer[1:].split("/"):
        if re.search(r"~(?![01])", token):
            raise ValueError(f"invalid JSON pointer escape: {pointer}")
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(value, list):
            if not re.fullmatch(r"0|[1-9][0-9]*", token) or int(token) >= len(value):
                raise ValueError(f"invalid array index in {pointer}")
            value = value[int(token)]
        elif isinstance(value, dict) and token in value:
            value = value[token]
        else:
            raise ValueError(f"unresolved JSON pointer: {pointer}")
    return value


def check_index(document):
    if document.get("rule_version") != "agent-learning-evidence-v3-unbounded":
        raise ValueError("unexpected rule_version")
    chains = document.get("evidence_chains")
    if not isinstance(chains, list):
        raise ValueError("missing local evidence_chains array")
    seen = set()
    for chain in chains:
        for field in ("student_evidence", "result_evidence"):
            refs = chain.get(field) if isinstance(chain, dict) else None
            if not isinstance(refs, list):
                raise ValueError(f"missing array: {field}")
            for ref in refs:
                if not isinstance(ref, dict):
                    raise ValueError(f"invalid reference in {field}")
                pointer, quote = ref.get("pointer"), ref.get("quote")
                value = resolve(document, pointer)
                if not isinstance(value, str) or not isinstance(quote, str) or not 1 <= len(quote) <= 240:
                    raise ValueError(f"invalid excerpt or target in {pointer}")
                if quote not in value or quote in seen:
                    raise ValueError(f"unmatched or repeated excerpt in {pointer}")
                seen.add(quote)
    return len(chains), len(seen)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, nargs="?", default=Path(__file__).resolve().parents[1] / "agent" / "conversation.json")
    parser.add_argument("--evidence", action="store_true", help="validate this repository's evidence_chains index")
    parser.add_argument("--mirror", type=Path, help="also require byte equality with this copy")
    args = parser.parse_args()
    try:
        raw, material = read_material(args.path)
        if args.mirror and raw != args.mirror.read_bytes():
            raise ValueError("material copies differ")
        if args.evidence:
            count, refs = check_index(material)
            print(f"PASS: {count} local chains, {refs} distinct exact excerpts and valid pointers")
    except (OSError, ValueError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        sys.exit(1)
    print(f"PASS: UTF-8 JSON object, unique keys, standard constants, {len(raw)}/131072 bytes")
    if args.mirror:
        print("PASS: byte-identical mirror")
    print("Content authenticity, completeness, secrets, and official acceptance require separate review.")
