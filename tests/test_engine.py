"""Engine lifecycle fixtures. Decoder is mocked: these are not inference evidence."""
import copy
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import wave

from audio_transcribe import benchmark, config, engine, storage
from audio_transcribe.execution import ExecutionOwner, OperationContext
from audio_transcribe.thermal import ThermalController


class EngineTests(unittest.TestCase):
    def test_previous_candidate_a_identity_is_narrowly_compatible(self):
        current = engine.transform_identity()
        self.assertEqual(current["implementation_sha256"], engine._VALIDATED_STREAMING_A_SHA256)
        previous = {**current, "implementation_sha256": engine._PREVIOUS_AUDIO_SHA256}
        self.assertTrue(engine.compatible_transform_identity(previous, {"candidate": "A"}))
        self.assertFalse(engine.compatible_transform_identity(previous, {"candidate": "B"}))
        self.assertFalse(engine.compatible_transform_identity({**previous, "dependencies": {}}, {"candidate": "A"}))

    def test_decoder_allocation_log_classifier_is_specific(self):
        log = self.root / "decoder.log"
        log.write_text("ggml_metal: failed to allocate buffer\n", encoding="utf-8")
        self.assertTrue(engine._allocation_failure_log(log))
        log.write_text("ordinary whisper log\nThe speaker said out of memory.\n", encoding="utf-8")
        self.assertFalse(engine._allocation_failure_log(log))

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.data = self.root / "data"
        self.settings = {"roots": {"data": str(self.data), "app": str(self.root / "app")}}
        self.resolved = config.resolve_config(self.data)
        self.runtime = {"runtime": {"cli": "/fixture/whisper-cli", "sha256": "fixture-runtime", "release": "fixture"}}
        self.model = {"name": "large-v3", "path": "/fixture/model", "sha256": "fixture-model", "precision": "f16"}
        self.inferences = 0
        for name, value in (("load_runtime", self.runtime), ("model_identity", self.model)):
            patcher = patch("audio_transcribe.engine." + name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def source(self, name="source.wav", seconds=1.0, amplitude=10):
        path = self.root / name
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(struct.pack("<h", amplitude) * round(seconds * 16000))
        return path

    def session(self, multiple=False):
        sources = [self.source("one 空间.wav")]
        if multiple:
            sources.append(self.source("two.wav", 2, 20))
        return storage.import_sources(self.data, sources, order_confirmed=multiple)[0]

    def mocked_decoder(self, settings, runtime, model, wav_path, output_dir, asr, glossary, **kwargs):
        self.inferences += 1
        output_dir.mkdir(parents=True, exist_ok=True)
        with wave.open(str(wav_path), "rb") as audio:
            frames, rate = audio.getnframes(), audio.getframerate()
        native = output_dir / "native.json"
        if not native.exists():
            storage.write_json(native, {"transcription": [{"offsets": {"from": 100, "to": 500}, "text": "Synthetic fixture only."}]})
        return {"input_duration_seconds": frames / rate, "input_frames": frames, "elapsed_seconds": 0.01, "real_time_factor": 0.01 / (frames / rate), "native_sha256": storage.sha256_file(native)}

    def test_prepared_session_keeps_disk_metadata_and_rechecks_sources_and_derivatives(self):
        path = self.session(multiple=True)
        prepared = engine.prepare_session(self.settings, path, self.resolved)
        self.assertEqual(len(prepared.sources), 2)
        self.assertEqual(len(prepared.derivatives), 2)
        self.assertTrue(all(isinstance(item[0], Path) for item in prepared.derivatives))
        with patch.object(engine, "derivative", side_effect=AssertionError("must not prepare twice")), \
                patch.object(engine, "decode", side_effect=self.mocked_decoder):
            result = engine.run_session(self.settings, path, self.resolved, prepared=prepared)
        self.assertEqual(self.inferences, 2)
        self.assertEqual(result["state"], "completed")
        altered = prepared.derivatives[0][0]
        altered.write_bytes(altered.read_bytes() + b"changed")
        with patch.object(engine, "decode", side_effect=AssertionError("must not decode")), self.assertRaisesRegex(ValueError, "derivative hash"):
            engine.run_session(self.settings, path, self.resolved, prepared=prepared, force=True)
        original = storage.managed_source_path(path, prepared.sources[0])
        original.write_bytes(original.read_bytes() + b"changed")
        with self.assertRaisesRegex(ValueError, "Managed source"):
            engine.run_session(self.settings, path, self.resolved, prepared=prepared)

    def test_completed_cache_skips_derivative_and_execution_changes_do_not_invalidate(self):
        path = self.session()
        with patch.object(engine, "decode", side_effect=self.mocked_decoder):
            result = engine.run_session(self.settings, path, self.resolved)
        changed = copy.deepcopy(self.resolved)
        changed["execution"] = {"asr_workers": 2, "prepared_ahead": 1}
        changed["origins"]["execution.asr_workers"] = "request"
        with patch.object(engine, "derivative", side_effect=AssertionError("must skip preparation")), \
                patch.object(engine, "decode", side_effect=AssertionError("must not decode")):
            prepared = engine.prepare_session(self.settings, path, changed)
            reused = engine.run_session(self.settings, path, changed, prepared=prepared)
        self.assertEqual(prepared.derivatives, ())
        self.assertEqual(reused["run_id"], result["run_id"])
        self.assertEqual(self.inferences, 1)
        self.assertEqual(storage.read_doc(Path(result["path"]) / "manifest.json")["execution"]["asr_workers"], 1)

    def test_encoded_media_fast_cache_cannot_bypass_changed_decoder_pcm(self):
        from audio_transcribe import media
        encoded = self.root / "synthetic.mp3"
        encoded.write_bytes(b"synthetic encoded bytes; no real decoder needed")
        path = storage.import_sources(self.data, [encoded])[0]
        first_pcm = self.source("decoded-first.wav", amplitude=1000)
        second_pcm = self.source("decoded-second.wav", amplitude=3000)
        self.resolved["preprocessing"]["gain_db"] = 0.0
        with patch.object(media, "working_source", return_value=(first_pcm, {"identity": {"decoder": "first"}})), \
                patch.object(engine, "decode", side_effect=self.mocked_decoder):
            first = engine.run_session(self.settings, path, self.resolved)
        original = {p: p.read_bytes() for p in Path(first["path"]).rglob("*") if p.is_file()}
        with patch.object(media, "working_source", return_value=(second_pcm, {"identity": {"decoder": "changed"}})), \
                patch.object(engine, "decode", side_effect=self.mocked_decoder):
            prepared = engine.prepare_session(self.settings, path, self.resolved)
            self.assertIsNone(prepared.cached_result)
            second = engine.run_session(self.settings, path, self.resolved, prepared=prepared)
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertEqual(self.inferences, 2)
        for artifact, data in original.items():
            self.assertEqual(artifact.read_bytes(), data)
        # Even equal PCM must not misreport which media decoder was selected.
        with patch.object(media, "working_source", return_value=(second_pcm, {"identity": {"decoder": "third-same-pcm"}})), \
                patch.object(engine, "decode", side_effect=self.mocked_decoder):
            third = engine.run_session(self.settings, path, self.resolved)
            reused = engine.run_session(self.settings, path, self.resolved)
        self.assertNotEqual(second["run_id"], third["run_id"])
        self.assertEqual(reused["run_id"], third["run_id"])
        self.assertEqual(self.inferences, 3)
        self.assertEqual(storage.read_doc(Path(third["path"]) / "manifest.json")["media_decode_identities"],
                         [{"decoder": "third-same-pcm"}])

    def test_cached_preparation_can_be_explicitly_forced_to_new_decode(self):
        path = self.session()
        with patch.object(engine, "decode", side_effect=self.mocked_decoder):
            first = engine.run_session(self.settings, path, self.resolved)
            prepared = engine.prepare_session(self.settings, path, self.resolved)
            second = engine.run_session(self.settings, path, self.resolved, prepared=prepared, force=True)
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertEqual(self.inferences, 2)

    def test_timestamp_units_offsets_and_invalid_schema(self):
        native = {"transcription": [{"offsets": {"from": 1250, "to": 2250}, "text": "Fixture"}]}
        segment = engine.decoder_segments(native, "src-fixture", 3, source_start=90, global_offset=5)[0]
        self.assertEqual(segment["start_seconds"], 91.25)
        self.assertEqual(segment["end_seconds"], 92.25)
        self.assertEqual(segment["global_start_seconds"], 6.25)
        for offsets in ({"from": True, "to": 2}, {"from": 0, "to": 9000}, {"from": 2, "to": 1}):
            with self.subTest(offsets=offsets), self.assertRaises(ValueError):
                engine.decoder_segments({"transcription": [{"offsets": offsets, "text": "Fixture"}]}, "src", 3)
        with self.assertRaises(ValueError):
            engine.decoder_segments({}, "src", 3)

    def test_unjustified_legacy_50ms_clamp_is_no_longer_accepted(self):
        native = {"transcription": [{"offsets": {"from": 900, "to": 1040}, "text": "Fixture"}]}
        original = copy.deepcopy(native)
        with self.assertRaises(ValueError):
            engine.decoder_segments(native, "src-fixture", 1)
        self.assertEqual(native, original)

    def test_outside_timeline_never_publishes_validated_artifacts(self):
        path = self.session()
        def outside_decoder(*args, **kwargs):
            metadata = self.mocked_decoder(*args, **kwargs)
            native = Path(args[4]) / "native.json"
            storage.write_json(native, {"transcription": [{"offsets": {"from": 900, "to": 1040}, "text": "Synthetic fixture only."}]}, overwrite=True)
            metadata["native_sha256"] = storage.sha256_file(native)
            return metadata
        with patch("audio_transcribe.engine.decode", side_effect=outside_decoder):
            result = engine.run_session(self.settings, path, self.resolved)
        self.assertEqual(result["state"], "review_required")
        self.assertFalse(result["timestamp_valid"])
        self.assertFalse((path / "transcript/current.json").exists())
        self.assertEqual(len(list(path.glob("transcript/*/logs/*/native.json"))), 1)
        run = Path(result["path"])
        self.assertFalse(engine.verify_completed_run(run))
        self.assertTrue(engine.verify_review_run(run))
        document = storage.read_doc(run / "transcript.json")
        self.assertEqual(document["segments"][0]["end_seconds"], 1.04)
        self.assertEqual(document["segments"][0]["text"], "Synthetic fixture only.")
        self.assertFalse((run / "transcript.srt").exists())
        with patch.object(engine, "decode", side_effect=AssertionError("No repeat of known provisional ASR")):
            reused = engine.run_session(self.settings, path, self.resolved)
        self.assertTrue(reused["reused"])
        self.assertEqual(reused["state"], "review_required")

    def test_repetition_is_preserved_and_not_a_successful_current(self):
        path = self.session()
        def repeated_decoder(*args, **kwargs):
            metadata = self.mocked_decoder(*args, **kwargs)
            native = Path(args[4]) / "native.json"
            sentence = "A deliberately repeated synthetic fixture sentence. "
            storage.write_json(native, {"transcription": [{"offsets": {"from": 0, "to": 900}, "text": sentence * 8}]}, overwrite=True)
            return metadata
        with patch.object(engine, "decode", side_effect=repeated_decoder):
            result = engine.run_session(self.settings, path, self.resolved)
        self.assertEqual(result["state"], "review_required")
        self.assertTrue(result["timestamp_valid"])
        self.assertIn("repeated_phrase_loop", result["quality"]["reasons"])
        self.assertFalse((path / "transcript/current.json").exists())
        self.assertEqual((Path(result["path"]) / "transcript.txt").read_text().count("synthetic fixture"), 8)

    def test_default_context_policy_changes_the_inference_identity(self):
        self.assertEqual(self.resolved["asr"]["max_context"], 0)
        path = self.session()
        with patch.object(engine, "decode", side_effect=self.mocked_decoder):
            first = engine.run_session(self.settings, path, self.resolved)
            changed = copy.deepcopy(self.resolved)
            changed["asr"]["max_context"] = 224
            second = engine.run_session(self.settings, path, changed)
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertEqual(self.inferences, 2)

    def test_negative_raw_start_is_signed_in_provisional_markdown(self):
        path = self.session()
        def negative_decoder(*args, **kwargs):
            metadata = self.mocked_decoder(*args, **kwargs)
            native = Path(args[4]) / "native.json"
            storage.write_json(native, {"transcription": [{"offsets": {"from": -20, "to": 500}, "text": "Synthetic fixture only."}]}, overwrite=True)
            return metadata
        with patch.object(engine, "decode", side_effect=negative_decoder):
            result = engine.run_session(self.settings, path, self.resolved)
        self.assertEqual(result["state"], "review_required")
        self.assertIn("[-00:00:00.020", (Path(result["path"]) / "transcript.md").read_text())
        self.assertFalse((Path(result["path"]) / "transcript.srt").exists())

    def test_glossary_warning_is_saved_and_reused_without_claiming_it_was_applied(self):
        path = self.session()
        self.resolved["glossary"]["terms"] = ["SyntheticGlossaryTerm"]
        with patch.object(engine, "decode", side_effect=self.mocked_decoder):
            result = engine.run_session(self.settings, path, self.resolved)
            reused = engine.run_session(self.settings, path, self.resolved)
        self.assertEqual(self.inferences, 1)
        self.assertEqual(reused["state"], "review_required")
        self.assertIn("glossary_context_disabled", result["quality"]["reasons"])
        self.assertIn("no-context decoding disables glossary prompting", (Path(result["path"]) / "transcript.md").read_text())

    def test_final_session_write_failure_never_publishes_failed_current(self):
        for prior_success in (False, True):
            with self.subTest(prior_success=prior_success):
                path = storage.import_sources(self.data, [self.source()], separate=True)[0]
                prior_id = None
                if prior_success:
                    with patch("audio_transcribe.engine.decode", side_effect=self.mocked_decoder):
                        prior_id = engine.run_session(self.settings, path, self.resolved)["run_id"]
                    current = storage.read_doc(path / "transcript/current.json")
                    current["accepted_run_id"] = prior_id
                    storage.write_json(path / "transcript/current.json", current, overwrite=True)
                failed_once = False
                def fail_final_session_write(target, document, **kwargs):
                    nonlocal failed_once
                    if Path(target) == path / "session.yaml" and document.get("processing_status") == "completed" and not failed_once:
                        failed_once = True
                        raise OSError("Synthetic final session metadata write failure")
                    return storage.write_yaml(target, document, **kwargs)
                with patch("audio_transcribe.engine.decode", side_effect=self.mocked_decoder), patch("audio_transcribe.engine.write_yaml", side_effect=fail_final_session_write):
                    with self.assertRaises(OSError):
                        engine.run_session(self.settings, path, self.resolved, force=prior_success)
                current_path = path / "transcript/current.json"
                if current_path.exists():
                    current = storage.read_doc(current_path)
                    target = path / "transcript" / current["latest_successful_run_id"] / "manifest.json"
                    self.assertEqual(storage.read_doc(target)["state"], "completed")
                    if prior_success:
                        self.assertEqual(current["latest_successful_run_id"], prior_id)
                        self.assertEqual(current["accepted_run_id"], prior_id)

    def test_multisource_mapping_and_timeline(self):
        path = self.session(multiple=True)
        with patch("audio_transcribe.engine.decode", side_effect=self.mocked_decoder):
            result = engine.run_session(self.settings, path, self.resolved)
        document = storage.read_doc(Path(result["path"]) / "transcript.json")
        self.assertEqual(len(document["source_map"]), 2)
        self.assertEqual(document["source_map"][1]["global_offset_seconds"], 1)
        self.assertAlmostEqual(document["segments"][1]["start_seconds"], 0.1)
        self.assertAlmostEqual(document["segments"][1]["global_start_seconds"], 1.1)
        self.assertTrue(all(source["decoded_input_complete"] for source in document["source_map"]))
        srt = (Path(result["path"]) / "transcript.srt").read_text()
        self.assertIn("00:00:01,100 --> 00:00:01,500", srt)
        current = storage.read_doc(path / "transcript/current.json")
        self.assertIsNone(current["accepted_run_id"])
        self.assertEqual(current["latest_successful_run_id"], result["run_id"])
        self.assertTrue(engine.verify_completed_run(Path(result["path"])))

    def test_reuse_force_and_current_acceptance_preserved(self):
        path = self.session()
        with patch("audio_transcribe.engine.decode", side_effect=self.mocked_decoder):
            first = engine.run_session(self.settings, path, self.resolved)
            second = engine.run_session(self.settings, path, self.resolved)
            self.assertTrue(second["reused"])
            self.assertEqual(self.inferences, 1)
            current_path = path / "transcript/current.json"
            current = storage.read_doc(current_path)
            current["accepted_run_id"] = first["run_id"]
            storage.write_json(current_path, current, overwrite=True)
            forced = engine.run_session(self.settings, path, self.resolved, force=True)
        self.assertNotEqual(forced["run_id"], first["run_id"])
        self.assertEqual(storage.read_doc(current_path)["accepted_run_id"], first["run_id"])
        self.assertTrue(engine.verify_completed_run(Path(first["path"])))
        with patch("audio_transcribe.engine.decode", side_effect=RuntimeError("mock failure")):
            with self.assertRaises(RuntimeError):
                engine.run_session(self.settings, path, self.resolved, force=True)
        self.assertEqual(storage.read_doc(current_path)["latest_successful_run_id"], forced["run_id"])

    def test_failed_run_resumes_without_current_pointer(self):
        path = self.session()
        with patch("audio_transcribe.engine.decode", side_effect=RuntimeError("mock failure")):
            with self.assertRaises(RuntimeError):
                engine.run_session(self.settings, path, self.resolved)
        self.assertFalse((path / "transcript/current.json").exists())
        failed = next((path / "transcript").glob("*/manifest.json"))
        self.assertEqual(storage.read_doc(failed)["state"], "failed")
        with patch("audio_transcribe.engine.decode", side_effect=self.mocked_decoder):
            resumed = engine.run_session(self.settings, path, self.resolved)
        self.assertTrue(resumed["resumed"])
        self.assertEqual(resumed["run_id"], failed.parent.name)

    def test_partial_finalization_preserved_and_resumed(self):
        path = self.session()
        def interrupted_writer(run_path, segments, source_map):
            storage.write_json(run_path / "transcript.json", {"fixture": "partial"})
            raise KeyboardInterrupt()
        with patch("audio_transcribe.engine.decode", side_effect=self.mocked_decoder), patch("audio_transcribe.engine.write_transcripts", side_effect=interrupted_writer):
            with self.assertRaises(KeyboardInterrupt):
                engine.run_session(self.settings, path, self.resolved)
        self.assertFalse((path / "transcript/current.json").exists())
        with patch("audio_transcribe.engine.decode", side_effect=self.mocked_decoder):
            resumed = engine.run_session(self.settings, path, self.resolved)
        self.assertTrue(resumed["resumed"])
        self.assertTrue(list(Path(resumed["path"]).glob("logs/interrupted-finalization-*/transcript.json")))
        self.assertTrue(engine.verify_completed_run(Path(resumed["path"])))

    def test_completed_output_edit_requires_explicit_force(self):
        path = self.session()
        with patch("audio_transcribe.engine.decode", side_effect=self.mocked_decoder):
            completed = engine.run_session(self.settings, path, self.resolved)
        output = Path(completed["path"]) / "transcript.txt"
        output.write_text("Synthetic edited fixture", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "changed"):
            engine.run_session(self.settings, path, self.resolved)
        self.assertEqual(output.read_text(), "Synthetic edited fixture")

    def test_changed_interrupted_snapshot_is_rejected(self):
        path = self.session()
        with patch("audio_transcribe.engine.decode", side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                engine.run_session(self.settings, path, self.resolved)
        saved = next((path / "transcript").glob("*/resolved-config.yaml"))
        tampered = storage.read_doc(saved)
        tampered["asr"]["beam_size"] = 99
        storage.write_yaml(saved, tampered, overwrite=True)
        with patch("audio_transcribe.engine.decode", side_effect=self.mocked_decoder):
            with self.assertRaises(ValueError):
                engine.run_session(self.settings, path, self.resolved)

    def test_reuse_recovers_missing_current_pointer(self):
        path = self.session()
        with patch("audio_transcribe.engine.decode", side_effect=self.mocked_decoder):
            completed = engine.run_session(self.settings, path, self.resolved)
        (path / "transcript/current.json").unlink()
        with patch("audio_transcribe.engine.decode", side_effect=AssertionError("No inference should occur")):
            reused = engine.run_session(self.settings, path, self.resolved)
        self.assertTrue(reused["reused"])
        pointer = storage.read_doc(path / "transcript/current.json")
        self.assertEqual(pointer["latest_successful_run_id"], completed["run_id"])

    def test_presentation_update_reuses_verified_raw_without_inference(self):
        source=self.root / "resampled.wav"
        with wave.open(str(source),"wb") as w:
            w.setparams((1,2,44100,44101,"NONE","not compressed"))
            w.writeframes(struct.pack("<h",10)*44101)
        session=storage.import_sources(self.data,[source])[0]
        def cached_decoder(*args,**kwargs):
            meta=self.mocked_decoder(*args,**kwargs)
            settings,runtime,model,wav,out,asr,glossary=args
            storage.write_json(out / "native.json",{"transcription":[{"offsets":{"from":900,"to":1010},"text":"Fixture."}]},overwrite=True)
            meta["native_sha256"]=storage.sha256_file(out/"native.json")
            meta["checkpoint_identity"]=engine.digest({"input_sha256":storage.sha256_file(wav),"model_sha256":model["sha256"],"runtime_sha256":runtime["runtime"]["sha256"],"asr":asr,"glossary":glossary})
            storage.write_json(out/"complete.json",meta)
            return meta
        with patch.object(engine,"decode",side_effect=cached_decoder):
            first=engine.run_session(self.settings,session,self.resolved)
        old=Path(first["path"])
        doc=storage.read_doc(old/"transcript.json")
        # Reproduce legacy derivative-EOF normalization from a previous renderer.
        doc["segments"][0]["end_seconds"]=16001/16000
        doc["segments"][0]["global_end_seconds"]=16001/16000
        storage.write_json(old/"transcript.json",doc,overwrite=True)
        manifest=storage.read_doc(old/"manifest.json")
        manifest["assembly_identity"]={"fixture":"legacy presentation"}
        manifest["fingerprint"]="previous-fixture-fingerprint"
        manifest["output_hashes"]=engine.output_hashes(old)
        storage.write_json(old/"manifest.json",manifest,overwrite=True)
        before={p:p.read_bytes() for p in old.rglob("*") if p.is_file()}
        # This fixture forbids every subprocess during receipt reuse. Disable
        # the independent thermal observer without weakening the ASR guard.
        with ExecutionOwner(self.settings,thermal=ThermalController(monitor=False)) as owner, \
                patch.object(engine.subprocess,"Popen",side_effect=AssertionError("No new ASR")) as launch:
            result=engine.run_session(self.settings,session,self.resolved,context=OperationContext(owner))
            launch.assert_not_called()
        self.assertTrue(result["reused_asr"])
        self.assertNotEqual(result["run_id"],first["run_id"])
        self.assertEqual(self.inferences,1)
        updated=storage.read_doc(Path(result["path"])/"transcript.json")
        self.assertEqual(updated["segments"][0]["end_seconds"],44101/44100)
        self.assertTrue(updated["segments"][0]["timestamp_normalization"])
        for p,value in before.items():self.assertEqual(p.read_bytes(),value)
        with patch.object(engine,"decode",side_effect=AssertionError("No repeated reassembly")):
            reused=engine.run_session(self.settings,session,self.resolved)
        self.assertEqual(reused["run_id"],result["run_id"])

    def test_renderer_change_recovers_completed_receipt_from_interrupted_attempt(self):
        path = self.session()
        def receipt_decoder(*args, **kwargs):
            metadata = self.mocked_decoder(*args, **kwargs)
            settings, runtime, model, wav, output, asr, glossary = args
            metadata["checkpoint_identity"] = engine.digest({"input_sha256": storage.sha256_file(wav),
                "model_sha256": model["sha256"], "runtime_sha256": runtime["runtime"]["sha256"],
                "asr": asr, "glossary": glossary})
            storage.write_json(output / "complete.json", metadata)
            return metadata
        with patch.object(engine, "decode", side_effect=receipt_decoder), \
                patch.object(engine, "write_transcripts", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            engine.run_session(self.settings, path, self.resolved)
        original = next(path.glob("transcript/*/manifest.json")).parent
        saved = {p: p.read_bytes() for p in original.rglob("*") if p.is_file()}
        with ExecutionOwner(self.settings,thermal=ThermalController(monitor=False)) as owner, \
                patch.object(engine, "assembly_identity", return_value={"fixture": "new-renderer"}), \
                patch.object(engine.subprocess, "Popen", side_effect=AssertionError("Must reuse saved inference")) as launch:
            result = engine.run_session(self.settings, path, self.resolved,context=OperationContext(owner))
            launch.assert_not_called()
        self.assertNotEqual(Path(result["path"]), original)
        self.assertEqual(result["state"], "completed")
        self.assertTrue(result["reused_asr"])
        for artifact, content in saved.items():
            self.assertEqual(artifact.read_bytes(), content)
        self.assertEqual(self.inferences, 1)

    def test_source_path_escape_rejected(self):
        path = self.session()
        session = storage.read_doc(path / "session.yaml")
        outside = self.source("outside.wav", amplitude=30)
        source = session["sources"][0]
        source["path"] = str(outside)
        source["sha256"] = storage.sha256_file(outside)
        storage.write_yaml(path / "session.yaml", session, overwrite=True)
        with patch("audio_transcribe.engine.decode", side_effect=self.mocked_decoder):
            with self.assertRaises(ValueError):
                engine.run_session(self.settings, path, self.resolved)


class BenchmarkTests(unittest.TestCase):
    setUp = EngineTests.setUp
    source = EngineTests.source
    session = EngineTests.session
    mocked_decoder = EngineTests.mocked_decoder

    def run_fixture_benchmark(self, path, *, pilot=True):
        session = storage.read_doc(path / "session.yaml")
        source_id = session["source_order"][0]
        with patch("audio_transcribe.benchmark.load_runtime", return_value=self.runtime), patch("audio_transcribe.benchmark.model_identity", return_value=self.model), patch("audio_transcribe.benchmark.decode", side_effect=self.mocked_decoder):
            return benchmark.run_benchmark(self.settings, path, self.resolved, intervals=[(source_id, 0, 1)], pilot=pilot)

    def test_reference_status_and_archive_resolution(self):
        path = self.session()
        result = self.run_fixture_benchmark(path)
        reference = self.root / "human reference.txt"
        reference.write_text("Synthetic fixture only.", encoding="utf-8")
        expected_hash = storage.sha256_file(reference)
        session_id = storage.read_doc(path / "session.yaml")["id"]
        storage.archive_session(self.data, session_id, 2026)
        unverified = benchmark.evaluate_reference(self.settings, result["benchmark_id"], result["evaluation_id"], "clip-1", reference)
        self.assertEqual(unverified["reference_status"], "unverified")
        self.assertIsNone(unverified["scores"][0]["WER"])
        verified = benchmark.evaluate_reference(self.settings, result["benchmark_id"], result["evaluation_id"], "clip-1", reference, verified=True)
        self.assertEqual(verified["scores"][0]["WER"], 0)
        self.assertEqual(storage.sha256_file(reference), expected_hash)
        self.assertEqual(storage.sha256_file(Path(verified["path"]) / "reference.txt"), expected_hash)

    def test_active_session_wait_cancels_before_benchmark_mutation(self):
        path = self.session()
        with storage.session_lock(path):
            with patch("audio_transcribe.execution.time.sleep", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
                self.run_fixture_benchmark(path)
        self.assertEqual(list((self.data / "benchmarks").glob("*/evaluations/*/manifest.json")), [])

    def test_reference_rejects_changed_completed_hypothesis(self):
        path = self.session()
        result = self.run_fixture_benchmark(path)
        hypothesis = next(Path(result["path"]).glob("*/clip-1/hypothesis.txt"))
        hypothesis.write_text("Changed synthetic hypothesis", encoding="utf-8")
        reference = self.root / "reference.txt"
        reference.write_text("Synthetic fixture only.", encoding="utf-8")
        with self.assertRaises(ValueError):
            benchmark.evaluate_reference(self.settings, result["benchmark_id"], result["evaluation_id"], "clip-1", reference, verified=True)

    def test_reference_hash_path_traversal_rejected_without_external_read(self):
        path = self.session()
        result = self.run_fixture_benchmark(path)
        evaluation = Path(result["path"])
        manifest_path = evaluation / "manifest.json"
        original = storage.read_doc(manifest_path)
        external = self.root / "unrelated-private-fixture.txt"
        external.write_text("Unrelated synthetic material", encoding="utf-8")
        external_hash = storage.sha256_file(external)
        reference = self.root / "reference.txt"
        reference.write_text("Synthetic fixture only.", encoding="utf-8")
        import os
        candidates = [str(external), os.path.relpath(external, evaluation)]
        for outgoing in candidates:
            with self.subTest(outgoing=outgoing):
                altered = copy.deepcopy(original)
                altered["output_hashes"][outgoing] = external_hash
                storage.write_json(manifest_path, altered, overwrite=True)
                original_open = Path.open
                def guarded_open(file, *args, **kwargs):
                    if file.resolve() == external:
                        raise AssertionError("Reference evaluation read outside its artifact root")
                    return original_open(file, *args, **kwargs)
                with patch.object(Path, "open", guarded_open):
                    with self.assertRaises(ValueError):
                        benchmark.evaluate_reference(self.settings, result["benchmark_id"], result["evaluation_id"], "clip-1", reference, verified=True)

    def test_reference_result_path_traversal_rejected_without_external_read(self):
        path = self.session()
        result = self.run_fixture_benchmark(path)
        evaluation = Path(result["path"])
        manifest_path = evaluation / "manifest.json"
        manifest = storage.read_doc(manifest_path)
        external = self.root / "unrelated-result.json"
        storage.write_json(external, {"hypothesis_path": "synthetic-irrelevant"})
        manifest["results"][0]["result_path"] = str(external)
        storage.write_json(manifest_path, manifest, overwrite=True)
        reference = self.root / "reference.txt"
        reference.write_text("Synthetic fixture only.", encoding="utf-8")
        original_open = Path.open
        def guarded_open(file, *args, **kwargs):
            if file.resolve() == external:
                raise AssertionError("Reference evaluation read an uncontained result path")
            return original_open(file, *args, **kwargs)
        with patch.object(Path, "open", guarded_open):
            with self.assertRaises(ValueError):
                benchmark.evaluate_reference(self.settings, result["benchmark_id"], result["evaluation_id"], "clip-1", reference, verified=True)

    def test_reference_hypothesis_path_traversal_rejected_without_external_read(self):
        path = self.session()
        result = self.run_fixture_benchmark(path)
        evaluation = Path(result["path"])
        manifest_path = evaluation / "manifest.json"
        manifest = storage.read_doc(manifest_path)
        entry_path = evaluation / manifest["results"][0]["result_path"]
        entry = storage.read_doc(entry_path)
        external = self.root / "unrelated-hypothesis.txt"
        external.write_text("Unrelated synthetic material", encoding="utf-8")
        entry["hypothesis_path"] = str(external)
        storage.write_json(entry_path, entry, overwrite=True)
        # Keep the contained file's hash consistent: containment must stand on its own.
        manifest["output_hashes"][str(entry_path.relative_to(evaluation))] = storage.sha256_file(entry_path)
        storage.write_json(manifest_path, manifest, overwrite=True)
        reference = self.root / "reference.txt"
        reference.write_text("Synthetic fixture only.", encoding="utf-8")
        original_open = Path.open
        def guarded_open(file, *args, **kwargs):
            if file.resolve() == external:
                raise AssertionError("Reference evaluation read an uncontained hypothesis path")
            return original_open(file, *args, **kwargs)
        with patch.object(Path, "open", guarded_open):
            with self.assertRaises(ValueError):
                benchmark.evaluate_reference(self.settings, result["benchmark_id"], result["evaluation_id"], "clip-1", reference, verified=True)

    def test_general_benchmark_does_not_apply_sample_specific_gain(self):
        path = self.session()
        result = self.run_fixture_benchmark(path, pilot=False)
        manifest = storage.read_doc(Path(result["path"]) / "manifest.json")
        self.assertEqual(manifest["state"], "completed")
        self.assertTrue(all(entry["candidate"] != "large-v3-B" for entry in manifest["results"]))
        self.assertEqual({entry["candidate"] for entry in manifest["results"]}, {"large-v3-A", "large-v3-turbo-A"})


if __name__ == "__main__":
    unittest.main()
