"""Mocked decoder protocol, not model-accuracy evidence."""
import contextlib
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import wave

from audio_transcribe import config, engine, storage
from audio_transcribe.timeline import TimelineError
from audio_transcribe.execution import CancellationToken, ExecutionOwner, OperationContext
from audio_transcribe.telemetry import Telemetry
from audio_transcribe.thermal import ThermalController


class DecodeReceiptTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.wav = self.root / "synthetic.wav"
        with wave.open(str(self.wav), "wb") as stream:
            stream.setparams((1, 2, 16000, 16000, "NONE", "not compressed"))
            stream.writeframes(b"\0\0" * 16000)
        self.output = self.root / "result"
        self.settings = {"roots": {"app": str(self.root / "app")}}
        self.runtime = {"runtime": {"cli": "/synthetic/whisper-cli", "sha256": "synthetic-runtime"}}
        self.model = {"path": "/synthetic/model", "sha256": "synthetic-model"}
        self.end_ms, self.reported_frames = 1000, 16000
        self.invocations = []
        self.thermal = ThermalController(clock=lambda: 0, monitor=False)
        self.thermal.ingest({"state": "nominal", "seq": 1, "monotonic": 0}, source="injected")

    def fake_process(self, args, **kwargs):
        self.invocations.append(args)
        storage.write_json(Path(args[args.index("--receipt") + 1]),
                           {"nonce": args[args.index("--nonce") + 1], "state": "reaped", "returncode": 0})
        prefix = Path(args[args.index("-of") + 1])
        storage.write_json(prefix.with_suffix(".json"), {"transcription": [
            {"offsets": {"from": 0, "to": self.end_ms}, "text": "A synthetic output fixture."}]})
        kwargs["stderr"].write(f"processing synthetic.wav ({self.reported_frames} samples, 1.0 sec)\n".encode())
        class Process:
            pid, returncode = 123456789, 0
            def wait(self, timeout=None):
                return 0
            def poll(self):
                return 0
        return Process()

    def decode(self, **kwargs):
        with contextlib.ExitStack() as stack:
            if kwargs.get("context") is None:
                owner = stack.enter_context(ExecutionOwner(self.settings, thermal=self.thermal))
                kwargs["context"] = OperationContext(owner, progress=kwargs.get("progress"))
            stack.enter_context(patch.object(engine.subprocess, "Popen", side_effect=self.fake_process))
            stack.enter_context(patch.object(engine.platform, "machine", return_value="synthetic-cpu"))
            return engine.decode(self.settings, self.runtime, self.model, self.wav, self.output,
                                 config.defaults()["asr"], {"terms": []}, **kwargs)

    def test_default_disables_context_in_actual_cli_and_has_valid_receipt(self):
        result = self.decode()
        args = self.invocations[0]
        self.assertEqual(args[args.index("-mc") + 1], "0")
        self.assertTrue(result["timestamp_valid"])
        self.assertTrue((self.output / "complete.json").exists())
        self.assertFalse((self.output / "provisional.json").exists())

    def test_slot_released_before_timeline_validation_and_lightweight_timing_recorded(self):
        telemetry = Telemetry("batch-synthetic")
        events = []
        original_audit = engine.audit_timeline
        with ExecutionOwner(self.settings, telemetry=telemetry, thermal=self.thermal) as owner:
            context = OperationContext(owner, telemetry=telemetry, job_id="job-synthetic",
                                       on_decode_finished=lambda: events.append("reaped"))
            def audit(*args, **kwargs):
                self.assertEqual(events, ["reaped"])
                # Exercise public admission, with a bounded failure if decoder
                # validation accidentally retains its one ASR reservation.
                cancel = CancellationToken()
                deadline = threading.Timer(1, cancel.cancel)
                deadline.start()
                try:
                    with OperationContext(owner, cancel=cancel).inference_slot():
                        self.assertFalse(cancel.cancelled, "Validation must release ASR capacity")
                finally:
                    deadline.cancel()
                    deadline.join()
                return original_audit(*args, **kwargs)
            with patch.object(engine, "audit_timeline", side_effect=audit), \
                    patch.object(engine, "memory_snapshot", side_effect=AssertionError("No ordinary profiling")):
                self.decode(context=context)
        stages = [event["stage"] for event in telemetry.snapshot()["stages"]]
        self.assertIn("asr_process", stages)
        self.assertIn("quality_check", stages)

    def test_invalid_timing_only_returns_explicit_provisional_receipt(self):
        self.end_ms = 1040
        result = self.decode(permit_review=True)
        self.assertEqual(result["state"], "review_required")
        self.assertFalse(result["timestamp_valid"])
        self.assertFalse((self.output / "complete.json").exists())
        self.assertTrue((self.output / "provisional.json").exists())
        self.assertEqual(storage.read_doc(self.output / "native.json")["transcription"][0]["offsets"]["to"], 1040)
        self.assertEqual(self.decode(permit_review=True), result)
        self.assertEqual(len(self.invocations), 1)

    def test_strict_decoder_call_still_rejects_invalid_timing(self):
        self.end_ms = 1040
        with self.assertRaises(TimelineError):
            self.decode()
        self.assertFalse((self.output / "complete.json").exists())
        self.assertFalse((self.output / "provisional.json").exists())

    def test_provisional_permission_never_bypasses_complete_sample_evidence(self):
        self.end_ms = 1040
        self.reported_frames = 15999
        with self.assertRaisesRegex(RuntimeError, "sample-count"):
            self.decode(permit_review=True)
        self.assertFalse((self.output / "complete.json").exists())
        self.assertFalse((self.output / "provisional.json").exists())

    def test_child_exit_race_does_not_replace_cancellation_with_failure(self):
        original_process = self.fake_process
        waits = []
        def running_process(args, **kwargs):
            original_process(args, **kwargs)
            class Process:
                pid, returncode = 123456789, None
                def poll(self):
                    return self.returncode
                def wait(self, timeout=None):
                    waits.append(timeout)
                    self.returncode = 0
                    return 0
            return Process()
        self.fake_process = running_process
        def cancel_at_inference(event):
            if event["stage"] == "transcribing":
                raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.decode(progress=cancel_at_inference)
        self.assertEqual(waits, [12])
        self.assertFalse((self.output / "complete.json").exists())
