"""Bound additional ASR admissions using current, inspectable memory evidence.

This is an admission control for *additional* decoders, never a cancellation
policy. The first decoder can always make progress. Memory pressure is a signal,
not an estimate of free bytes: Warning may admit a second decoder after two
recent samples show no new swapouts and conservative reclaimable capacity.
"""
from __future__ import annotations

import os
import time

from .telemetry import memory_pressure_level, process_tree_sample, system_sample


class MemoryAdmission:
    def __init__(self, *, model_bytes=None, pressure=memory_pressure_level,
                 system_reader=None, process_reader=process_tree_sample,
                 clock=time.monotonic, sample_interval=1.0):
        self.model_bytes = model_bytes
        self.pressure = pressure
        self.system_reader = system_reader or (lambda: system_sample(pressure=self.pressure))
        self.process_reader = process_reader
        self.clock = clock
        self.sample_interval = sample_interval
        self.samples = []
        self.last_at = None
        self.sampled_active = 0
        self.extra_slot_suppressed = False

    def note_allocation_failure(self):
        """Do not refill a failed extra slot during this batch.

        Existing useful decoders continue. A later file can still take the
        first slot once it is free; the failed file needs an explicit retry.
        """
        self.extra_slot_suppressed = True

    def _sample(self):
        now = self.clock()
        if self.last_at is not None and now - self.last_at < self.sample_interval:
            return
        self.last_at = now
        try:
            system = self.system_reader()
            processes = self.process_reader(os.getpid())
        except (OSError, ValueError, TypeError):
            system, processes = {}, {}
        self.samples.append((now, system or {}, processes or {}))
        self.samples = self.samples[-3:]

    def decide(self, *, active, requested):
        """Return (admit, reason, evidence) for one further decoder.

        One worker remains live under Critical or missing telemetry, avoiding
        deadlock. This method does not kill or restart any already admitted job.
        """
        if active == 0:
            # The next additional decoder needs observations from the current
            # wave, not stale headroom from an earlier busy interval.
            self.samples = []
            self.last_at = None
            self.sampled_active = 0
            return True, "first_worker_progress", {"requested": requested, "admitted": 1}
        if active >= requested:
            return False, "worker_limit", {"requested": requested, "admitted": active}
        if self.extra_slot_suppressed:
            return False, "prior_allocation_failure", {"requested": requested, "admitted": active}
        if active > self.sampled_active:
            # In particular, never use the same pre-admission sample to approve
            # workers three through nine. Wait for current pressure/RSS and a
            # fresh swapout trend when reaching a new concurrency high. A
            # worker finishing only frees capacity, so its earlier high-water
            # observations remain usable while current samples keep updating.
            self.samples = []
            self.last_at = None
            self.sampled_active = active
        self._sample()
        if not self.samples:
            return False, "telemetry_unavailable", {"requested": requested, "admitted": active}
        _, system, processes = self.samples[-1]
        level = system.get("memory_pressure", "unknown")
        evidence = {"requested": requested, "admitted": active, "pressure": level}
        if level in {"critical", "unknown"}:
            return False, "pressure_" + level, evidence
        if level not in {"normal", "warning"}:
            return False, "pressure_unknown", evidence
        if processes.get("available") is not True or processes.get("root_present") is not True:
            return False, "process_telemetry_unavailable", evidence
        page_size = system.get("page_size_bytes")
        pages = system.get("vm_pages") or {}
        required = ("pages_free", "pages_inactive", "pages_speculative", "swapouts")
        if (type(page_size) is not int or page_size < 1
                or any(type(pages.get(key)) is not int or pages[key] < 0 for key in required)):
            return False, "telemetry_unavailable", evidence
        # Inactive pages may be reclaimable but are not promised free. Count only
        # half of free+inactive+speculative, preserving a 2x uncertainty margin.
        reclaimable = page_size * sum(pages[key] for key in required[:3]) // 2
        evidence["discounted_reclaimable_bytes"] = reclaimable
        if len(self.samples) < 2:
            return False, "awaiting_swap_trend", evidence
        before = self.samples[-2][1].get("vm_pages") or {}
        prior_swap = before.get("swapouts")
        if type(prior_swap) is not int or prior_swap > pages["swapouts"]:
            return False, "swap_trend_unknown", evidence
        swap_delta = pages["swapouts"] - prior_swap
        evidence["new_swapout_pages"] = swap_delta
        if swap_delta:
            return False, "active_swapouts", evidence
        if type(self.model_bytes) is not int or self.model_bytes < 1:
            return False, "model_cost_unknown", evidence
        # A separately loaded whisper.cpp context has its own model and scratch
        # memory. Two model-file sizes is a conservative estimate until an
        # observed decoder RSS is higher; add 25% for preparation/driver variance.
        descendants = processes.get("processes") or []
        observed = max((p.get("rss_bytes", 0) for p in descendants
                        if p.get("pid") != os.getpid() and type(p.get("rss_bytes")) is int), default=0)
        if observed < 1:
            return False, "decoder_rss_unavailable", evidence
        estimate = max(2 * self.model_bytes, observed)
        required_bytes = estimate * 5 // 4
        evidence.update({"model_bytes": self.model_bytes, "observed_child_rss_bytes": observed,
                         "additional_worker_budget_bytes": required_bytes})
        if reclaimable < required_bytes:
            return False, "insufficient_reclaimable_estimate", evidence
        return True, "headroom_verified", evidence
