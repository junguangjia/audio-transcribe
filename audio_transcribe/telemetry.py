"""Monotonic batch timing and opt-in, bounded benchmark resource sampling.

This module never writes protocol stdout, records transcript text, or infers an
accuracy score. Stage worker seconds may overlap and are not batch wall time.
Sampled RSS is neither physical footprint nor a true peak on unified memory.
"""
from __future__ import annotations

import contextlib
import copy
import ctypes
import json
import math
import os
from pathlib import Path
import platform
import re
import subprocess
import threading
import time
from datetime import datetime, timezone

from .storage import validate_id, write_json

TELEMETRY_VERSION = 1
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\Z")
_CACHE_KINDS = {"derived_audio", "transcript", "report", "decoder_receipt", "source_import"}
_TIMING = re.compile(r"^whisper_print_timings:\s+(load|fallback|mel|sample|encode|decode|batchd|prompt|total)\s+time\s*=\s*([0-9]+(?:\.[0-9]+)?)\s*ms(?:\s*/\s*([0-9]+)\s+runs)?")
_PRESSURE_NAMES = {1: "normal", 2: "warning", 4: "critical"}


def _label(value):
    if not isinstance(value, str) or not _LABEL.fullmatch(value):
        raise ValueError("Telemetry labels must be short machine identifiers.")
    return value


def _identity(job_id=None, attempt_id=None, index=None):
    result = {}
    for key, value in (("job_id", job_id), ("attempt_id", attempt_id)):
        if value is not None:
            result[key] = validate_id(value, key)
    if index is not None:
        if type(index) is not int or index < 0:
            raise ValueError("Telemetry index must be a nonnegative integer.")
        result["index"] = index
    return result


def parse_whisper_timings(text):
    """Parse only actual whisper timing counters; milliseconds become seconds.

    ``batchd`` is an internal decoder counter, not evidence that separate input
    files were tensor-batched. Repeated timing blocks are retained separately.
    """
    counters = []
    for line in text.splitlines():
        match = _TIMING.match(line.strip())
        if not match:
            continue
        name, milliseconds, runs = match.groups()
        seconds = float(milliseconds) / 1000
        if not math.isfinite(seconds):
            continue
        counter = {"counter": "model_load" if name == "load" else name,
                   "seconds": seconds, "reported_unit": "ms"}
        if runs is not None:
            counter["runs"] = int(runs)
        counters.append(counter)
    return {"counters": counters, "multi_file_tensor_batching": False,
            "interpretation": "Internal whisper.cpp counters; they may overlap and do not replace process or batch wall time."}


def memory_pressure_level():
    """Read macOS dispatch pressure flags without launching a process.

    Apple XNU's sysctl handler converts internal pressure enums to the public
    dispatch flags: 1 normal, 2 warning, 4 critical. Unknown never means normal.
    See bsd/kern/kern_memorystatus_notify.c and bsd/sys/event_private.h in XNU.
    """
    if platform.system() != "Darwin":
        return "unknown"
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        query = libc.sysctlbyname
        query.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
        query.restype = ctypes.c_int
        result = ctypes.c_uint32()
        size = ctypes.c_size_t(ctypes.sizeof(result))
        if query(b"kern.memorystatus_vm_pressure_level", ctypes.byref(result), ctypes.byref(size), None, 0) != 0:
            return "unknown"
        if size.value != ctypes.sizeof(result):
            return "unknown"
        return _PRESSURE_NAMES.get(result.value, "unknown")
    except (OSError, AttributeError):
        return "unknown"


