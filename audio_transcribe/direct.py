"""One recording, one readable result with portable factual provenance.

This module never decodes audio, invokes ASR, groups recordings, or changes old
sessions. Labels are separate user edits; export is an atomic exclusive create.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile

import yaml

from . import lifecycle
from .direct_metadata import recording_metadata
from .evaluation import timestamp
from .product import APP_VERSION, APP_BUILD
from .storage import (_file_lock, _write_atomic, locate_session, resolve_source_path, source_stat,
                      read_doc, sha256_file, validate_id, write_json, write_yaml, session_lock)

SCHEMA = "audiotranscribe/v1"
_LABELS = ("course", "speaker", "event")
_FORMATS = {"md", "txt", "srt", "json"}


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _root(settings):
    return Path(settings["roots"]["data"]).expanduser().resolve()


def _inside(root, value):
    path = Path(value).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("Result artifact is outside its managed directory.")
    return path


def _labels(value=None):
    if value is None:
        return dict.fromkeys(_LABELS)
    if not isinstance(value, dict) or set(value) - set(_LABELS):
        raise ValueError("Labels may contain only course, speaker and event.")
    result = {}
    for key in _LABELS:
        text = value.get(key)
        if text is not None and (not isinstance(text, str) or len(text) > 500 or "\x00" in text):
            raise ValueError("Each label must be text of at most 500 characters, or null.")
        result[key] = text.strip() or None if isinstance(text, str) else None
    return result


def _read_labels(settings, identity):
    path = _root(settings) / "results" / "labels" / (validate_id(identity, "result ID") + ".json")
    if not path.is_file():
        return _labels()
    return _labels(read_doc(_inside(_root(settings) / "results", path)).get("user_labels"))


def _segments(value):
    if not isinstance(value, list):
        raise ValueError("A result requires source-relative transcript segments.")
    result = copy.deepcopy(value)
    for segment in result:
        if (not isinstance(segment, dict) or not isinstance(segment.get("text"), str)
                or any(type(segment.get(key)) not in (int, float) or not math.isfinite(segment[key])
                       for key in ("start_seconds", "end_seconds"))):
            raise ValueError("Transcript segments need finite source-relative times and literal text.")
    return result


def _srt_valid(document):
    quality = document["transcription"].get("quality", {})
    if quality.get("timestamp_valid") is False:
        return False
    duration = document["audio"].get("duration_seconds")
    if type(duration) not in (int, float) or not math.isfinite(duration):
        return False
    previous = -1
    for s in document["segments"]:
        start, end = s["start_seconds"], s["end_seconds"]
        # SRT has millisecond precision; a zero-length rounded cue is invalid.
        if not s["text"].strip() or not 0 <= start < end <= duration or start < previous or round(end*1000) <= round(start*1000):
            return False
        previous = start
    return bool(document["segments"])


def _readable(document, *, timed=True):
    return "\n\n".join((f"[{timestamp(s['start_seconds'])}]\n" if timed else "") + s["text"]
                        for s in document["segments"]) + ("\n" if document["segments"] else "")


def _markdown_escape(text):
    return re.sub(r"([\\`*_{}\[\]<>#!|])", r"\\\1", text)


def front_matter(document):
    source, clock, audio, transcription = (document[k] for k in ("source", "recording", "audio", "transcription"))
    result = {"schema": SCHEMA, "source_filename": source["filename"], "source_sha256": source.get("sha256"),
            "source_size_bytes": source.get("size_bytes"), "recording_started_at": clock.get("started_at"),
            "recording_ended_at": clock.get("ended_at"), "recording_time_source": clock.get("time_source"),
            "recording_time_confidence": clock.get("time_confidence"), "timezone": clock.get("timezone"),
            "imported_at": source.get("imported_at"), "duration_seconds": audio.get("duration_seconds"),
            "audio": {k: audio.get(k) for k in ("sample_rate_hz", "channels", "sample_format", "codec")},
            "transcription": {k: transcription.get(k) for k in ("language", "model", "generated_at", "app_version", "app_build")},
            "quality_status": transcription.get("quality", {}).get("status", "unknown"),
            "user_labels": document.get("user_labels", _labels())}
    if "ownership" in source:
        result["source_ownership"] = source["ownership"]
    return result


def render_export(document, format):
    if format not in _FORMATS:
        raise ValueError("Choose Markdown, TXT, SRT or JSON.")
    if format == "json":
        return json.dumps(document, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    if format == "md":
        front = yaml.safe_dump(front_matter(document), sort_keys=False, allow_unicode=True)
        warning = "\n> Automatic checks need review. Raw text and timestamps are preserved.\n" if document["transcription"].get("state") == "review_required" else ""
        return "---\n" + front + "---\n\n# Transcript\n" + warning + "\n" + "\n\n".join(
            f"[{timestamp(s['start_seconds'])}]\n\n{_markdown_escape(s['text'])}" for s in document["segments"]) + "\n"
    if format == "txt":
        source, clock, audio, asr = (document[k] for k in ("source", "recording", "audio", "transcription"))
        recorded = clock.get("started_at")
        header = [f"Source: {source['filename']}",
                  "Recorded: " + (f"{recorded} ({clock['time_confidence']}; {clock['time_source']}; timezone {clock.get('timezone') or 'unknown'})" if recorded else "unknown"),
                  f"Duration: {timestamp(audio['duration_seconds']) if audio.get('duration_seconds') is not None else 'unknown'}",
                  f"Language: {asr.get('language') or 'unknown'}", f"Model: {asr.get('model') or 'unknown'}"]
        header.extend(f"{key.title()}: {value}" for key, value in document.get("user_labels", {}).items() if value)
        if asr.get("state") == "review_required":
            header.append("Status: needs review; raw text and timestamps retained")
        return "\n".join(header) + "\n\n---\n\n" + _readable(document, timed=False)
    if not _srt_valid(document):
        raise ValueError("SRT is unavailable because timestamps are unvalidated or cannot form valid subtitle cues. Export Markdown, TXT or JSON to preserve the raw result.")
    # Literal ASR strings stay in JSON. Blank cue-body lines would split SRT
    # blocks, so SRT alone collapses internal newlines to a readable single line.
    return "\n\n".join(f"{i}\n{timestamp(s['start_seconds'], srt=True)} --> {timestamp(s['end_seconds'], srt=True)}\n" +
                        " ".join(s["text"].splitlines()).strip()
                        for i, s in enumerate(document["segments"], 1)) + "\n"


def _load_context(settings, entry):
    ref = entry.get("transcript", {})
    session_path = locate_session(_root(settings), ref["session_id"])
    session = read_doc(session_path / "session.yaml")
    source = next((s for s in session["sources"] if s["id"] == ref["source_id"]), None)
    if source is None or source["sha256"] != entry["sha256"]:
        raise ValueError("Result source identity does not match the managed recording.")
    audio_path = resolve_source_path(session_path, source, verify_hash=True)
    run = _inside(session_path / "transcript", session_path / "transcript" / validate_id(ref["run_id"], "run ID"))
    manifest = read_doc(run / "manifest.json")
    if manifest.get("session_id") != session_path.name or manifest.get("run_id") != run.name:
        raise ValueError("Run identity does not match its managed directory.")
    for field, file in (("run_manifest_sha256", "manifest.json"), ("transcript_json_sha256", "transcript.json")):
        if ref.get(field) and sha256_file(run / file) != ref[field]:
            raise ValueError("Transcript changed before direct-result publication.")
    transform = {}
    if (run / "diagnostics.json").is_file():
        diagnostics = read_doc(run / "diagnostics.json")
        transform = next((s.get("transform", {}) for s in diagnostics.get("sources", []) if s.get("source_id") == source["id"]), {})
    return session_path, session, source, audio_path, run, manifest, transform


def _document(entry, segments, resolved, runtime, model, info, context):
    session_path, session, source, audio_path, run, manifest, transform = context
    info = info or transform.get("source", {})
    media = info.get("media_decode") or transform.get("media_decode")
    representation = info.get("sample_format") or info.get("format")
    representation = {"IEEE_FLOAT32": "float32", "IEEE_FLOAT64": "float64", "PCM_S16": "int16", "PCM_S24": "int24", "PCM_S32": "int32", "PCM16": "int16", "PCM24": "int24", "PCM32": "int32"}.get(representation, representation)
    duration = info.get("duration_seconds", entry.get("duration_seconds"))
    quality = copy.deepcopy(entry.get("transcript", {}).get("quality") or manifest.get("quality") or {"status": "unknown", "accuracy_verified": False})
    audio = {"duration_seconds": duration, "sample_rate_hz": info.get("sample_rate"), "channels": info.get("channels"),
             "sample_format": None if media else representation, "codec": (media.get("detected_audio_codec") if media else representation),
             "container": (media.get("detected_container") if media else "WAVE" if audio_path.suffix.lower() in {".wav", ".wave"} else None),
             "measurement_basis": "decoded source audio" if media else "original audio header and complete source frames",
             "decoded_complete_frames": info.get("complete_frames"), "discarded_orphan_bytes_in_derivative": transform.get("derivative_only_discarded_orphan_bytes", 0)}
    if media:
        audio["decoded_sample_format"] = representation
        audio["sample_format_basis"] = "Original encoded source has no PCM sample format here; decoded_sample_format identifies the working representation."
    transformations = {k: copy.deepcopy(transform.get(k)) for k in ("gain_db", "gain_calibration", "resampler", "quantization", "limiter", "timeline_changed", "output_sample_rate", "output_frames", "output_sha256") if k in transform}
    if media:
        transformations["media_decode"] = {k: media.get(k) for k in ("detected_container", "detected_audio_codec", "decoded_frames", "sample_rate", "channels", "duration_basis", "resampled", "gain_applied", "decoded_sha256")}
    model_info = {k: model.get(k) for k in ("sha256", "precision", "ggml_ftype", "repository_revision") if k in model}
    ref = entry.get("transcript", {})
    # A second consumer of fresh shared work is a cache hit for computation,
    # but its inference was still generated by this application invocation.
    generated_here = ref.get("generated_in_current_invocation", not ref.get("reused_transcript", False))
    return {"schema": SCHEMA,
            "source": {"filename": entry["filename"], "sha256": entry["sha256"], "size_bytes": entry.get("size_bytes", source.get("size_bytes")),
                       "ownership": source.get("ownership", "managed"),
                       "imported_at": entry.get("imported_at") or info.get("imported_at") or source.get("imported_at") or session.get("imported_at")},
            "recording": recording_metadata(audio_path, duration, source=source, session=session,
                                                legacy_time=entry.get("recording_time") or entry.get("transcript", {}).get("recording_time")),
            "audio": audio,
            "transcription": {"language": resolved.get("asr", {}).get("language"),
                              "model": model.get("name") or model.get("model_id") or resolved.get("asr", {}).get("model"),
                              "model_identity": model_info, "generated_at": manifest.get("completed_at") or manifest.get("started_at"),
                              "app_version": APP_VERSION if generated_here else manifest.get("application_version"),
                              "app_build": APP_BUILD if generated_here else None,
                              "export_application": {"version": APP_VERSION, "build": APP_BUILD},
                              "decoder_application_version": manifest.get("application_version"),
                              "state": entry.get("state", manifest.get("state", "review_required")),
                              "quality": quality, "parameters": copy.deepcopy(resolved.get("asr", {})),
                              "runtime": {k: (runtime.get("runtime") or runtime).get(k) for k in ("release", "commit", "sha256")}},
            "transformations": transformations, "user_labels": _labels(), "segments": _segments(segments)}


def _summary(identity, document, markdown_path, audio_path, *, legacy=False):
    transcription = document["transcription"]
    quality = transcription.get("quality", {})
    return {"result_id": identity, "filename": document["source"]["filename"],
            "source_ownership": document["source"].get("ownership", "managed"),
            "duration_seconds": document["audio"].get("duration_seconds"), "model": transcription.get("model"),
            "model_label": transcription.get("model"), "state": transcription.get("state"),
            "quality_status": quality.get("status", "unknown"),
            "quality_message": "Automatic checks need review; raw transcript preserved." if transcription.get("state") == "review_required" else "Automatic checks do not establish recognition accuracy.",
            "markdown_path": str(markdown_path), "report": str(markdown_path), "audio_path": str(audio_path) if audio_path else None,
            "generated_at": transcription.get("generated_at"), "legacy": legacy, "labels_editable": True}


def _inherit_generation_application(settings, document, internal):
    """Recover product generation identity only from verified exact-run records."""
    versions = set()
    bound_fields = ("session_id", "source_id", "run_id", "run_manifest_sha256", "transcript_sha256")
    for path in (_root(settings) / "results").glob("r-*/result.json"):
        try:
            saved = _new_record(settings, path.parent.name)
            if any(saved["internal"].get(k) != internal[k] for k in bound_fields):
                continue
            prior = copy.deepcopy(saved["document"])
            if (prior["source"]["sha256"] != document["source"]["sha256"]
                    or prior["segments"] != document["segments"]
                    or not _validate_saved_run(settings, saved, prior)):
                continue
            version, build = (prior["transcription"].get(k) for k in ("app_version", "app_build"))
            if isinstance(version, str) and version and (build is None or isinstance(build, str)):
                versions.add((version, build))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    if len(versions) == 1:
        document["transcription"]["app_version"], document["transcription"]["app_build"] = versions.pop()
    elif versions:
        # Conflicting saved claims do not establish one authoritative version.
        document["transcription"].update(app_version=None, app_build=None)


def publish_result(settings, entry, segments, resolved, runtime, model, info=None):
    ref = entry['transcript']
    token = entry.get('lifecycle_token')
    if token:
        token = lifecycle.bind_source(settings, token, ref['session_id'], ref['source_id'], entry['filename'])
    with lifecycle.publication_guard(settings, token, ref['session_id'], ref['source_id'], entry['filename']) as epoch:
        return _publish_result(settings, entry, segments, resolved, runtime, model, info, epoch=epoch)


def _publish_result(settings, entry, segments, resolved, runtime, model, info=None, *, epoch=0):
    """Publish one source's saved transcription; repeated cache reuse is stable."""
    context = _load_context(settings, entry)
    ref = entry["transcript"]
    saved_segments = [s for s in read_doc(context[4] / "transcript.json")["segments"] if s.get("source_id") == ref["source_id"]]
    if _segments(segments) != saved_segments:
        raise ValueError("Published text must exactly match the saved source transcript.")
    key = {"source_sha256": entry["sha256"], "filename": entry["filename"], "run_id": ref["run_id"],
           "transcript_sha256": sha256_file(context[4] / "transcript.json")}
    if epoch:
        key["lifecycle_epoch"] = epoch
    fingerprint = _digest(key)
    identity = "r-" + fingerprint[:40]
    parent = _root(settings) / "results"
    parent.mkdir(parents=True, exist_ok=True)
    final = parent / identity
    with _file_lock(parent / ".publish.lock", blocking=True):
        if (final / "result.json").is_file():
            saved = _new_record(settings, identity)
            if saved["fingerprint"] != fingerprint:
                raise ValueError("Direct-result identity collision; existing files preserved.")
            return _summary(identity, saved["document"], final / "transcript.md", saved["internal"]["audio_path"])
        document = _document(entry, segments, resolved, runtime, model, info, context)
        internal = {"audio_path": str(context[3]), "session_id": ref["session_id"], "source_id": ref["source_id"],
                    "run_id": ref["run_id"], "run_manifest_sha256": sha256_file(context[4] / "manifest.json"),
                    "transcript_sha256": key["transcript_sha256"], "lifecycle_epoch": epoch}
        if not ref.get("generated_in_current_invocation", not ref.get("reused_transcript", False)):
            _inherit_generation_application(settings, document, internal)
        stage = Path(tempfile.mkdtemp(prefix=".result-", dir=parent))
        try:
            saved = {"schema_version": 1, "result_id": identity, "fingerprint": fingerprint,
                     "document": document, "document_sha256": _digest(document), "internal": internal, "internal_sha256": _digest(internal)}
            write_json(stage / "result.json", saved)
            _write_atomic(stage / "transcript.md", render_export(document, "md"))
            os.rename(stage, final)
        finally:
            if stage.exists():
                # Only remove our unpublished, bounded staging artifacts.
                for item in stage.iterdir():
                    item.unlink()
                stage.rmdir()
    return _summary(identity, document, final / "transcript.md", context[3])


