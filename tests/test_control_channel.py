"""Real local pipes and protocol coordination; no imports or inference of audio."""
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
import uuid

from audio_transcribe.cli import ControlReader
from audio_transcribe.scheduler import BatchCoordinator


class ControlChannelTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.settings = {"roots": {"app": str(Path(temporary.name) / "app")}}

    def coordinator(self, count=3, events=None):
        paths = [f"fixture-{i}.wav" for i in range(count)]
        items = [{"job_id": str(uuid.uuid4()), "path": path, "index": index}
                 for index, path in enumerate(paths)]
        coordinator = BatchCoordinator(self.settings, paths, items=items,
                                       batch_id=str(uuid.uuid4()), events=events)
        coordinator.start()
        return coordinator

    def channel(self, coordinator):
        read_fd, write_fd = os.pipe()
        stream = os.fdopen(read_fd, "rb", buffering=0)
        writer = os.fdopen(write_fd, "wb", buffering=0)
        reader = ControlReader(coordinator, stream)
        reader.start()
        def close():
            reader.close()
            writer.close()
            stream.close()
        self.addCleanup(close)
        return reader, writer

    def wait_for(self, predicate):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.01)
        self.fail("Synthetic pipe operation exceeded its bounded wait")

    @staticmethod
    def command(coordinator, index, **overrides):
        job = coordinator.jobs[index]
        return {"action": "cancel_job", "batch_id": coordinator.batch_id,
                "job_id": job.job_id, "attempt_id": job.attempt_id, **overrides}

    @staticmethod
    def send(writer, *commands):
        writer.write(b"".join(json.dumps(command).encode() + b"\n" for command in commands))

    def test_initial_queued_identity_and_stale_controls(self):
        events = []
        coordinator = self.coordinator(events=events.append)
        self.assertEqual([event["index"] for event in events], [0, 1, 2])
        self.assertTrue(all(event["state"] == "waiting" and event["stage"] == "queued"
                            and event["attempt_id"] for event in events))
        reader, writer = self.channel(coordinator)
        self.send(writer,
            self.command(coordinator, 0, batch_id=str(uuid.uuid4())),
            self.command(coordinator, 1, attempt_id=str(uuid.uuid4())),
            self.command(coordinator, 2, action="unknown_action"),
            self.command(coordinator, 2))
        self.wait_for(lambda: coordinator.jobs[2].cancelled)
        self.assertFalse(coordinator.jobs[0].cancelled)
        self.assertFalse(coordinator.jobs[1].cancelled)
        self.assertFalse(coordinator.cancel.cancelled)
        self.assertTrue(reader.thread.is_alive())

    def test_fragmented_command_and_malformed_lines_recover(self):
        coordinator = self.coordinator()
        reader, writer = self.channel(coordinator)
        command = self.command(coordinator, 0)
        # Queued native rows may not have observed attempt_id yet.
        command.pop("attempt_id")
        encoded = json.dumps(command).encode() + b"\n"
        writer.write(b"not JSON\n\xff\n[]\n" + encoded[:20])
        time.sleep(.04)
        self.assertFalse(coordinator.jobs[0].cancelled)
        writer.write(encoded[20:])
        self.wait_for(lambda: coordinator.jobs[0].cancelled)
        self.assertFalse(coordinator.jobs[1].cancelled)
        self.assertTrue(reader.thread.is_alive())

    def test_eof_cancels_unfinished_jobs_and_preserves_completed(self):
        coordinator = self.coordinator()
        coordinator._job_event(coordinator.jobs[0], "completed", "finished")
        reader, writer = self.channel(coordinator)
        writer.close()
        self.wait_for(lambda: not reader.thread.is_alive())
        self.assertTrue(coordinator.cancel.cancelled)
        self.assertEqual([job.state for job in coordinator.jobs], ["completed", "cancelled", "cancelled"])

    def test_explicit_close_stops_thread_without_disconnecting_batch(self):
        coordinator = self.coordinator()
        reader, writer = self.channel(coordinator)
        started = time.monotonic()
        reader.close()
        self.assertLess(time.monotonic() - started, 1.1)
        self.assertFalse(reader.thread.is_alive())
        writer.close()
        self.assertFalse(coordinator.cancel.cancelled)
        self.assertTrue(all(job.state == "waiting" for job in coordinator.jobs))

    def test_overlong_control_input_cancels_and_stops_reader(self):
        coordinator = self.coordinator()
        reader, writer = self.channel(coordinator)
        writer.write(b"x" * 65537)
        self.wait_for(lambda: not reader.thread.is_alive())
        self.assertTrue(coordinator.cancel.cancelled)

    def test_concurrent_emissions_remain_whole_json_lines_with_global_sequence(self):
        # Intentionally split each output write. The coordinator must serialize
        # the complete callback even when worker scheduling interrupts a line.
        read_fd, write_fd = os.pipe()
        stream = os.fdopen(read_fd, "rb", buffering=0)
        writer = os.fdopen(write_fd, "wb", buffering=0)
        received, errors = [], []
        def collect():
            try:
                for line in stream:
                    received.append(json.loads(line))
            except BaseException as error:
                errors.append(error)
        collector = threading.Thread(target=collect, daemon=True)
        collector.start()
        def emit(value):
            data = json.dumps(value).encode() + b"\n"
            midpoint = len(data) // 2
            writer.write(data[:midpoint])
            time.sleep(.0005)
            writer.write(data[midpoint:])
        coordinator = self.coordinator(events=emit)
        def worker(worker_id):
            for ordinal in range(15):
                coordinator.emit({"type": "batch", "stage": "fixture",
                                  "worker": worker_id, "ordinal": ordinal})
        workers = [threading.Thread(target=worker, args=(index,), daemon=True) for index in range(4)]
        try:
            for thread in workers:
                thread.start()
            for thread in workers:
                thread.join(3)
                self.assertFalse(thread.is_alive())
        finally:
            writer.close()
            collector.join(3)
            stream.close()
        self.assertFalse(collector.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(received), 63)
        self.assertEqual([event["seq"] for event in received], list(range(1, 64)))
        self.assertTrue(all(event["batch_id"] == coordinator.batch_id for event in received))
        self.assertEqual({(event["worker"], event["ordinal"]) for event in received[3:]},
                         {(worker, ordinal) for worker in range(4) for ordinal in range(15)})


if __name__ == "__main__":
    unittest.main()
