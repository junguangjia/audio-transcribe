"""Report lifecycle fixtures. Decoder/dialog mocks are not live inference/UI evidence."""
import copy
import io
from pathlib import Path
import tempfile
import sys
import types
import unittest
from unittest.mock import patch
import wave
import struct

from audio_transcribe import cli, config, engine, export, library, storage


class ExportTests(unittest.TestCase):
    def test_deleted_job_cannot_recreate_final_batch_metrics(self):
        import uuid
        from audio_transcribe import lifecycle
        identity = str(uuid.uuid4())
        token = lifecycle.capture(self.settings, identity, self.root / 'selected.wav')
        coordinator = types.SimpleNamespace(jobs=[types.SimpleNamespace(lifecycle_token=token)])
        target = self.root / 'removed-job-metrics.json'
        self.assertTrue(export._publish_batch_metrics(self.settings, coordinator,
            lambda: storage.write_json(target, {'complete': True})))
        target.unlink()
        with lifecycle.lock(self.settings):
            value = lifecycle.fence(lifecycle.state(self.settings), [], [], [identity], 'fixture-delete')
            lifecycle.save(self.settings, value)
        self.assertFalse(export._publish_batch_metrics(self.settings, coordinator,
            lambda: storage.write_json(target, {'complete': True})))
        self.assertFalse(target.exists())

    def test_sampler_finally_preserves_failure_before_lifecycle_capture(self):
        from unittest.mock import Mock
        coordinator = types.SimpleNamespace(jobs=[types.SimpleNamespace()],
            policy={'resource_sampling': True}, batch_id='uncaptured')
        sampler = Mock()
        with patch.object(export, 'ResourceSampler', return_value=sampler), \
             patch.object(export, '_build_report', side_effect=RuntimeError('original capture failure')):
            with self.assertRaisesRegex(RuntimeError, 'original capture failure'):
                export.build_report(self.settings, [], self.resolved, coordinator=coordinator)
        sampler.stop.assert_called_once()
        sampler.snapshot.assert_not_called()
        self.assertFalse((self.data / '.performance' / 'performance' / 'uncaptured-resources.json').exists())

    def test_execution_policy_change_reuses_historic_asr_and_report(self):
        paths = [self.source("strategy-a.wav", 10), self.source("strategy-b.wav", 20)]
        first = export.build_report(self.settings, paths, self.resolved, execution={"mode": "serial"})
        count = self.calls
        with patch.object(engine, "decode", side_effect=AssertionError("Execution policy must not invalidate decoding")):
            second = export.build_report(self.settings, paths, self.resolved,
                                         execution={"mode": "pipeline", "asr_workers": 2})
        self.assertEqual(self.calls, count)
        self.assertEqual(first["report"], second["report"])
        self.assertTrue(second["reused_report"])
        metrics = storage.read_doc(second["performance_record"])
        self.assertTrue(metrics["batch_complete"])
        self.assertEqual(metrics["execution"]["effective_asr_workers"], 2)
        self.assertTrue(all(e["hit"] for e in metrics["cache_events"] if e["cache"] == "transcript"))

    def test_cancelled_selection_is_explicit_partial_and_retains_other_result(self):
        from audio_transcribe.scheduler import BatchCoordinator
        paths = [self.source("cancelled.wav", 10), self.source("kept.wav", 20)]
        coordinator = BatchCoordinator(self.settings, paths)
        coordinator.start()
        coordinator.cancel_job(coordinator.jobs[0].job_id)
        result = export.build_report(self.settings, paths, self.resolved, coordinator=coordinator)
        self.assertEqual((result["state"], result["completed"], result["cancelled"]), ("partial", 1, 1))
        self.assertEqual(self.calls, 1)
        report = Path(result["report"])
        entries = storage.read_doc(report.with_name("manifest.json"))["ordered_sources"]
        self.assertEqual([e["state"] for e in entries], ["cancelled", "completed"])
        self.assertIn("**CANCELLED", report.read_text())
        self.assertNotIn("**Complete transcript compilation.**", report.read_text())

    def test_cancelled_duplicate_keeps_original_positions_and_single_compute(self):
        from audio_transcribe.scheduler import BatchCoordinator
        path = self.source("duplicate.wav", 10)
        coordinator = BatchCoordinator(self.settings, [path, path])
        coordinator.start()
        coordinator.cancel_job(coordinator.jobs[0].job_id)
        result = export.build_report(self.settings, [path, path], self.resolved, coordinator=coordinator)
        entries = storage.read_doc(Path(result["report"]).with_name("manifest.json"))["ordered_sources"]
        self.assertEqual(self.calls, 1)
        self.assertEqual(entries[1]["duplicate_of"], 1)
        self.assertEqual([e["position"] for e in entries], [1, 2])

    def test_legacy_multi_source_new_decode_preserves_session_and_selected_sources(self):
        paths = [self.source("legacy-a.wav", 10), self.source("legacy-b.wav", 20)]
        session, _, _ = storage.import_sources(self.data, paths, order_confirmed=True)
        before = {p: storage.sha256_file(p) for p in session.rglob("*") if p.is_file()}
        result = self.report(list(reversed(paths)))
        self.assertEqual(result["completed"], 2)
        self.assertEqual(self.calls, 2)
        for p, value in before.items():
            self.assertEqual(storage.sha256_file(p), value)
        entries = storage.read_doc(Path(result["report"]).with_name("manifest.json"))["ordered_sources"]
        self.assertEqual([e["filename"] for e in entries], [p.name for p in reversed(paths)])

    def test_explicit_failed_retry_preserves_old_attempt_and_success_cache(self):
        good=self.source("good.wav",10); retry=self.source("retry.wav",20)
        baseline=self.report([good])
        session,record,_=storage.import_sources(self.data,[retry])
        def invalid(*args,**kwargs):
            metadata=self.decode(*args,**kwargs)
            native=Path(args[4])/'native.json'
            storage.write_json(native,{'transcription':[{'offsets':{'from':100,'to':3000},'text':'Preserved failed fixture.'}]},overwrite=True)
            metadata['native_sha256']=storage.sha256_file(native)
            return metadata
        with patch.object(engine,'decode',side_effect=invalid):
            provisional=engine.run_session(self.settings,session,self.resolved)
        self.assertEqual(provisional['state'],'review_required')
        old={p:p.read_bytes() for p in session.glob('transcript/*/**/*') if p.is_file()}
        before=self.calls
        recovered=export.build_report(self.settings,[good,retry],self.resolved,retry_failed=True)
        self.assertEqual(recovered['completed'],2)
        self.assertEqual(self.calls,before+1)
        self.assertEqual(recovered['reused_transcripts'],1)
        for p,value in old.items():self.assertEqual(p.read_bytes(),value)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.data = self.root / "data"
        self.settings = {"roots": {"data": str(self.data), "app": str(self.root / "app"),
                                   "code": str(self.root / "code")}, "storage_approved": True}
        self.resolved = config.resolve_config(self.data)
        self.runtime = {"runtime": {"cli": "/fixture/whisper", "sha256": "runtime", "release": "test"}}
        self.model = {"name": "large-v3", "sha256": "model", "path": "/fixture/model", "precision": "f16"}
        self.calls = 0
        self.raw = [" Same repeated text <untrusted> *not formatted*.", " Same repeated text <untrusted> *not formatted*."]
        for module in (engine, export):
            for name, value in (("load_runtime", self.runtime), ("model_identity", self.model)):
                p = patch.object(module, name, return_value=value)
                p.start(); self.addCleanup(p.stop)
        p = patch.object(engine, "decode", side_effect=self.decode)
        p.start(); self.addCleanup(p.stop)

    def source(self, name, amplitude):
        path = self.root / "files 空间" / name
        path.parent.mkdir(exist_ok=True)
        with wave.open(str(path), "wb") as f:
            f.setparams((1, 2, 16000, 16000, "NONE", "not compressed"))
            f.writeframes(struct.pack("<h", amplitude) * 16000)
        return path

    def decode(self, settings, runtime, model, wav, output, asr, glossary, **kwargs):
        self.calls += 1
        output.mkdir(parents=True, exist_ok=True)
        storage.write_json(output / "native.json", {"transcription": [
            {"offsets": {"from": 100, "to": 400}, "text": self.raw[0]},
            {"offsets": {"from": 400, "to": 900}, "text": self.raw[1]}]})
        return {"input_frames": 16000, "input_duration_seconds": 1.0, "elapsed_seconds": .01,
                "native_sha256": storage.sha256_file(output / "native.json")}

    def report(self, paths, resolved=None):
        return export.build_report(self.settings, paths, resolved or self.resolved)

    def independent(self, paths, *, publisher=None, **options):
        def publish(settings, entry, segments, resolved, runtime, model, info=None):
            identity = storage.sha256_file(Path(entry["selected_path"]))[:12] + "-" + entry["transcript"]["run_id"]
            markdown = self.data / "results" / identity / "transcript.md"
            markdown.parent.mkdir(parents=True, exist_ok=True)
            if not markdown.exists():
                markdown.write_text("\n".join(s["text"] for s in segments))
            return {"result_id": identity, "filename": entry["filename"], "duration_seconds": info["duration_seconds"],
                    "model": resolved["asr"]["model"], "state": entry["state"], "markdown_path": str(markdown),
                    "audio_path": entry["selected_path"], "generated_at": "synthetic", "legacy": False}
        with patch.dict(sys.modules, {"audio_transcribe.direct": types.SimpleNamespace(publish_result=publisher or publish)}):
            return export.build_independent(self.settings, paths, self.resolved, **options)

    def test_independent_results_publish_before_later_file_finishes_without_grouping(self):
        paths = [self.source("first 中文.wav", 10), self.source("second space.wav", 20)]
        hashes = [storage.sha256_file(p) for p in paths]
        ready = []
        def event(value):
            if value.get("result_id"):
                self.assertTrue(Path(value["result"]["markdown_path"]).is_file())
                ready.append((value["index"], self.calls, value))
        with patch.object(export, "normalize_groups", side_effect=AssertionError("No grouping in direct workflow")), \
             patch.object(export, "_publish_report", side_effect=AssertionError("No combined report")):
            result = self.independent(paths, execution={"mode": "serial"}, events=event)
        self.assertEqual([(i, calls) for i, calls, _ in ready], [(0, 1), (1, 2)])
        self.assertEqual([r["filename"] for r in result["results"]], [p.name for p in paths])
        self.assertEqual(len({r["result_id"] for r in result["results"]}), 2)
        self.assertEqual(result["completed"], 2)
        self.assertNotIn("report", result)
        self.assertNotIn("groups", result)
        self.assertFalse((self.data / "exports").exists())
        self.assertEqual([storage.sha256_file(p) for p in paths], hashes)
        self.assertTrue(all(self.raw[0] not in str(e) for _, _, e in ready))

    def test_independent_actual_direct_publisher_exports_each_source_with_provenance(self):
        from audio_transcribe import direct
        paths = [self.source("direct one 空间.wav", 10), self.source("direct two.wav", 20)]
        events = []
        first = export.build_independent(self.settings, paths, self.resolved, events=events.append)
        self.assertEqual(len(first["results"]), 2)
        for path, result in zip(paths, first["results"]):
            opened = direct.read_result(self.settings, result["result_id"])
            self.assertEqual(opened["provenance"]["source"]["filename"], path.name)
            self.assertEqual(opened["provenance"]["source"]["sha256"], storage.sha256_file(path))
            self.assertEqual(opened["provenance"]["audio"]["duration_seconds"], 1)
            self.assertTrue(opened["provenance"]["source"]["imported_at"])
            self.assertEqual([s["text"] for s in opened["segments"]], self.raw)
            self.assertTrue(Path(result["markdown_path"]).is_file())
        ready = [e for e in events if e.get("result_id")]
        self.assertEqual([e["result_id"] for e in ready], [r["result_id"] for r in first["results"]])
        reused = export.build_independent(self.settings, paths, self.resolved)
        self.assertEqual(self.calls, 2)
        self.assertEqual([r["result_id"] for r in first["results"]], [r["result_id"] for r in reused["results"]])

    def test_independent_metadata_and_actual_import_time_reach_publisher(self):
        path = self.source("metadata.wav", 10)
        seen, events = [], []
        def publish(settings, entry, segments, resolved, runtime, model, info=None):
            seen.append((copy.deepcopy(entry), copy.deepcopy(info)))
            return {"result_id": "synthetic-result", "filename": entry["filename"], "duration_seconds": 1,
                    "model": resolved["asr"]["model"], "state": entry["state"]}
        self.independent([path], publisher=publish, events=events.append)
        entry, info = seen[0]
        self.assertEqual((info["sample_rate"], info["channels"], info["complete_frames"]), (16000, 1, 16000))
        self.assertEqual(entry["imported_at"], info["imported_at"])
        self.assertNotIn("recording_time", entry)
        metadata = next(e for e in events if e["type"] == "metadata")
        self.assertEqual(metadata["duration_seconds"], 1)
        self.assertTrue(all(metadata[k] for k in ("job_id", "attempt_id", "batch_id", "seq")))
        self.independent([path], publisher=publish)
        self.assertNotIn("imported_at", seen[1][0])

    def test_independent_shared_fresh_generation_is_distinct_from_historic_reuse(self):
        from audio_transcribe import direct
        from audio_transcribe.product import APP_VERSION, APP_BUILD
        paths = [self.source("first alias.wav", 10), self.source("second alias.wav", 10)]
        seen = []
        publish = direct.publish_result
        def capture(settings, entry, *args, **kwargs):
            seen.append(copy.deepcopy(entry["transcript"]))
            return publish(settings, entry, *args, **kwargs)
        with patch.object(direct, "publish_result", side_effect=capture):
            fresh = export.build_independent(self.settings, paths, self.resolved)
            cached = export.build_independent(self.settings, paths, self.resolved)
        self.assertEqual(self.calls, 1)
        self.assertEqual([ref["generated_in_current_invocation"] for ref in seen], [True, True, False, False])
        self.assertEqual([ref["reused_transcript"] for ref in seen[:2]], [False, True])
        self.assertEqual(len({result["result_id"] for result in fresh["results"]}), 2)
        self.assertEqual([result["result_id"] for result in fresh["results"]], [result["result_id"] for result in cached["results"]])
        for result in fresh["results"]:
            opened = direct.read_result(self.settings, result["result_id"])
            asr = opened["provenance"]["transcription"]
            self.assertEqual((asr["app_version"], asr["app_build"]), (APP_VERSION, APP_BUILD))
        metrics = storage.read_doc(fresh["performance_record"])
        milestones = metrics["milestones"]
        self.assertLessEqual(milestones["first_readable_report"], milestones["batch_complete"])
        self.assertTrue(all("first_readable_report" in job["milestones"] for job in metrics["job_timings"]))

    def test_independent_cancel_resume_preserves_completed_cache(self):
        from audio_transcribe.scheduler import BatchCoordinator
        paths = [self.source("cancelled.wav", 10), self.source("kept.wav", 20)]
        coordinator = BatchCoordinator(self.settings, paths)
        coordinator.start(); coordinator.cancel_job(coordinator.jobs[0].job_id)
        first = self.independent(paths, coordinator=coordinator)
        self.assertEqual((first["completed"], first["cancelled"], len(first["results"])), (1, 1, 1))
        self.assertEqual(self.calls, 1)
        resumed = self.independent(paths)
        self.assertEqual((resumed["completed"], resumed["reused_transcripts"], self.calls), (2, 1, 2))
        self.assertEqual(first["results"][0]["result_id"], resumed["results"][1]["result_id"])

    def test_independent_cancelled_duplicate_has_no_result_but_other_alias_completes(self):
        from audio_transcribe.scheduler import BatchCoordinator
        path = self.source("duplicate.wav", 10)
        coordinator = BatchCoordinator(self.settings, [path, path])
        coordinator.start(); coordinator.cancel_job(coordinator.jobs[0].job_id)
        first = self.independent([path, path], coordinator=coordinator)
        self.assertEqual(self.calls, 1)
        self.assertEqual([r["index"] for r in first["results"]], [1])
        self.assertEqual(first["cancelled"], 1)

    def test_independent_force_bypasses_success_cache_and_preserves_old_run(self):
        path = self.source("again.wav", 10)
        first = self.independent([path])
        old = {p: p.read_bytes() for p in (self.data / "sessions").glob("*/transcript/run-*/**/*") if p.is_file()}
        cached = self.independent([path])
        self.assertEqual(self.calls, 1)
        self.assertEqual(cached["results"][0]["result_id"], first["results"][0]["result_id"])
        forced = self.independent([path], force=True)
        self.assertEqual(self.calls, 2)
        self.assertNotEqual(forced["results"][0]["result_id"], first["results"][0]["result_id"])
        self.assertEqual(forced["reused_transcripts"], 0)
        for p, original in old.items():
            self.assertEqual(p.read_bytes(), original)
        self.assertEqual(len(list((self.data / "sessions").glob("*/session.yaml"))), 1)

    def test_independent_force_legacy_multi_session_never_decodes_unselected_source(self):
        paths = [self.source("legacy-a.wav", 10), self.source("legacy-b.wav", 20)]
        session, _, _ = storage.import_sources(self.data, paths, order_confirmed=True)
        before = {p: p.read_bytes() for p in session.rglob("*") if p.is_file()}
        result = self.independent([paths[1]], force=True)
        self.assertEqual((result["completed"], self.calls), (1, 1))
        for p, original in before.items():
            self.assertEqual(p.read_bytes(), original)

    def test_interrupted_explicit_retranscription_restarts_without_old_success_substitution(self):
        from audio_transcribe.execution import OperationCancelled
        path = self.source("force interrupted.wav", 10)
        first = self.independent([path])
        original_id = first["results"][0]["result_id"]
        def cancel(*args, **kwargs):
            self.calls += 1
            raise OperationCancelled()
        with patch.object(engine, "decode", side_effect=cancel):
            interrupted = self.independent([path], force=True)
        self.assertEqual((interrupted["cancelled"], interrupted["results"], self.calls), (1, [], 2))
        restarted = self.independent([path], force=True)
        self.assertEqual((restarted["completed"], restarted["reused_transcripts"], self.calls), (1, 0, 3))
        self.assertNotEqual(restarted["results"][0]["result_id"], original_id)

    def test_interrupted_initial_job_resumes_existing_run_without_force(self):
        from audio_transcribe.execution import OperationCancelled
        path = self.source("initial interrupted.wav", 10)
        with patch.object(engine, "decode", side_effect=OperationCancelled()):
            interrupted = self.independent([path])
        self.assertEqual(interrupted["cancelled"], 1)
        attempts = list((self.data / "sessions").glob("*/transcript/run-*/manifest.json"))
        self.assertEqual(len(attempts), 1)
        self.assertEqual(storage.read_doc(attempts[0])["state"], "interrupted")
        resumed = self.independent([path])
        self.assertEqual((resumed["completed"], self.calls), (1, 1))
        self.assertEqual(list((self.data / "sessions").glob("*/transcript/run-*/manifest.json")), attempts)
        self.assertEqual(storage.read_doc(attempts[0])["state"], "completed")
        self.assertTrue(storage.read_doc(attempts[0]).get("resumed_at"))

    def test_independent_publication_failure_is_per_file_and_retry_reuses_asr(self):
        paths = [self.source("failed export.wav", 10), self.source("working.wav", 20)]
        def publish(settings, entry, *args, **kwargs):
            if entry["position"] == 1:
                raise OSError("synthetic publication failure")
            return {"result_id": "kept", "filename": entry["filename"], "state": entry["state"]}
        first = self.independent(paths, publisher=publish)
        self.assertEqual((first["failed"], first["completed"], self.calls), (1, 1, 2))
        retried = self.independent(paths)
        self.assertEqual((retried["completed"], retried["reused_transcripts"], self.calls), (2, 2, 2))

    def test_independent_selected_source_mutation_prevents_result_publication(self):
        path = self.source("mutable.wav", 10)
        def decode(*args, **kwargs):
            result = self.decode(*args, **kwargs)
            path.write_bytes(path.read_bytes() + b"changed")
            return result
        with patch.object(engine, "decode", side_effect=decode):
            result = self.independent([path])
        self.assertEqual((result["failed"], result["results"]), (1, []))
        self.assertFalse((self.data / "results").exists())

    def test_independent_single_file_uses_one_effective_slot_even_when_two_requested(self):
        result = self.independent([self.source("single.wav", 10)],
                                  execution={"mode": "pipeline", "asr_workers": 2})
        self.assertEqual(result["completed"], 1)
        self.assertEqual(result["execution"]["effective_asr_workers"], 1)

    def test_progress_is_indexed_and_does_not_expose_transcript(self):
        paths = [self.source("a.wav", 10), self.source("b.wav", 20)]
        events = []
        def decode(*args, progress=None, **kwargs):
            progress({"stage": "transcribing", "percent": 45})
            return self.decode(*args)
        with patch.object(engine, "decode", side_effect=decode):
            first = export.build_report(self.settings, paths, self.resolved, events=events.append)
        updates = [e for e in events if e["type"] == "progress" and e["stage"] == "transcribing"]
        self.assertEqual([e["index"] for e in updates], [0, 1])
        self.assertTrue(all(e["percent"] == 45 and e["total"] == 2 for e in updates))
        self.assertNotIn(self.raw[0].strip(), str(events))
        events.clear()
        with patch.object(engine, "decode", side_effect=AssertionError("Progress must not invalidate ASR cache")):
            second = export.build_report(self.settings, paths, self.resolved, events=events.append)
        self.assertEqual(first["report"], second["report"])
        self.assertEqual(second["reused_transcripts"], 2)
        self.assertFalse(any(e["type"] == "progress" for e in events))

    def test_natural_order_and_confirmed_manual_order(self):
        a, b = self.source("part2 空间.wav", 10), self.source("part10 空间.wav", 20)
        self.assertEqual(export.natural_order([b, a]), [a, b])
        result = self.report([b, a])
        doc = storage.read_doc(Path(result["report"]).with_name("manifest.json"))
        self.assertEqual([e["filename"] for e in doc["ordered_sources"]], [b.name, a.name])
        text = Path(result["report"]).read_text()
        self.assertLess(text.index("## 1. " + b.name), text.index("## 2. " + a.name))
        self.assertEqual(text.count("[00:00:00.100 – 00:00:00.400]"), 2)
        self.assertEqual(text.count(export.markdown_text(self.raw[0])), 4)
        self.assertEqual(doc["completed"], 2)
        self.assertEqual(len(list((self.data / "sessions").glob("*/session.yaml"))), 2)
        self.assertEqual(self.calls, 2)

    def test_confirmed_classes_preserve_one_complete_dated_batch_report(self):
        paths = [self.source("audio_240102_090000.wav", 10), self.source("audio_240103_110000.wav", 20)]
        groups = [{"indices": [i], "title": title, "date": date, "confirmed": True}
                  for i, title, date in [(0, "Morning class", "2024-01-02"), (1, "Next class", "2024-01-03")]]
        result = export.build_report(self.settings, paths, self.resolved, groups=groups)
        report = Path(result["report"])
        self.assertTrue(report.is_relative_to(self.data / "exports" / "multiple-dates"))
        text = report.read_text()
        self.assertLess(text.index("# Morning class"), text.index("# Next class"))
        for index, path in enumerate(paths, 1):
            self.assertEqual(text.count(f"## {index}. {export.markdown_text(path.name)}"), 1)
        snapshot = library.list_library(self.settings)
        self.assertEqual(len(snapshot["reports"]), 1)
        self.assertEqual(len(snapshot["dates"]), 2)
        detail = library.read_report(self.settings, result["batch_id"])
        self.assertEqual(len(detail["segments"]), 4)
        self.assertTrue(all(Path(s["audio_path"]).is_relative_to(self.data / "sessions") for s in detail["sources"]))
        before = self.calls
        repeated = export.build_report(self.settings, paths, self.resolved, groups=groups)
        self.assertEqual(repeated["report"], result["report"])
        self.assertEqual(before, self.calls)

    def test_current_quality_gate_reassesses_completed_cache(self):
        path = self.source("audio_240102_090000.wav", 10)
        baseline = self.report([path])
        gate = {"version": "test-stricter-rule", "status": "review_required", "accuracy_verified": False,
                "timestamp_valid": True, "reasons": ["repeated_phrase_loop"], "findings": [], "sources": []}
        with patch.object(export, "assess_quality", return_value=gate), patch.object(export, "run_session", side_effect=AssertionError("Only reassessment is required")):
            result = self.report([path])
        self.assertEqual(result["review_required"], 1)
        self.assertEqual(result["completed"], 0)
        self.assertEqual(result["failed"], 0)
        self.assertNotEqual(baseline["report"], result["report"])
        self.assertIn("REVIEW REQUIRED", Path(result["report"]).read_text())
        self.assertIn(self.raw[0].strip().replace("<", "\\<").replace(">", "\\>").replace("*", "\\*"), Path(result["report"]).read_text())
        before = self.calls
        with patch.object(export, "assess_quality", return_value=gate):
            retry = export.build_report(self.settings, [path], self.resolved, retry_failed=True)
        self.assertEqual(self.calls, before + 1)
        self.assertEqual(retry["review_required"], 1)

    def test_final_confirmed_group_clock_uses_managed_recorded_at(self):
        path = self.source("audio_240102_090000.wav", 10)
        session_path, session, _ = storage.import_sources(self.data, [path])
        session["recorded_at"] = "2024-01-02T13:30:00"
        storage.write_yaml(session_path / "session.yaml", session, overwrite=True)
        groups = [{"indices": [0], "title": "Afternoon class", "date": "2024-01-02", "confirmed": True}]
        result = export.build_report(self.settings, [path], self.resolved, groups=groups)
        self.assertEqual(result["groups"][0]["start_time"], "13:30:00")
        self.assertIn("133000-Afternoon-class", result["report"])

    def test_provisional_negative_timestamp_remains_visible_without_clamping(self):
        path = self.source("audio_240102_090000.wav", 10)
        def decode(*args, **kwargs):
            metadata = self.decode(*args, **kwargs)
            native = Path(args[4]) / "native.json"
            storage.write_json(native, {"transcription": [{"offsets": {"from": -100, "to": 900},
                                                          "text": "Synthetic negative time remains visible."}]}, overwrite=True)
            metadata["native_sha256"] = storage.sha256_file(native)
            return metadata
        with patch.object(engine, "decode", side_effect=decode):
            result = self.report([path])
        text = Path(result["report"]).read_text()
        self.assertEqual(result["review_required"], 1)
        self.assertIn("[-00:00:00.100 – 00:00:00.900]", text)
        self.assertIn("Synthetic negative time remains visible.", text)

    def test_no_context_glossary_limitation_is_visible(self):
        path = self.source("audio_240102_090000.wav", 10)
        resolved = copy.deepcopy(self.resolved)
        resolved["glossary"]["terms"] = ["synthetic classroom vocabulary"]
        resolved["glossary"]["sha256"] = engine.digest(resolved["glossary"]["terms"])
        result = self.report([path], resolved)
        self.assertEqual(result["review_required"], 1)
        text = Path(result["report"]).read_text()
        self.assertIn("glossary", text.lower())
        self.assertNotIn("synthetic classroom vocabulary", text)
        entry = storage.read_doc(Path(result["report"]).with_name("manifest.json"))["ordered_sources"][0]
        self.assertIn("glossary_context_disabled", entry["transcript"]["quality"]["reasons"])

    def test_reuses_results_and_equivalent_export_without_inference(self):
        paths = [self.source("a.wav", 10), self.source("b.wav", 20)]
        first = self.report(paths)
        before = {p: p.read_bytes() for p in self.data.glob("sessions/*/transcript/*/*") if p.is_file()}
        with patch.object(export, "run_session", side_effect=AssertionError("Cached ASR must not run")):
            second = self.report(paths)
        self.assertTrue(second["reused_report"])
        self.assertEqual(first["report"], second["report"])
        self.assertEqual(second["reused_transcripts"], 2)
        for p, value in before.items(): self.assertEqual(p.read_bytes(), value)

    def test_trashed_report_is_not_reused_but_audio_and_asr_are_preserved(self):
        paths = [self.source("audio_240102_090000.wav", 10)]
        first = self.report(paths)
        before = {p: p.read_bytes() for p in self.data.rglob("*") if p.is_file()}
        library.library_request(self.settings, {"action": "trash", "report_id": first["batch_id"]})
        with patch.object(export, "run_session", side_effect=AssertionError("Trashing a report must not discard cached ASR")):
            second = self.report(paths)
        self.assertNotEqual(first["report"], second["report"])
        self.assertFalse(second["reused_report"])
        self.assertEqual(second["reused_transcripts"], 1)
        self.assertEqual(self.calls, 1)
        self.assertEqual([r["id"] for r in library.list_library(self.settings)["reports"]], [second["batch_id"]])
        self.assertEqual([r["id"] for r in library.list_library(self.settings, view="trash")["reports"]], [first["batch_id"]])
        for path, content in before.items():
            # List annotations and the new task-generation registry are mutable
            # control metadata. Original audio and every ASR artifact stay exact.
            if path.parent.name not in {"library", "lifecycle"}:
                self.assertEqual(path.read_bytes(), content)
        library.library_request(self.settings, {"action": "restore", "report_id": first["batch_id"]})
        self.assertEqual(library.list_library(self.settings)["active_count"], 2)

    def test_renamed_report_folder_cannot_abort_export_or_be_reused(self):
        paths = [self.source("audio_240102_090000.wav", 10)]
        for renamed in ("renamed report", "batch-renamed"):
            first = self.report(paths)
            folder = Path(first["report"]).parent
            saved = {path.name: path.read_bytes() for path in folder.iterdir() if path.is_file()}
            moved = folder.with_name(renamed)
            folder.rename(moved)
            with patch.object(export, "run_session", side_effect=AssertionError("Intact ASR should still be reused")):
                replacement = self.report(paths)
            self.assertFalse(replacement["reused_report"])
            self.assertNotEqual(Path(replacement["report"]).parent, moved)
            self.assertEqual({path.name: path.read_bytes() for path in moved.iterdir() if path.is_file()}, saved)
        self.assertEqual(self.calls, 1)

    def test_existing_multi_source_session_is_not_reclassified(self):
        a, b = self.source("a.wav", 10), self.source("b.wav", 20)
        session, _, _ = storage.import_sources(self.data, [a, b], order_confirmed=True)
        run = engine.run_session(self.settings, session, self.resolved)
        before = (session / "session.yaml").read_bytes()
        with patch.object(export, "run_session", side_effect=AssertionError("No extra ASR")):
            report = self.report([b, a])
        entries = storage.read_doc(Path(report["report"]).with_name("manifest.json"))["ordered_sources"]
        self.assertEqual({e["transcript"]["run_id"] for e in entries}, {run["run_id"]})
        self.assertEqual({e["transcript"]["session_id"] for e in entries}, {session.name})
        self.assertEqual(before, (session / "session.yaml").read_bytes())

    def test_failed_input_is_retained_and_remaining_valid_input_completes(self):
        a, b = self.source("first.wav", 10), self.source("last.wav", 20)
        missing = self.root / "missing 空间.wav"
        result = self.report([a, missing, b])
        self.assertEqual((result["state"], result["completed"], result["failed"]), ("partial", 2, 1))
        self.assertEqual(result["quality"]["status"], "incomplete")
        text = Path(result["report"]).read_text()
        self.assertIn("PARTIAL REPORT", text)
        self.assertIn("## 2. missing 空间.wav", text)
        self.assertIn("**FAILED", text)
        self.assertIn("## 3. last.wav", text)
        self.assertEqual(self.calls, 2)

    def test_decoder_failure_does_not_disappear_or_stop_next_file(self):
        paths = [self.source("failed.wav", 10), self.source("good.wav", 20)]
        def decode(*args, **kwargs):
            if self.calls == 0:
                self.calls += 1
                raise RuntimeError("mock decoder failure; raw text must not appear in report")
            return self.decode(*args)
        with patch.object(engine, "decode", side_effect=decode):
            result = self.report(paths)
        self.assertEqual((result["completed"], result["failed"]), (1, 1))
        self.assertNotIn("raw text must not", Path(result["report"]).read_text())

    def test_allocation_failure_reports_resource_action_without_retrying_failed_file(self):
        paths = [self.source("allocation.wav", 10), self.source("good.wav", 20)]
        def decode(*args, **kwargs):
            if self.calls == 0:
                self.calls += 1
                raise engine.ResourceAllocationError("synthetic allocation failure")
            return self.decode(*args)
        with patch.object(engine, "decode", side_effect=decode):
            result = self.report(paths)
        self.assertEqual((result["completed"], result["failed"], self.calls), (1, 1, 2))
        entries = storage.read_doc(Path(result["report"]).with_name("manifest.json"))["ordered_sources"]
        self.assertIn("could not allocate memory", entries[0]["failure_message"])
        self.assertNotIn("recording is intact", entries[0]["failure_message"])

    def test_failed_raw_timing_is_revalidated_without_new_asr(self):
        import platform
        path=self.source("invalid.wav",10)
        def invalid_decoder(*args, **kwargs):
            meta=self.decode(*args)
            settings,runtime,model,wav,out,asr,glossary=args
            storage.write_json(out/"native.json",{"transcription":[{"offsets":{"from":900,"to":1751},"text":"Raw fixture remains preserved."}]},overwrite=True)
            command=[runtime["runtime"]["cli"],"-m",model["path"],"-f",str(wav),"-l",asr["language"],"-t",str(asr["threads"]),"-bs",str(asr["beam_size"]),"-tp",str(asr["temperature"]),"-tpi",str(asr["temperature_increment"]),"-ojf","-of",str(out/"native")]
            if platform.machine() != "arm64":command.append("-ng")
            storage.write_json(out/"invocation.json",{"args":command,"input_sha256":storage.sha256_file(wav),"model_sha256":model["sha256"]})
            return meta
        with patch.object(engine,"decode",side_effect=invalid_decoder):
            first=self.report([path])
        raw=next(self.data.glob("sessions/*/transcript/*/logs/*/native.json")); before=raw.read_bytes()
        with patch.object(export,"run_session",side_effect=AssertionError("Invalid cached raw must not be retranscribed")):
            second=self.report([path])
        self.assertEqual(second["review_required"],1)
        self.assertEqual(second["failed"],0)
        manifest=storage.read_doc(Path(second["report"]).with_name("manifest.json"))
        source=manifest["ordered_sources"][0]
        self.assertEqual(source["state"], "review_required")
        self.assertIn("invalid_timestamp", source["transcript"]["quality"]["reasons"])
        self.assertIn("Raw fixture remains preserved.", Path(second["report"]).read_text())
        self.assertEqual(raw.read_bytes(),before)
        self.assertEqual(self.calls,1)

    def test_newer_failure_does_not_hide_older_verified_success(self):
        path=self.source("cached-good.wav",10)
        good=self.report([path])
        session=next(self.data.glob("sessions/*/session.yaml")).parent
        with patch.object(engine,"decode",side_effect=RuntimeError("Fixture failed retry")):
            with self.assertRaises(RuntimeError):
                engine.run_session(self.settings,session,self.resolved,force=True)
        with patch.object(export,"reject_preserved_invalid_timeline",side_effect=AssertionError("Successful cache must be preferred")),patch.object(export,"run_session",side_effect=AssertionError("No inference")):
            reused=self.report([path])
        self.assertEqual(reused["report"],good["report"])
        self.assertEqual(reused["completed"],1)

    def test_identical_selection_is_explicitly_retained_without_extra_decode(self):
        a = self.source("part2.wav", 10)
        alias = self.root / "same bytes.wav"
        alias.write_bytes(a.read_bytes())
        result = self.report([a, alias, a])
        entries = storage.read_doc(Path(result["report"]).with_name("manifest.json"))["ordered_sources"]
        self.assertEqual([e["duplicate_of"] for e in entries], [None, 1, 1])
        self.assertEqual(result["completed"], 3)
        self.assertEqual(self.calls, 1)
        self.assertEqual(Path(result["report"]).read_text().count(export.markdown_text(self.raw[0])), 6)

    def test_same_name_changed_bytes_and_changed_settings_do_not_reuse_stale_result(self):
        a = self.source("same.wav", 10)
        first = self.report([a])
        self.source("same.wav", 20)
        second = self.report([a])
        self.assertEqual(self.calls, 2)
        changed = copy.deepcopy(self.resolved)
        changed["asr"]["beam_size"] = 6
        third = self.report([a], changed)
        self.assertEqual(self.calls, 3)
        self.assertEqual(len({r["report"] for r in (first, second, third)}), 3)

    def test_source_specific_gain_is_recomputed(self):
        a, b = self.source("quiet.wav", 10), self.source("louder.wav", 10000)
        report = self.report([a, b])
        entries = storage.read_doc(Path(report["report"]).with_name("manifest.json"))["ordered_sources"]
        gains = []
        for e in entries:
            t = e["transcript"]
            d = storage.read_doc(self.data / "sessions" / t["session_id"] / "transcript" / t["run_id"] / "diagnostics.json")
            gains.append(d["sources"][0]["transform"]["gain_db"])
        self.assertGreater(gains[0], gains[1])

    def test_interrupted_publication_has_no_visible_report_and_retry_reuses_asr(self):
        paths = [self.source("a.wav", 10), self.source("b.wav", 20)]
        original = export.os.rename
        def rename(a, b):
            if str(a).endswith(".pending"): raise OSError("simulated publication interruption")
            return original(a, b)
        with patch.object(export.os, "rename", side_effect=rename), self.assertRaises(OSError):
            self.report(paths)
        self.assertFalse(library.list_library(self.settings)["reports"])
        self.assertEqual(self.calls, 2)
        result = self.report(paths)
        self.assertEqual(self.calls, 2)
        self.assertEqual(result["completed"], 2)

    def test_changed_export_is_preserved_and_versioned(self):
        a = self.source("single.wav", 10)
        first = self.report([a])
        report = Path(first["report"])
        report.write_text("Owner edited fixture", encoding="utf-8")
        second = self.report([a])
        self.assertNotEqual(first["report"], second["report"])
        self.assertEqual(report.read_text(), "Owner edited fixture")
        self.assertEqual(self.calls, 1)

    def test_unrelated_corrupt_export_does_not_block_new_report(self):
        broken = self.data / "exports" / "batch-unrelated" / "manifest.json"
        broken.parent.mkdir(parents=True)
        broken.write_text("not valid JSON")
        result = self.report([self.source("new.wav", 10)])
        self.assertEqual(result["completed"], 1)
        self.assertEqual(broken.read_text(), "not valid JSON")

    def test_native_launch_reveals_markdown_even_when_partial(self):
        paths = [str(self.source("a.wav", 10)), str(self.root / "missing.wav")]
        payload = {"files": paths, "profiles": {"speaker": None, "capture": None, "glossary": None}}
        with patch.object(cli, "native_dialog", return_value=payload), patch.object(cli.subprocess, "run") as opener, patch("sys.stdout", new_callable=io.StringIO):
            result = cli.launch(self.settings)
        self.assertEqual(result["state"], "partial")
        self.assertEqual(opener.call_args.args[0], ["/usr/bin/open", "-R", result["report"]])

    def test_cli_confirmation_cancel_correction_and_progress(self):
        a, b = self.source("part2.wav", 10), self.source("part10.wav", 20)
        args = cli.parser().parse_args(["report", str(b), str(a)])
        with patch.object(cli.sys.stdin, "isatty", return_value=False), self.assertRaises(ValueError):
            cli.report_command(self.settings, args)
        with patch.object(cli.sys.stdin, "isatty", return_value=True), patch("builtins.input", return_value=""), patch("sys.stdout", new_callable=io.StringIO):
            self.assertTrue(cli.report_command(self.settings, args)["cancelled"])
        self.assertFalse(self.data.exists())
        with patch.object(cli.sys.stdin, "isatty", return_value=True), patch("builtins.input", return_value="2,1"), patch("sys.stdout", new_callable=io.StringIO) as stdout:
            result = cli.report_command(self.settings, args)
        entries = storage.read_doc(Path(result["report"]).with_name("manifest.json"))["ordered_sources"]
        self.assertEqual([e["filename"] for e in entries], [b.name, a.name])
        self.assertIn("2 of 2", stdout.getvalue())
        self.assertNotIn(self.raw[0], stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