def _new_record(settings, identity):
    validate_id(identity, "result ID")
    root = _root(settings) / "results"
    path = _inside(root, root / identity / "result.json")
    saved = read_doc(path)
    if (saved.get("schema_version") != 1 or saved.get("result_id") != identity
            or saved.get("document_sha256") != _digest(saved.get("document"))
            or saved.get("internal_sha256") != _digest(saved.get("internal"))):
        raise ValueError("Direct-result integrity check failed; prior files preserved.")
    if saved["document"].get("schema") != SCHEMA:
        raise ValueError("Unsupported direct-result schema.")
    _segments(saved["document"]["segments"])
    return saved


def _legacy_records(settings):
    """Metadata-only discovery. Reading a result separately verifies its artifacts."""
    root = _root(settings)
    sessions = list((root / "sessions").glob("*/session.yaml")) + list((root / "archive").glob("[0-9][0-9][0-9][0-9]/*/session.yaml"))
    for session_file in sessions:
        if not session_file.resolve().is_relative_to(root):
            continue
        try:
            session = read_doc(session_file)
            if session_file.parent.name != validate_id(session.get("id"), "session ID"):
                continue
            for mpath in session_file.parent.glob("transcript/*/manifest.json"):
                if not mpath.resolve().is_relative_to(session_file.parent.resolve()):
                    continue
                m = read_doc(mpath)
                if m.get("state") not in {"completed", "review_required"} or not (mpath.parent / "transcript.json").is_file():
                    continue
                if m.get("run_id") != mpath.parent.name or m.get("session_id") != session["id"]:
                    continue
                covered = {c.get("source_id") for c in m.get("coverage", [])}
                for source in session.get("sources", []):
                    if source.get("id") not in covered:
                        continue
                    identity = "legacy-source-" + _digest([str(mpath.relative_to(root)), source["id"]])[:32]
                    coverage = next(c for c in m["coverage"] if c.get("source_id") == source["id"])
                    audio = resolve_source_path(session_file.parent, source)
                    summary = {"result_id": identity, "filename": source["original_basename"], "duration_seconds": coverage.get("duration_seconds"),
                               "model": m.get("resolved_config", {}).get("asr", {}).get("model"), "state": m["state"],
                               "quality_status": m.get("quality", {}).get("status", "unknown"), "quality_message": "Historical result; recognition accuracy remains unverified.",
                               "markdown_path": str(mpath.parent / "transcript.md"), "report": str(mpath.parent / "transcript.md"),
                               "audio_path": str(audio), "generated_at": m.get("completed_at") or m.get("started_at"),
                               "legacy": True, "labels_editable": True}
                    summary["model_label"] = summary["model"]
                    yield identity, {"kind": "source", "summary": summary, "source": source, "session_path": session_file.parent,
                                     "run_path": mpath.parent, "manifest": m, "coverage": coverage}
        except (OSError, ValueError, KeyError, TypeError, StopIteration):
            continue
    # Keep old complete batch reports accessible as historical documents only.
    from .library import _reports
    for old_id, (summary, manifest, path) in _reports(root).items():
        if summary.get("deleted_at"):
            continue
        identity = "legacy-report-" + _digest(str(path.relative_to(root)))[:32]
        names = [e.get("filename", "Recording") for e in manifest.get("ordered_sources", [])]
        name = "Previous report: " + ", ".join(names)
        yield identity, {"kind": "report", "manifest": manifest, "path": path,
                        "summary": {"result_id": identity, "filename": name, "duration_seconds": None,
                                    "model": None, "model_label": "Previous report", "state": manifest.get("state", "unknown"),
                                    "quality_status": manifest.get("quality", {}).get("status", "unknown"),
                                    "quality_message": "Historical full report retained without regrouping or migration.",
                                    "markdown_path": str(path.parent / "transcript-report.md"), "report": str(path.parent / "transcript-report.md"),
                                    "audio_path": None, "generated_at": manifest.get("created_at"), "legacy": True, "labels_editable": False}}


