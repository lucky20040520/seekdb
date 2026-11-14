"""Strict parsers for the cgroup v2 counters used by the benchmark.

An absent counter is unknown, not zero.  Callers must take both I/O
snapshots from the same explicitly identified device and reporting window.
"""

import re


_DEVICE = re.compile(r"[0-9]+:[0-9]+\Z")
_UNSIGNED = re.compile(r"[0-9]+\Z")


def parse_linux_device_number(text):
    """Decode Linux stat st_dev as the major:minor used in cgroup io.stat."""
    value = text.strip()
    if not _UNSIGNED.fullmatch(value) or int(value) >= 1 << 64:
        raise ValueError("invalid Linux device number")
    device = int(value)
    major = ((device & 0x00000000000fff00) >> 8) | ((device & 0xfffff00000000000) >> 32)
    minor = (device & 0x00000000000000ff) | ((device & 0x00000ffffff00000) >> 12)
    return f"{major}:{minor}"


def parse_io_stat(text):
    """Parse io.stat into device -> read/write byte counters.

    Unknown cgroup fields are allowed, but their syntax and uniqueness are
    validated.  An empty snapshot or a device missing either byte counter is
    invalid, so an unavailable measurement cannot silently become zero.
    """
    result = {}
    for line in text.splitlines():
        fields = line.split()
        if not fields:
            continue
        device = fields[0]
        if not _DEVICE.fullmatch(device) or device in result:
            raise ValueError("invalid or duplicate io.stat device")
        counters = {}
        for field in fields[1:]:
            if field.count("=") != 1:
                raise ValueError("invalid io.stat counter")
            key, value = field.split("=", 1)
            if not key or key in counters or not _UNSIGNED.fullmatch(value):
                raise ValueError("invalid or duplicate io.stat counter")
            counters[key] = int(value)
        if "rbytes" not in counters or "wbytes" not in counters:
            raise ValueError("io.stat missing byte counter")
        result[device] = {key: counters[key] for key in ("rbytes", "wbytes")}
    if not result:
        raise ValueError("empty io.stat snapshot")
    return result


def io_delta(before, after, device):
    """Return total bytes read and written on one device between snapshots."""
    if not isinstance(device, str) or not _DEVICE.fullmatch(device):
        raise ValueError("an explicit major:minor device is required")
    if device not in before or device not in after:
        raise ValueError("selected device missing from io.stat snapshot")
    total = 0
    for key in ("rbytes", "wbytes"):
        try:
            start, end = before[device][key], after[device][key]
        except (KeyError, TypeError) as exc:
            raise ValueError("missing byte counter") from exc
        if (not isinstance(start, int) or isinstance(start, bool) or start < 0
                or not isinstance(end, int) or isinstance(end, bool) or end < 0
                or end < start):
            raise ValueError("invalid or decreasing io.stat counter")
        total += end - start
    return total


def parse_memory_peak(text):
    """Parse memory.peak bytes; reject empty, negative and malformed values."""
    value = text.strip()
    if not _UNSIGNED.fullmatch(value):
        raise ValueError("invalid memory.peak")
    return int(value)
