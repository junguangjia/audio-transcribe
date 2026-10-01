"""Synthetic referenced-input and watched-folder lifecycle checks."""
from pathlib import Path
import os
import struct
import threading
import time
import unittest
from unittest.mock import patch
import uuid
from types import SimpleNamespace

import test_export as fixtures
from audio_transcribe import audio, compute, deletion, direct, engine, export, lifecycle, storage


class WatchedReferenceTests(unittest.TestCase):
    def setUp(self):
        fixtures.ExportTests.setUp(self)
        def local_audio(settings, context, operation, source, *, output=None, policy=None):
            return audio.inspect_audio(source) if operation == "inspect" else audio.prepare_audio(source, output, policy=policy)
        fixture = patch.object(compute, "run_audio_operation", side_effect=local_audio)
        fixture.start(); self.addCleanup(fixture.stop)
        self.settings["roots"].update(cache=str(self.root / "cache"), log=str(self.root / "logs"))
        check = patch.object(deletion, "legacy_barrier", return_value=None)
        check.start(); self.addCleanup(check.stop)

    source = fixtures.ExportTests.source
    decode = fixtures.ExportTests.decode
    report = fixtures.ExportTests.report

    def test_dataless_source_is_rejected_before_content_open(self):
        original = self.source("cloud-placeholder.wav", 2)
        actual = original.lstat()
        fields = {key: getattr(actual, key) for key in
                  ("st_mode", "st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")}
        with patch.object(Path, "lstat", return_value=SimpleNamespace(**fields, st_flags=0x40000000)):
            with patch.object(storage, "sha256_file", side_effect=AssertionError("must not read bytes")):
                with self.assertRaisesRegex(ValueError, "not downloaded"):
                    storage.source_stat(original)
        self.assertEqual(storage.source_stat(original)["size_bytes"], actual.st_size)

    def test_deferred_read_keeps_saved_text_visible_without_trusting_original(self):
        original = self.source("deferred.wav", 10)
        result = export.build_independent(self.settings, [original], self.resolved,
                                          input_modes=["referenced"])["results"][0]
        identity = result["result_id"]
        actual_hash = storage.sha256_file

        def reject_original_hash(path):
            if Path(path) == original:
                raise AssertionError("Deferred read hashed the original audio")
            return actual_hash(path)

        with patch.object(storage, "sha256_file", side_effect=reject_original_hash):
            opened = direct.direct_request(self.settings, {"action": "read", "result_id": identity,
                                                           "defer_source_verification": True})
        self.assertIsNone(opened["audio_path"])
        self.assertEqual(opened["source_integrity"], "pending")
        self.assertEqual(opened["run_integrity"], "verified")
        self.assertIn("Same repeated text", opened["readable_text"])
        verified = direct.direct_request(self.settings, {"action": "verify_source", "result_id": identity})
        self.assertEqual((verified["result_id"], verified["audio_path"], verified["source_integrity"]),
                         (identity, str(original), "verified"))
        self.assertEqual(verified["verified_source_stat"], storage.source_stat(original))
        self.assertNotIn("pending", verified["quality_message"])
        self.assertEqual(direct.read_result(self.settings, identity)["source_integrity"], "verified")
        with self.assertRaisesRegex(ValueError, "boolean"):
            direct.direct_request(self.settings, {"action": "read", "result_id": identity,
                                                  "defer_source_verification": "true"})

        # Preserve size, inode and mtime to prove that verify_source does not
        # mistake metadata equality for byte equality.
        before = original.stat()
        with original.open("r+b") as handle:
            handle.seek(-1, os.SEEK_END)
            last = handle.read(1)
            handle.seek(-1, os.SEEK_END)
            handle.write(bytes([last[0] ^ 1]))
        os.utime(original, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(storage.source_stat(original)["mtime_ns"], before.st_mtime_ns)
        changed = direct.direct_request(self.settings, {"action": "verify_source", "result_id": identity})
        self.assertEqual((changed["result_id"], changed["audio_path"], changed["source_integrity"]),
                         (identity, None, "missing_or_changed"))
        self.assertIsNone(changed["verified_source_stat"])
        self.assertIn("unavailable or changed", changed["quality_message"])
        still_readable = direct.direct_request(self.settings, {"action": "read", "result_id": identity,
                                                               "defer_source_verification": True})
        self.assertEqual(still_readable["readable_text"], opened["readable_text"])
        self.assertIsNone(still_readable["audio_path"])
        original.rename(original.with_name("relocated-deferred.wav"))
        self.assertEqual(direct.direct_request(self.settings, {"action": "verify_source", "result_id": identity})["source_integrity"],
                         "missing_or_changed")

    def test_replacement_after_hash_does_not_return_a_playable_identity(self):
        original = self.source("hash-race.wav", 10)
        result = export.build_independent(self.settings, [original], self.resolved,
                                          input_modes=["referenced"])["results"][0]
        identity = result["result_id"]
        real_hash = direct.sha256_file
        replaced = False

        def replace_after_hash(path):
            nonlocal replaced
            digest = real_hash(path)
            if Path(path) == original and not replaced:
                replacement = original.with_name("same-bytes-new-inode.wav")
                replacement.write_bytes(original.read_bytes())
                os.replace(replacement, original)
                replaced = True
            return digest

        with patch.object(direct, "sha256_file", side_effect=replace_after_hash):
            checked = direct.verify_source(self.settings, identity)
        self.assertTrue(replaced)
        self.assertIsNone(checked["audio_path"])
        self.assertIsNone(checked["verified_source_stat"])
        self.assertEqual(checked["source_integrity"], "missing_or_changed")
        self.assertIn("Same repeated text", direct.read_result(
            self.settings, identity, defer_source_verification=True)["readable_text"])

    def test_legacy_source_defer_request_keeps_full_original_behavior_and_bwf_clock(self):
        original = self.source("legacy-deferred.wav", 10)
        raw = original.read_bytes()
        payload = bytearray(602)
        payload[320:330] = b"2024-02-29"
        payload[330:338] = b"23:59:59"
        chunk = b"bext" + struct.pack("<I", len(payload)) + payload
        raw = raw[:12] + chunk + raw[12:]
        original.write_bytes(raw[:4] + struct.pack("<I", len(raw) - 8) + raw[8:])
        self.report([original])
        legacy = next(row for row in direct.direct_request(self.settings, {"action": "list"})["results"]
                      if row["result_id"].startswith("legacy-source-"))
        identity = legacy["result_id"]
        default = direct.read_result(self.settings, identity)
        self.assertEqual(default["source_integrity"], "verified")
        self.assertEqual(default["provenance"]["recording"]["started_at"], "2024-02-29T23:59:59")
        managed = Path(default["audio_path"])
        deferred = direct.direct_request(self.settings, {"action": "read", "result_id": identity,
                                                         "defer_source_verification": True})
        self.assertEqual(deferred, default)
        self.assertEqual(direct.direct_request(self.settings, {"action": "verify_source", "result_id": identity})["audio_path"],
                         str(managed))
        report = next(row for row in direct.direct_request(self.settings, {"action": "list"})["results"]
                      if row["result_id"].startswith("legacy-report-"))
        self.assertIn("Same repeated text", direct.direct_request(self.settings, {"action": "read",
                          "result_id": report["result_id"], "defer_source_verification": True})["readable_text"])
        self.assertEqual(direct.direct_request(self.settings, {"action": "verify_source",
                         "result_id": report["result_id"]})["source_integrity"], "not_applicable")

    def test_locate_rejects_app_managed_copy_even_when_bytes_match(self):
        original = self.source("outside-data.wav", 10)
        result = export.build_independent(self.settings, [original], self.resolved,
                                          input_modes=["referenced"])["results"][0]
        identity = result["result_id"]
        inside = self.data / "matching-managed-copy.wav"
        inside.write_bytes(original.read_bytes())
        with self.assertRaisesRegex(ValueError, "inside application-managed data"):
            direct.direct_request(self.settings, {"action": "locate_source", "result_id": identity,
                                                  "path": str(inside)})
        self.assertEqual(direct.direct_request(self.settings, {"action": "verify_source", "result_id": identity})["audio_path"],
                         str(original))

    def test_locate_busy_session_releases_lifecycle_fence_and_can_retry(self):
        original = self.source("busy-reference.wav", 10)
        result = export.build_independent(self.settings, [original], self.resolved,
                                          input_modes=["referenced"])["results"][0]
        saved = storage.read_doc(Path(result["markdown_path"]).with_name("result.json"))
        session = storage.locate_session(self.data, saved["internal"]["session_id"])
        before = (session / "session.yaml").read_bytes()
        replacement = original.with_name("relocated-busy-reference.wav")
        original.rename(replacement)
        request = {"action": "locate_source", "result_id": result["result_id"],
                   "path": str(replacement)}
        finished = threading.Event()
        outcomes = []

        def locate():
            try:
                outcomes.append(direct.direct_request(self.settings, request))
            except Exception as error:
                outcomes.append(error)
            finally:
                finished.set()

        # A cached transcription owns this lock while it binds its lifecycle
        # scope. A relink must fail promptly and release the global fence, so
        # that cached work (and unrelated jobs) can continue.
        with storage.session_lock(session):
            worker = threading.Thread(target=locate, daemon=True)
            worker.start()
            returned_while_busy = finished.wait(3)
            if returned_while_busy:
                with lifecycle.lock(self.settings, blocking=False):
                    self.assertEqual((session / "session.yaml").read_bytes(), before)
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertTrue(returned_while_busy, "Relink blocked while holding the lifecycle fence")
        self.assertIsInstance(outcomes[0], RuntimeError)
        self.assertIn("retry after it finishes", str(outcomes[0]))
        located = direct.direct_request(self.settings, request)
        self.assertEqual(located["audio_path"], str(replacement))

    def test_modern_result_avoids_library_scan_and_mutations_avoid_source_hash(self):
        original = self.source("fast-modern.wav", 10)
        result = export.build_independent(self.settings, [original], self.resolved,
                                          input_modes=["referenced"])["results"][0]
        identity = result["result_id"]
        actual_hash = storage.sha256_file

        def reject_original_hash(path):
            if Path(path) == original:
                raise AssertionError("A label or export operation hashed the original audio")
            return actual_hash(path)

        with patch.object(direct, "_records", side_effect=AssertionError("Modern result scanned whole library")):
            opened = direct.direct_request(self.settings, {"action": "read", "result_id": identity,
                                                           "defer_source_verification": True})
            self.assertEqual(opened["source_integrity"], "pending")
            with patch.object(storage, "sha256_file", side_effect=reject_original_hash):
                labelled = direct.direct_request(self.settings, {"action": "labels", "result_id": identity,
                                                                 "user_labels": {"course": "Synthetic"}})
                destination = self.root / "fast-export.md"
                direct.direct_request(self.settings, {"action": "export", "result_id": identity,
                                                      "format": "md", "path": str(destination)})
            self.assertEqual(labelled["source_integrity"], "pending")
            self.assertIn("Synthetic", destination.read_text())
            self.assertEqual(direct.direct_request(self.settings, {"action": "verify_source",
                             "result_id": identity})["audio_path"], str(original))
            with patch.object(lifecycle, "visible", return_value=False):
                with self.assertRaises(FileNotFoundError):
                    direct.read_result(self.settings, identity, defer_source_verification=True)
                with self.assertRaises(FileNotFoundError):
                    direct.verify_source(self.settings, identity)

    def test_source_verification_message_preserves_run_review_status(self):
        original = self.source("review-message.wav", 10)
        result = export.build_independent(self.settings, [original], self.resolved,
                                          input_modes=["referenced"])["results"][0]
        identity = result["result_id"]
        saved = storage.read_doc(Path(result["markdown_path"]).with_name("result.json"))
        internal = saved["internal"]
        session = storage.locate_session(self.data, internal["session_id"])
        run_manifest = session / "transcript" / internal["run_id"] / "manifest.json"
        run_manifest.write_text("{}\n", encoding="utf-8")
        deferred = direct.read_result(self.settings, identity, defer_source_verification=True)
        self.assertEqual((deferred["run_integrity"], deferred["state"]),
                         ("unavailable_or_changed", "review_required"))
        verified = direct.verify_source(self.settings, identity)
        self.assertEqual(verified["source_integrity"], "verified")
        self.assertEqual(verified["quality_message"], "Saved transcript needs review.")

    def test_external_original_is_never_managed_or_deleted_and_missing_text_survives(self):
        original = self.source("referenced.wav", 10)
        before = original.read_bytes()
        result = export.build_independent(self.settings, [original], self.resolved,
                                          input_modes=["referenced"])["results"][0]
        saved = storage.read_doc(Path(result["markdown_path"]).with_name("result.json"))
        session_path = storage.locate_session(self.data, saved["internal"]["session_id"])
        source = storage.read_doc(session_path / "session.yaml")["sources"][0]
        self.assertEqual(source["ownership"], "external_referenced")
        self.assertNotIn("path", source)
        self.assertFalse((session_path / "source").exists())
        self.assertEqual(original.read_bytes(), before)
        opened = direct.read_result(self.settings, result["result_id"])
        self.assertEqual(opened["audio_path"], str(original))
        relocated = original.with_name("moved.wav")
        original.rename(relocated)
        missing = direct.read_result(self.settings, result["result_id"])
        self.assertIsNone(missing["audio_path"])
        self.assertIn("Same repeated text", missing["plain_text"])
        self.assertEqual(missing["state"], opened["state"])
        self.assertEqual(set(missing["available_formats"]), set(opened["available_formats"]))
        with self.assertRaisesRegex(ValueError, "differ"):
            direct.direct_request(self.settings, {"action": "locate_source", "result_id": result["result_id"],
                                                   "path": str(self.source("wrong.wav", 20))})
        located = direct.direct_request(self.settings, {"action": "locate_source", "result_id": result["result_id"],
                                                        "path": str(relocated)})
        self.assertEqual(located["audio_path"], str(relocated))
        plan = direct.direct_request(self.settings, {"action": "delete_plan", "result_ids": [result["result_id"]]})
        self.assertFalse(any(target.get("path", "").startswith(str(relocated))
                             for target in plan.get("targets", [])))
        answer = direct.direct_request(self.settings, {"action": "delete_commit", "selection": plan["selection"],
                                                       "plan_token": plan["plan_token"], "operation_id": str(uuid.uuid4()),
                                                       "confirmed": True, "stop_selected": True})
        self.assertEqual(answer["state"], "completed")
        self.assertEqual(relocated.read_bytes(), before)
        self.assertIn(__import__("audio_transcribe.watch", fromlist=["version_key"]).version_key(relocated, source["sha256"]),
                      lifecycle.state(self.settings)["ignored_source_versions"])

    def test_discover_stability_version_change_and_explicit_readd(self):
        from audio_transcribe import watch
        original = self.source("new.wav", 10)
        request = {"action": "discover", "folder": str(original.parent), "paths": [str(original)]}
        first = direct.direct_request(self.settings, request)["items"][0]
        self.assertEqual(first["state"], "copying")
        with patch.object(watch, "_STABLE_SECONDS", 0):
            ready = direct.direct_request(self.settings, request)["items"][0]
        self.assertEqual(ready["state"], "ready")
        self.assertEqual(ready["source_sha256"], storage.sha256_file(original))
        candidate = direct.direct_request(self.settings, request)["items"][0]
        self.assertEqual(candidate["state"], "ready")
        self.assertEqual(candidate["verification"], "metadata_candidate")
        self.assertNotIn("source_sha256", candidate)
        direct.direct_request(self.settings, {"action": "watch_ignore", "path": str(original),
                                              "version_key": ready["version_key"]})
        self.assertEqual(direct.direct_request(self.settings, request)["items"][0]["state"], "ignored")
        direct.direct_request(self.settings, {"action": "watch_readd", "path": str(original),
                                              "version_key": ready["version_key"]})
        self.assertEqual(direct.direct_request(self.settings, request)["items"][0]["state"], "ready")
        changed = self.source("new.wav", 20)
        self.assertEqual(direct.direct_request(self.settings, request)["items"][0]["state"], "copying")
        with patch.object(watch, "_STABLE_SECONDS", 0):
            new = direct.direct_request(self.settings, request)["items"][0]
        self.assertEqual(new["state"], "ready")
        self.assertNotEqual(new["version_key"], ready["version_key"])

    def test_cached_source_reconciles_new_result_across_native_queue_identities(self):
        from audio_transcribe import watch
        original = self.source("late-result.wav", 10)
        alternate = self.root / "other folder" / original.name
        alternate.parent.mkdir()
        alternate.write_bytes(original.read_bytes())
        request = {"action": "discover", "folder": str(original.parent), "paths": [str(original)]}
        direct.direct_request(self.settings, request)
        with patch.object(watch, "_STABLE_SECONDS", 0):
            ready = direct.direct_request(self.settings, request)["items"][0]
        self.assertEqual(ready["state"], "ready")
        self.assertIsNone(storage.read_doc(watch._root(self.settings) / "discovery.json")["files"][str(original)]["result_id"])

        # The same filename and bytes at a different external path are not a
        # result for the watched original.
        export.build_independent(self.settings, [alternate], self.resolved,
                                 input_modes=["referenced"])
        with patch.object(watch, "sha256_file", side_effect=AssertionError("original audio must not be rehashed")):
            self.assertEqual(direct.direct_request(self.settings, request)["items"][0]["state"], "ready")

        result = export.build_independent(self.settings, [original], self.resolved,
                                          input_modes=["referenced"])["results"][0]
        # A fresh native bundle ID has no queue rows, but it shares this data
        # root. Discovery must link its unchanged cached source to the result.
        with patch.object(watch, "sha256_file", side_effect=AssertionError("original audio must not be rehashed")):
            candidate = direct.direct_request(self.settings, request)["items"][0]
        self.assertEqual((candidate["state"], candidate["result_id"]),
                         ("processed_candidate", result["result_id"]))
        self.assertEqual(storage.read_doc(watch._root(self.settings) / "discovery.json")["files"][str(original)]["result_id"],
                         result["result_id"])

        with patch.object(direct, "_records", side_effect=AssertionError("cached modern ID must avoid a library scan")):
            cached = direct.direct_request(self.settings, request)["items"][0]
        self.assertEqual((cached["state"], cached["result_id"]),
                         ("processed_candidate", result["result_id"]))

        direct.direct_request(self.settings, {"action": "watch_ignore", "path": str(original),
                                                  "version_key": ready["version_key"]})
        ignored = direct.direct_request(self.settings, request)["items"][0]
        self.assertEqual((ignored["state"], ignored["result_id"]), ("ignored", None))
        self.assertIsNone(storage.read_doc(watch._root(self.settings) / "discovery.json")["files"][str(original)]["result_id"])
        direct.direct_request(self.settings, {"action": "watch_readd", "path": str(original),
                                                  "version_key": ready["version_key"]})
        with patch.object(watch, "_STABLE_SECONDS", 0):
            readded = direct.direct_request(self.settings, request)["items"][0]
        self.assertEqual((readded["state"], readded["result_id"]),
                         ("processed", result["result_id"]))

        # If the result vanishes from the visible library, do not hide a
        # possibly new recording behind the now-stale cached result ID.
        with patch.object(direct, "_record_for_identity", return_value=None), patch.object(direct, "_records", return_value={}):
            stale = direct.direct_request(self.settings, request)["items"][0]
        self.assertEqual((stale["state"], stale["result_id"]), ("ready", None))

    def test_readd_deleted_result_revalidates_stale_processed_cache(self):
        from audio_transcribe import watch
        original = self.source("deleted-readd.wav", 10)
        result = export.build_independent(self.settings, [original], self.resolved,
                                          input_modes=["referenced"])["results"][0]
        request = {"action": "discover", "folder": str(original.parent), "paths": [str(original)]}
        direct.direct_request(self.settings, request)
        with patch.object(watch, "_STABLE_SECONDS", 0):
            processed = direct.direct_request(self.settings, request)["items"][0]
        self.assertEqual((processed["state"], processed["result_id"]),
                         ("processed", result["result_id"]))

        plan = direct.direct_request(self.settings, {"action": "delete_plan", "result_ids": [result["result_id"]]})
        deleted = direct.direct_request(self.settings, {"action": "delete_commit", "selection": plan["selection"],
                                                       "plan_token": plan["plan_token"], "operation_id": str(uuid.uuid4()),
                                                       "confirmed": True, "stop_selected": True})
        self.assertEqual(deleted["state"], "completed")
        self.assertTrue(original.is_file())
        direct.direct_request(self.settings, {"action": "watch_ignore", "path": str(original),
                                                  "version_key": processed["version_key"]})
        self.assertEqual(direct.direct_request(self.settings, request)["items"][0]["state"], "ignored")

        direct.direct_request(self.settings, {"action": "watch_readd", "path": str(original),
                                                  "version_key": processed["version_key"]})
        with patch.object(watch, "_STABLE_SECONDS", 0):
            readded = direct.direct_request(self.settings, request)["items"][0]
        self.assertEqual(readded["state"], "ready")
        self.assertEqual(readded["version_key"], processed["version_key"])
        self.assertIsNone(readded["result_id"])

    def test_readd_keeps_valid_historical_result_processed(self):
        from audio_transcribe import watch
        original = self.source("retained-readd.wav", 10)
        result = export.build_independent(self.settings, [original], self.resolved,
                                          input_modes=["referenced"])["results"][0]
        request = {"action": "discover", "folder": str(original.parent), "paths": [str(original)]}
        direct.direct_request(self.settings, request)
        with patch.object(watch, "_STABLE_SECONDS", 0):
            processed = direct.direct_request(self.settings, request)["items"][0]
        self.assertEqual(processed["state"], "processed")
        direct.direct_request(self.settings, {"action": "watch_ignore", "path": str(original),
                                                  "version_key": processed["version_key"]})
        direct.direct_request(self.settings, {"action": "watch_readd", "path": str(original),
                                                  "version_key": processed["version_key"]})
        with patch.object(watch, "_STABLE_SECONDS", 0):
            reconciled = direct.direct_request(self.settings, request)["items"][0]
        self.assertEqual((reconciled["state"], reconciled["result_id"]),
                         ("processed", result["result_id"]))

    def test_stable_malformed_wav_fails_after_grace_without_blocking_ready_neighbor(self):
        from audio_transcribe import watch
        malformed = self.source("malformed.wav", 10)
        malformed.write_bytes(b"RIFF")
        neighbor = self.source("ready-neighbor.wav", 20)
        request = {"action": "discover", "folder": str(malformed.parent),
                   "paths": [str(malformed), str(neighbor)]}
        first = direct.direct_request(self.settings, request)["items"]
        self.assertEqual([item["state"] for item in first], ["copying", "copying"])
        cache_path = self.data / "watch" / "discovery.json"
        cache = storage.read_doc(cache_path)
        for path in (malformed, neighbor):
            cache["files"][str(path)]["first_seen"] -= watch._INVALID_GRACE_SECONDS + 1
        storage.write_json(cache_path, cache, overwrite=True)
        states = {item["filename"]: item["state"]
                  for item in direct.direct_request(self.settings, request)["items"]}
        self.assertEqual(states, {"malformed.wav": "failed", "ready-neighbor.wav": "ready"})

    def test_equal_bytes_at_two_paths_keep_distinct_referenced_sources(self):
        first = self.source("first.wav", 10)
        second = first.with_name("second.wav")
        second.write_bytes(first.read_bytes())
        result = export.build_independent(self.settings, [first, second], self.resolved,
                                          input_modes=["referenced", "referenced"])
        self.assertEqual(result["completed"], 2)
        rows = result["results"]
        self.assertNotEqual(rows[0]["result_id"], rows[1]["result_id"])
        self.assertEqual(direct.read_result(self.settings, rows[0]["result_id"])["audio_path"], str(first))
        self.assertEqual(direct.read_result(self.settings, rows[1]["result_id"])["audio_path"], str(second))
        refs = [storage.read_doc(Path(row["markdown_path"]).with_name("result.json"))["internal"]
                for row in rows]
        self.assertNotEqual(refs[0]["session_id"], refs[1]["session_id"])

    def test_discovery_does_not_mark_different_filename_same_bytes_processed(self):
        from audio_transcribe import watch
        original = self.source("historical.wav", 10)
        export.build_independent(self.settings, [original], self.resolved)
        alias = original.with_name("new-independent.wav")
        alias.write_bytes(original.read_bytes())
        request = {"action": "discover", "folder": str(original.parent),
                   "paths": [str(original), str(alias)]}
        direct.direct_request(self.settings, request)
        with patch.object(watch, "_STABLE_SECONDS", 0):
            result = direct.direct_request(self.settings, request)
        states = {row["filename"]: row["state"] for row in result["items"]}
        self.assertEqual(states, {"historical.wav": "processed", "new-independent.wav": "ready"})

    def test_source_changed_after_decode_cannot_publish_old_hash_result(self):
        original = self.source("changing.wav", 10)
        normal_decode = self.decode
        def change_source(*args, **kwargs):
            before = original.read_bytes()
            original.write_bytes(before[:-2] + b"\x01\x02")
            return normal_decode(*args, **kwargs)
        with patch.object(engine, "decode", side_effect=change_source):
            result = export.build_independent(self.settings, [original], self.resolved,
                                              input_modes=["referenced"])
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["completed"], 0)
        self.assertEqual(direct.direct_request(self.settings, {"action": "list"})["results"], [])

    def test_watched_job_is_bound_to_discovered_bytes_and_new_version_can_start(self):
        from audio_transcribe import watch
        original = self.source("selected-version.wav", 10)
        request = {"action": "discover", "folder": str(original.parent), "paths": [str(original)]}
        direct.direct_request(self.settings, request)
        with patch.object(watch, "_STABLE_SECONDS", 0):
            old = direct.direct_request(self.settings, request)["items"][0]
        self.assertEqual(old["state"], "ready")
        before = storage.source_stat(original)
        time.sleep(0.002)
        with original.open("r+b") as handle:
            handle.seek(-1, os.SEEK_END)
            byte = handle.read(1)
            handle.seek(-1, os.SEEK_END)
            handle.write(bytes([byte[0] ^ 1]))
        os.utime(original, ns=(original.stat().st_atime_ns, before["mtime_ns"]))
        after = storage.source_stat(original)
        self.assertEqual({key: after[key] for key in ("device", "inode", "size_bytes", "mtime_ns")},
                         {key: before[key] for key in ("device", "inode", "size_bytes", "mtime_ns")})
        self.assertNotEqual(after["ctime_ns"], before["ctime_ns"])
        self.assertEqual(direct.direct_request(self.settings, request)["items"][0]["state"], "copying")
        with patch.object(watch, "_STABLE_SECONDS", 0):
            fresh = direct.direct_request(self.settings, request)["items"][0]
        self.assertEqual(fresh["state"], "ready")
        self.assertNotEqual(fresh["version_key"], old["version_key"])
        stale = export.build_independent(self.settings, [original], self.resolved,
                                         input_modes=["referenced"],
                                         expected_version_keys=[old["version_key"]])
        self.assertEqual((stale["failed"], stale["completed"]), (1, 0))
        self.assertEqual(direct.direct_request(self.settings, {"action": "list"})["results"], [])
        self.assertEqual(direct.direct_request(self.settings, request)["items"][0]["version_key"], fresh["version_key"])
        current = export.build_independent(self.settings, [original], self.resolved,
                                           input_modes=["referenced"],
                                           expected_version_keys=[fresh["version_key"]])
        self.assertEqual((current["failed"], current["completed"]), (0, 1))

    def test_legacy_reference_and_discovery_cache_rehash_without_ctime(self):
        from audio_transcribe import watch
        original = self.source("legacy-ctime.wav", 10)
        request = {"action": "discover", "folder": str(original.parent), "paths": [str(original)]}
        direct.direct_request(self.settings, request)
        with patch.object(watch, "_STABLE_SECONDS", 0):
            ready = direct.direct_request(self.settings, request)["items"][0]
        cache_path = self.data / "watch" / "discovery.json"
        cache = storage.read_doc(cache_path)
        cache["files"][str(original)]["stat"].pop("ctime_ns")
        storage.write_json(cache_path, cache, overwrite=True)
        self.assertEqual(direct.direct_request(self.settings, request)["items"][0]["state"], "copying")
        with patch.object(watch, "_STABLE_SECONDS", 0):
            reverified = direct.direct_request(self.settings, request)["items"][0]
        self.assertEqual((reverified["state"], reverified["version_key"]), ("ready", ready["version_key"]))
        session_path, session, _ = storage.import_sources(self.data, [original], ownership="external_referenced")
        source = session["sources"][0]
        source["external_identity"].pop("ctime_ns")
        session["sources"][0] = source
        storage.write_yaml(session_path / "session.yaml", session, overwrite=True)
        self.assertEqual(storage.resolve_source_path(session_path, source), original)
        before = original.stat()
        with original.open("r+b") as handle:
            handle.seek(-1, os.SEEK_END)
            byte = handle.read(1)
            handle.seek(-1, os.SEEK_END)
            handle.write(bytes([byte[0] ^ 1]))
        os.utime(original, ns=(before.st_atime_ns, before.st_mtime_ns))
        with self.assertRaisesRegex(ValueError, "bytes changed"):
            storage.resolve_source_path(session_path, source)


if __name__ == "__main__":
    unittest.main()