def _records(settings, *, include_deleted=False):
    found = {}
    adopted = set()
    for path in (_root(settings) / "results").glob("r-*/result.json"):
        try:
            saved = _new_record(settings, path.parent.name)
            internal = saved["internal"]
            adopted.add((internal["session_id"], internal["source_id"], internal["run_id"]))
            found[path.parent.name] = {"kind": "new", "saved": saved,
                "summary": _summary(path.parent.name, saved["document"], path.parent / "transcript.md", internal["audio_path"])}
        except (OSError, ValueError, KeyError, TypeError):
            continue
    for identity, record in _legacy_records(settings):
        if record["kind"] == "source" and (record["session_path"].name, record["source"]["id"], record["run_path"].name) in adopted:
            continue
        if identity in found:
            raise ValueError("Ambiguous result identity.")
        found[identity] = record
    if not include_deleted:
        current = lifecycle.state(settings)
        found = {identity: record for identity, record in found.items() if lifecycle.visible(record, identity, current)}
    return found


def _record_for_identity(settings, identity):
    """Open a modern result directly; only historical IDs need a library scan."""
    if not identity.startswith("r-"):
        return _records(settings).get(identity)
    try:
        saved = _new_record(settings, identity)
        internal = saved["internal"]
        result_path = _root(settings) / "results" / identity
        record = {"kind": "new", "saved": saved,
                  "summary": _summary(identity, saved["document"], result_path / "transcript.md",
                                      internal["audio_path"])}
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return record if lifecycle.visible(record, identity, lifecycle.state(settings)) else None


