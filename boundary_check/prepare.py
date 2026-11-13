"""Export the requested revisions without changing the working checkout."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
REVISIONS = {
    "python": "f1b9f4e204f08f5043b315ba1ebec500ad7af255",
    "cpp": "8c2bbcac83953c360a0f55e601da8538e5b18326",
}


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    destination = args.output.resolve()
    if not destination.is_relative_to(ROOT):
        parser.error("snapshot directory must be inside the workspace")
    destination.mkdir(parents=True, exist_ok=False)
    result = {}
    for name, revision in REVISIONS.items():
        resolved = git("rev-parse", revision).decode().strip()
        subprocess.run(["git", "merge-base", "--is-ancestor", resolved,
                        "origin/2026-competition-round2"], cwd=ROOT, check=True)
        paths = git("ls-tree", "-r", "--name-only", resolved, "--", "Dockerfile",
                    "requirements.txt", ".dockerignore", "minivector", "cpp",
                    "mysqltest", "protocol_check", "STDIO_PROTOCOL.md").decode().splitlines()
        hashes = {}
        for relative in paths:
            target = (destination / name / relative).resolve()
            if not target.is_relative_to(destination / name):
                raise ValueError("unsafe archive path")
            content = git("show", f"{resolved}:{relative}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            hashes[relative] = hashlib.sha256(content).hexdigest()
        result[name] = {"revision": resolved, "source_sha256": hashes,
                        "context": str(destination / name)}
    (destination / "manifest.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({name: {"revision": entry["revision"],
                             "files": len(entry["source_sha256"])}
                      for name, entry in result.items()}, indent=2))


if __name__ == "__main__":
    main()
