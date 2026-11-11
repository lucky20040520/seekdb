"""Bounded, synchronous JSONL transport for a binary Popen process.

Reader and writer operations run on daemon threads so a stalled child cannot
hold the caller past the configured timeout. A failed session cannot be reused;
the owner remains responsible for terminating and cleaning up the process.
"""

import json
import math
import queue
import subprocess
import threading
import time


class ProtocolError(RuntimeError):
    """The child violated the protocol or exited before completing a request."""


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key: " + key)
        result[key] = value
    return result


def _float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("non-finite JSON number")
    return number


def _constant(value):
    raise ValueError("non-standard JSON constant: " + value)


class JsonlSession:
    def __init__(self, process, startup_timeout=120, request_timeout=60, max_response_bytes=16777216):
        if process.stdin is None or process.stdout is None or process.stderr is None:
            raise ValueError("process must have binary stdin, stdout and stderr pipes")
        if (isinstance(max_response_bytes, bool) or not isinstance(max_response_bytes, int)
                or max_response_bytes <= 0):
            raise ValueError("max_response_bytes must be a positive integer")
        for name, timeout in (("startup_timeout", startup_timeout), ("request_timeout", request_timeout)):
            if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                    or not math.isfinite(timeout) or timeout <= 0):
                raise ValueError(f"{name} must be a positive finite number")
        self.process = process
        self.startup_timeout = startup_timeout
        self.request_timeout = request_timeout
        self.max_response_bytes = max_response_bytes
        self._messages = queue.Queue()
        self._lock = threading.Lock()
        self._ready = False
        self._broken = False
        self._next_id = 1
        self._stderr = bytearray()
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self):
        try:
            while True:
                line = self.process.stdout.readline(self.max_response_bytes + 2)
                if not line:
                    self._messages.put(("eof", None))
                    return
                if not isinstance(line, bytes):
                    raise TypeError("stdout must be binary")
                if len(line) > self.max_response_bytes + 1 or not line.endswith(b"\n"):
                    self._messages.put(("error", "response exceeds limit or lacks newline"))
                    return
                self._messages.put(("line", line[:-1]))
        except Exception as exc:
            self._messages.put(("error", f"reading stdout failed: {exc}"))

    def _read_stderr(self):
        try:
            stream = self.process.stderr
            read = getattr(stream, "read1", stream.read)
            while True:
                chunk = read(4096)
                if not chunk:
                    return
                self._stderr.extend(chunk)
                if len(self._stderr) > 65536:
                    del self._stderr[:-65536]
        except (OSError, ValueError, TypeError):
            return

    @property
    def stderr_tail(self):
        return bytes(self._stderr).decode("utf-8", errors="replace")

    def _get(self, timeout):
        try:
            kind, value = self._messages.get(timeout=max(0, timeout))
        except queue.Empty as exc:
            raise TimeoutError("timed out waiting for JSONL response") from exc
        if kind == "eof":
            raise ProtocolError("process exited before response")
        if kind == "error":
            raise ProtocolError(value)
        try:
            obj = json.loads(value.decode("utf-8"), object_pairs_hook=_pairs,
                             parse_float=_float, parse_constant=_constant)
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise ProtocolError(f"invalid JSON response: {exc}") from exc
        if not isinstance(obj, dict):
            raise ProtocolError("JSON response must be an object")
        return obj

    def wait_ready(self):
        with self._lock:
            if self._ready or self._broken:
                raise ProtocolError("session is not awaiting ready")
            try:
                message = self._get(self.startup_timeout)
                if message != {"type": "ready"}:
                    raise ProtocolError("expected exact ready object")
                self._ready = True
                return message
            except Exception:
                self._broken = True
                raise

    def _request(self, payload):
        if not self._ready or self._broken:
            raise ProtocolError("session is not ready")
        if not self._messages.empty():
            self._broken = True
            raise ProtocolError("unsolicited response or exited process")
        deadline = time.monotonic() + self.request_timeout
        request_id = self._next_id
        self._next_id += 1
        payload = {"id": request_id, **payload}
        data = (json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")
        sent = queue.Queue(maxsize=1)

        def write():
            try:
                self.process.stdin.write(data)
                self.process.stdin.flush()
                sent.put(None)
            except Exception as exc:
                sent.put(exc)

        threading.Thread(target=write, daemon=True).start()
        try:
            try:
                error = sent.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty as exc:
                raise TimeoutError("timed out writing JSONL request") from exc
            if error is not None:
                raise ProtocolError(f"writing request failed: {error}") from error
            response = self._get(deadline - time.monotonic())
            if type(response.get("id")) is not int or response["id"] != request_id:
                raise ProtocolError("response id or its type differs from request")
            if type(response.get("ok")) is not bool:
                raise ProtocolError("response ok must be a boolean")
            return response
        except Exception:
            self._broken = True
            raise

    def execute(self, sql, mode="exact"):
        if not isinstance(sql, str) or mode not in ("exact", "ann"):
            raise ValueError("sql must be a string and mode must be exact or ann")
        with self._lock:
            return self._request({"op": "execute", "sql": sql, "search_mode": mode})

    def shutdown(self):
        with self._lock:
            start = time.monotonic()
            response = self._request({"op": "shutdown"})
            if not response["ok"]:
                self._broken = True
                raise ProtocolError("shutdown response indicates failure")
            try:
                self.process.stdin.close()
                remaining = max(0, self.request_timeout - (time.monotonic() - start))
                code = self.process.wait(timeout=remaining)
                self._reader.join(timeout=max(0, self.request_timeout - (time.monotonic() - start)))
                if self._reader.is_alive():
                    raise TimeoutError("timed out waiting for stdout to close")
                kind, _ = self._messages.get_nowait()
                if kind != "eof" or not self._messages.empty():
                    raise ProtocolError("extra stdout after shutdown")
                if code != 0:
                    raise ProtocolError(f"process exited with status {code}")
                self._ready = False
                return response
            except (subprocess.TimeoutExpired, queue.Empty) as exc:
                self._broken = True
                raise TimeoutError("timed out waiting for process shutdown") from exc
            except Exception:
                self._broken = True
                raise