def _legacy_document(settings, record):
    from .engine import verify_completed_run, verify_review_run, validate_run_quality, validate_run_timeline
    run, source, m = record["run_path"], record["source"], record["manifest"]
    if not (verify_completed_run(run) or verify_review_run(run)):
        raise ValueError("Historical transcript integrity check failed; the old artifacts were preserved.")
    stored = read_doc(run / "transcript.json")
    segments = [s for s in stored["segments"] if s.get("source_id") == source["id"]]
    quality = copy.deepcopy(m.get("quality") or {"status": "unknown", "accuracy_verified": False})
    try:
        timing = validate_run_timeline(run)
        current = validate_run_quality(run)
    except (ValueError, KeyError, OSError):
        timing, current = False, False
    if not current or not timing:
        quality.update(status="review_required", accuracy_verified=False, timestamp_valid=timing)
    ref = {"session_id": record["session_path"].name, "source_id": source["id"], "run_id": run.name, "quality": quality}
    entry = {"filename": source["original_basename"], "sha256": source["sha256"], "size_bytes": source["size_bytes"],
             "duration_seconds": record["coverage"].get("duration_seconds"),
             "state": "review_required" if quality.get("status") in {"unknown", "review_required"} else m["state"], "transcript": ref}
    context = _load_context(settings, entry)
    info = context[-1].get("source", {})
    result = _document(entry, segments, m.get("resolved_config", {}), m.get("runtime", {}), m.get("model", {}), info, context)
    # Decoder generation time/version remains factual; the new adapter does not
    # pretend a historic inference was generated by the newly installed version.
    result["transcription"]["app_version"] = m.get("application_version")
    result["transcription"]["app_build"] = None
    result["transcription"]["export_adapter_version"] = APP_VERSION
    return result


