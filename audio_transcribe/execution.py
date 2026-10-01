"""Batch execution ownership, cancellable waits, and owned subprocess lifetimes.

Lock order is global owner, then session, then inference slot. The global lock
is closed rather than explicitly unlocked: inherited copies must continue to
reserve capacity after an unexpected coordinator or guardian exit.
"""
from __future__ import annotations

import contextlib
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid


class OperationCancelled(KeyboardInterrupt):
    """Cooperative cancellation of owned work, including a resource wait."""


class CancellationToken:
    def __init__(self):
        self._event = threading.Event()

    @property
    def cancelled(self):
        return self._event.is_set()

    def cancel(self):
        self._event.set()

    def check(self):
        if self.cancelled:
            raise OperationCancelled("Operation cancelled; completed artifacts are preserved.")

    def wait(self, seconds):
        self._event.wait(seconds)
        self.check()


@dataclass
class OperationContext:
    owner: "ExecutionOwner"
    cancel: CancellationToken | None = None
    progress: object = None
    telemetry: object = None
    job_id: str | None = None
    attempt_id: str | None = None
    index: int | None = None
    inference_slots: object = None
    on_decode_started: object = None
    on_decode_finished: object = None
    on_allocation_failure: object = None

    def check_cancelled(self):
        self.owner.check()
        if self.cancel is not None:
            self.cancel.check()

    @contextlib.contextmanager
    def heavy(self, kind):
        """Bound app-owned computation while preserving nested work and cancellation."""
        self.check_cancelled()
        previous = getattr(self, "_heavy_reserved", False)
        reservation = self.owner.heavy_slot(self) if not previous else contextlib.nullcontext()
        with reservation:
            self._heavy_reserved = True
            try:
                self.check_cancelled()
                yield
                if kind != "asr":
                    self.check_cancelled()
            finally:
                self._heavy_reserved = previous

    def emit(self, value):
        if self.progress is not None:
            self.progress(value)

    def span(self, stage):
        if self.telemetry is None:
            return contextlib.nullcontext()
        return self.telemetry.span(stage, job_id=self.job_id, attempt_id=self.attempt_id, index=self.index)

    def mark(self, name):
        if self.telemetry is not None:
            self.telemetry.mark(name, job_id=self.job_id, attempt_id=self.attempt_id, index=self.index)

    def cache(self, kind, hit):
        if self.telemetry is not None:
            self.telemetry.cache(kind, hit, job_id=self.job_id)

    @contextlib.contextmanager
    def inference_slot(self):
        external = self.inference_slots() if self.inference_slots else contextlib.nullcontext()
        with external, self.owner.slot(self), self.heavy("asr"):
            yield


class ExecutionOwner:
    def __init__(self, settings, *, cancel=None, progress=None, telemetry=None,
                 asr_workers=1, prepare_workers=1, thermal=None):
        if type(asr_workers) is not int or not 1 <= asr_workers <= 9:
            raise ValueError("Execution ownership supports one to nine ASR workers.")
        if type(prepare_workers) is not int or not 1 <= prepare_workers <= 4:
            raise ValueError("Execution ownership supports one to four preparation workers.")
        self.path = Path(settings["roots"]["app"]) / "inference.lock"
        self.settings = settings
        self.cancel = cancel or CancellationToken()
        self.progress, self.telemetry = progress, telemetry
        self.asr_workers = asr_workers
        self.prepare_workers = prepare_workers
        # ``thermal`` is accepted for older callers and injected fixtures only.
        # It has no authority over ownership, capacity, waits, or cancellation.
        self._slot_condition = threading.Condition()
        self._active_slots = 0
        self._active_heavy = 0
        self._children = set()
        self._children_lock = threading.Lock()
        self._handle = None
        self._broken = False

    @property
    def acquired(self):
        return self._handle is not None

    @property
    def fd(self):
        self.check()
        return self._handle.fileno()

    def check(self):
        self.cancel.check()
        if not self.acquired:
            raise RuntimeError("Execution ownership must be acquired before session work.")
        if self._broken:
            raise RuntimeError("An owned process guardian stopped unexpectedly; no further inference may start.")

    def __enter__(self):
        if self.acquired:
            raise RuntimeError("Execution ownership is not reentrant; pass the acquired context to nested work.")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        span = self.telemetry.span("global_wait") if self.telemetry else contextlib.nullcontext()
        try:
            with span:
                next_event = 0.0
                while True:
                    self.cancel.check()
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if self.progress and time.monotonic() >= next_event:
                            self.progress({"stage": "waiting_for_execution"})
                            next_event = time.monotonic() + 1
                        self.cancel.wait(0.2)
            self._handle = handle
            return self
        except BaseException:
            handle.close()
            self._handle = None
            raise

    def __exit__(self, exc_type, exc, traceback):
        with self._children_lock:
            children = list(self._children)
        for child in children:
            with contextlib.suppress(Exception):
                child.cancel()
        # Do not LOCK_UN. A surviving owned CLI still holds its inherited copy.
        if self._handle:
            self._handle.close()
            self._handle = None

    @contextlib.contextmanager
    def heavy_slot(self, context):
        """One total budget across ASR, decoding and preprocessing."""
        with context.span("heavy_wait"):
            next_event = 0.0
            while True:
                context.check_cancelled()
                with self._slot_condition:
                    if self._active_heavy < self.asr_workers + self.prepare_workers:
                        self._active_heavy += 1
                        break
                    self._slot_condition.wait(timeout=0.2)
                if time.monotonic() >= next_event:
                    context.emit({"stage": "waiting_for_resource"})
                    next_event = time.monotonic() + 1
        try:
            yield
        finally:
            with self._slot_condition:
                self._active_heavy -= 1
                self._slot_condition.notify_all()

    @contextlib.contextmanager
    def slot(self, context):
        with context.span("asr_wait"):
            next_event = 0.0
            while True:
                context.check_cancelled()
                with self._slot_condition:
                    if self._active_slots < self.asr_workers:
                        self._active_slots += 1
                        break
                    self._slot_condition.wait(timeout=0.2)
                if time.monotonic() >= next_event:
                    context.emit({"stage": "waiting_for_engine"})
                    next_event = time.monotonic() + 1
        try:
            context.check_cancelled()
            yield
        finally:
            with self._slot_condition:
                self._active_slots -= 1
                self._slot_condition.notify_all()

    def spawn(self, command, *, stdout, stderr, output_dir):
        self.check()
        child = OwnedProcess(self, command, stdout=stdout, stderr=stderr, output_dir=output_dir)
        with self._children_lock:
            self._children.add(child)
        return child

    def _forget(self, child):
        with self._children_lock:
            self._children.discard(child)


