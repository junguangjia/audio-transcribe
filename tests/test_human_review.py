"""Synthetic human attestations only; these tests establish no real ASR accuracy."""
import copy
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from audio_transcribe.human_review import (NORMALIZATION, ReviewValidationError, load_json,
                                         row_snapshot, score_review, text_sha256, tokens)

STAMP = "2026-09-26T23:00:00+00:00"


def package(baseline="alpha beta gamma", comparison="alpha delta gamma"):
    def candidate(text):
        return {"label": "Synthetic ASR", "state": "completed", "segments": [
            {"start_seconds": 0, "end_seconds": 1, "source_start_seconds": 2,
             "source_end_seconds": 3, "text": text}]}
    return {"schema_version": 1, "benchmark_id": "synthetic-human-review",
            "source": {"sha256": "a" * 64, "sample_rate": 48000, "complete_frames": 480000,
                       "duration_seconds": 10.0, "filename": "synthetic.wav", "session_id": "s-synthetic"},
            "environment": {"fixture": True},
            "candidates": {"baseline": {"model": "synthetic-baseline"}, "comparison": {"model": "synthetic-comparison"}},
            "normalization": copy.deepcopy(NORMALIZATION),
            "clips": [{"clip_id": "clip-a", "source_start_seconds": 2.0, "source_end_seconds": 4.0,
                       "start_frame": 96000, "end_frame": 192000, "sample_rate": 48000,
                       "audio_path": "clips/clip-a.wav", "original_audio_path": "clips/clip-a-original.wav",
                       "baseline": candidate(baseline), "comparison": candidate(comparison),
                       "reference": {"human_text": "", "status": "not_verified", "created_at": STAMP, "updated_at": STAMP},
                       "preference": None, "tags": [], "critical_content": []}],
            "accuracy": {"WER": None, "technical_term_accuracy": None, "status": "awaiting_human_review"}}


def verify(clip, text):
    clip["reference"] = {"human_text": text, "status": "verified", "human_confirmed": True,
                         "created_at": STAMP, "updated_at": STAMP, "verified_at": STAMP,
                         "verified_text_sha256": text_sha256(text)}


def term(clip, identity="term-1", text="beta", baseline="correct", comparison="incorrect", category="technicalterm"):
    row = {"id": identity, "category": category, "reference_text": text, "source_seconds": 2.5,
           "baseline": baseline, "comparison": comparison, "notes": "Synthetic human judgment",
           "status": "verified", "human_confirmed": True,
           "verified_reference_sha256": clip["reference"]["verified_text_sha256"]}
    row["verified_row_snapshot"] = row_snapshot(row)
    return row