def _refresh_source(settings, document, internal):
    """Verify bytes and return the exact file identity observed during hashing."""
    try:
        session = locate_session(_root(settings), internal["session_id"])
        metadata = read_doc(session / "session.yaml")
        source = next(s for s in metadata["sources"] if s["id"] == internal["source_id"])
        path = resolve_source_path(session, source, verify_hash=False)
        before = source_stat(path)
        if sha256_file(path) != source["sha256"]:
            raise ValueError("Source bytes changed; cached provenance cannot be used.")
        after = source_stat(path)
        if before != after:
            raise ValueError("Source changed during verification.")
        if source["sha256"] != document["source"]["sha256"]:
            raise ValueError("Source identity changed.")
        return path, "verified", after
    except (OSError, ValueError, KeyError, StopIteration):
        # A saved transcript remains readable, but never play changed bytes as
        # if they were the original or offer them as an exact cached match.
        return None, "missing_or_changed", None


def _source_quality_message(integrity, state):
    if integrity == "pending":
        return "Saved transcript is ready; original audio verification is pending."
    if integrity != "verified":
        return "Original audio is unavailable or changed; playback and duplicate reuse are disabled. Saved transcript retained."
    if state == "review_required":
        return "Saved transcript needs review."
    return "Automatic checks do not establish recognition accuracy."


