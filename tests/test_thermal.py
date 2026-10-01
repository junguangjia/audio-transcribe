"""Temperature observations never control current-version work or ownership."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from audio_transcribe.execution import CancellationToken, ExecutionOwner, OperationCancelled, OperationContext
from audio_transcribe.scheduler import BatchCoordinator, PreparedWork, WorkResult
from audio_transcribe.thermal import ThermalController


class ThermalRemovalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = {"roots": {"app": str(self.root)}}
        self.now = 100.0

    def observation(self, state):
        controller = ThermalController(clock=lambda: self.now, monitor=True)
        if state is not None:
            self.assertTrue(controller.ingest({"state": state, "seq": 1,
                "monotonic": self.now}, source="injected"))
        return controller

    def test_all_states_stale_and_missing_observation_allow_two_slots(self):
        for state in ("nominal", "fair", "serious", "critical", "unknown", None):
            with self.subTest(state=state):
                controller = self.observation(state)
                events = []
                with ExecutionOwner(self.settings, asr_workers=2, thermal=controller,
                                    progress=events.append) as owner:
                    with OperationContext(owner).inference_slot(), OperationContext(owner).inference_slot():
                        self.assertEqual((owner._active_slots, owner._active_heavy), (2, 2))
                        self.now += 30  # Any prior observation is now stale.
                        OperationContext(owner).check_cancelled()
                    self.assertEqual((owner._active_slots, owner._active_heavy), (0, 0))
                self.assertFalse(any(event.get("type") == "thermal" or event.get("stage") == "cooling"
                                     for event in events))
                self.assertFalse((self.root / "thermal-policy.json").exists())

    def test_hot_transition_does_not_cancel_owned_compute(self):
        controller = self.observation("nominal")
        output = self.root / "child"
        output.mkdir()
        marker = output / "finished"
        with ExecutionOwner(self.settings, thermal=controller) as owner:
            context = OperationContext(owner)
            with context.inference_slot():
                child = owner.spawn([sys.executable, "-c",
                    f"import pathlib,time; time.sleep(.2); pathlib.Path({str(marker)!r}).touch()"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, output_dir=output)
                self.assertTrue(controller.ingest({"state": "critical", "seq": 2,
                    "monotonic": self.now}, source="injected"))
                deadline = time.monotonic() + 3
                while child.poll() is None and time.monotonic() < deadline:
                    context.check_cancelled()
                    time.sleep(.01)
                self.assertEqual(child.poll(), 0)
                self.assertTrue(marker.exists())
            self.assertFalse(owner._children)

    def test_old_latched_or_damaged_policy_is_left_untouched_and_ignored(self):
        policy = self.root / "thermal-policy.json"
        for saved in (
            {"schema_version": 1, "severe_latched": True, "recovery_required": True,
             "last_state": "critical", "last_reason": "thermal_critical"},
            "damaged legacy content",
        ):
            with self.subTest(saved=saved):
                original = (json.dumps(saved) if isinstance(saved, dict) else saved).encode()
                policy.write_bytes(original)
                with ExecutionOwner(self.settings, asr_workers=2,
                                    thermal=self.observation("critical")) as owner:
                    with OperationContext(owner).inference_slot(), OperationContext(owner).inference_slot():
                        self.assertEqual(owner._active_slots, 2)
                self.assertEqual(policy.read_bytes(), original)

    def test_missing_helper_cannot_delay_or_block_work(self):
        controller = self.observation(None)
        controller.start(self.settings)
        controller.unavailable("helper")
        with ExecutionOwner(self.settings, thermal=controller) as owner:
            with OperationContext(owner).inference_slot():
                owner.check()
        controller.close()
        self.assertEqual(controller.snapshot()["state"], "unknown")

    def test_heavy_budget_and_user_cancellation_still_work_under_critical_state(self):
        token, waiting, caught = CancellationToken(), threading.Event(), []
        with ExecutionOwner(self.settings, asr_workers=2,
                            thermal=self.observation("critical")) as owner:
            first, second, prep = (OperationContext(owner) for _ in range(3))
            with first.inference_slot(), second.inference_slot(), prep.heavy("preprocess"), prep.heavy("audio_decode"):
                self.assertEqual(owner._active_heavy, 3)
                def fourth():
                    context = OperationContext(owner, cancel=token,
                        progress=lambda event: waiting.set() if event.get("stage") == "waiting_for_resource" else None)
                    try:
                        with context.heavy("preprocess"):
                            caught.append("unexpected admission")
                    except OperationCancelled:
                        caught.append("cancelled")
                thread = threading.Thread(target=fourth)
                thread.start()
                self.assertTrue(waiting.wait(2))
                token.cancel()
                thread.join(2)
                self.assertFalse(thread.is_alive())
                self.assertEqual(caught, ["cancelled"])
            self.assertEqual((owner._active_slots, owner._active_heavy), (0, 0))

    def test_scheduler_ignores_injected_state_and_completes_each_file(self):
        for state in ("nominal", "fair", "serious", "critical", "unknown", None):
            with self.subTest(state=state):
                events = []
                coordinator = BatchCoordinator(self.settings, ["one", "two"],
                    execution={"mode": "pipeline", "asr_workers": 2},
                    pressure=lambda: "normal", thermal=self.observation(state), events=events.append)
                self.assertFalse(coordinator.control({"action": "thermal_state", "batch_id": coordinator.batch_id,
                    "state": state or "unknown", "seq": 2, "monotonic": self.now}))
                outcomes = coordinator.run(lambda job: (job.path, job.index),
                    lambda value, _: PreparedWork(value),
                    lambda value, context: WorkResult(value))
                self.assertEqual([item.job.state for item in outcomes], ["completed", "completed"])
                self.assertFalse(any(event["type"] == "thermal" for event in events))
                self.assertFalse(any(item.job.restart_required for item in outcomes))


if __name__ == "__main__":
    unittest.main()
