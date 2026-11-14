"""Collect a private parent cgroup after its database child has exited.

Run from a Linux driver outside the measured cgroup. Only the randomly named
parent and the benchmark's own Docker volume are accessed. No cache dropping,
exec helpers in the database, or host-wide counter subtraction is used.
"""
import ctypes
import json
import os
from pathlib import Path
import subprocess
import time
import uuid

from .resources import parse_io_stat, parse_memory_peak


class Measurement:
    def __init__(self, args, volume):
        self.args = args
        self.name = "minivector-measure-" + uuid.uuid4().hex
        self.path = Path(args.cgroup_root) / self.name
        self.path.mkdir()
        try:
            (self.path / "cgroup.subtree_control").write_text("+cpu +memory +io +pids")
            info = json.loads(subprocess.check_output(["docker", "volume", "inspect", volume]))[0]
            mountpoint = Path(info["Mountpoint"])
            if not volume.startswith("perfcheck-data-") or mountpoint.name != "_data" or mountpoint.parent.name != volume:
                raise ValueError("unexpected volume mountpoint")
            self.data = Path(args.volume_root) / volume / "_data"
            dev = self.data.stat().st_dev
            self.device = f"{os.major(dev)}:{os.minor(dev)}"
        except BaseException:
            self.path.rmdir()
            raise

    def finish(self):
        raw = {"collector": "external-parent-cgroup-v2", "cgroup": self.name,
               "data_device": self.device, "errors": [], "sync_exit_code": None}
        io_bytes = memory_bytes = None
        try:
            # syncfs is performed by the driver, outside the measured parent.
            # Dirty inode writeback remains attributable to the writer's cgroup.
            libc = ctypes.CDLL(None, use_errno=True)
            fd = os.open(self.data, os.O_RDONLY | os.O_DIRECTORY)
            try:
                if libc.syncfs(fd):
                    raise OSError(ctypes.get_errno(), "data filesystem syncfs failed")
            finally:
                os.close(fd)
            raw["sync_exit_code"] = 0
            prior, stable = None, 0
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                snapshot = {name: (self.path / name).read_text() for name in
                            ("io.stat", "memory.peak", "memory.stat", "memory.events", "cgroup.events", "cpu.stat")}
                mem = dict(line.split() for line in snapshot["memory.stat"].splitlines())
                events = dict(line.split() for line in snapshot["cgroup.events"].splitlines())
                if events.get("populated") != "0":
                    raise RuntimeError("measurement must finish after all database processes exit")
                clean = mem.get("file_dirty") == "0" and mem.get("file_writeback") == "0"
                stable = stable + 1 if clean and snapshot["io.stat"] == prior else 0
                prior = snapshot["io.stat"]
                if stable >= 3:
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError("writeback or I/O counters did not settle")
            raw.update(snapshot)
            raw["settled_samples"] = stable
            raw["data_files"] = sum(1 for p in self.data.iterdir() if p.is_file())
            raw["logical_data_bytes"] = sum(p.stat().st_size for p in self.data.iterdir() if p.is_file())
            memory_bytes = parse_memory_peak(snapshot["memory.peak"])
            memory_events = dict(line.split() for line in snapshot["memory.events"].splitlines())
            if any(int(memory_events.get(key, "0")) for key in ("oom", "oom_kill", "max")):
                raise RuntimeError("memory resource limit reached")
            if not self.args.io_device or self.args.io_device != self.device:
                raise ValueError("explicit I/O device is absent or differs from the data volume")
            values = parse_io_stat(snapshot["io.stat"])[self.device]
            io_bytes = values["rbytes"] + values["wbytes"]
        except (OSError, ValueError, KeyError, RuntimeError) as exc:
            raw["errors"].append(str(exc))
        return {"io_bytes": io_bytes, "memory_bytes": memory_bytes, "raw": raw}

    def cleanup(self):
        for _ in range(100):
            try:
                self.path.rmdir()
                return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("benchmark cgroup is still busy: " + str(self.path))
