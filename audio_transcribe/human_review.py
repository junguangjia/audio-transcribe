"""Additive, offline evaluation of explicitly verified human listening references.

The trusted package supplies all hypotheses and identities. Imported scores are
ignored; draft/model-copied text never supplies ground truth. Nothing here edits
ASR, cache, application defaults, or the original review package.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
import math
import re
import unicodedata

NORMALIZATION = {
    "version": "human-review-nfc-casefold-token-v1",
    "rules": "Unicode NFC and casefold; curly apostrophes become ASCII apostrophes; Unicode minus becomes ASCII minus. Keep contractions, Unicode letters/names, digit strings with decimal/group separators and scientific notation, and mathematical operators/currency symbols. Ignore other punctuation and whitespace. Do not expand contractions, remove negation, rewrite numbers, stem words, substitute terms/names, or infer inaudible speech.",
    "edit_tie_order": ["substitution", "deletion", "insertion"],
    "number_policy": "Numeric forms remain literal: 1,000 and 1000, 2 and two, and 2.0 and 2 are distinct. Signs and mathematical operators are separate scored tokens.",
}
CATEGORIES = ("technicalterm", "propernoun", "number", "negation", "logicalcondition",
              "math", "sentenceomission", "hallucination")
JUDGMENTS = {"correct", "incorrect", "unclear", "pending"}
PREFERENCES = {None, "baseline", "comparison", "same", "unclear"}
TAGS = {"technical_term_error", "missing_word", "inserted_hallucinated_word",
        "negation_condition_error", "number_error", "timestamp_problem", "unintelligible_source_audio"}
CANDIDATES = ("baseline", "comparison")
_MUTABLE_CLIP = {"reference", "preference", "tags", "critical_content"}
_APOSTROPHES = str.maketrans({"\u2018": "'", "\u2019": "'", "\u02bc": "'", "\u2212": "-"})
# Longest word-like match comes before the number pattern when it starts with
# a letter, preserving names such as H1N1 and contractions such as O'Neill's.
_TOKEN = re.compile(r"[^\W\d_][^\W_]*(?:'[^\W_]+)*|\d+(?:[.,]\d+)*(?:e[+-]?\d+)?|[^\W_]+(?:'[^\W_]+)*|[+\-*/=<>≤≥≠±×÷%]")
_SHA256 = re.compile(r"[a-f0-9]{64}\Z")
# Unresolved listening annotations are drafts, never lexical ground truth.
# Bare spoken words such as "unclear" remain valid; do not guess their meaning.
_UNCERTAINTY = re.compile(
    r"\?{3,}|[\[(（【][^\])）】\r\n]*(?:\b(?:inaudible|unclear|unintelligible)\b|听不清|无法听清|无法辨认|不清楚)[^\])）】\r\n]*[\])）】]",
    re.IGNORECASE | re.ASCII,
)


class ReviewValidationError(ValueError):
    """A review or its claimed human verification does not match its package."""


def text_sha256(text):
    if not isinstance(text, str):
        raise ReviewValidationError("Human text must be a string.")
    try:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()
    except UnicodeEncodeError:
        raise ReviewValidationError("Human text contains invalid Unicode.") from None


def tokens(text):
    if not isinstance(text, str):
        raise ReviewValidationError("Transcript text must be a string.")
    normalized = unicodedata.normalize("NFC", text).casefold().translate(_APOSTROPHES)
    result, cursor = [], 0
    for match in _TOKEN.finditer(normalized):
        result.extend(char for char in normalized[cursor:match.start()] if unicodedata.category(char) in {"Sm", "Sc"})
        result.append(match.group())
        cursor = match.end()
    result.extend(char for char in normalized[cursor:] if unicodedata.category(char) in {"Sm", "Sc"})
    return result


def _unmeasured(status):
    return {"reference_status": status, "WER": None, "S": None, "D": None, "I": None, "N": None}


def _verified_wer(reference, hypothesis):
    ref, hyp = tokens(reference), tokens(hypothesis)
    if not ref:
        raise ReviewValidationError("A verified reference must contain scored words, numbers, or mathematical symbols.")
    # Rolling rows keep memory bounded. Stable min order fixes equal-cost ties.
    previous = [(j, 0, 0, j) for j in range(len(hyp) + 1)]
    for i, word in enumerate(ref, 1):
        row = [(i, 0, i, 0)]
        for j, other in enumerate(hyp, 1):
            if word == other:
                row.append(previous[j - 1])
            else:
                cost, substitutions, deletions, insertions = previous[j - 1]
                substitution = (cost + 1, substitutions + 1, deletions, insertions)
                cost, substitutions, deletions, insertions = previous[j]
                deletion = (cost + 1, substitutions, deletions + 1, insertions)
                cost, substitutions, deletions, insertions = row[-1]
                insertion = (cost + 1, substitutions, deletions, insertions + 1)
                row.append(min((substitution, deletion, insertion), key=lambda item: item[0]))
        previous = row
    errors, substitutions, deletions, insertions = previous[-1]
    return {"reference_status": "verified", "WER": errors / len(ref), "S": substitutions,
            "D": deletions, "I": insertions, "N": len(ref)}


def _mapping(value, label):
    if not isinstance(value, dict):
        raise ReviewValidationError(f"{label} must be an object.")
    return value


def _number(value, label):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ReviewValidationError(f"{label} must be a finite number.")
    return value


def _integer(value, label, minimum=0):
    if type(value) is not int or value < minimum:
        raise ReviewValidationError(f"{label} must be an integer of at least {minimum}.")
    return value


def _timestamp(value, label):
    if not isinstance(value, str) or not value:
        raise ReviewValidationError(f"{label} must be an explicit timestamp.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ReviewValidationError(f"{label} must be an ISO 8601 timestamp.") from None
    if parsed.tzinfo is None:
        raise ReviewValidationError(f"{label} must include a timezone.")


def _same(value, expected, label):
    # JSON structural equality must not silently equate bool with int.
    if isinstance(expected, dict):
        if not isinstance(value, dict) or set(value) != set(expected):
            raise ReviewValidationError(f"{label} does not match the trusted package.")
        for key in expected:
            _same(value[key], expected[key], f"{label}.{key}")
    elif isinstance(expected, list):
        if not isinstance(value, list) or len(value) != len(expected):
            raise ReviewValidationError(f"{label} does not match the trusted package.")
        for i, (left, right) in enumerate(zip(value, expected)):
            _same(left, right, f"{label}[{i}]")
    elif type(expected) in (int, float):
        if type(value) not in (int, float) or value != expected or not math.isfinite(value):
            raise ReviewValidationError(f"{label} does not match the trusted package.")
    elif type(value) is not type(expected) or value != expected:
        raise ReviewValidationError(f"{label} does not match the trusted package.")


def _validate_package(package):
    _mapping(package, "Package")
    if type(package.get("schema_version")) is not int or package["schema_version"] != 1:
        raise ReviewValidationError("Unsupported human-review schema version.")
    if not isinstance(package.get("benchmark_id"), str) or not package["benchmark_id"]:
        raise ReviewValidationError("The package needs a benchmark ID.")
    source = _mapping(package.get("source"), "Package source")
    if not isinstance(source.get("sha256"), str) or not _SHA256.fullmatch(source["sha256"]):
        raise ReviewValidationError("The package needs the complete source SHA-256.")
    rate = _integer(source.get("sample_rate"), "Source sample rate", 1)
    frames = _integer(source.get("complete_frames"), "Source complete frames", 1)
    duration = _number(source.get("duration_seconds"), "Source duration")
    if abs(duration - frames / rate) > 1e-9:
        raise ReviewValidationError("Source duration does not match complete frames / sample rate.")
    _same(package.get("normalization"), NORMALIZATION, "Normalization policy")
    candidates = _mapping(package.get("candidates"), "Package candidates")
    if "baseline" not in candidates or set(candidates) - set(CANDIDATES):
        raise ReviewValidationError("Package must have baseline and optionally comparison candidates.")
    clips = package.get("clips")
    if not isinstance(clips, list) or not clips:
        raise ReviewValidationError("The package must contain review clips.")
    ids = set()
    for clip in clips:
        _mapping(clip, "Clip")
        clip_id = clip.get("clip_id")
        if not isinstance(clip_id, str) or not clip_id or clip_id in ids:
            raise ReviewValidationError("Clip IDs must be nonempty and unique.")
        ids.add(clip_id)
        start = _integer(clip.get("start_frame"), "Clip start frame")
        end = _integer(clip.get("end_frame"), "Clip end frame", 1)
        if not start < end <= frames or _integer(clip.get("sample_rate"), "Clip sample rate", 1) != rate:
            raise ReviewValidationError("Clip source-frame bounds/rate do not match the original recording.")
        for key, frame in (("source_start_seconds", start), ("source_end_seconds", end)):
            if abs(_number(clip.get(key), key) - frame / rate) > 1e-9:
                raise ReviewValidationError("Clip source offsets do not match original source frames.")
        for candidate in candidates:
            result = _mapping(clip.get(candidate), f"Clip {candidate}")
            segments = result.get("segments")
            if not isinstance(segments, list):
                raise ReviewValidationError("Candidate segments must be a list, including for an empty hypothesis.")
            for segment in segments:
                _mapping(segment, "ASR segment")
                if not isinstance(segment.get("text"), str):
                    raise ReviewValidationError("ASR segment text must be a string.")
                for key in ("start_seconds", "end_seconds", "source_start_seconds", "source_end_seconds"):
                    _number(segment.get(key), "ASR " + key)
                # Preserve invalid model times for review; only validate their
                # stated mapping to the original source, never clamp or repair.
                for local, original in (("start_seconds", "source_start_seconds"), ("end_seconds", "source_end_seconds")):
                    if abs(segment[original] - (clip["source_start_seconds"] + segment[local])) > 1e-9:
                        raise ReviewValidationError("ASR source-relative offsets do not match the clip mapping.")
    return candidates


def verified_reference(reference):
    reference = _mapping(reference, "Human reference")
    status = reference.get("status", "not_verified")
    if status not in {"not_verified", "draft", "verified"}:
        raise ReviewValidationError("Unknown human reference status.")
    text = reference.get("human_text", "")
    if not isinstance(text, str):
        raise ReviewValidationError("Human reference text must be a string.")
    if status != "verified":
        return None
    if reference.get("human_confirmed") is not True:
        raise ReviewValidationError("Verified references require explicit human confirmation.")
    _timestamp(reference.get("created_at"), "Reference created_at")
    _timestamp(reference.get("updated_at"), "Reference updated_at")
    _timestamp(reference.get("verified_at"), "Reference verified_at")
    if reference.get("verified_text_sha256") != text_sha256(text):
        raise ReviewValidationError("Human reference changed after verification; listen and verify again.")
    if _UNCERTAINTY.search(text):
        raise ReviewValidationError("Unresolved listening annotations cannot be verified ground truth; keep this reference as a draft.")
    if not tokens(text):
        raise ReviewValidationError("An empty human reference cannot establish word accuracy.")
    return text


def row_snapshot(row):
    return {"id": row.get("id"), "category": row.get("category"),
            "reference_text": row.get("reference_text"), "source_seconds": row.get("source_seconds"),
            "baseline": row.get("baseline", "pending"), "comparison": row.get("comparison", "pending"),
            "notes": row.get("notes", "")}


def _critical_rows(clip, reference, candidates):
    rows = clip.get("critical_content", [])
    if not isinstance(rows, list):
        raise ReviewValidationError("Critical-content rows must be a list.")
    result, ids = [], set()
    for row in rows:
        _mapping(row, "Critical-content row")
        identity = row.get("id")
        if not isinstance(identity, str) or not identity or identity in ids:
            raise ReviewValidationError("Critical-content row IDs must be unique and nonempty within each clip.")
        ids.add(identity)
        if row.get("category") not in CATEGORIES or row.get("status", "pending") not in {"pending", "unclear", "verified"}:
            raise ReviewValidationError("Unknown critical-content category or review status.")
        if not isinstance(row.get("reference_text", ""), str) or not isinstance(row.get("notes", ""), str):
            raise ReviewValidationError("Critical-content text and notes must be strings.")
        if row.get("source_seconds") is not None:
            second = _number(row["source_seconds"], "Critical-content source_seconds")
            if not clip["source_start_seconds"] <= second <= clip["source_end_seconds"]:
                raise ReviewValidationError("Critical-content source time is outside this clip.")
        for candidate in CANDIDATES:
            if row.get(candidate, "pending") not in JUDGMENTS:
                raise ReviewValidationError("Unknown candidate human judgment.")
        verified = row.get("status") == "verified"
        if verified:
            if reference is None or row.get("human_confirmed") is not True:
                raise ReviewValidationError("Verified critical content requires a verified human reference and confirmation.")
            if row.get("verified_reference_sha256") != text_sha256(reference):
                raise ReviewValidationError("Critical-content verification belongs to a different human reference.")
            _same(row.get("verified_row_snapshot"), row_snapshot(row), "Verified critical-content snapshot")
            if not row.get("reference_text", "").strip():
                raise ReviewValidationError("Verified critical content needs an explicit human description of the instance.")
        result.append({**row_snapshot(row), "status": row.get("status", "pending"), "verified": verified,
                       "candidate_available": {candidate: clip[candidate].get("state") in {"completed", "review_required"}
                                               for candidate in candidates}})
    term_counts = {}
    reference_tokens = tokens(reference) if reference is not None else []
    for row in result:
        if row["verified"] and row["category"] == "technicalterm":
            phrase = tuple(tokens(row["reference_text"]))
            if not phrase:
                raise ReviewValidationError("A verified technical term needs a literal scored phrase from the reference.")
            term_counts[phrase] = term_counts.get(phrase, 0) + 1
    for phrase, count in term_counts.items():
        occurrences = sum(tuple(reference_tokens[i:i + len(phrase)]) == phrase
                          for i in range(len(reference_tokens) - len(phrase) + 1))
        if count > occurrences:
            raise ReviewValidationError("Verified technical-term rows exceed matching instances in the human reference.")
    return result


def _critical_metrics(rows, candidates):
    result = {}
    for candidate in candidates:
        terms = [row for row in rows if row["verified"] and row["category"] == "technicalterm"]
        decided = [row for row in terms if row[candidate] in {"correct", "incorrect"}]
        correct = sum(row[candidate] == "correct" for row in decided)
        unavailable = any(not row["candidate_available"][candidate] for row in terms)
        complete = bool(terms) and len(decided) == len(terms) and not unavailable
        categories = {category: {judgment: sum(row["verified"] and row["category"] == category
                                               and row[candidate] == judgment for row in rows)
                                  for judgment in ("correct", "incorrect", "unclear", "pending")}
                      for category in CATEGORIES}
        result[candidate] = {
            "technical_term_accuracy": correct / len(terms) if complete else None,
            "correct_verified_term_instances": correct,
            "verified_term_instances": len(terms), "decisively_judged_instances": len(decided),
            "unverified_term_rows": sum(not row["verified"] and row["category"] == "technicalterm" for row in rows),
            "status": "candidate_unavailable" if unavailable else ("verified_subset" if complete else ("incomplete_judgments" if terms else "not_measured")),
            "scope": "Only explicitly verified human term instances; not an exhaustive census of the lecture. Pending or unclear candidate judgments prevent a percentage.",
            "critical_content_counts": categories,
        }
    return result


def score_review(package, review):
    """Validate an exported review against a trusted package and calculate scores."""
    candidates = _validate_package(package)
    _mapping(review, "Exported review")
    for key in ("schema_version", "benchmark_id", "source", "candidates", "normalization"):
        _same(review.get(key), package.get(key), key)
    if "environment" in package:
        _same(review.get("environment"), package["environment"], "environment")
    clips = review.get("clips")
    if not isinstance(clips, list) or len(clips) != len(package["clips"]):
        raise ReviewValidationError("Exported clip count does not match the trusted package.")
    results, all_rows = [], []
    for expected, clip in zip(package["clips"], clips):
        _mapping(clip, "Exported clip")
        immutable = {key: value for key, value in clip.items() if key not in _MUTABLE_CLIP}
        expected_immutable = {key: value for key, value in expected.items() if key not in _MUTABLE_CLIP}
        _same(immutable, expected_immutable, "Clip identity or immutable ASR")
        if clip.get("preference") not in PREFERENCES:
            raise ReviewValidationError("Unknown candidate preference.")
        tags = clip.get("tags", [])
        if not isinstance(tags, list) or any(not isinstance(tag, str) or tag not in TAGS for tag in tags):
            raise ReviewValidationError("Unknown review tag.")
        reference = verified_reference(clip.get("reference", {}))
        status = clip.get("reference", {}).get("status", "not_verified")
        scores = {}
        for candidate in candidates:
            hypothesis = "\n".join(segment["text"] for segment in expected[candidate]["segments"])
            if expected[candidate].get("state") not in {"completed", "review_required"}:
                scores[candidate] = {**_unmeasured(status), "undefined_reason": "candidate_unavailable"}
            else:
                scores[candidate] = _unmeasured(status) if reference is None else _verified_wer(reference, hypothesis)
        rows = _critical_rows(clip, reference, candidates)
        all_rows.extend(rows)
        results.append({"clip_id": clip["clip_id"], "source_start_seconds": clip["source_start_seconds"],
                        "source_end_seconds": clip["source_end_seconds"], "reference_status": status,
                        "reference_sha256": text_sha256(reference) if reference is not None else None,
                        "candidates": scores, "critical_content": {"rows": rows, "candidates": _critical_metrics(rows, candidates)},
                        "preference": clip.get("preference"), "tags": tags})
    aggregate = {}
    for candidate in candidates:
        measured = [clip["candidates"][candidate] for clip in results if clip["candidates"][candidate]["WER"] is not None]
        total = {key: sum(score[key] for score in measured) for key in ("S", "D", "I", "N")}
        aggregate[candidate] = ({**total, "WER": (total["S"] + total["D"] + total["I"]) / total["N"],
                                  "reference_status": "verified_subset"} if measured else _unmeasured("not_verified"))
        aggregate[candidate].update(verified_clips=len(measured), total_clips=len(results),
                                    scope="Token-weighted verified clips only; not full-recording accuracy.")
    verified_count = sum(clip["reference_status"] == "verified" for clip in results)
    return {"schema_version": 1, "benchmark_id": package["benchmark_id"], "source_sha256": package["source"]["sha256"],
            "generated_at": datetime.now(timezone.utc).isoformat(), "normalization": copy.deepcopy(NORMALIZATION),
            "status": "verified_references" if verified_count == len(results) else ("partially_verified" if verified_count else "awaiting_human_review"),
            "accuracy_scope": "Human verification is a manual assertion, not authentication. ASR agreement and automatic checks are not reference truth.",
            "clips": results, "aggregate": aggregate, "critical_content": _critical_metrics(all_rows, candidates)}


def parse_json(value):
    """Parse a single snapshot, rejecting duplicate keys and non-finite numbers."""
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ReviewValidationError("Review JSON contains duplicate object keys.")
            result[key] = value
        return result
    def constant(_):
        raise ReviewValidationError("Review JSON contains a non-finite number.")
    return json.loads(value, object_pairs_hook=pairs, parse_constant=constant)


def load_json(path):
    with open(path, "rb") as stream:
        return parse_json(stream.read())
