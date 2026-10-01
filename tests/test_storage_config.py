import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from audio_transcribe import config, storage


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.data = self.root / "data"
        self.external = self.root / "original files 空间"
        self.external.mkdir()

    def source(self, name="录音 with spaces.wav", contents=b"fixture bytes"):
        path = self.external / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
        return path

    def test_import_preserves_source_and_duplicate_identity(self):
        source = self.source()
        before = source.read_bytes()
        path, session, reused = storage.import_sources(self.data, [source])
        self.assertFalse(reused)
        self.assertEqual((path / session["sources"][0]["path"]).read_bytes(), before)
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(session["sources"][0]["original_basename"], source.name)
        self.assertIsNone(session["recorded_at"])
        self.assertEqual(session["date_hints"][0]["kind"], "filesystem_mtime")
        second_path, second, reused = storage.import_sources(self.data, [source])
        self.assertTrue(reused)
        self.assertEqual(second_path, path)
        self.assertEqual(second["id"], session["id"])
        separate_path, _, reused = storage.import_sources(self.data, [source], separate=True)
        self.assertFalse(reused)
        self.assertNotEqual(separate_path, path)

    def test_collision_multi_order_and_partial_overlap(self):
        one = self.source("one/讲课.wav", b"first")
        two = self.source("two/讲课.wav", b"second")
        with self.assertRaisesRegex(ValueError, "order confirmation"):
            storage.import_sources(self.data, [one, two])
        path, session, _ = storage.import_sources(self.data, [two, one], order_confirmed=True)
        sources = session["sources"]
        self.assertEqual(session["source_order"], [item["id"] for item in sources])
        self.assertEqual((path / sources[0]["path"]).read_bytes(), b"second")
        self.assertEqual(Path(sources[1]["path"]).parts[1], sources[1]["id"])
        self.assertEqual(sources[0]["original_basename"], sources[1]["original_basename"])
        with self.assertRaisesRegex(ValueError, "overlaps"):
            storage.import_sources(self.data, [one])
        with self.assertRaisesRegex(ValueError, "overlaps"):
            storage.import_sources(self.data, [one, two], order_confirmed=True)
        self.assertTrue(storage.import_sources(self.data, [two, one], order_confirmed=True)[2])

    def test_same_name_different_bytes_not_duplicate(self):
        one = self.source("one/file.wav", b"one")
        two = self.source("two/file.wav", b"two")
        a, _, _ = storage.import_sources(self.data, [one])
        b, _, reused = storage.import_sources(self.data, [two])
        self.assertFalse(reused)
        self.assertNotEqual(a, b)

    def test_archive_restore_reference_and_inactive(self):
        source = self.source()
        path, session, _ = storage.import_sources(self.data, [source])
        session_id = session["id"]
        hashes = [item["sha256"] for item in session["sources"]]
        with storage.session_lock(path):
            with self.assertRaisesRegex(RuntimeError, "active"):
                storage.archive_session(self.data, session_id, 2026)
        session["processing_status"] = "running"
        storage.write_yaml(path / "session.yaml", session, overwrite=True)
        with self.assertRaisesRegex(ValueError, "active"):
            storage.archive_session(self.data, session_id, 2026)
        session["processing_status"] = "complete"
        storage.write_yaml(path / "session.yaml", session, overwrite=True)
        archived = storage.archive_session(self.data, session_id, 2026)
        self.assertEqual(archived, self.data / "archive/2026" / session_id)
        self.assertEqual(storage.locate_session(self.data, session_id), archived)
        self.assertTrue(storage.import_sources(self.data, [source])[2])
        restored = storage.restore_session(self.data, session_id)
        self.assertEqual(restored, path)
        self.assertEqual(hashes, [item["sha256"] for item in storage.read_doc(path / "session.yaml")["sources"]])

    def test_existing_corrupt_source_is_not_reused(self):
        source = self.source()
        path, session, _ = storage.import_sources(self.data, [source])
        (path / session["sources"][0]["path"]).write_bytes(b"damaged")
        with self.assertRaisesRegex(ValueError, "integrity"):
            storage.import_sources(self.data, [source])

    def test_failed_copy_does_not_publish_session(self):
        source = self.source()
        with patch("audio_transcribe.storage.shutil.copyfileobj", side_effect=OSError("fixture failure")):
            with self.assertRaises(OSError):
                storage.import_sources(self.data, [source])
        self.assertEqual(list((self.data / "sessions").iterdir()), [])
        self.assertEqual(source.read_bytes(), b"fixture bytes")

    def test_missing_invalid_and_duplicate_selection(self):
        with self.assertRaises(ValueError):
            storage.import_sources(self.data, [])
        with self.assertRaises(FileNotFoundError):
            storage.import_sources(self.data, [self.root / "missing.wav"])
        with self.assertRaises(ValueError):
            storage.locate_session(self.data, "../bad")
        source = self.source()
        with self.assertRaisesRegex(ValueError, "selected more than once"):
            storage.import_sources(self.data, [source, source], order_confirmed=True)

    def test_session_validation_rejects_escape_ids_and_order_drift(self):
        source = self.source()
        path, session, _ = storage.import_sources(self.data, [source])
        self.assertEqual(storage.validate_session(path, verify_sources=True)["id"], session["id"])
        for field, value in (("path", str(source)), ("id", "../outside"), ("size_bytes", True), ("order", True)):
            damaged = copy.deepcopy(session)
            damaged["sources"][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                storage.validate_session(path, damaged)
        damaged = copy.deepcopy(session)
        damaged["source_order"] *= 2
        with self.assertRaises(ValueError):
            storage.validate_session(path, damaged)

    def test_safe_atomic_files(self):
        path = self.root / "document.yaml"
        storage.write_yaml(path, {"schema_version": 1, "label": "空间"})
        with self.assertRaises(FileExistsError):
            storage.write_yaml(path, {"label": "replacement"})
        self.assertEqual(storage.read_doc(path)["label"], "空间")
        path.write_text("unsafe: !!python/object/apply:os.system ['false']", encoding="utf-8")
        with self.assertRaises(Exception):
            storage.read_doc(path)
        path.write_text("date: 2026-01-01", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "quote dates"):
            storage.read_doc(path)
        with self.assertRaises(ValueError):
            storage.write_json(self.root / "bad.json", {"value": float("nan")})


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def profile(self, kind, identifier, **changes):
        path = config.create_profile(self.root, kind, identifier, "Display label")
        document = storage.read_doc(path)
        document.update(changes)
        storage.write_yaml(path, document, overwrite=True)
        return path

    def test_defaults_do_not_create_directories(self):
        missing = self.root / "absent/settings.json"
        settings = config.load_settings(missing)
        self.assertFalse(missing.parent.exists())
        self.assertFalse(settings["storage_approved"])
        self.assertFalse(settings["settings_exists"])
        result = config.resolve_config(self.root)
        self.assertEqual(result["asr"]["model"], "large-v3-turbo")
        self.assertEqual(result["glossary"]["terms"], [])
        self.assertEqual(result["selected_profiles"]["speaker"], None)

    def test_settings_explicit_environment_precedence_and_approval(self):
        env_path, explicit = self.root / "env.json", self.root / "explicit.json"
        storage.write_json(env_path, {"schema_version": 1, "roots": {"data": str(self.root / "envdata")}})
        data = self.root / "approved"
        storage.write_json(explicit, {"schema_version": 1, "roots": {"data": str(data)}, "storage_approval": {"data_root": str(data), "mode": "local_alternative", "approved_at": "2026-09-23T00:00:00Z"}})
        with patch.dict(os.environ, {"AUDIO_TRANSCRIBE_SETTINGS": str(env_path)}):
            self.assertEqual(config.load_settings()["roots"]["data"], str(self.root / "envdata"))
            settings = config.load_settings(explicit)
        self.assertEqual(settings["roots"]["data"], str(data))
        self.assertTrue(settings["storage_approved"])
        document = storage.read_doc(explicit)
        document["roots"]["data"] = str(self.root / "changed")
        storage.write_json(explicit, document, overwrite=True)
        self.assertFalse(config.load_settings(explicit)["storage_approved"])

    def test_resolution_precedence_provenance_snapshot(self):
        speaker_path = self.profile("speaker", "teacher", asr={"beam_size": 8}, terms=["verified term"])
        self.profile("capture", "room", preprocessing={"channel": "left", "max_gain_db": 25})
        self.profile("glossary", "topic", terms=["topic term"])
        result = config.resolve_config(self.root, "teacher", "room", "topic", {"asr": {"beam_size": 7}})
        self.assertEqual(result["asr"]["beam_size"], 7)
        self.assertEqual(result["preprocessing"]["channel"], "left")
        self.assertEqual(result["origins"]["asr.beam_size"], "explicit_override")
        self.assertEqual(result["origins"]["preprocessing.channel"], "capture:room")
        self.assertEqual(result["glossary"]["terms"], ["verified term", "topic term"])
        snapshot = copy.deepcopy(result)
        document = storage.read_doc(speaker_path)
        document["asr"]["beam_size"] = 3
        storage.write_yaml(speaker_path, document, overwrite=True)
        self.assertEqual(result, snapshot)
        self.assertEqual(result["profile_snapshots"]["speaker"]["document"]["asr"]["beam_size"], 8)
        self.assertEqual(config.resolve_config(self.root, "teacher")["asr"]["beam_size"], 3)

    def test_unknown_cross_domain_types_and_unknown_profiles(self):
        with self.assertRaises(FileNotFoundError):
            config.resolve_config(self.root, speaker="missing")
        self.profile("speaker", "wrong", asr={"gain_db": 5})
        with self.assertRaisesRegex(ValueError, "cross-domain"):
            config.resolve_config(self.root, speaker="wrong")
        for override in ({"asr": {"beam_size": True}}, {"preprocessing": {"beam_size": 3}}, {"preprocessing": {"vad": True}}, {"asr": {"model": "cloud"}}, {"other": {}}, {"preprocessing": {"gain_db": 31}}):
            with self.subTest(override=override):
                with self.assertRaises(ValueError):
                    config.resolve_config(self.root, overrides=override)
        self.profile("capture", "unknown", imaginary="value")
        with self.assertRaisesRegex(ValueError, "Unknown"):
            config.load_profile(self.root, "capture", "unknown")

    def test_profile_creation_exclusive_and_listing(self):
        self.profile("speaker", "s1")
        with self.assertRaises(FileExistsError):
            config.create_profile(self.root, "speaker", "s1", "new")
        with self.assertRaises(ValueError):
            config.create_profile(self.root, "speaker", "../outside")
        self.assertEqual([item["id"] for item in config.list_profiles(self.root, "speaker")], ["s1"])

    def test_validated_recipe_exact_scope_and_explicit_override(self):
        speaker = self.profile("speaker", "speaker", asr={"beam_size": 6})
        self.profile("capture", "room")
        self.profile("capture", "different-room")
        glossary_hash = config.resolve_config(self.root)["glossary"]["sha256"]
        recipe = {"id": "recipe", "state": "validated_for_listed_clips", "applicability": {"speaker_id": "speaker", "capture_id": "room", "glossary_sha256": glossary_hash, "apply_to_matching_profiles": True}, "asr": {"beam_size": 9}, "preprocessing": {"channel": "right"}, "model_precision": "f16", "evaluation_refs": ["benchmark-1/evaluation-1"]}
        document = storage.read_doc(speaker)
        document["recipes"] = [recipe]
        storage.write_yaml(speaker, document, overwrite=True)
        resolved = config.resolve_config(self.root, "speaker", "room")
        self.assertEqual(resolved["asr"]["beam_size"], 9)
        self.assertEqual(resolved["preprocessing"]["channel"], "right")
        self.assertEqual(config.resolve_config(self.root, "speaker", "different-room")["asr"]["beam_size"], 6)
        self.assertIsNone(config.resolve_config(self.root, "speaker")["applied_recipe"])
        self.assertEqual(config.resolve_config(self.root, "speaker", "room", overrides={"asr": {"beam_size": 4}})["asr"]["beam_size"], 4)
        self.profile("glossary", "new-vocab", terms=["different"])
        self.assertIsNone(config.resolve_config(self.root, "speaker", "room", "new-vocab")["applied_recipe"])


if __name__ == "__main__":
    unittest.main()
