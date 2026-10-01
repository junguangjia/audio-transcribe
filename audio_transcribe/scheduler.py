"""One batch owner with bounded preparation and inference slots.

Lock order: execution ownership -> import/media/session locks -> ASR slot.
No session lock is held while acquiring execution ownership. The coordinator
alone publishes results; workers return values and use a serialized event sink.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import errno
import threading
import time
import uuid

from .config import execution_policy
from .execution import CancellationToken, ExecutionOwner, OperationCancelled, OperationContext
from .memory_admission import MemoryAdmission
from .telemetry import Telemetry, memory_pressure_level

TERMINAL = {"completed", "review_required", "failed", "cancelled"}


@dataclass
class Job:
    index: int
    path: str
    job_id: str
    attempt_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    state: str = "waiting"
    cancelled: bool = False
    key: str | None = None
    duplicate_of: int | None = None
    reason: str | None = None
    restart_required: bool = False
    drained: bool = False


@dataclass
class WorkResult:
    value: object
    state: str = "completed"


@dataclass
class PreparedWork:
    value: object = None
    cached_result: WorkResult | None = None
    legacy: bool = False


@dataclass
class Outcome:
    job: Job
    result: WorkResult | None = None
    error: BaseException | None = None


@dataclass
class SharedWork:
    key: str
    jobs: list
    identity: object
    cancel: CancellationToken = field(default_factory=CancellationToken)
    context: object = None
    prepared: PreparedWork | None = None
    released: bool = False
    wait_span: object = None
    terminal: bool = False
    terminal_result: WorkResult | None = None
    terminal_error: BaseException | None = None


class BatchCoordinator:
    def __init__(self, settings, paths, *, execution=None, items=None, batch_id=None,
                 events=None, telemetry=None, pressure=None, submitted_monotonic=None,
                 thermal=None, memory=None):
        self.settings = settings
        self.policy = execution_policy(settings, execution, file_count=len(paths))
        self.batch_id = str(uuid.UUID(batch_id)) if batch_id else str(uuid.uuid4())
        if items is not None:
            if not isinstance(items, list) or len(items) != len(paths):
                raise ValueError("Task identities must match the ordered selections.")
            for i, item in enumerate(items):
                if (not isinstance(item, dict) or item.get("path") != str(paths[i])
                        or type(item.get("index")) is not int or item["index"] != i):
                    raise ValueError("Task identity/order mismatch.")
            ids = [str(uuid.UUID(item["job_id"])) for item in items]
        else:
            ids = [str(uuid.uuid4()) for _ in paths]
        if len(set(ids)) != len(ids):
            raise ValueError("Each selection needs a distinct task identity.")
        self.jobs = [Job(i, str(path), ids[i]) for i, path in enumerate(paths)]
        self.telemetry = telemetry or Telemetry(self.batch_id, execution=self.policy, started_at=submitted_monotonic)
        self.started = time.monotonic() if submitted_monotonic is None else submitted_monotonic
        self.events = events
        self.sequence = 0
        self.condition = threading.Condition(threading.RLock())
        self.cancel = CancellationToken()
        self.work = {}
        self.pending = []
        self.late_aliases = []
        self.identification_done = False
        self.outcomes = [None] * len(paths)
        self.admitted = set()
        # `thermal` remains an inert compatibility argument for older callers.
        self.memory = memory or MemoryAdmission(pressure=pressure or memory_pressure_level)
        self._last_admission = None
        self._allocation_seen = False
        self._admission_lock = threading.Lock()
        self.initialized = False
        self.owner = None
        self.max_prepared_ahead = 0
        self.validate_result = None

    def set_model_bytes(self, value):
        if type(value) is not int or value < 1:
            raise ValueError("Verified model size must be a positive byte count.")
        self.memory.model_bytes = value

    def _admission_event(self, *, requested, admitted, reason, evidence):
        signature = (requested, admitted, reason)
        if signature != self._last_admission:
            self._last_admission = signature
            self.emit({"type": "admission", "stage": "asr_admission", "requested_workers": requested,
                       "admitted_workers": admitted, "reason": reason, "resource_evidence": evidence})

    def emit(self, value):
        with self.condition:
            self.sequence += 1
            event = {"job_id": None, "attempt_id": None, "index": None,
                     "state": "processing", "stage": "finalizing", **value,
                     "batch_id": self.batch_id, "seq": self.sequence,
                     "elapsed": time.monotonic() - self.started}
            if self.events:
                self.events(event)
            return event

    def start(self):
        with self.condition:
            if not self.initialized:
                self.initialized = True
                for job in self.jobs:
                    self._job_event(job, "waiting", "queued")

    def _job_event(self, job, state, stage, *, event_type="file", **extra):
        with self.condition:
            if job.state in TERMINAL and state != job.state:
                return
            if job.cancelled and state != "cancelled":
                return
            job.state = state
            self.emit({"type": event_type, "job_id": job.job_id, "attempt_id": job.attempt_id,
                       "index": job.index, "total": len(self.jobs), "state": state,
                       "stage": stage, **extra})

    def cancel_job(self, job_id, attempt_id=None):
        with self.condition:
            job = next((j for j in self.jobs if j.job_id.lower() == str(job_id).lower()), None)
            if (job is None or job.state in TERMINAL
                    or (attempt_id and job.attempt_id.lower() != str(attempt_id).lower())):
                return False
            job.cancelled = True
            self._job_event(job, "cancelled", "cancelled")
            if job.key in self.work:
                work = self.work[job.key]
                # A later selected path may be the same bytes. Until its hash
                # is known, retain the shared computation for that alias.
                if self.identification_done and all(j.cancelled for j in work.jobs):
                    work.cancel.cancel()
            if all(j.state in TERMINAL for j in self.jobs):
                self.cancel.cancel()
            self.condition.notify_all()
            return True

    def cancel_batch(self):
        with self.condition:
            self.cancel.cancel()
            for job in self.jobs:
                self.cancel_job(job.job_id)
            for work in self.work.values():
                work.cancel.cancel()
            self.condition.notify_all()

    def control(self, value):
        if not isinstance(value, dict) or str(value.get("batch_id", "")).lower() != self.batch_id:
            return False
        if value.get("action") == "cancel_batch":
            self.cancel_batch()
            return True
        if value.get("action") == "cancel_job":
            return self.cancel_job(value.get("job_id"), value.get("attempt_id"))
        return False

    def _progress(self, work, detail):
        stage = detail.get("stage", "preparing")
        kind = "progress" if stage == "transcribing" else "file"
        safe = {key: detail[key] for key in ("percent",) if key in detail}
        for job in work.jobs:
            self._job_event(job, "processing", stage, event_type=kind, **safe)

    def _ownership_progress(self, detail):
        for job in self.jobs:
            if job.state not in TERMINAL:
                self._job_event(job, "waiting", detail.get("stage", "waiting_for_execution"))

    def _release(self, work, *args, **kwargs):
        with self.condition:
            if work.prepared is not None and not work.prepared.legacy:
                self.admitted.discard(work.key)
                work.released = True
                self.condition.notify_all()

    def _allocation_failure(self):
        with self._admission_lock:
            if self._allocation_seen:
                return
            self._allocation_seen = True
            self.memory.note_allocation_failure()
        with self.condition:
            self._admission_event(requested=self.policy["effective_asr_workers"],
                                  admitted=len(self.admitted), reason="prior_allocation_failure",
                                  evidence={"allocation_failure": True})
            self.condition.notify_all()

    def _finish_job(self, job, result=None, error=None):
        if self.outcomes[job.index] is not None:
            return
        job_error = error
        if not job.cancelled and error is None and self.validate_result:
            try:
                self.validate_result(job, result)
            except BaseException as caught:
                job_error = caught
        with self.condition:
            if job.cancelled or isinstance(job_error, OperationCancelled):
                job.cancelled = True
                self.outcomes[job.index] = Outcome(job, error=job_error if isinstance(job_error, OperationCancelled) else OperationCancelled())
                self._job_event(job, "cancelled", "restart_required" if job.restart_required else "cancelled",
                                reason=job.reason, restart_required=job.restart_required)
            else:
                self.outcomes[job.index] = Outcome(job, result, job_error)
                state = "failed" if job_error else result.state
                self._job_event(job, state, "finished")
            job.drained = self.owner is None or not self.owner._broken
            self.emit({"type": "drained" if job.drained else "cleanup_required", "job_id": job.job_id, "attempt_id": job.attempt_id,
                       "index": job.index, "state": job.state, "stage": "drained" if job.drained else "cleanup_required",
                       "reason": job.reason, "restart_required": job.restart_required})
            self.telemetry.mark("job_complete", job_id=job.job_id, index=job.index,
                                attempt_id=job.attempt_id)
            self.condition.notify_all()

    def _finish(self, work, result=None, error=None):
        with self.condition:
            if work.terminal:
                return
            self.admitted.discard(work.key)
            work.terminal = True
            work.terminal_result = result
            work.terminal_error = error
            jobs = list(work.jobs)
        if (isinstance(error, MemoryError)
                or isinstance(error, OSError) and error.errno == errno.ENOMEM
                or getattr(error, "resource_allocation_failure", False) is True):
            self._allocation_failure()
        for job in jobs:
            self._finish_job(job, result, error)

    def run(self, identify, prepare, execute, *, validate_result=None):
        """identify(job)->(key,metadata); prepare/execute receive a shared context.

        A bounded identifier produces work while ready earlier recordings
        advance. Shared computation is not canceled for a single alias until
        all later selected paths have been identified.
        """
        self.start()
        self.validate_result = validate_result

        def identify_all():
            try:
                for job in self.jobs:
                    if self.cancel.cancelled:
                        break
                    try:
                        self._job_event(job, "checking", "checking")
                        with self.telemetry.span("file_check", job_id=job.job_id):
                            key, metadata = identify(job)
                        with self.condition:
                            job.key = key
                            if key in self.work:
                                shared = self.work[key]
                                job.duplicate_of = shared.jobs[0].index + 1
                                shared.jobs.append(job)
                                if shared.terminal:
                                    self.late_aliases.append((shared, job))
                            else:
                                shared = SharedWork(key, [job], metadata)
                                self.work[key] = shared
                                self.pending.append(shared)
                            self.condition.notify_all()
                    except KeyboardInterrupt:
                        self.cancel_batch()
                        break
                    except Exception as error:
                        with self.condition:
                            self.outcomes[job.index] = Outcome(job, error=error)
                            self._job_event(job, "failed", "finished")
                            self.condition.notify_all()
            finally:
                with self.condition:
                    self.identification_done = True
                    for work in self.work.values():
                        if all(job.cancelled for job in work.jobs):
                            work.cancel.cancel()
                    self.condition.notify_all()

        identifier = threading.Thread(target=identify_all, name="audio-identify", daemon=True)
        identifier.start()
        workers = self.policy["effective_asr_workers"]
        prepare_workers = self.policy["effective_prepare_workers"]
        owner = ExecutionOwner(self.settings, cancel=self.cancel, progress=self._ownership_progress,
                               telemetry=self.telemetry, asr_workers=workers,
                               prepare_workers=prepare_workers)
        try:
            with owner:
                self.owner = owner
                self._pipeline(prepare, execute, workers)
        except (OperationCancelled, KeyboardInterrupt):
            self.cancel_batch()
        finally:
            identifier.join()
            for work in list(self.work.values()):
                if work.terminal:
                    for job in work.jobs:
                        if self.outcomes[job.index] is None:
                            self._finish_job(job, work.terminal_result, work.terminal_error)
                elif any(self.outcomes[j.index] is None for j in work.jobs):
                    self._finish(work, error=OperationCancelled())
            for job in self.jobs:
                if self.outcomes[job.index] is None:
                    self.outcomes[job.index] = Outcome(job, error=OperationCancelled())
        return self.outcomes

    def _pipeline(self, prepare, execute, workers):
        pending = self.pending
        prepared = []
        preparing = []
        running = {}
        preparation_workers = self.policy["effective_prepare_workers"]
        prepared_ahead = self.policy["effective_prepared_ahead"]
        # The one extra compute lane lets finalization overlap the next ASR;
        # the preparation pool and ready-ahead queue have separate finite caps.
        preparation_pool = ThreadPoolExecutor(preparation_workers, thread_name_prefix="audio-prepare")
        compute_pool = ThreadPoolExecutor(workers + 1, thread_name_prefix="audio-asr")
        def wake(_):
            with self.condition:
                self.condition.notify_all()
        try:
            while pending or preparing or prepared or running or self.late_aliases or not self.identification_done:
                try:
                    while self.late_aliases:
                        with self.condition:
                            work, job = self.late_aliases.pop(0)
                        self._finish_job(job, work.terminal_result, work.terminal_error)
                    # Drain in submission order so parallel preparation cannot
                    # silently change the user's file execution order.
                    while preparing and preparing[0][1].done():
                        work, future = preparing.pop(0)
                        try:
                            work.prepared = future.result()
                            if work.cancel.cancelled:
                                self._finish(work, error=OperationCancelled())
                            elif work.prepared.cached_result is not None:
                                self._finish(work, work.prepared.cached_result)
                            else:
                                prepared.append(work)
                                self.max_prepared_ahead = max(self.max_prepared_ahead, len(prepared))
                                work.wait_span = work.context.span("prepared_wait_for_asr")
                                work.wait_span.__enter__()
                                self._progress(work, {"stage": "waiting_for_slot"})
                        except BaseException as error:
                            self._finish(work, error=error)
                    for future in list(running):
                        if future.done():
                            work = running.pop(future)
                            try:
                                self._finish(work, future.result())
                            except BaseException as error:
                                self._finish(work, error=error)
                    # A cancelled ready item must drain even if memory pressure
                    # denies another decoder or the one-worker slot is full.
                    for work in list(prepared):
                        if not work.cancel.cancelled:
                            continue
                        prepared.remove(work)
                        if work.wait_span:
                            work.wait_span.__exit__(None, None, None)
                            work.wait_span = None
                        self._finish(work, error=OperationCancelled())
                    # Admission is released at decoder exit, before validation.
                    # Legacy sessions keep it until all their sources finalize.
                    if prepared and len(self.admitted) < workers and len(running) < workers + 1:
                        allow, reason, evidence = self.memory.decide(active=len(self.admitted), requested=workers)
                        if not allow:
                            self._progress(prepared[0], {"stage": "waiting_for_memory"})
                        if allow:
                            # The resource sample may block while a decoder
                            # reports allocation failure. Serialize only the
                            # final admission/submit against that callback.
                            with self._admission_lock, self.condition:
                                if self._allocation_seen and self.admitted:
                                    allow = False
                                    reason = "prior_allocation_failure"
                                    evidence = {"allocation_failure": True}
                                else:
                                    work = prepared.pop(0)
                                    work.wait_span.__exit__(None, None, None)
                                    work.wait_span = None
                                    if work.cancel.cancelled:
                                        reason = "cancelled_before_admission"
                                        self._finish(work, error=OperationCancelled())
                                    else:
                                        self.admitted.add(work.key)
                                        future = compute_pool.submit(execute, work.prepared.value, work.context)
                                        running[future] = work
                                        future.add_done_callback(wake)
                            if not allow:
                                self._progress(prepared[0], {"stage": "waiting_for_memory"})
                        if reason != "cancelled_before_admission":
                            self._admission_event(requested=workers, admitted=len(self.admitted),
                                                  reason=reason, evidence=evidence)
                    while (pending and len(preparing) < preparation_workers
                            and len(preparing) + len(prepared) < prepared_ahead
                            and (not running or self.policy["pipeline"])):
                        work = pending.pop(0)
                        if work.cancel.cancelled:
                            self._finish(work, error=OperationCancelled())
                        else:
                            first = work.jobs[0]
                            work.context = OperationContext(self.owner, cancel=work.cancel,
                                progress=lambda detail, w=work: self._progress(w, detail),
                                telemetry=self.telemetry, job_id=first.job_id,
                                attempt_id=first.attempt_id, index=first.index,
                                on_allocation_failure=self._allocation_failure,
                                on_decode_finished=lambda *a, w=work, **kw: self._release(w))
                            future = preparation_pool.submit(prepare, work.identity, work.context)
                            preparing.append((work, future))
                            future.add_done_callback(wake)
                    with self.condition:
                        if pending or preparing or prepared or running or self.late_aliases or not self.identification_done:
                            self.condition.wait(timeout=0.25)
                except KeyboardInterrupt:
                    self.cancel_batch()
        finally:
            if pending or preparing or prepared or running or self.late_aliases:
                for work in list(self.work.values()):
                    work.cancel.cancel()
            preparation_pool.shutdown(wait=True, cancel_futures=True)
            compute_pool.shutdown(wait=True, cancel_futures=True)
