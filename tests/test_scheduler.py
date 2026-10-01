"""Bounded scheduler fixtures using the real owner lock, never an ASR process."""
from pathlib import Path
import tempfile
import threading
import time
import unittest
import uuid

from audio_transcribe.execution import ExecutionOwner, OperationCancelled
from audio_transcribe.scheduler import BatchCoordinator, PreparedWork, WorkResult


class StubMemory:
    model_bytes = None

    def __init__(self, permit=True):
        self.permit = permit
        self.allocation_failures = 0

    def note_allocation_failure(self):
        self.allocation_failures += 1
        self.permit = False

    def decide(self, *, active, requested):
        allowed = active == 0 or self.permit
        return allowed, "synthetic_headroom" if allowed else "synthetic_shortage", {
            "requested": requested, "admitted": active + int(allowed)}


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.settings = {"roots": {"app": str(Path(temporary.name) / "app")}}
        self.events = []

    def coordinator(self, count=3, *, workers=2, memory=None, keys=None,
                    prepare_workers=1, prepared_ahead=1, mode="pipeline"):
        paths = [f"fixture-{i}.wav" for i in range(count)]
        result = BatchCoordinator(self.settings, paths,
            execution={"mode": mode, "asr_workers": workers,
                       "prepare_workers": prepare_workers,
                       "prepared_ahead": prepared_ahead},
            events=self.events.append, memory=memory or StubMemory())
        identify = lambda job: ((keys or paths)[job.index], job.index)
        return result, identify

    def launch(self, coordinator, identify, prepare, execute, **kwargs):
        result, errors = [], []
        def run():
            try:
                result.extend(coordinator.run(identify, prepare, execute, **kwargs))
            except BaseException as error:
                errors.append(error)
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.addCleanup(coordinator.cancel_batch)
        return thread, result, errors

    def joined(self, launched):
        thread, result, errors = launched
        thread.join(5)
        self.assertFalse(thread.is_alive(), "Synthetic batch did not finish within its bound")
        if errors:
            raise errors[0]
        return result

    def wait_for(self, predicate, message="Synthetic state was not reached"):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.01)
        self.fail(message)

    @staticmethod
    def wait_gate(gate, context):
        deadline = time.monotonic() + 5
        while not gate.wait(.01):
            context.check_cancelled()
            if time.monotonic() >= deadline:
                raise TimeoutError("Synthetic worker gate timed out")
        context.check_cancelled()

    def test_capacity_and_preparation_bound(self):
        for workers in (1, 2, 4, 9):
            with self.subTest(workers=workers):
                self.events.clear()
                coordinator, identify = self.coordinator(count=workers + 1, workers=workers)
                release = threading.Event()
                self.addCleanup(release.set)
                lock = threading.Lock()
                prepared, active, maxima = [], set(), [0]
                def prepare(value, context):
                    context.check_cancelled()
                    with lock:
                        prepared.append(value)
                    return PreparedWork(value=value)
                def execute(value, context):
                    with context.inference_slot():
                        with lock:
                            active.add(value)
                            maxima[0] = max(maxima[0], len(active))
                        self.wait_gate(release, context)
                        with lock:
                            active.remove(value)
                    context.on_decode_finished()
                    return WorkResult(value)
                launched = self.launch(coordinator, identify, prepare, execute)
                self.wait_for(lambda: len(active) == workers and len(prepared) == workers + 1)
                # Full ASR slots may retain one prepared item; a second CPU
                # preparation must not start while that item waits.
                time.sleep(.15)
                self.assertEqual(len(prepared), workers + 1)
                self.assertEqual(maxima[0], workers)
                release.set()
                result = self.joined(launched)
                self.assertEqual([item.result.value for item in result], list(range(workers + 1)))
                self.assertLessEqual(coordinator.max_prepared_ahead, 1)
                self.assertLessEqual(maxima[0], workers)
                sequences = [event["seq"] for event in self.events]
                self.assertEqual(sequences, list(range(1, len(sequences) + 1)))

    def test_parallel_preparation_respects_bound_and_order(self):
        coordinator, identify = self.coordinator(count=5, workers=1,
            prepare_workers=2, prepared_ahead=3)
        release = threading.Event()
        self.addCleanup(release.set)
        preparing = set()
        maxima = [0]
        lock = threading.Lock()
        executed = []
        def prepare(value, context):
            with lock:
                preparing.add(value)
                maxima[0] = max(maxima[0], len(preparing))
            if value in (0, 1):
                self.wait_gate(release, context)
            with lock:
                preparing.remove(value)
            return PreparedWork(value)
        def execute(value, context):
            executed.append(value)
            with context.inference_slot():
                pass
            context.on_decode_finished()
            return WorkResult(value)
        launched = self.launch(coordinator, identify, prepare, execute)
        self.wait_for(lambda: maxima[0] == 2)
        self.assertLessEqual(maxima[0], 2)
        release.set()
        result = self.joined(launched)
        self.assertEqual(executed, list(range(5)))
        self.assertEqual([item.result.value for item in result], list(range(5)))
        self.assertLessEqual(coordinator.max_prepared_ahead, 3)

    def test_serial_mode_clamps_all_effective_capacities(self):
        coordinator, _ = self.coordinator(count=9, workers=9,
            prepare_workers=4, prepared_ahead=9, mode="serial")
        self.assertEqual(coordinator.policy["effective_asr_workers"], 1)
        self.assertEqual(coordinator.policy["effective_prepare_workers"], 1)
        self.assertEqual(coordinator.policy["effective_prepared_ahead"], 1)

    def test_completion_order_does_not_change_selection_order(self):
        coordinator, identify = self.coordinator(count=2)
        first_release = threading.Event()
        self.addCleanup(first_release.set)
        def execute(value, context):
            with context.inference_slot():
                if value == 0:
                    self.wait_gate(first_release, context)
            context.on_decode_finished()
            return WorkResult(value, "review_required" if value == 1 else "completed")
        launched = self.launch(coordinator, identify, lambda value, _: PreparedWork(value), execute)
        self.wait_for(lambda: coordinator.jobs[1].state == "review_required")
        self.assertNotEqual(coordinator.jobs[0].state, "completed")
        first_release.set()
        result = self.joined(launched)
        self.assertEqual([item.job.index for item in result], [0, 1])
        self.assertEqual([item.result.value for item in result], [0, 1])
        terminals = [e["index"] for e in self.events if e["type"] == "file" and e["state"] in {"completed", "review_required"}]
        self.assertEqual(terminals, [1, 0])

    def test_first_ready_file_dispatches_during_later_identification(self):
        coordinator, base_identify = self.coordinator(count=2, workers=1)
        release = threading.Event()
        second_checking = threading.Event()
        first_started = threading.Event()
        self.addCleanup(release.set)
        def identify(job):
            if job.index == 1:
                second_checking.set()
                self.assertTrue(release.wait(4))
            return base_identify(job)
        def execute(value, context):
            with context.inference_slot():
                if value == 0:
                    first_started.set()
            context.on_decode_finished()
            return WorkResult(value)
        launched = self.launch(coordinator, identify,
                               lambda value, _: PreparedWork(value), execute)
        self.assertTrue(second_checking.wait(3))
        self.assertTrue(first_started.wait(3), "First ready file waited for the later hash")
        self.assertFalse(release.is_set())
        release.set()
        self.assertEqual([item.result.value for item in self.joined(launched)], [0, 1])

    def test_late_duplicate_of_completed_work_is_finalized_once(self):
        coordinator, base_identify = self.coordinator(count=2, workers=1,
                                                      keys=["same", "same"])
        release = threading.Event()
        self.addCleanup(release.set)
        calls = []
        validated = []
        def identify(job):
            if job.index == 1:
                self.assertTrue(release.wait(4))
            return base_identify(job)
        def execute(value, context):
            calls.append(value)
            with context.inference_slot():
                pass
            context.on_decode_finished()
            return WorkResult("shared")
        launched = self.launch(coordinator, identify,
                               lambda value, _: PreparedWork(value), execute,
                               validate_result=lambda job, _: validated.append(job.index))
        self.wait_for(lambda: coordinator.jobs[0].state == "completed")
        self.assertEqual(calls, [0])
        release.set()
        result = self.joined(launched)
        self.assertEqual([item.result.value for item in result], ["shared", "shared"])
        self.assertEqual(validated, [0, 1])
        self.assertEqual(coordinator.jobs[1].duplicate_of, 1)
        self.assertEqual(calls, [0])

    def test_canceled_early_alias_preserves_later_unknown_duplicate(self):
        coordinator, base_identify = self.coordinator(count=2, workers=1,
                                                      keys=["same", "same"])
        identify_release = threading.Event()
        decoder_release = threading.Event()
        first_started = threading.Event()
        self.addCleanup(identify_release.set)
        self.addCleanup(decoder_release.set)
        def identify(job):
            if job.index == 1:
                self.assertTrue(identify_release.wait(4))
            return base_identify(job)
        def execute(value, context):
            with context.inference_slot():
                first_started.set()
                self.wait_gate(decoder_release, context)
            context.on_decode_finished()
            return WorkResult("shared")
        launched = self.launch(coordinator, identify,
                               lambda value, _: PreparedWork(value), execute)
        self.assertTrue(first_started.wait(3))
        self.assertTrue(coordinator.cancel_job(coordinator.jobs[0].job_id))
        self.assertFalse(coordinator.work["same"].cancel.cancelled)
        identify_release.set()
        self.wait_for(lambda: coordinator.jobs[1].key == "same")
        self.assertFalse(coordinator.work["same"].cancel.cancelled)
        decoder_release.set()
        result = self.joined(launched)
        self.assertIsInstance(result[0].error, OperationCancelled)
        self.assertEqual(result[1].result.value, "shared")

    def test_next_decode_overlaps_finalization_only_for_single_source_work(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                coordinator, identify = self.coordinator(count=2, workers=1)
                finalized = threading.Event()
                second_started = threading.Event()
                first_reaped = threading.Event()
                self.addCleanup(finalized.set)
                def execute(value, context):
                    with context.inference_slot():
                        if value == 1:
                            second_started.set()
                    context.on_decode_finished()
                    if value == 0:
                        first_reaped.set()
                        self.wait_gate(finalized, context)
                    return WorkResult(value)
                launched = self.launch(coordinator, identify,
                    lambda value, _: PreparedWork(value=value, legacy=legacy), execute)
                self.assertTrue(first_reaped.wait(3))
                if legacy:
                    self.assertFalse(second_started.wait(.4))
                else:
                    self.assertTrue(second_started.wait(3))
                finalized.set()
                self.assertEqual(len(self.joined(launched)), 2)

    def test_memory_fallback_preserves_progress_without_starvation(self):
        for reason in ("critical", "telemetry_unknown", "insufficient_resources"):
            with self.subTest(reason=reason):
                memory = StubMemory(permit=False)
                coordinator, identify = self.coordinator(count=2, memory=memory)
                release = threading.Event()
                self.addCleanup(release.set)
                started = []
                def execute(value, context):
                    with context.inference_slot():
                        started.append(value)
                        if value == 0:
                            self.wait_gate(release, context)
                    context.on_decode_finished()
                    return WorkResult(value)
                launched = self.launch(coordinator, identify, lambda value, _: PreparedWork(value), execute)
                self.wait_for(lambda: any(e["stage"] == "waiting_for_memory" for e in self.events))
                self.assertEqual(started, [0])
                release.set()
                self.assertEqual(len(self.joined(launched)), 2)
                self.assertEqual(started, [0, 1])
                self.events.clear()

    def test_dynamic_memory_slot_increase_starts_second_decoder(self):
        memory = StubMemory(permit=False)
        coordinator, identify = self.coordinator(count=2, memory=memory)
        release = threading.Event()
        self.addCleanup(release.set)
        active = set()
        def execute(value, context):
            with context.inference_slot():
                active.add(value)
                self.wait_gate(release, context)
                active.remove(value)
            context.on_decode_finished()
            return WorkResult(value)
        launched = self.launch(coordinator, identify, lambda value, _: PreparedWork(value), execute)
        self.wait_for(lambda: any(e.get("reason") == "synthetic_shortage" for e in self.events))
        self.assertEqual(active, {0})
        memory.permit = True
        self.wait_for(lambda: active == {0, 1})
        release.set()
        self.assertEqual(len(self.joined(launched)), 2)
        self.assertTrue(any(e.get("type") == "admission" and e.get("admitted_workers") == 2
                            for e in self.events))

    def test_cancelled_ready_item_drains_and_later_cache_hit_publishes(self):
        memory = StubMemory(permit=False)
        coordinator, identify = self.coordinator(count=3, memory=memory)
        release = threading.Event()
        self.addCleanup(release.set)
        started = threading.Event()
        def prepare(value, context):
            return PreparedWork(cached_result=WorkResult("cache")) if value == 2 else PreparedWork(value)
        def execute(value, context):
            with context.inference_slot():
                if value == 0:
                    started.set()
                    self.wait_gate(release, context)
            context.on_decode_finished()
            return WorkResult(value)
        launched = self.launch(coordinator, identify, prepare, execute)
        self.assertTrue(started.wait(3))
        self.wait_for(lambda: coordinator.jobs[1].state == "processing"
                      and any(e.get("index") == 1 and e.get("stage") == "waiting_for_memory" for e in self.events))
        self.assertTrue(coordinator.cancel_job(coordinator.jobs[1].job_id))
        self.wait_for(lambda: coordinator.jobs[1].drained and coordinator.jobs[2].state == "completed")
        self.assertFalse(release.is_set(), "First decoder must still be active when cache is published")
        release.set()
        result = self.joined(launched)
        self.assertEqual([item.job.state for item in result], ["completed", "cancelled", "completed"])

    def test_allocation_failure_suppresses_refilling_extra_slot(self):
        memory = StubMemory()
        coordinator, identify = self.coordinator(count=3, memory=memory)
        release = threading.Event()
        self.addCleanup(release.set)
        started = []
        def execute(value, context):
            with context.inference_slot():
                started.append(value)
                if value == 0:
                    self.wait_gate(release, context)
                elif value == 1:
                    raise MemoryError("synthetic allocation failure")
            context.on_decode_finished()
            return WorkResult(value)
        launched = self.launch(coordinator, identify, lambda value, _: PreparedWork(value), execute)
        self.wait_for(lambda: coordinator.jobs[1].state == "failed")
        self.assertEqual(memory.allocation_failures, 1)
        self.wait_for(lambda: any(e.get("reason") == "synthetic_shortage" for e in self.events))
        self.assertEqual(started, [0, 1])
        release.set()
        result = self.joined(launched)
        self.assertEqual([item.job.state for item in result], ["completed", "failed", "completed"])
        self.assertEqual(started, [0, 1, 2])

    def test_allocation_report_during_sample_wins_before_second_submit(self):
        memory = StubMemory()
        coordinator, identify = self.coordinator(count=2, memory=memory)
        ordinary_decide = memory.decide
        injected = [False]
        def race_decide(*, active, requested):
            if active == 1 and not injected[0]:
                injected[0] = True
                coordinator._allocation_failure()
                return True, "stale_headroom", {"requested": requested, "admitted": 2}
            return ordinary_decide(active=active, requested=requested)
        memory.decide = race_decide
        release = threading.Event()
        self.addCleanup(release.set)
        started = []
        def execute(value, context):
            with context.inference_slot():
                started.append(value)
                if value == 0:
                    self.wait_gate(release, context)
            context.on_decode_finished()
            return WorkResult(value)
        launched = self.launch(coordinator, identify, lambda value, _: PreparedWork(value), execute)
        self.wait_for(lambda: injected[0])
        self.wait_for(lambda: any(e.get("reason") == "prior_allocation_failure" and
                                  e.get("type") == "admission" for e in self.events))
        time.sleep(.2)
        self.assertEqual(started, [0])
        self.assertEqual(memory.allocation_failures, 1)
        release.set()
        self.assertEqual([item.job.state for item in self.joined(launched)], ["completed", "completed"])

    def test_duplicate_cancellation_preserves_other_consumer(self):
        coordinator, identify = self.coordinator(count=2, keys=["same", "same"])
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        calls = []
        def execute(value, context):
            calls.append(value)
            with context.inference_slot():
                started.set()
                self.wait_gate(release, context)
            context.on_decode_finished()
            return WorkResult("shared transcript")
        launched = self.launch(coordinator, identify, lambda value, _: PreparedWork(value), execute)
        self.assertTrue(started.wait(3))
        self.assertFalse(coordinator.cancel_job(coordinator.jobs[0].job_id, str(uuid.uuid4())))
        self.assertTrue(coordinator.cancel_job(coordinator.jobs[0].job_id, coordinator.jobs[0].attempt_id))
        self.assertFalse(coordinator.work["same"].cancel.cancelled)
        release.set()
        result = self.joined(launched)
        self.assertEqual(calls, [0])
        self.assertIsInstance(result[0].error, OperationCancelled)
        self.assertEqual(result[1].result.value, "shared transcript")
        self.assertEqual(result[1].job.duplicate_of, 1)

    def test_cancelling_all_duplicate_consumers_stops_shared_work(self):
        coordinator, identify = self.coordinator(count=2, keys=["same", "same"])
        started = threading.Event()
        def execute(value, context):
            with context.inference_slot():
                started.set()
                self.wait_gate(threading.Event(), context)
            return WorkResult(value)
        launched = self.launch(coordinator, identify, lambda value, _: PreparedWork(value), execute)
        self.assertTrue(started.wait(3))
        for job in coordinator.jobs:
            coordinator.cancel_job(job.job_id)
        result = self.joined(launched)
        self.assertTrue(coordinator.work["same"].cancel.cancelled)
        self.assertTrue(all(isinstance(item.error, OperationCancelled) for item in result))

    def test_failure_does_not_cancel_unrelated_files_and_cache_skips_decode(self):
        coordinator, identify = self.coordinator(count=4)
        executed = []
        def prepare(value, context):
            if value == 0:
                raise ValueError("Synthetic invalid media")
            if value == 2:
                return PreparedWork(cached_result=WorkResult(value, "review_required"))
            return PreparedWork(value)
        def execute(value, context):
            executed.append(value)
            with context.inference_slot():
                if value == 1:
                    raise RuntimeError("Synthetic decoder failure")
            return WorkResult(value)
        result = coordinator.run(identify, prepare, execute)
        self.assertEqual([item.job.state for item in result], ["failed", "failed", "review_required", "completed"])
        self.assertEqual(executed, [1, 3])
        self.assertEqual(result[2].result.value, 2)

    def test_global_owner_wait_is_cancellable_without_acquiring(self):
        for individual in (False, True):
            with self.subTest(individual=individual):
                self.events.clear()
                coordinator, identify = self.coordinator(count=2)
                entered = []
                with ExecutionOwner(self.settings):
                    launched = self.launch(coordinator, identify,
                        lambda value, _: entered.append(value) or PreparedWork(value),
                        lambda value, _: WorkResult(value))
                    self.wait_for(lambda: any(e["stage"] == "waiting_for_execution" for e in self.events))
                    if individual:
                        for job in coordinator.jobs:
                            coordinator.cancel_job(job.job_id)
                    else:
                        coordinator.cancel_batch()
                    result = self.joined(launched)
                    self.assertEqual(entered, [])
                    self.assertTrue(all(isinstance(item.error, OperationCancelled) for item in result))

    def test_alias_cancelled_during_identification_does_not_poison_later_alias(self):
        coordinator, base_identify = self.coordinator(count=2, keys=["same", "same"])
        def identify(job):
            if job.index == 1:
                coordinator.cancel_job(coordinator.jobs[0].job_id)
            return base_identify(job)
        result = coordinator.run(identify, lambda value, _: PreparedWork(value), lambda value, _: WorkResult(value))
        self.assertIsInstance(result[0].error, OperationCancelled)
        self.assertEqual(result[1].result.value, 0)

    def test_selected_alias_validation_failure_is_scoped(self):
        coordinator, identify = self.coordinator(count=2, keys=["same", "same"])
        def validate(job, _):
            if job.index == 1:
                raise ValueError("Synthetic selection changed after preparation")
        result = coordinator.run(identify, lambda value, _: PreparedWork(value),
                                 lambda value, _: WorkResult(value), validate_result=validate)
        self.assertEqual([item.job.state for item in result], ["completed", "failed"])
        self.assertIsInstance(result[1].error, ValueError)

    def test_batch_cancel_preserves_completed_job_and_cancels_preparation(self):
        coordinator, identify = self.coordinator(count=3, workers=1)
        preparing = threading.Event()
        executed = []
        def prepare(value, context):
            if value == 1:
                preparing.set()
                self.wait_gate(threading.Event(), context)
            return PreparedWork(value)
        def execute(value, context):
            executed.append(value)
            with context.inference_slot():
                context.check_cancelled()
            context.on_decode_finished()
            return WorkResult(value)
        launched = self.launch(coordinator, identify, prepare, execute)
        self.assertTrue(preparing.wait(3))
        self.wait_for(lambda: coordinator.jobs[0].state == "completed")
        coordinator.cancel_batch()
        result = self.joined(launched)
        self.assertEqual([item.job.state for item in result], ["completed", "cancelled", "cancelled"])
        self.assertEqual(executed, [0])
        self.assertEqual(result[0].result.value, 0)

    def test_cancelled_prepared_job_does_not_start_decoder(self):
        coordinator, identify = self.coordinator(count=3, workers=1)
        release = threading.Event()
        self.addCleanup(release.set)
        executed = []
        def execute(value, context):
            executed.append(value)
            with context.inference_slot():
                if value == 0:
                    self.wait_gate(release, context)
            context.on_decode_finished()
            return WorkResult(value)
        launched = self.launch(coordinator, identify, lambda value, _: PreparedWork(value), execute)
        self.wait_for(lambda: any(e["index"] == 1 and e["stage"] == "waiting_for_slot" for e in self.events))
        coordinator.cancel_job(coordinator.jobs[1].job_id)
        release.set()
        result = self.joined(launched)
        self.assertEqual(executed, [0, 2])
        self.assertEqual([item.job.state for item in result], ["completed", "cancelled", "completed"])


if __name__ == "__main__":
    unittest.main()