class OwnedProcess:
    """A guardian owns the direct CLI child; this parent owns the control pipe."""
    def __init__(self, owner, command, *, stdout, stderr, output_dir):
        self.owner = owner
        self.nonce = uuid.uuid4().hex
        self.receipt = Path(output_dir) / "guardian.json"
        self.returncode = None
        self._finished = False
        control_read, self._control_write = os.pipe()
        guardian = Path(__file__).with_name("process_guardian.py")
        args = [sys.executable, str(guardian), "--control-fd", str(control_read),
                "--lock-fd", str(owner.fd), "--receipt", str(self.receipt), "--nonce", self.nonce, "--", *command]
        try:
            self.process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                                            start_new_session=True, pass_fds=(control_read, owner.fd))
        except BaseException:
            os.close(self._control_write)
            self._control_write = None
            raise
        finally:
            os.close(control_read)

    @property
    def pid(self):
        return self.process.pid

    def _close_control(self):
        if self._control_write is not None:
            os.close(self._control_write)
            self._control_write = None

    def poll(self):
        if self._finished:
            return self.returncode
        if self.process.poll() is None:
            return None
        self._close_control()
        self.owner._forget(self)
        try:
            record = json.loads(self.receipt.read_text())
            if record.get("nonce") != self.nonce or record.get("state") != "reaped" or type(record.get("returncode")) is not int:
                raise ValueError("Invalid guardian receipt")
            self.returncode = record["returncode"]
            self._finished = True
            return self.returncode
        except (OSError, ValueError, KeyError):
            # A live orphan retains the lock fd. Poison this owner too, so its
            # scheduler cannot replace a lost guardian with another ASR worker.
            self.owner._broken = True
            raise RuntimeError("Owned process guardian ended without a reap receipt; inference ownership is retained by any surviving child.") from None

    def cancel(self):
        self._close_control()
        try:
            self.process.wait(timeout=12)
        except subprocess.TimeoutExpired:
            self.owner._broken = True
            # Signal only the still-owned guardian; never a stored child PID.
            with contextlib.suppress(ProcessLookupError):
                self.process.terminate()
            raise RuntimeError("Owned process guardian did not finish cancellation; no new inference may start.") from None
        finally:
            self.owner._forget(self)
        return self.poll()


@contextlib.contextmanager
def cancellable_file_lock(path, context, *, stage="waiting_for_resource"):
    """Wait on a shared file lock only after acquiring global execution ownership."""
    context.check_cancelled()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        next_event = 0.0
        while True:
            context.check_cancelled()
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= next_event:
                    context.emit({"stage": stage})
                    next_event = time.monotonic() + 1
                time.sleep(0.2)
        try:
            context.check_cancelled()
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def cancellable_session_lock(session_path, context):
    session_path = Path(session_path)
    if not session_path.is_dir():
        raise FileNotFoundError("Session directory does not exist.")
    with cancellable_file_lock(session_path / ".session.lock", context, stage="waiting_for_session"):
        yield
