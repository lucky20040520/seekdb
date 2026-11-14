"""Validate post-exit writeback attribution on this Docker host before scoring."""
import argparse
import json
from pathlib import Path
from types import SimpleNamespace
import uuid

from .checker import docker, save
from .measurement import Measurement


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--image", required=True, help="image containing Python")
    p.add_argument("--io-device", required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    volume = "perfcheck-data-" + uuid.uuid4().hex
    docker("volume", "create", volume)
    measure = None
    try:
        measure = Measurement(SimpleNamespace(cgroup_root="/host-cgroup", volume_root="/host-volumes", io_device=args.io_device), volume)
        # The writer closes the file and exits without fsync. The parent
        # remains, and the external collector must account for the writeback.
        script = "import os; f=open('/data/delayed','wb'); f.write(b'x'*(8*1024*1024)); f.close(); os._exit(0)"
        docker("run", "--rm", "--network=none", "--cgroup-parent=/"+measure.name,
               "--mount", "type=volume,src="+volume+",dst=/data", "--entrypoint", "python", args.image, "-c", script)
        before = {name: (measure.path/name).read_text() for name in ("io.stat", "memory.stat", "cgroup.events")}
        result = measure.finish()
        result["before_external_sync"] = before
        result["passed"] = (not result["raw"]["errors"] and result["io_bytes"] >= 8*1024*1024
                            and result["memory_bytes"] > 8*1024*1024 and result["raw"]["logical_data_bytes"] == 8*1024*1024)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        save(args.output, result)
        print(json.dumps({key: result[key] for key in ("passed", "io_bytes", "memory_bytes")}), flush=True)
        if not result["passed"]:
            raise RuntimeError("post-exit writeback attribution probe failed")
    finally:
        if measure:
            measure.cleanup()
        docker("volume", "rm", volume, check=False)


if __name__ == "__main__":
    main()
