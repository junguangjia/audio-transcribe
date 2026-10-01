"""Passive thermal observations for legacy injection and optional diagnostics.

Normal execution never constructs or reads this class. It has no admission,
cancel, cooldown, helper, or persisted-policy behavior. The old constructor and
ingestion API remain available to existing local callers during migration.
"""
from __future__ import annotations

import math
import threading
import time

STATES = {"nominal", "fair", "serious", "critical", "unknown"}


class ThermalController:
    def __init__(self, *, clock=time.monotonic, stale_seconds=25, monitor=True,
                 recovery_seconds=None, startup_wait_seconds=None):
        self.clock = clock
        self.stale_seconds = stale_seconds
        self._lock = threading.RLock()
        self._observed = {}
        self._callbacks = []

    def subscribe(self, callback):
        with self._lock:
            self._callbacks.append(callback)

    def snapshot(self):
        with self._lock:
            now = self.clock()
            fresh = [value for value in self._observed.values()
                     if now - value["monotonic"] <= self.stale_seconds]
            latest = max(fresh, key=lambda value: value["monotonic"]) if fresh else None
            return {"state": latest["state"] if latest else "unknown",
                    "telemetry_available": latest is not None}

    def ingest(self, value, *, source="native"):
        if (not isinstance(value, dict) or not isinstance(value.get("state"), str)
                or value["state"] not in STATES):
            return False
        seq, observed = value.get("seq"), value.get("monotonic")
        now = self.clock()
        if (type(seq) is not int or seq < 0 or type(observed) not in (int, float)
                or not math.isfinite(observed) or observed > now + 2
                or now - observed > self.stale_seconds):
            return False
        with self._lock:
            previous = self._observed.get(source)
            if previous and (seq <= previous["seq"] or observed < previous["monotonic"]):
                return False
            self._observed[source] = {"state": value["state"], "seq": seq,
                                      "monotonic": observed}
            callbacks = tuple(self._callbacks)
        for callback in callbacks:
            callback(self.snapshot())
        return True

    def unavailable(self, source="helper"):
        with self._lock:
            self._observed.pop(source, None)
            callbacks = tuple(self._callbacks)
        for callback in callbacks:
            callback(self.snapshot())

    def start(self, settings):
        """Legacy no-op: neither reads policy state nor launches a monitor."""

    def close(self):
        """Legacy no-op: there is no helper or persisted state to close."""