class HumanReviewTests(unittest.TestCase):
    def test_initial_and_asr_copied_draft_have_no_metrics(self):
        expected = package()
        for status in ("not_verified", "draft"):
            with self.subTest(status=status):
                review = copy.deepcopy(expected)
                review["clips"][0]["reference"].update(human_text="alpha beta gamma", status=status)
                review["accuracy"] = {"WER": 0, "technical_term_accuracy": 1, "status": "verified"}
                result = score_review(expected, review)
                self.assertEqual(result["status"], "awaiting_human_review")
                for candidate in ("baseline", "comparison"):
                    self.assertTrue(all(result["clips"][0]["candidates"][candidate][key] is None for key in ("WER", "S", "D", "I", "N")))
                    self.assertIsNone(result["aggregate"][candidate]["WER"])
                    self.assertIsNone(result["critical_content"][candidate]["technical_term_accuracy"])

    def test_known_substitution_deletion_and_insertion_counts(self):
        cases = [("alpha delta gamma", (1, 0, 0)), ("alpha gamma", (0, 1, 0)),
                 ("alpha beta gamma extra", (0, 0, 1)), ("", (0, 3, 0)),
                 ("alpha delta gamma extra", (1, 0, 1))]
        for hypothesis, counts in cases:
            with self.subTest(hypothesis=hypothesis):
                expected = package(baseline=hypothesis)
                review = copy.deepcopy(expected)
                verify(review["clips"][0], "alpha beta gamma")
                metric = score_review(expected, review)["clips"][0]["candidates"]["baseline"]
                self.assertEqual(tuple(metric[key] for key in ("S", "D", "I")), counts)
                self.assertEqual(metric["N"], 3)
                self.assertEqual(metric["WER"], sum(counts) / 3)

    def test_tokenizer_preserves_negation_numbers_names_and_contractions(self):
        self.assertEqual(tokens("O’Neill can’t reject H1N1 at −2.50%; Neyman, not Newman."),
                         ["o'neill", "can't", "reject", "h1n1", "at", "-", "2.50", "%", "neyman", "not", "newman"])
        self.assertEqual(tokens("1,000 1000 2e-3 ¬p ∧ q ≠ 0 $5"),
                         ["1,000", "1000", "2e-3", "¬", "p", "∧", "q", "≠", "0", "$", "5"])
        cases = [("It is not independent.", "It is independent.", 1),
                 ("It can't increase.", "It can increase.", 1),
                 ("Use 2.0 values.", "Use 20 values.", 1),
                 ("Neyman", "Newman", 1), ("HELLO, World!", "hello world", 0)]
        for reference, hypothesis, errors in cases:
            with self.subTest(reference=reference):
                expected = package(baseline=hypothesis)
                review = copy.deepcopy(expected)
                verify(review["clips"][0], reference)
                score = score_review(expected, review)["aggregate"]["baseline"]
                self.assertEqual(score["S"] + score["D"] + score["I"], errors)

    def test_verified_reference_needs_attestation_exact_text_hash_and_nonempty_text(self):
        expected = package()
        for mutation in (lambda ref: ref.update(human_confirmed=False),
                         lambda ref: ref.update(human_text="changed after verification"),
                         lambda ref: ref.pop("verified_text_sha256"),
                         lambda ref: ref.update(verified_at="yesterday"),
                         lambda ref: ref.update(created_at=None)):
            review = copy.deepcopy(expected)
            verify(review["clips"][0], "alpha beta gamma")
            mutation(review["clips"][0]["reference"])
            with self.assertRaises(ReviewValidationError):
                score_review(expected, review)
        for text in ("", "   ", ".,!"):
            review = copy.deepcopy(expected)
            verify(review["clips"][0], text)
            with self.assertRaises(ReviewValidationError):
                score_review(expected, review)

    def test_unresolved_listening_annotations_cannot_become_gold_words(self):
        expected = package()
        for marker in ("[inaudible]", "(unclear word)", "[UNINTELLIGIBLE]", "【听不清】",
                       "（无法辨认）", "[无法听清]", "(不清楚)", "???"):
            with self.subTest(marker=marker):
                review = copy.deepcopy(expected)
                clip = review["clips"][0]
                clip["reference"].update(human_text="alpha " + marker, status="draft")
                self.assertIsNone(score_review(expected, review)["aggregate"]["baseline"]["WER"])
                verify(clip, "alpha " + marker)
                with self.assertRaisesRegex(ReviewValidationError, "Unresolved listening annotations"):
                    score_review(expected, review)
        review = copy.deepcopy(expected)
        verify(review["clips"][0], "The unclear explanation was unintelligible.")
        self.assertEqual(score_review(expected, review)["aggregate"]["baseline"]["N"], 5)

    def test_provenance_or_imported_hypothesis_change_is_rejected(self):
        expected = package()
        mutations = [lambda doc: doc.update(benchmark_id="different"),
                     lambda doc: doc["source"].update(sha256="b" * 64),
                     lambda doc: doc["source"].update(complete_frames=480001),
                     lambda doc: doc["clips"][0].update(clip_id="different"),
                     lambda doc: doc["clips"][0].update(start_frame=96001),
                     lambda doc: doc["clips"][0].update(source_start_seconds=2.001),
                     lambda doc: doc["clips"][0].update(audio_path="other.wav"),
                     lambda doc: doc["clips"][0]["baseline"]["segments"][0].update(text="forged hypothesis"),
                     lambda doc: doc["clips"][0]["baseline"]["segments"][0].update(source_end_seconds=3.1),
                     lambda doc: doc["candidates"]["baseline"].update(model="different"),
                     lambda doc: doc["normalization"].update(version="ignore-negative-words"),
                     lambda doc: doc["environment"].update(fixture=False)]
        for index, mutation in enumerate(mutations):
            with self.subTest(index=index):
                review = copy.deepcopy(expected)
                verify(review["clips"][0], "alpha beta gamma")
                mutation(review)
                with self.assertRaises(ReviewValidationError):
                    score_review(expected, review)

    def test_internally_inconsistent_package_mapping_is_rejected(self):
        for mutation in (lambda doc: doc["source"].update(duration_seconds=10.01),
                         lambda doc: doc["clips"][0].update(source_start_seconds=1.99),
                         lambda doc: doc["clips"][0].update(sample_rate=16000),
                         lambda doc: doc["clips"][0].update(start_frame=True),
                         lambda doc: doc["clips"][0]["baseline"]["segments"][0].update(source_start_seconds=2.1)):
            expected = package()
            mutation(expected)
            with self.assertRaises(ReviewValidationError):
                score_review(expected, copy.deepcopy(expected))

    def test_raw_invalid_asr_times_remain_raw_and_are_not_silently_corrected(self):
        expected = package()
        segment = expected["clips"][0]["baseline"]["segments"][0]
        segment.update(start_seconds=-.02, source_start_seconds=1.98)
        before = copy.deepcopy(expected)
        result = score_review(expected, copy.deepcopy(expected))
        self.assertEqual(result["status"], "awaiting_human_review")
        self.assertEqual(expected, before)

    def test_technical_term_denominator_uses_verified_instances_only(self):
        expected = package()
        review = copy.deepcopy(expected)
        clip = review["clips"][0]
        verify(clip, "alpha beta gamma")
        clip["critical_content"] = [term(clip), {"id": "draft-term", "category": "technicalterm",
            "reference_text": "not human verified", "status": "pending", "baseline": "correct", "comparison": "correct"},
            term(clip, identity="semantic", text="dependent versus independent", category="negation")]
        result = score_review(expected, review)
        baseline, comparison = (result["critical_content"][key] for key in ("baseline", "comparison"))
        self.assertEqual(baseline["verified_term_instances"], 1)
        self.assertEqual(baseline["unverified_term_rows"], 1)
        self.assertEqual(baseline["technical_term_accuracy"], 1)
        self.assertEqual(comparison["technical_term_accuracy"], 0)
        self.assertEqual(comparison["critical_content_counts"]["negation"]["incorrect"], 1)

    def test_unclear_verified_term_judgment_does_not_become_percentage(self):
        expected = package()
        review = copy.deepcopy(expected)
        clip = review["clips"][0]
        verify(clip, "alpha beta gamma")
        clip["critical_content"] = [term(clip, comparison="unclear")]
        result = score_review(expected, review)["critical_content"]
        self.assertEqual(result["baseline"]["technical_term_accuracy"], 1)
        self.assertIsNone(result["comparison"]["technical_term_accuracy"])
        self.assertEqual(result["comparison"]["status"], "incomplete_judgments")

    def test_critical_attestation_detects_changed_reference_and_row(self):
        expected = package()
        for mutation in (lambda clip: clip["reference"].update(status="draft"),
                         lambda clip: verify(clip, "alpha beta gamma extra"),
                         lambda clip: clip["critical_content"][0].update(comparison="correct"),
                         lambda clip: clip["critical_content"][0].update(source_seconds=2.7),
                         lambda clip: clip["critical_content"][0].update(notes="changed after verification"),
                         lambda clip: clip["critical_content"][0].update(human_confirmed=False)):
            review = copy.deepcopy(expected)
            clip = review["clips"][0]
            verify(clip, "alpha beta gamma")
            clip["critical_content"] = [term(clip)]
            mutation(clip)
            with self.assertRaises(ReviewValidationError):
                score_review(expected, review)

    def test_verified_term_must_occur_in_reference_and_not_duplicate_instance_count(self):
        expected = package()
        review = copy.deepcopy(expected)
        clip = review["clips"][0]
        verify(clip, "alpha beta gamma")
        for rows in ([term(clip, text="unspoken")], [term(clip), term(clip, identity="duplicate")]):
            clip["critical_content"] = rows
            with self.assertRaises(ReviewValidationError):
                score_review(expected, review)
        verify(clip, "alpha beta gamma beta")
        clip["critical_content"] = [term(clip), term(clip, identity="second-instance")]
        self.assertEqual(score_review(expected, review)["critical_content"]["baseline"]["verified_term_instances"], 2)

    def test_aggregate_is_word_weighted_verified_subset_not_average_or_full_accuracy(self):
        expected = package(baseline="alpha delta gamma")
        second = copy.deepcopy(expected["clips"][0])
        second.update(clip_id="clip-b")
        second["baseline"]["segments"][0]["text"] = "alpha"
        expected["clips"].append(second)
        expected["clips"].append({**copy.deepcopy(second), "clip_id": "clip-c"})
        review = copy.deepcopy(expected)
        verify(review["clips"][0], "alpha beta gamma")
        verify(review["clips"][1], "alpha")
        result = score_review(expected, review)
        self.assertEqual(result["status"], "partially_verified")
        score = result["aggregate"]["baseline"]
        self.assertEqual(score["WER"], 1 / 4)
        self.assertEqual((score["N"], score["verified_clips"], score["total_clips"]), (4, 2, 3))

    def test_failed_candidate_is_unavailable_not_a_fake_accuracy_measurement(self):
        expected = package()
        expected["clips"][0]["comparison"].update(state="failed", segments=[])
        review = copy.deepcopy(expected)
        verify(review["clips"][0], "alpha beta gamma")
        review["clips"][0]["critical_content"] = [term(review["clips"][0])]
        result = score_review(expected, review)
        self.assertIsNone(result["critical_content"]["comparison"]["technical_term_accuracy"])
        self.assertEqual(result["critical_content"]["comparison"]["status"], "candidate_unavailable")
        self.assertIsNone(result["clips"][0]["candidates"]["comparison"]["WER"])
        self.assertEqual(result["clips"][0]["candidates"]["comparison"]["undefined_reason"], "candidate_unavailable")
        self.assertIsNone(result["aggregate"]["comparison"]["WER"])
        self.assertEqual(result["aggregate"]["baseline"]["WER"], 0)

    def test_optional_comparison_absence_keeps_baseline_review_usable(self):
        expected = package()
        expected["candidates"].pop("comparison")
        expected["clips"][0].pop("comparison")
        review = copy.deepcopy(expected)
        verify(review["clips"][0], "alpha beta gamma")
        result = score_review(expected, review)
        self.assertEqual(set(result["aggregate"]), {"baseline"})
        self.assertEqual(result["aggregate"]["baseline"]["WER"], 0)

    def test_cli_preserves_inputs_and_refuses_existing_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            expected, reviewed, output = (root / name for name in ("expected.json", "review.json", "score.json"))
            data = package()
            expected.write_text(json.dumps(data))
            verify(data["clips"][0], "alpha beta gamma")
            reviewed.write_text(json.dumps(data))
            before = (expected.read_bytes(), reviewed.read_bytes())
            script = Path(__file__).resolve().parents[1] / "scripts/score-human-review.py"
            command = [sys.executable, str(script), "--expected", str(expected), "--review", str(reviewed), "--output", str(output)]
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(load_json(output)["aggregate"]["baseline"]["WER"], 0)
            saved = output.read_bytes()
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(saved, output.read_bytes())
            self.assertEqual(before, (expected.read_bytes(), reviewed.read_bytes()))
            self.assertNotIn("alpha beta gamma", result.stdout + result.stderr)

    def test_cli_hashes_the_exact_scored_snapshots_and_preserves_racing_output(self):
        script = Path(__file__).resolve().parents[1] / "scripts/score-human-review.py"
        spec = importlib.util.spec_from_file_location("human_review_cli_fixture", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            expected, reviewed, output = (root / name for name in ("expected.json", "review.json", "score.json"))
            data = package()
            expected.write_text(json.dumps(data))
            expected_bytes = expected.read_bytes()
            verify(data["clips"][0], "alpha beta gamma")
            reviewed.write_text(json.dumps(data))
            command = [str(script), "--expected", str(expected), "--review", str(reviewed), "--output", str(output)]
            read_bytes = Path.read_bytes
            def concurrently_changed(path):
                snapshot = read_bytes(path)
                if path == expected:
                    changed = json.loads(snapshot)
                    changed["source"]["sha256"] = "b" * 64
                    path.write_text(json.dumps(changed))
                return snapshot
            with patch.object(sys, "argv", command), patch.object(Path, "read_bytes", concurrently_changed), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(module.main(), 0)
            result = load_json(output)
            self.assertEqual(result["source_sha256"], "a" * 64)
            self.assertEqual(result["input_files"]["trusted_package"]["sha256"], hashlib.sha256(expected_bytes).hexdigest())
            self.assertNotEqual(expected.read_bytes(), expected_bytes)
            expected.write_bytes(expected_bytes)
            collision = root / "concurrent.json"
            command[-1] = str(collision)
            def racing_writer(*args):
                collision.write_text("Concurrent owner's result")
                return score_review(*args)
            with patch.object(sys, "argv", command), patch.object(module, "score_review", side_effect=racing_writer), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(module.main(), 2)
            self.assertEqual(collision.read_text(), "Concurrent owner's result")

    def test_json_duplicate_keys_and_nonfinite_values_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "review.json"
            for content in ('{"a": 1, "a": 2}', '{"a": NaN}', '{"a": Infinity}'):
                path.write_text(content)
                with self.assertRaises(ReviewValidationError):
                    load_json(path)


if __name__ == "__main__":
    unittest.main()
