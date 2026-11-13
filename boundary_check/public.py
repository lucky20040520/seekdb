"""Run the unmodified public checker in Linux against default Docker entries."""
import argparse
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to(ROOT):
        parser.error("output must be inside the workspace")
    output.mkdir(parents=True, exist_ok=False)
    result = {}
    for label,image in [("python","minivector-boundary:python-f1b9f4e"),
                        ("cpp","minivector-boundary:cpp-8c2bbcac8")]:
        command = ["docker","run","--rm","--network","none",
                   "--mount","type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock",
                   "--mount",f"type=bind,src={ROOT},dst=/work,readonly",
                   "--mount",f"type=bind,src={output},dst=/out",
                   "--entrypoint","python","minivectordb-protocol-check:driver",
                   "/work/protocol_check/run.py","--image",image,"--output",f"/out/{label}"]
        print("START official public " + label,flush=True)
        run = subprocess.run(command, stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=900)
        (output/f"{label}-driver.txt").write_bytes(run.stdout)
        result[label] = {"exit_code":run.returncode,"image":image}
        print(json.dumps(result[label]),flush=True)
    (output/"summary.json").write_text(json.dumps(result,indent=2),encoding="utf-8")
    return int(any(item["exit_code"] for item in result.values()))


if __name__=="__main__":
    raise SystemExit(main())