def _validate_saved_run(settings, saved, document):
    from .engine import verify_completed_run, verify_review_run, validate_run_quality, validate_run_timeline
    internal = saved["internal"]
    try:
        session = locate_session(_root(settings), internal["session_id"])
        run = _inside(session / "transcript", session / "transcript" / validate_id(internal["run_id"], "run ID"))
        if (sha256_file(run / "manifest.json") != internal["run_manifest_sha256"]
                or sha256_file(run / "transcript.json") != internal["transcript_sha256"]
                or not (verify_completed_run(run) or verify_review_run(run))):
            return False
        stored = [s for s in read_doc(run / "transcript.json")["segments"] if s.get("source_id") == internal["source_id"]]
        if stored != document["segments"]:
            return False
        try:
            current, timing = validate_run_quality(run), validate_run_timeline(run)
        except (ValueError, KeyError):
            current, timing = False, False
        if not current or not timing:
            document["transcription"]["state"] = "review_required"
            document["transcription"]["quality"].update(status="review_required", accuracy_verified=False, timestamp_valid=timing)
        return True
    except (OSError, ValueError, KeyError):
        return False


def _legacy_copy_text(settings, record, intact):
    """Copy literal verified source transcripts, never raw report diagnostics."""
    if not intact:
        return None
    from .engine import verify_completed_run, verify_review_run
    blocks = []
    try:
        for entry in record["manifest"].get("ordered_sources", []):
            if entry.get("state") in {"failed", "cancelled"}:
                blocks.append(f"{entry['filename']}\n[{entry['state']}]")
                continue
            ref = entry["transcript"]
            session = locate_session(_root(settings), ref["session_id"])
            run = _inside(session / "transcript", session / "transcript" / validate_id(ref["run_id"], "run ID"))
            if not (verify_completed_run(run) or verify_review_run(run)):
                return None
            if ref.get("transcript_json_sha256") != sha256_file(run / "transcript.json"):
                return None
            text = read_doc(run / "transcript.json")
            segments = [s for s in text["segments"] if s.get("source_id") == ref["source_id"]]
            blocks.append(entry["filename"] + "\n\n" + "\n\n".join(s["text"] for s in segments))
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return "\n\n".join(blocks) + "\n" if blocks else None


def read_result(settings, identity, *, defer_source_verification=False):
    if type(defer_source_verification) is not bool:
        raise ValueError("Deferred source verification must be a boolean.")
    validate_id(identity, "result ID")
    record = _record_for_identity(settings, identity)
    if record is None:
        raise FileNotFoundError("Result not found or its metadata failed validation.")
    summary = copy.deepcopy(record["summary"])
    if record["kind"] == "report":
        path = record["path"].parent / "transcript-report.md"
        text = path.read_text(encoding="utf-8")
        intact = sha256_file(path) == record["manifest"].get("report_sha256")
        copy_text = _legacy_copy_text(settings, record, intact)
        return {**summary, "readable_text": text, "plain_text": copy_text or "", "copy_text": copy_text or "", "copy_allowed": copy_text is not None, "markdown": text, "segments": [],
                "provenance": {"legacy_report": True, "integrity": "intact" if intact else "modified", "source_filenames": [e.get("filename") for e in record["manifest"].get("ordered_sources", [])]},
                "user_labels": _labels(), "available_formats": ["md", "txt"]}
    deferred = defer_source_verification and record["kind"] == "new"
    document = copy.deepcopy(record["saved"]["document"]) if record["kind"] == "new" else _legacy_document(settings, record)
    document["user_labels"] = _read_labels(settings, identity)
    internal = record["saved"]["internal"] if record["kind"] == "new" else {"session_id": record["session_path"].name, "source_id": record["source"]["id"]}
    audio_path, source_integrity, verified_stat = ((None, "pending", None) if deferred
                                                   else _refresh_source(settings, document, internal))
    run_integrity = "verified"
    if record["kind"] == "new" and not _validate_saved_run(settings, record["saved"], document):
        run_integrity = "unavailable_or_changed"
        document["transcription"].update(state="review_required")
        document["transcription"]["quality"].update(status="review_required", timestamp_valid=False, accuracy_verified=False)
    summary["audio_path"] = str(audio_path) if audio_path else None
    summary["quality_message"] = _source_quality_message(source_integrity, document["transcription"]["state"])
    summary.update(state=document["transcription"]["state"], quality_status=document["transcription"].get("quality", {}).get("status", "unknown"))
    return {**summary, "source_integrity": source_integrity, "verified_source_stat": verified_stat,
            "run_integrity": run_integrity, "copy_allowed": True, "copy_text": _readable(document, timed=False),
            "readable_text": _readable(document), "plain_text": _readable(document, timed=False),
            "markdown": render_export(document, "md"), "segments": document["segments"],
            "provenance": {k: copy.deepcopy(v) for k, v in document.items() if k != "segments"},
            "user_labels": document["user_labels"], "available_formats": ["md", "txt", "json"] + (["srt"] if _srt_valid(document) else [])}


