"""Deterministic telemetry tests; fake samples are not machine benchmark evidence."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from audio_transcribe.telemetry import (ResourceSampler, Telemetry, memory_pressure_level,
                                        parse_whisper_timings, process_tree_sample, system_sample)


class Clock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value


class TelemetryTests(unittest.TestCase):
    def test_submission_origin_includes_prior_startup_without_changing_stage_duration(self):
        clock = Clock()
        recorder = Telemetry("batch-fixture", clock=clock, started_at=95)
        with recorder.span("runtime_verification"):
            clock.value += 2
        recorder.mark("batch_complete")
        result = recorder.snapshot()
        self.assertEqual(result["batch_wall_seconds"], 7)
        self.assertEqual(result["stage_worker_seconds"]["runtime_verification"], 2)
        self.assertEqual(result["origin"], "caller_supplied_submission")
        self.assertEqual(Telemetry("batch-default", clock=clock).snapshot()["origin"], "recorder_creation")
        for value in (True, "95", float("nan"), float("inf"), 103):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Telemetry("batch-fixture", clock=clock, started_at=value)

    def test_overlapping_worker_seconds_never_replace_batch_wall(self):
        clock = Clock()
        recorder = Telemetry("batch-fixture", {"asr_workers": 2}, clock=clock)
        first = recorder.span("asr_process", job_id="job-a", attempt_id="attempt-a", index=0)
        second = recorder.span("asr_process", job_id="job-b", attempt_id="attempt-b", index=1)
        first.__enter__()
        clock.value += 1
        second.__enter__()
        clock.value += 4
        recorder.mark("source_artifact_ready", job_id="job-b")
        recorder.mark("job_complete", job_id="job-b")
        second.__exit__(None, None, None)
        clock.value += 1
        first.__exit__(None, None, None)
        recorder.mark("job_complete", job_id="job-a")
        clock.value += 2
        recorder.mark("first_readable_report")
        clock.value += 1
        recorder.mark("batch_complete")
        clock.value += 50
        result = recorder.snapshot(audio_seconds=90)
        self.assertEqual(result["stage_worker_seconds"]["asr_process"], 10)
        self.assertEqual(result["batch_wall_seconds"], 9)
        self.assertEqual(result["observation_wall_seconds"], 59)
        self.assertEqual(result["milestones"]["source_artifact_ready"], 5)
        self.assertEqual(result["milestones"]["first_readable_report"], 8)
        self.assertEqual(result["audio_seconds_per_batch_wall_second"], 10)
        self.assertEqual([job["job_id"] for job in result["job_timings"]], ["job-b", "job-a"])

    def test_failed_stage_records_no_exception_or_private_text(self):
        recorder = Telemetry("batch-fixture")
        with self.assertRaises(ValueError):
            with recorder.span("quality_check", job_id="job-a"):
                raise ValueError("private transcript phrase must not be copied")
        result = recorder.snapshot()
        self.assertEqual(result["stages"][0]["outcome"], "interrupted_or_failed")
        self.assertNotIn("private transcript", json.dumps(result))
        self.assertIsNone(result["audio_seconds_per_batch_wall_second"])

    def test_concurrent_events_have_unique_serial_sequence(self):
        recorder = Telemetry("batch-fixture")
        def worker(index):
            for _ in range(25):
                with recorder.span("preprocess", job_id=f"job-{index}"):
                    recorder.cache("derived_audio", False, job_id=f"job-{index}")
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        result = recorder.snapshot()
        self.assertEqual([event["sequence"] for event in result["events"]], list(range(1, 301)))
        self.assertEqual(len(result["stages"]), 100)
        self.assertEqual(len(result["cache_events"]), 100)
        elapsed = [event["elapsed_seconds"] for event in result["events"]]
        self.assertEqual(elapsed, sorted(elapsed))

    def test_whisper_counter_units_and_batchd_are_not_file_batching(self):
        raw = """Private words that must be discarded.