class Telemetry:
    def __init__(self, batch_id, execution=None, *, clock=time.monotonic, started_at=None):
        self.batch_id = validate_id(batch_id, "batch ID")
        self.execution = json.loads(json.dumps(execution or {}, allow_nan=False))
        self._clock = clock
        observed = clock()
        # A simultaneous observation anchors monotonic durations to real clocks.
        # Caller submission remains monotonic; do not infer it from file mtimes.
        wall = datetime.now(timezone.utc)
        self.clock_anchor = {"observed_monotonic": observed, "utc": wall.isoformat(),
                             "local": wall.astimezone().isoformat()}
        if started_at is not None and (type(started_at) not in (int, float)
                                      or not math.isfinite(started_at) or started_at > observed):
            raise ValueError("Submission origin must be a finite, nonfuture monotonic time.")
        self._started = observed if started_at is None else started_at
        self._origin = "recorder_creation" if started_at is None else "caller_supplied_submission"
        self._lock = threading.RLock()
        self._events = []
        self._stages = []
        self._caches = []
        self._whisper = []
        self._milestones = {}
        self._jobs = {}

    def _elapsed(self):
        value = self._clock() - self._started
        if not math.isfinite(value) or value < 0:
            raise ValueError("Telemetry requires a monotonic clock.")
        return value

    def _event(self, kind, **fields):
        with self._lock:
            event = {"sequence": len(self._events) + 1, "batch_id": self.batch_id,
                     "elapsed_seconds": self._elapsed(), "kind": kind, **fields}
            self._events.append(event)
            return copy.deepcopy(event)

    @contextlib.contextmanager
    def span(self, stage, *, job_id=None, attempt_id=None, index=None):
        stage = _label(stage)
        identity = _identity(job_id, attempt_id, index)
        started = self._event("stage_start", stage=stage, **identity)
        outcome = "completed"
        try:
            yield
        except BaseException:
            outcome = "interrupted_or_failed"
            raise
        finally:
            with self._lock:
                finished = self._event("stage_end", stage=stage, outcome=outcome, **identity)
                self._stages.append({"stage": stage, **identity,
                                     "started_at_seconds": started["elapsed_seconds"],
                                     "ended_at_seconds": finished["elapsed_seconds"],
                                     "worker_seconds": finished["elapsed_seconds"] - started["elapsed_seconds"],
                                     "outcome": outcome})

    def mark(self, name, *, job_id=None, attempt_id=None, index=None):
        name = _label(name)
        identity = _identity(job_id, attempt_id, index)
        with self._lock:
            event = self._event("milestone", name=name, **identity)
            self._milestones.setdefault(name, event["elapsed_seconds"])
            if job_id is not None:
                self._jobs.setdefault(job_id, {**identity, "milestones": {}})["milestones"].setdefault(name, event["elapsed_seconds"])
            return event

    def cache(self, kind, hit, *, job_id=None, attempt_id=None, index=None):
        if kind not in _CACHE_KINDS or type(hit) is not bool:
            raise ValueError("Cache telemetry requires a known cache category and a boolean hit.")
        with self._lock:
            event = self._event("cache", cache=kind, hit=hit, **_identity(job_id, attempt_id, index))
            self._caches.append(event)
            return event

    def record_whisper_log(self, path, *, job_id=None, attempt_id=None, index=None):
        # Read incrementally; retain only recognized numeric infrastructure lines.
        with Path(path).open(encoding="utf-8", errors="replace") as handle:
            result = parse_whisper_timings("\n".join(line for line in handle if line.startswith("whisper_print_timings:")))
        with self._lock:
            self._whisper.append({**_identity(job_id, attempt_id, index), **result})
        return copy.deepcopy(result)

    def snapshot(self, audio_seconds=None):
        if audio_seconds is not None and (type(audio_seconds) not in (int, float) or not math.isfinite(audio_seconds) or audio_seconds < 0):
            raise ValueError("Audio duration must be finite and nonnegative.")
        with self._lock:
            observed = self._elapsed()
            completed = self._milestones.get("batch_complete")
            wall = completed if completed is not None else observed
            stage_totals = {}
            for stage in self._stages:
                stage_totals[stage["stage"]] = stage_totals.get(stage["stage"], 0.0) + stage["worker_seconds"]
            result = {"schema_version": TELEMETRY_VERSION, "batch_id": self.batch_id,
                      "clock": "monotonic", "origin": self._origin, "execution": self.execution,
                      "clock_anchor": self.clock_anchor, "submitted_monotonic": self._started,
                      "batch_wall_seconds": wall, "observation_wall_seconds": observed,
                      "batch_complete": completed is not None, "milestones": self._milestones,
                      "job_timings": list(self._jobs.values()), "stages": self._stages,
                      "stage_worker_seconds": stage_totals, "cache_events": self._caches,
                      "whisper_timings": self._whisper, "events": self._events,
                      "audio_seconds": audio_seconds,
                      "audio_seconds_per_batch_wall_second": audio_seconds / wall if audio_seconds is not None and wall > 0 and completed is not None else None,
                      "interpretation": {"worker_seconds": "Sum within each stage across workers; overlapping or nested stages must not be added as batch elapsed time.",
                                         "source_artifact_ready": "A saved source artifact exists; this does not mean the UI can read a report yet.",
                                         "first_readable_report": "Only mark after an actual readable report exists.",
                                         "system_page_cache": "Uncontrolled unless a benchmark separately records its conditions; model files present on disk are not an in-process loaded-model cache."}}
            return copy.deepcopy(result)

    def write(self, path, *, audio_seconds=None):
        write_json(path, self.snapshot(audio_seconds), overwrite=True)


def _command(args, timeout=2):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
        return result.stdout if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _cpu_seconds(value):
    try:
        days, clock = value.split("-", 1) if "-" in value else ("0", value)
        pieces = [float(piece) for piece in clock.split(":")]
        total = 0.0
        for piece in pieces:
            total = total * 60 + piece
        return total + int(days) * 86400
    except ValueError:
        return None