def verify_source(settings, identity):
    """Return a playable path only after the saved source's full SHA-256 check."""
    validate_id(identity, "result ID")
    record = _record_for_identity(settings, identity)
    if record is None:
        raise FileNotFoundError("Result not found or its metadata failed validation.")
    if record["kind"] == "report":
        return {"result_id": identity, "audio_path": None, "source_integrity": "not_applicable"}
    if record["kind"] == "new":
        document = copy.deepcopy(record["saved"]["document"])
        internal = record["saved"]["internal"]
        if not _validate_saved_run(settings, record["saved"], document):
            document["transcription"]["state"] = "review_required"
    else:
        document = {"source": {"sha256": record["source"]["sha256"]}}
        internal = {"session_id": record["session_path"].name,
                    "source_id": record["source"]["id"]}
    path, integrity, verified_stat = _refresh_source(settings, document, internal)
    return {"result_id": identity, "audio_path": str(path) if path else None,
            "source_integrity": integrity, "verified_source_stat": verified_stat,
            "quality_message": _source_quality_message(integrity, document.get("transcription", {}).get("state", record["summary"]["state"]))}


def _lookup(settings, path, records):
    path = Path(path).expanduser()
    if not path.is_absolute() or not path.is_file():
        raise ValueError("Select an existing local audio file.")
    before = path.stat()
    digest = sha256_file(path)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise ValueError("Source changed during duplicate lookup; retry after copying finishes.")
    matches = []
    for identity, record in records.items():
        saved_hash = record["saved"]["document"]["source"].get("sha256") if record["kind"] == "new" else record.get("source", {}).get("sha256")
        if saved_hash == digest:
            try:
                # Validate before offering Open Existing Result. This reads only
                # stored transcript artifacts, never runs inference or migration.
                opened = read_result(settings, identity)
                if opened.get("source_integrity") == "verified" and opened.get("run_integrity") == "verified":
                    matches.append({k: opened[k] for k in record["summary"]})
            except (OSError, ValueError, KeyError, TypeError):
                continue
    matches.sort(key=lambda s: (s.get("generated_at") or "", s["result_id"]), reverse=True)
    return {"path": str(path), "sha256": digest, "existing": matches}


def _matches_query(settings, identity, record, query):
    """Search saved local content; never infer transcript text from filenames."""
    if query in record["summary"]["filename"].casefold():
        return True
    try:
        if any(query in (value or "").casefold() for value in _read_labels(settings, identity).values()):
            return True
        if record["kind"] == "new":
            return any(query in segment["text"].casefold() for segment in record["saved"]["document"]["segments"])
        if record["kind"] == "source":
            document = read_doc(record["run_path"] / "transcript.json")
            return any(segment.get("source_id") == record["source"]["id"]
                       and query in segment.get("text", "").casefold()
                       for segment in document.get("segments", []))
        if record["kind"] == "report":
            return query in (record["path"].parent / "transcript-report.md").read_text(encoding="utf-8").casefold()
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return False


