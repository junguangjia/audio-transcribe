"""Synthetic subprocess lifetimes and locks; never launches an ASR model."""
import contextlib
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from audio_transcribe.execution import (CancellationToken, ExecutionOwner, OperationContext,
                                        OperationCancelled, cancellable_session_lock)


def until(predicate, seconds=5):
    deadline = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("Synthetic lifecycle condition timed out")
        time.sleep(.01)


def lock_available(path):
    with path.open("a+b") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(stream, fcntl.LOCK_UN)
        return True


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = {"roots": {"app": str(self.root)}}

    def spawn(self, owner, script, name="child"):
        output = self.root / name
        output.mkdir()
        return owner.spawn([sys.executable, "-c", script], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, output_dir=output)

    def test_global_wait_is_cancellable_and_leaves_original_owner_intact(self):
        token, events, caught = CancellationToken(), [], []
        with ExecutionOwner(self.settings) as first:
            def waiter():
                try:
                    with ExecutionOwner(self.settings, cancel=token, progress=events.append):
                        self.fail("Second owner entered")
                except OperationCancelled:
                    caught.append(True)
            thread = threading.Thread(target=waiter)
            thread.start()
            until(lambda: events)
            token.cancel()
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(caught, [True])
            first.check()
            self.assertFalse(lock_available(first.path))
        self.assertTrue(lock_available(first.path))

    def test_session_wait_is_cancellable_and_does_not_reuse_an_unheld_owner(self):
        session = self.root / "session"
        session.mkdir()
        token, events, caught = CancellationToken(), [], []
        with ExecutionOwner(self.settings) as owner:
            context = OperationContext(owner, cancel=token, progress=events.append)
            with (session / ".session.lock").open("a+b") as held:
                fcntl.flock(held, fcntl.LOCK_EX)
                def waiter():
                    try:
                        with cancellable_session_lock(session, context):
                            self.fail("Session waiter entered")
                    except OperationCancelled:
                        caught.append(True)
                thread = threading.Thread(target=waiter)
                thread.start()
                until(lambda: events)
                token.cancel()
                thread.join(2)
                self.assertEqual(caught, [True])
                self.assertFalse(thread.is_alive())
        with self.assertRaises(RuntimeError), cancellable_session_lock(session, OperationContext(owner)):
            pass

    def test_bounded_slots_are_authoritative_with_waiting_cancellation(self):
        for capacity in (1, 2, 4, 9):
            with self.subTest(capacity=capacity), ExecutionOwner(self.settings, asr_workers=capacity) as owner:
                token, events, caught = CancellationToken(), [], []
                with contextlib.ExitStack() as stack:
                    for _ in range(capacity):
                        stack.enter_context(OperationContext(owner).inference_slot())
                    def waiter():
                        try:
                            with OperationContext(owner, cancel=token, progress=events.append).inference_slot():
                                self.fail("Exceeded capacity")
                        except OperationCancelled:
                            caught.append(True)
                    thread = threading.Thread(target=waiter)
                    thread.start()
                    until(lambda: events)
                    token.cancel()
                    thread.join(2)
                    self.assertEqual(caught, [True])
                with OperationContext(owner).inference_slot():
                    pass

    def test_owner_rejects_unbounded_worker_or_preparation_counts(self):
        for options in ({"asr_workers": 0}, {"asr_workers": 10},
                        {"prepare_workers": 0}, {"prepare_workers": 5}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                ExecutionOwner(self.settings, **options)

    def test_real_child_return_code_and_immediate_cancel_have_reap_receipts(self):
        with ExecutionOwner(self.settings) as owner:
            child = self.spawn(owner, "raise SystemExit(7)", "exit")
            until(lambda: child.poll() is not None)
            self.assertEqual(child.returncode, 7)
            self.assertEqual(json.loads(child.receipt.read_text())["reason"], "completed")
            child = self.spawn(owner, "import time; time.sleep(10)", "cancel")
            self.assertIn(child.cancel(), (130, -signal.SIGTERM))
            owner.check()
            self.assertFalse(owner._children)

    def test_cancelling_one_owned_child_preserves_other_child_and_global_owner(self):
        ready, finished = self.root / "other-ready", self.root / "other-finished"
        with ExecutionOwner(self.settings, asr_workers=2) as owner:
            first = self.spawn(owner, "import time; time.sleep(10)", "cancel-first")
            second = self.spawn(owner,
                f"import pathlib,time; pathlib.Path({str(ready)!r}).touch(); time.sleep(.4); pathlib.Path({str(finished)!r}).touch()",
                "keep-second")
            until(ready.exists)
            first.cancel()
            owner.check()
            until(lambda: second.poll() is not None)
            self.assertEqual(second.returncode, 0)
            self.assertTrue(finished.exists())
            self.assertFalse(lock_available(owner.path))

    def test_spawn_failure_has_receipt_without_poisoning_owner(self):
        with ExecutionOwner(self.settings) as owner:
            output = self.root / "missing"
            output.mkdir()
            child = owner.spawn(["/synthetic/nonexistent-executable"], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, output_dir=output)
            until(lambda: child.poll() is not None)
            self.assertEqual(child.returncode, 127)
            owner.check()

    def test_lost_guardian_poison_prevents_new_child_and_cli_holds_lock(self):
        marker = self.root / "ready"
        owner = ExecutionOwner(self.settings).__enter__()
        child = self.spawn(owner, f"import pathlib,time; pathlib.Path({str(marker)!r}).touch(); time.sleep(1.2)")
        try:
            until(marker.exists)
            child.process.kill()
            child.process.wait()
            with self.assertRaisesRegex(RuntimeError, "reap receipt"):
                child.poll()
            with self.assertRaisesRegex(RuntimeError, "guardian"):
                self.spawn(owner, "pass", "blocked")
        finally:
            owner.__exit__(None, None, None)
        self.assertFalse(lock_available(owner.path), "Actual child must inherit the global lock")
        until(lambda: lock_available(owner.path))

    @unittest.skipUnless(Path("/usr/bin/sandbox-exec").exists(), "macOS sandbox wrapper")
    def test_sandbox_wrapper_preserves_cli_lock_after_guardian_loss(self):
        marker = self.root / "sandbox-ready"
        owner = ExecutionOwner(self.settings).__enter__()
        output = self.root / "sandbox-child"
        output.mkdir()
        command = ["/usr/bin/sandbox-exec", "-p", "(version 1)(allow default)(deny network*)",
                   sys.executable, "-c",
                   f"import pathlib,time; pathlib.Path({str(marker)!r}).touch(); time.sleep(1.2)"]
        child = owner.spawn(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, output_dir=output)
        try:
            until(marker.exists)
            child.process.kill()
            child.process.wait()
            with self.assertRaisesRegex(RuntimeError, "reap receipt"):
                child.poll()
        finally:
            owner.__exit__(None, None, None)
        self.assertFalse(lock_available(owner.path), "Sandbox wrapper must preserve the CLI lock descriptor")
        until(lambda: lock_available(owner.path))

    def test_hard_killed_coordinator_retains_capacity_until_owned_child_is_reaped(self):
        # The child delays TERM briefly. This proves that parent death cannot
        # release capacity before the guardian finishes actual child cleanup.
        marker = self.root / "ready"
        child_script = ("import pathlib,time,signal; "
                        "signal.signal(signal.SIGTERM, lambda *a: (time.sleep(.6), exit(0))); "
                        f"pathlib.Path({str(marker)!r}).touch(); time.sleep(10)")
        script = f'''import pathlib,subprocess,time
from audio_transcribe.execution import ExecutionOwner
with ExecutionOwner({self.settings!r}) as owner:
    owner.spawn([{sys.executable!r}, "-c", {child_script!r}], stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, output_dir=pathlib.Path({str(self.root)!r}))
    time.sleep(20)
'''
        parent = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            until(marker.exists)
            parent.kill()
            parent.wait()
            self.assertFalse(lock_available(self.root / "inference.lock"))
            until(lambda: lock_available(self.root / "inference.lock"))
            receipt = json.loads((self.root / "guardian.json").read_text())
            self.assertEqual(receipt["reason"], "parent_cancelled_or_exited")
            self.assertEqual(receipt["returncode"], 0)
        finally:
            if parent.poll() is None:
                parent.kill()
                parent.wait()


if __name__ == "__main__":
    unittest.main()