def process_tree_sample(root_pid, *, command=_command):
    # One process listing per interval, without command arguments or names.
    text = command(["/bin/ps", "-axo", "pid=,ppid=,rss=,%cpu=,time=,etime="])
    if text is None:
        return {"available": False, "processes": [], "root_present": None}
    processes = {}
    for line in text.splitlines():
        values = line.split()
        if len(values) != 6:
            continue
        try:
            pid, parent, rss = (int(value) for value in values[:3])
            cpu = float(values[3])
            if pid < 1 or parent < 0 or rss < 0 or not math.isfinite(cpu):
                continue
        except ValueError:
            continue
        processes[pid] = {"pid": pid, "ppid": parent, "rss_bytes": rss * 1024,
                          "cpu_percent": cpu, "cpu_seconds": _cpu_seconds(values[4]),
                          "process_elapsed_seconds": _cpu_seconds(values[5])}
    members = {root_pid} if root_pid in processes else set()
    changed = True
    while changed:
        previous = len(members)
        members.update(pid for pid, item in processes.items() if item["ppid"] in members)
        changed = len(members) != previous
    return {"available": True, "root_present": root_pid in processes,
            "processes": [processes[pid] for pid in sorted(members)]}


def system_sample(*, command=_command, pressure=memory_pressure_level):
    result = {"memory_pressure": pressure(), "page_size_bytes": None, "vm_pages": {}}
    text = command(["/usr/bin/vm_stat"])
    if text:
        match = re.search(r"page size of (\d+) bytes", text)
        if match:
            result["page_size_bytes"] = int(match[1])
        names = {"Pages free", "Pages active", "Pages inactive", "Pages speculative", "Pages wired down",
                 "Pages occupied by compressor", "Pages stored in compressor", "Swapins", "Swapouts"}
        for line in text.splitlines():
            match = re.fullmatch(r"([^:]+):\s*(\d+)\.?", line.strip())
            if match and match[1] in names:
                result["vm_pages"][match[1].lower().replace(" ", "_")] = int(match[2])
    return result


class ResourceSampler:
    """Opt-in finite benchmark observer; never controls or terminates processes."""
    def __init__(self, root_pid=None, *, interval_seconds=2.0, max_samples=600, system_every=5,
                 clock=time.monotonic, process_reader=process_tree_sample, system_reader=system_sample):
        if (type(interval_seconds) not in (int, float) or not math.isfinite(interval_seconds)
                or interval_seconds < 0.5 or type(max_samples) is not int
                or not 1 <= max_samples <= 10000 or type(system_every) is not int or system_every < 1):
            raise ValueError("Resource sampling requires a bounded cadence and sample count.")
        self.root_pid = os.getpid() if root_pid is None else root_pid
        if type(self.root_pid) is not int or self.root_pid < 1:
            raise ValueError("Resource sampler requires a valid root PID.")
        self.interval_seconds, self.max_samples, self.system_every = interval_seconds, max_samples, system_every
        self._clock, self._process_reader, self._system_reader = clock, process_reader, system_reader
        self._samples, self._lock, self._stop = [], threading.RLock(), threading.Event()
        self._started, self._thread = None, None
        self._stop_reason = None

    def sample(self):
        with self._lock:
            if self._started is None:
                self._started = self._clock()
            if len(self._samples) >= self.max_samples:
                self._stop_reason = "sample_limit"
                self._stop.set()
                return None
            number = len(self._samples)
            result = {"elapsed_seconds": self._clock() - self._started,
                      **self._process_reader(self.root_pid)}
            if number % self.system_every == 0:
                result["system"] = self._system_reader()
            self._samples.append(result)
            if result.get("root_present") is False:
                self._stop_reason = "root_exited"
                self._stop.set()
            elif len(self._samples) >= self.max_samples:
                self._stop_reason = "sample_limit"
                self._stop.set()
            return copy.deepcopy(result)

    def _run(self):
        while not self._stop.wait(self.interval_seconds):
            self.sample()

    def start(self):
        if self._thread is not None:
            raise RuntimeError("This resource sampler was already started.")
        self.sample()
        self._thread = threading.Thread(target=self._run, name="audio-benchmark-sampler", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=6)
            if self._thread.is_alive():
                raise RuntimeError("Resource sampler did not stop within its command deadlines.")
        if self._stop_reason is None:
            self._stop_reason = "requested"
        return self.snapshot()

    def snapshot(self):
        with self._lock:
            by_process = {}
            for sample in self._samples:
                for process in sample.get("processes", []):
                    pid = str(process["pid"])
                    item = by_process.setdefault(pid, {"pid": process["pid"], "sample_count": 0, "sampled_max_rss_bytes": 0})
                    item["sample_count"] += 1
                    item["sampled_max_rss_bytes"] = max(item["sampled_max_rss_bytes"], process["rss_bytes"])
            return copy.deepcopy({"schema_version": 1, "root_pid": self.root_pid, "interval_seconds": self.interval_seconds,
                                  "sample_limit": self.max_samples, "sample_count": len(self._samples),
                                  "stop_reason": self._stop_reason, "process_summaries": list(by_process.values()),
                                  "samples": self._samples,
                                  "interpretation": "Sampled RSS per process, not true peak or physical/unified-memory footprint. Values are not summed because shared mappings may be counted repeatedly. Short-lived children may be missed; a startup-only sample does not establish low memory use."})

    def __enter__(self):
        return self.start()

    def __exit__(self, exc_type, exc_value, traceback):
        self.stop()