def direct_request(settings, request):
    if not isinstance(request, dict):
        raise ValueError("Result request must be an object.")
    action = request.get("action", "list")
    if action == "discover":
        from .watch import discover
        return discover(settings, request)
    if action in {"watch_ignore", "watch_readd"}:
        from .watch import ignore
        return ignore(settings, request, restore=action == "watch_readd")
    if action == "locate_source":
        return locate_source(settings, request)
    if action in {"delete_plan", "delete_commit", "delete_retry", "delete_status"}:
        from .deletion import request as delete_request
        return delete_request(settings, request)
    if action == "list":
        query = request.get("query", "")
        if not isinstance(query, str) or len(query) > 256:
            raise ValueError("Search query must be text of at most 256 characters.")
        records = _records(settings)
        needle = query.casefold().strip()
        results = [record["summary"] for identity, record in records.items()
                   if not needle or _matches_query(settings, identity, record, needle)]
        results.sort(key=lambda r: (r.get("generated_at") or "", r["result_id"]), reverse=True)
        return {"schema_version": 1, "results": results}
    if action == "lookup":
        records = _records(settings)
        if "paths" in request:
            if not isinstance(request["paths"], list):
                raise ValueError("Lookup paths must be a list.")
            matches = []
            for index, path in enumerate(request["paths"]):
                try:
                    matches.append({"index": index, **_lookup(settings, path, records)})
                except (OSError, ValueError, TypeError) as error:
                    matches.append({"index": index, "path": path, "existing": [], "error": str(error)})
            return {"matches": matches}
        return _lookup(settings, request.get("path"), records)
    identity = validate_id(request.get("result_id"), "result ID")
    if action in {"labels", "export"}:
        with lifecycle.lock(settings):
            return _write_result_request(settings, request, identity)
    if action == "read":
        return read_result(settings, identity, defer_source_verification=request.get("defer_source_verification", False))
    if action == "verify_source":
        return verify_source(settings, identity)
    raise ValueError("Unknown result action.")


def locate_source(settings, request):
    """Relink one missing external original only after an exact-byte check."""
    identity = validate_id(request.get("result_id"), "result ID")
    raw = request.get("path")
    if not isinstance(raw, str) or not Path(raw).is_absolute():
        raise ValueError("Choose a local replacement file.")
    candidate = Path(raw).expanduser().absolute()
    if candidate.resolve().is_relative_to(_root(settings)):
        raise ValueError("Replacement cannot be inside application-managed data.")
    before = source_stat(candidate)
    digest = sha256_file(candidate)
    if source_stat(candidate) != before:
        raise ValueError("Replacement changed during verification.")
    with lifecycle.lock(settings):
        record = _record_for_identity(settings, identity)
        if record is None or record["kind"] != "new":
            raise ValueError("Choose an existing individual result with an external reference.")
        internal = record["saved"]["internal"]
        expected = record["saved"]["document"]["source"]["sha256"]
        if digest != expected:
            raise ValueError("Replacement bytes differ from the processed recording; source was not relinked.")
        session_path = locate_session(_root(settings), internal["session_id"])
        # Cache reuse may hold this session while binding its lifecycle scope.
        # Never wait for it with the lifecycle fence held: a busy session can
        # be retried, but a blocking acquisition would deadlock both requests.
        with session_lock(session_path, blocking=False):
            session = read_doc(session_path / "session.yaml")
            source = next((s for s in session["sources"] if s["id"] == internal["source_id"]), None)
            if not source or source.get("ownership") != "external_referenced" or source["sha256"] != digest:
                raise ValueError("Result source is not the expected external reference.")
            if source_stat(candidate) != before:
                raise ValueError("Replacement changed before relinking.")
            if candidate.resolve().is_relative_to(_root(settings)):
                raise ValueError("Replacement cannot be inside application-managed data.")
            source["external_path"] = str(candidate.resolve())
            source["external_identity"] = before
            write_yaml(session_path / "session.yaml", session, overwrite=True)
    return read_result(settings, identity)


def _write_result_request(settings, request, identity):
    # Re-read inside the deletion fence, so a completed purge cannot be
    # followed by a stale label or an app-owned export being recreated.
    action = request['action']
    # Export and label changes are fenced against deletion, but do not need to
    # hash a multi-GB original while holding the global lifecycle lock. Saved
    # transcript artifacts are still validated and source playback remains
    # unavailable until an explicit verification request succeeds.
    opened = read_result(settings, identity, defer_source_verification=True)
    if action == "labels":
        if not opened["labels_editable"]:
            raise ValueError("Whole historical batch reports are read-only. Label an individual recording result instead.")
        labels = _labels(request.get("user_labels"))
        root = _root(settings) / "results"
        root.mkdir(parents=True, exist_ok=True)
        with _file_lock(root / ".labels.lock", blocking=True):
            write_json(root / "labels" / (identity + ".json"), {"user_labels": labels}, overwrite=True)
        return read_result(settings, identity, defer_source_verification=True)
    if action == "export":
        format = request.get("format", "md")
        if format not in opened["available_formats"]:
            raise ValueError("This export format is unavailable for this result.")
        path = request.get("path")
        if not isinstance(path, str) or not Path(path).expanduser().is_absolute():
            raise ValueError("Choose an absolute destination path for the export.")
        destination = Path(path).expanduser()
        text = opened["markdown"] if opened.get("provenance", {}).get("legacy_report") else render_export({**opened["provenance"], "segments": opened["segments"]}, format)
        _write_atomic(destination, text, overwrite=False)
        return {"result_id": identity, "format": format, "path": str(destination), "overwritten": False}
    raise ValueError("Unknown result action.")