whisper_print_timings:     load time =  1450.25 ms
whisper_print_timings:   encode time = 20000.00 ms / 20 runs ( 1000.00 ms per run)
whisper_print_timings:   decode time = 2500.00 ms / 40 runs
whisper_print_timings:   batchd time = 3000.00 ms / 90 runs
whisper_print_timings:    total time = 30120.00 ms
"""
        parsed = parse_whisper_timings(raw)
        self.assertEqual(parsed["counters"][0], {"counter": "model_load", "seconds": 1.45025, "reported_unit": "ms"})
        self.assertEqual(parsed["counters"][1]["seconds"], 20)
        self.assertEqual(parsed["counters"][1]["runs"], 20)
        self.assertFalse(parsed["multi_file_tensor_batching"])
        self.assertNotIn("Private words", json.dumps(parsed))
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "stderr.log"
            path.write_text(raw)
            recorder = Telemetry("batch-fixture")
            recorder.record_whisper_log(path, job_id="job-a")
            recorder.write(Path(temporary) / "telemetry.json")
            saved = json.loads((Path(temporary) / "telemetry.json").read_text())
        self.assertEqual(saved["whisper_timings"][0]["counters"], parsed["counters"])
        self.assertNotIn("Private words", json.dumps(saved))

    def test_cache_categories_are_explicit_and_snapshot_is_a_copy(self):
        recorder = Telemetry("batch-fixture")
        recorder.cache("transcript", True)
        recorder.cache("report", False)
        result = recorder.snapshot()
        result["cache_events"].clear()
        self.assertEqual(len(recorder.snapshot()["cache_events"]), 2)
        with self.assertRaises(ValueError):
            recorder.cache("gpu_memory", True)
        with self.assertRaises(ValueError):
            recorder.mark("private speech with spaces")


class ResourceTests(unittest.TestCase):
    def test_resource_sampler_rejects_unbounded_or_invalid_sampling(self):
        for options in ({"interval_seconds": float("nan")}, {"interval_seconds": float("inf")},
                        {"interval_seconds": 0.1}, {"max_samples": 2.5}, {"max_samples": True},
                        {"max_samples": 10001}, {"system_every": 1.5}, {"root_pid": True}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                ResourceSampler(**options)

    def test_tree_sampling_excludes_other_processes_and_uses_kib(self):
        raw = """100 1 1000 2.5 00:01.20 01:00
101 100 4000000 34.0 00:20.00 00:45
102 101 1000000 1.0 00:02.00 00:40
200 1 9000000 80.0 02:00.00 02:00
"""
        calls = []
        def command(args):
            calls.append(args)
            return raw
        result = process_tree_sample(100, command=command)
        self.assertEqual([p["pid"] for p in result["processes"]], [100, 101, 102])
        self.assertEqual(result["processes"][1]["rss_bytes"], 4000000 * 1024)
        self.assertEqual(result["processes"][0]["cpu_seconds"], 1.2)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("command", calls[0][-1])
        self.assertFalse(process_tree_sample(999, command=command)["root_present"])
        unavailable = process_tree_sample(100, command=lambda args: None)
        self.assertFalse(unavailable["available"])
        self.assertIsNone(unavailable["root_present"])

    def test_sampler_is_finite_and_reports_sampled_per_process_not_true_peak(self):
        clock = Clock()
        reads, system_reads = [], []
        def process(pid):
            reads.append(pid)
            return {"available": True, "root_present": True,
                    "processes": [{"pid": pid, "rss_bytes": len(reads) * 1024}]}
        def system():
            system_reads.append(1)
            return {"memory_pressure": "normal"}
        sampler = ResourceSampler(100, max_samples=3, system_every=2, clock=clock,
                                  process_reader=process, system_reader=system)
        for _ in range(5):
            sampler.sample()
            clock.value += 2
        result = sampler.stop()
        self.assertEqual(result["sample_count"], 3)
        self.assertEqual(result["stop_reason"], "sample_limit")
        self.assertEqual(len(reads), 3)
        self.assertEqual(len(system_reads), 2)
        self.assertEqual(result["process_summaries"][0]["sampled_max_rss_bytes"], 3072)
        self.assertNotIn("total_memory_bytes", result)
        self.assertIn("Short-lived children may be missed", result["interpretation"])

    def test_sampler_context_stops_thread_even_on_exception(self):
        reader = lambda pid: {"available": True, "root_present": True, "processes": []}
        sampler = ResourceSampler(100, process_reader=reader, system_reader=lambda: {})
        with self.assertRaises(RuntimeError):
            with sampler:
                raise RuntimeError("synthetic benchmark failure")
        self.assertFalse(sampler._thread.is_alive())
        self.assertEqual(sampler.snapshot()["stop_reason"], "requested")

    def test_system_sample_keeps_vm_counts_and_unknown_pressure_distinct(self):
        raw = 'Mach Virtual Memory Statistics: (page size of 16384 bytes)\nPages free:  12.\nPages occupied by compressor: 100.\nSwapouts: 4.\n'
        result = system_sample(command=lambda args: raw, pressure=lambda: "unknown")
        self.assertEqual(result["page_size_bytes"], 16384)
        self.assertEqual(result["vm_pages"]["pages_occupied_by_compressor"], 100)
        self.assertEqual(result["memory_pressure"], "unknown")
        with patch("audio_transcribe.telemetry.platform.system", return_value="Linux"):
            self.assertEqual(memory_pressure_level(), "unknown")


if __name__ == "__main__":
    unittest.main()
