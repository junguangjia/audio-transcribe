"""Compile verified raw transcripts locally, without joining audio or using an LLM."""
from __future__ import annotations

import copy
import contextlib
import errno
import math
import os
from pathlib import Path
import re
import unicodedata
import time

from .audio import inspect_audio
from .media import inspect_media, MediaError
from .engine import (digest, load_runtime, model_identity, new_id, now, run_session, prepare_session,
                     transform_identity, compatible_transform_identity, ResourceAllocationError,
                     verify_completed_run, verify_review_run,
                     validate_run_timeline, validate_run_quality)
from .evaluation import timestamp
from .quality import assess_quality
from .library import (export_directory, export_manifests, normalize_groups,
                      rebuild_index, recording_time, report_is_trashed)
from .timeline import audit_timeline, TimelineError
from .storage import (import_sources, resolve_source_path, source_stat, read_doc, session_lock,
                      sha256_file, validate_id, validate_session, write_json)
from .execution import OperationCancelled, cancellable_session_lock
from .scheduler import BatchCoordinator, PreparedWork, WorkResult
from .telemetry import ResourceSampler
from . import lifecycle

EXPORT_VERSION = 3


def _publish_batch_metrics(settings, coordinator, publish):
    """A drained batch must not recreate per-job logs after confirmed deletion."""
    with lifecycle.lock(settings):
        try:
            for job in coordinator.jobs:
                lifecycle.assert_live(settings, getattr(job, "lifecycle_token", None))
        except ValueError:
            return False
        publish()
        return True


def natural_order(paths):
    """Stable locale-independent proposal; CLI --order-confirmed retains argument order."""
    def key(value):
        text = unicodedata.normalize("NFC", Path(value).name).casefold()
        parts = tuple((0, int(part)) if part.isdigit() else (1, part)
                      for part in re.split(r"([0-9]+)", text) if part)
        return parts, str(value)
    return sorted(paths, key=key)


def markdown_text(text):
    # Escape formatting/HTML syntax only; no ASR-word editing or reconstruction.
    return re.sub(r"([\\`*_{}\[\]<>#!|])", r"\\\1", text)


def effective_settings(resolved):
    return {"asr": resolved["asr"], "preprocessing": resolved["preprocessing"],
            "glossary_sha256": resolved["glossary"]["sha256"]}


def import_cancellable(data_root, path, context, *, separate=False, ownership="managed",
                       verified_hash=None, verified_stat=None):
    while True:
        context.check_cancelled()
        try:
            return import_sources(data_root, [path], separate=separate, ownership=ownership,
                                  verified_hashes=[verified_hash] if verified_hash else None,
                                  verified_stats=[verified_stat] if verified_stat else None)
        except RuntimeError as error:
            if not isinstance(error.__cause__, BlockingIOError):
                raise
            context.emit({"stage": "waiting_for_import"})
            time.sleep(0.25)


def matching_sessions(data_root, source_hash):
    root = Path(data_root)
    paths = sorted((root / "sessions").glob("*/session.yaml"))
    paths += sorted((root / "archive").glob("[0-9][0-9][0-9][0-9]/*/session.yaml"))
    for path in paths:
        if path.parent.name.startswith("."):
            continue
        try:
            record = read_doc(path)
        except (OSError, ValueError):
            continue
        if any(isinstance(s, dict) and s.get("sha256") == source_hash for s in record.get("sources", [])):
            yield path.parent


def read_source_result(session_path, source, run_path, info, *, reused):
    provisional = verify_review_run(run_path)
    if not provisional and not verify_completed_run(run_path):
        raise ValueError("Completed transcript integrity check failed; prior files were preserved.")
    try:
        timing_valid = validate_run_timeline(run_path)
    except TimelineError:
        if not provisional:
            raise
        timing_valid = False
    if not provisional and not timing_valid:
        raise ValueError("Cached presentation needs regeneration from its verified decoder result.")
    manifest = read_doc(run_path / "manifest.json")
    validate_id(manifest["run_id"], "run ID")
    if manifest["run_id"] != run_path.name or manifest["session_id"] != session_path.name:
        raise ValueError("Transcript provenance does not match its managed location.")
    document = read_doc(run_path / "transcript.json")
    coverage = [c for c in document["source_map"] if c["source_id"] == source["id"]]
    if (len(coverage) != 1 or coverage[0]["source_sha256"] != info["sha256"]
            or not coverage[0].get("decoded_input_complete")
            or abs(coverage[0]["duration_seconds"] - info["duration_seconds"]) > 1 / 16000 + 1e-9):
        raise ValueError("Source coverage does not match the selected recording.")
    segments = [s for s in document["segments"] if s["source_id"] == source["id"]]
    for s in segments:
        a, b = s["start_seconds"], s["end_seconds"]
        if (not isinstance(s["text"], str) or any(type(n) not in (int, float) or not math.isfinite(n) for n in (a, b))
                or (not provisional and not 0 <= a <= b <= info["duration_seconds"] + 0.05)):
            raise ValueError("Invalid source-relative segment in stored transcript.")
    # Reassess every returned source under current rules. A historic "completed"
    # label or an older quality policy must not make known-bad text acceptable.
    audit = audit_timeline(read_doc(run_path / "logs" / source["id"] / "native.json"),
                           frames=info["complete_frames"], rate=info["sample_rate"])
    resolved = manifest.get("resolved_config", {})
    quality = assess_quality(segments, [{"source_id": source["id"], "audit": audit}],
                             asr=resolved.get("asr"), glossary=resolved.get("glossary"))
    state = "review_required" if provisional or quality["status"] == "review_required" else "completed"
    explicit_time = source.get("recorded_at")
    if explicit_time is None:
        session = read_doc(session_path / "session.yaml")
        explicit_time = session.get("recorded_at") if len(session.get("sources", [])) == 1 else None
    return {"session_id": session_path.name, "source_id": source["id"], "run_id": run_path.name,
            "state": state, "quality": quality,
            "recording_time": recording_time(source["original_basename"], explicit_time),
            "run_manifest_sha256": sha256_file(run_path / "manifest.json"),
            "transcript_json_sha256": sha256_file(run_path / "transcript.json"),
            "segment_count": len(segments), "raw_segments_sha256": digest(segments),
            "timestamp_normalizations": [s["timestamp_normalization"] for s in segments if s.get("timestamp_normalization")],
            "review_relative_to_session": f"transcript/{run_path.name}/review.md",
            "reused_transcript": reused}, segments


def reject_preserved_invalid_timeline(session_path, source, run_path, info, resolved, runtime, model, manifest):
    """Reuse a negative validation result, never promote an unverified failed run.

    Older decoder attempts may have written native JSON before validation failed,
    without a completion receipt. Matching provenance plus a reproducible timing
    violation is enough to retain failure; it is NOT proof of successful decoding
    or historical output integrity. No old file is changed and no ASR is run.
    """
    logs = run_path / "logs" / source["id"]
    if not (logs / "native.json").is_file() or not (logs / "invocation.json").is_file():
        return
    invocation = read_doc(logs / "invocation.json")
    for transform_path in (session_path / "derived").glob("*/transform.json"):
        transform = read_doc(transform_path)
        wav = transform_path.with_name("audio.wav")
        if (transform["source"]["sha256"] != info["sha256"]
                or transform["output_sha256"] not in manifest["derivatives"]
                or transform["policy"] != resolved["preprocessing"]):
            continue
        asr = resolved["asr"]
        expected_args = [runtime["runtime"]["cli"], "-m", model["path"], "-f", str(wav),
                         "-l", asr["language"], "-t", str(asr["threads"]), "-bs", str(asr["beam_size"]),
                         "-tp", str(asr["temperature"]), "-tpi", str(asr["temperature_increment"]),
                         "-mc", str(asr.get("max_context", 0)), "-ojf", "-of", str(logs / "native")]
        import platform
        if platform.machine() != "arm64":
            expected_args.append("-ng")
        if resolved["glossary"].get("terms"):
            expected_args.extend(["--prompt", ", ".join(resolved["glossary"]["terms"])])
        if (invocation.get("args") != expected_args
                or invocation.get("input_sha256") != transform["output_sha256"]
                or invocation.get("model_sha256") != model["sha256"]
                or sha256_file(wav) != transform["output_sha256"]):
            continue
        raw = logs / "native.json"
        audit = audit_timeline(read_doc(raw), frames=info["complete_frames"], rate=info["sample_rate"])
        if not audit["valid"]:
            raise TimelineError(audit, {"session_id": session_path.name, "source_id": source["id"],
                                       "run_id": run_path.name, "raw_native_relative_to_session": str(raw.relative_to(session_path)),
                                       "raw_native_sha256": sha256_file(raw), "run_manifest_sha256": sha256_file(run_path / "manifest.json"),
                                       "reused_raw_for_rejection_only": True,
                                       "completion_receipt_present": (logs / "complete.json").exists()})


def resolve_source(settings, path, info, resolved, runtime, model, *, on_processing=None, retry_failed=False,
                   on_progress=None, context=None, prepare_only=False, force=False, on_import=None, on_source=None,
                   ownership="managed", verified_stat=None):
    """Reuse one source from any intact run, including a multi-source session."""
    data_root = settings["roots"]["data"]
    existing = []
    for candidate in matching_sessions(data_root, info["sha256"]):
        try:
            metadata = read_doc(candidate / "session.yaml")
            matching = [s for s in metadata["sources"] if s["sha256"] == info["sha256"]
                        and s.get("ownership", "managed") == ownership]
            if ownership == "external_referenced":
                matching = [s for s in matching if s.get("external_path") == str(Path(path).resolve())
                            and isinstance(s.get("external_identity"), dict)
                            and verified_stat is not None
                            and all(verified_stat.get(key) == value
                                    for key, value in s["external_identity"].items())]
            if matching:
                existing.append(candidate)
        except (OSError, ValueError, KeyError, TypeError):
            continue
    def locked(session_path):
        return cancellable_session_lock(session_path, context) if context else session_lock(session_path)
    def cached(value):
        if on_source:
            on_source(value[0]["session_id"], value[0]["source_id"])
        if context:
            context.cache("transcript", True)
            context.mark("source_artifact_ready")
        return PreparedWork(cached_result=WorkResult(value, value[0]["state"])) if prepare_only else value
    failed_candidates = []
    for session_path in ([] if force else existing):
        with locked(session_path):
            session = validate_session(session_path)
            source = next(s for s in session["sources"] if s["sha256"] == info["sha256"])
            if sha256_file(resolve_source_path(session_path, source)) != info["sha256"]:
                raise ValueError("Original failed its hash check; no replacement was made.")
            for mpath in sorted((session_path / "transcript").glob("*/manifest.json"), reverse=True):
                m = read_doc(mpath)
                if info.get("media_decode") and m.get("state") in {"completed", "review_required"}:
                    diagnostics = read_doc(mpath.parent / "diagnostics.json")
                    saved_media = next((d["transform"].get("media_decode") for d in diagnostics["sources"]
                                        if d["source_id"] == source["id"]), None)
                    if not saved_media or saved_media["identity"] != info["media_decode"]["identity"]:
                        continue
                if (effective_settings(m["resolved_config"]) == effective_settings(resolved)
                        and m["model"]["sha256"] == model["sha256"]
                        and m["runtime"]["sha256"] == runtime["runtime"]["sha256"]
                        and compatible_transform_identity(m.get("transform_identity"), resolved["preprocessing"])):
                    covered = any(c.get("source_id") == source["id"] and c.get("source_sha256") == info["sha256"]
                                  for c in m.get("coverage", []))
                    if m.get("state") == "completed" and covered:
                        if not verify_completed_run(mpath.parent):
                            raise ValueError("Completed transcript integrity check failed; prior files were preserved.")
                        if validate_run_timeline(mpath.parent):
                            candidate = read_source_result(session_path, source, mpath.parent, info, reused=True)
                            if not (retry_failed and candidate[0]["state"] == "review_required"):
                                return cached(candidate)
                    if (m.get("state") == "review_required" and not retry_failed
                            and verify_review_run(mpath.parent) and validate_run_quality(mpath.parent)):
                        return cached(read_source_result(session_path, source, mpath.parent, info, reused=True))
                    if m.get("state") == "failed":
                        failed_candidates.append((session_path, source, mpath, m))
    # A newer failed attempt must never hide an older matching successful run.
    for session_path, source, mpath, manifest in ([] if retry_failed else failed_candidates):
        with locked(session_path):
            if read_doc(mpath) != manifest:
                raise ValueError("Stored attempt changed during validation; retry with a stable session.")
            reject_preserved_invalid_timeline(session_path, source, mpath.parent, info,
                                              resolved, runtime, model, manifest)
    if existing:
        # Never transcribe unselected sources merely to create an export.
        singles = [p for p in existing if len(validate_session(p)["sources"]) == 1]
        if not singles:
            if context:
                # Do not silently transcribe unselected members of an old
                # multi-source session. A new single-source import is explicit
                # provenance, and leaves every legacy artifact untouched.
                with context.span("import"):
                    session_path, _, reused = import_cancellable(data_root, path, context, separate=True,
                                                                   ownership=ownership,
                                                                   verified_hash=info["sha256"], verified_stat=verified_stat)
                context.cache("source_import", reused)
                if not reused and on_import:
                    on_import(now())
            else:
                raise ValueError("No matching completed result for this source in its multi-source session. Transcribe that existing session with the requested settings first, then export it.")
        else:
            session_path = singles[0]
    else:
        if context:
            context.emit({"stage": "importing"})
            with context.span("import"):
                session_path, _, reused = import_cancellable(data_root, path, context,
                                                               ownership=ownership,
                                                               verified_hash=info["sha256"], verified_stat=verified_stat)
            context.cache("source_import", reused)
            if not reused and on_import:
                on_import(now())
        else:
            session_path, _, reused = import_sources(data_root, [path], ownership=ownership,
                                                     verified_hashes=[info["sha256"]],
                                                     verified_stats=[verified_stat] if verified_stat else None)
            if not reused and on_import:
                on_import(now())
    if on_source:
        session = validate_session(session_path)
        source = next(s for s in session["sources"] if s["sha256"] == info["sha256"])
        on_source(session_path.name, source["id"])
    if context:
        context.cache("transcript", False)
    if prepare_only:
        prepared = prepare_session(settings, session_path, resolved, runtime=runtime, model=model,
                                   context=context, force=force or retry_failed)
        return PreparedWork(value={"session_path": session_path, "info": info, "prepared": prepared,
                                   "force": force or retry_failed},
                            legacy=len(validate_session(session_path)["sources"]) > 1)
    # The original engine handles per-file gain, native decoding, checkpoints and integrity.
    if on_processing:
        on_processing()
    result = run_session(settings, session_path, resolved, **({"force": True} if force or retry_failed else {}),
                         **({"progress": on_progress} if on_progress else {}),
                         **({"context": context} if context else {}))
    with session_lock(session_path):
        session = validate_session(session_path)
        source = next(s for s in session["sources"] if s["sha256"] == info["sha256"])
        return read_source_result(session_path, source, Path(result["path"]), info,
                                  reused=result.get("reused", False) or result.get("reused_asr", False))


def finish_source(settings, work, resolved, runtime, model, context):
    session_path, info = work["session_path"], work["info"]
    result = run_session(settings, session_path, resolved, force=work["force"],
                         prepared=work["prepared"], runtime=runtime, model=model, context=context,
                         progress=context.progress)
    with cancellable_session_lock(session_path, context), context.span("source_validation"):
        session = validate_session(session_path)
        source = next(s for s in session["sources"] if s["sha256"] == info["sha256"])
        value = read_source_result(session_path, source, Path(result["path"]), info,
                                   reused=result.get("reused", False) or result.get("reused_asr", False))
    return WorkResult(value, value[0]["state"])


def report_lines(entries, contents, language, created_at=None, groups=None):
    completed = sum(e["state"] == "completed" for e in entries)
    review = sum(e["state"] == "review_required" for e in entries)
    failed = sum(e["state"] == "failed" for e in entries)
    cancelled = sum(e["state"] == "cancelled" for e in entries)
    duplicates = sum(e.get("duplicate_of") is not None for e in entries)
    lines = ["# Transcript Report", "",
             "**PARTIAL REPORT — one or more selected recordings failed or were cancelled.**" if failed or cancelled else ("**REVIEW REQUIRED — provisional transcription includes flagged passages.**" if review else "**Complete transcript compilation.**"),
             "", f"Selected files: {len(entries)} · Completed: {completed} · Review required: {review} · Failed: {failed} · Cancelled: {cancelled}",
             f"Created: {created_at}" if created_at else "",
             f"Language: {language} (English). Machine transcription; accuracy review pending.", "",
             "Timestamps restart at zero for each source. File order does not imply a shared lecture, speaker or continuous real-world timeline.",
             "", "All stored ASR text is retained. Existing listening-review flags remain separate from the raw transcript.", ""]
    yield from lines
    if duplicates:
        yield from [f"Identical selections: {duplicates}. Each is retained as its own numbered entry, reusing the same verified transcript without extra inference.", ""]
    group_starts = {group["indices"][0]: group for group in (groups or [])}
    for source_index, (entry, segments) in enumerate(zip(entries, contents)):
        if source_index in group_starts:
            group = group_starts[source_index]
            yield from [f"# {markdown_text(group['title'])}", "",
                        f"Recording date: {group['date'] or 'unknown'} · Grouping: {'owner confirmed' if group.get('confirmed') else 'unconfirmed compilation'}.", ""]
        yield from [f"## {entry['position']}. {markdown_text(entry['filename'])}", ""]
        clock = entry.get("recording_time", recording_time(entry["filename"]))
        yield from [f"Recording clock: {clock['recorded_at'] or 'unknown'} · Evidence: {clock['provenance']} · Timezone: {clock['timezone'] or 'unknown'}.", ""]
        duration = entry.get("duration_seconds")
        yield from [f"Measured duration: {timestamp(duration)} ({duration:.6f} seconds)." if duration is not None else "Measured duration: unavailable.", ""]
        if entry["state"] == "cancelled":
            yield from ["**CANCELLED — this selection was cancelled; already saved source artifacts remain preserved.**", ""]
            continue
        if entry["state"] == "failed":
            yield from ["**FAILED — no transcript is included for this selected source.**", "",
                      markdown_text(entry["failure_message"]), ""]
            continue
        if entry["state"] == "review_required":
            quality = entry.get("transcript", {}).get("quality", {})
            reasons = quality.get("reasons", [])
            yield from ["**REVIEW REQUIRED — provisional text is shown below; flagged timestamps or repeated passages must be checked against the audio.**", "",
                        "Review signals: " + ", ".join(markdown_text(reason.replace("_", " ")) for reason in reasons) + ".", ""]
            for finding in quality.get("findings", []):
                message = finding.get("message") or finding['category'].replace('_', ' ')
                yield f"- Review [{timestamp(finding['start_seconds'])} – {timestamp(finding['end_seconds'])}]: {markdown_text(message)}."
            yield ""
        if entry.get("duplicate_of") is not None:
            yield from [f"Identical audio to selection {entry['duplicate_of']}; its complete transcript is intentionally repeated here.", ""]
        if entry.get("transcript", {}).get("timestamp_normalizations"):
            yield from ["Timestamp warning: the final decoder tick enclosing the measured source end was normalized to the exact source boundary. Raw timestamps and the reason are preserved in provenance; text is unchanged.", ""]
        if not segments:
            yield from ["The completed ASR run returned no text segments.", ""]
        for segment in segments:
            yield from [f"[{timestamp(segment['start_seconds'])} – {timestamp(segment['end_seconds'])}] {markdown_text(segment['text'])}", ""]
def report_text(entries, contents, language):
    """Convenience for small callers/tests; publication streams lines to disk."""
    return "\n".join(report_lines(entries, contents, language))


def build_report(settings, paths, resolved, *, progress=None, events=None, retry_failed=False,
                 groups=None, execution=None, coordinator=None):
    if events is None and progress:
        shown = set()
        def waiting_event(value):
            stage = value.get("stage")
            if stage in {"waiting_for_execution", "waiting_for_memory", "waiting_for_session", "waiting_for_import"} and stage not in shown:
                shown.add(stage)
                progress("Waiting: " + stage.replace("_", " ") + ". Ctrl+C cancels this batch.")
        events = waiting_event
    coordinator = coordinator or BatchCoordinator(settings, paths, execution=execution, events=events)
    sampler = ResourceSampler(os.getpid()) if coordinator.policy["resource_sampling"] else None
    if sampler:
        sampler.start()
    try:
        return _build_report(settings, paths, resolved, progress=progress, retry_failed=retry_failed,
                             groups=groups, coordinator=coordinator)
    finally:
        log_root = Path(settings["roots"].get("log", str(Path(settings["roots"]["data"]) / ".performance")))
        if sampler:
            sampler.stop()
            _publish_batch_metrics(settings, coordinator, lambda:
                write_json(log_root / "performance" / (coordinator.batch_id + "-resources.json"), sampler.snapshot()))


def build_independent(settings, paths, resolved, *, events=None, retry_failed=False, force=False,
                      execution=None, coordinator=None, experimental_parallel=False, input_modes=None,
                      expected_version_keys=None):
    """Publish one directly readable result per input; never group or concatenate.

    The validated coordinator still owns progress, cancellation, duplicate
    computation reuse and recovery. Publication occurs in its per-job validation
    callback, before that job's terminal event, rather than at batch completion.
    An interrupted explicit retranscription retains force=True on restart: it
    starts fresh instead of silently selecting a pre-existing successful run.
    Ordinary first-time jobs retain the existing checkpoint recovery behavior.
    """
    requested = dict(execution or {})
    if type(experimental_parallel) is not bool:
        raise ValueError("experimental_parallel must be boolean.")
    if experimental_parallel:
        requested.setdefault("mode", "pipeline")
        requested.setdefault("asr_workers", 2)
    coordinator = coordinator or BatchCoordinator(settings, paths, execution=requested, events=events)
    input_modes = ["managed"] * len(paths) if input_modes is None else list(input_modes)
    if len(input_modes) != len(paths) or any(mode not in {"managed", "referenced"} for mode in input_modes):
        raise ValueError("Each recording needs a managed or referenced input mode.")
    expected_version_keys = ([None] * len(paths) if expected_version_keys is None
                             else list(expected_version_keys))
    if len(expected_version_keys) != len(paths) or any(
        key is not None and (mode != "referenced" or not isinstance(key, str)
                             or re.fullmatch(r"[0-9a-f]{64}", key) is None)
        for mode, key in zip(input_modes, expected_version_keys)
    ):
        raise ValueError("Watched source version must be a verified referenced-file token.")
    return _build_report(settings, paths, resolved, retry_failed=retry_failed, coordinator=coordinator,
                         independent=True, force=force, input_modes=input_modes,
                         expected_version_keys=expected_version_keys)


def _build_report(settings, paths, resolved, *, progress=None, retry_failed=False, groups=None, coordinator,
                  independent=False, force=False, input_modes=None, expected_version_keys=None):
    if not settings.get("storage_approved") or not paths:
        raise ValueError("Approved storage and at least one recording are required.")
    tokens = []
    try:
        for job in coordinator.jobs:
            job.lifecycle_token = lifecycle.capture(settings, job.job_id, job.path)
            tokens.append(job.lifecycle_token)
        return _build_report_live(settings, paths, resolved, progress=progress, retry_failed=retry_failed,
                                  groups=groups, coordinator=coordinator, independent=independent, force=force,
                                  input_modes=input_modes, expected_version_keys=expected_version_keys)
    finally:
        for token in tokens:
            with contextlib.suppress(ValueError, OSError):
                lifecycle.finish(settings, token)


def _build_report_live(settings, paths, resolved, *, progress=None, retry_failed=False, groups=None, coordinator,
                       independent=False, force=False, input_modes=None, expected_version_keys=None):
    if not settings.get("storage_approved"):
        raise ValueError("Data storage approval is required before importing or exporting recordings.")
    if not paths:
        raise ValueError("Select at least one recording.")
    if not independent:
        normalize_groups([{"filename": Path(p).name} for p in paths], groups)
    coordinator.start()
    telemetry = coordinator.telemetry
    with telemetry.span("runtime_verification"):
        runtime = load_runtime(settings)
        model = model_identity(runtime, resolved["asr"]["model"])
        if type(model.get("bytes")) is int and model["bytes"] > 0:
            coordinator.set_model_bytes(model["bytes"])
    identities = {}
    infos = {}
    published = {}
    compute_identity = {"settings": effective_settings(resolved), "model": model["sha256"],
                        "runtime": runtime["runtime"]["sha256"], "transform": transform_identity()}

    def mode_of(job):
        return input_modes[job.index] if input_modes is not None else "managed"

    def info_key(job):
        mode = mode_of(job)
        return (identities.get(job.index), mode,
                str(Path(job.path).resolve()) if mode == "referenced" else None)

    def same_source(job, identity):
        return (identities.get(job.index) == identity["sha256"]
                and mode_of(job) == identity["input_mode"]
                and (identity["input_mode"] != "referenced"
                     or str(Path(job.path).resolve()) == identity["source_path"]))

    def identify(job):
        lifecycle.assert_live(settings, job.lifecycle_token)
        path = Path(job.path).expanduser().absolute()
        observed = source_stat(path)
        source_hash = sha256_file(path)
        if source_stat(path) != observed:
            raise ValueError("Selected file changed while hashing; retry after copying finishes.")
        mode = mode_of(job)
        reference_path = str(path.resolve()) if mode == "referenced" else None
        expected_version = expected_version_keys[job.index] if expected_version_keys is not None else None
        if expected_version is not None:
            from .watch import version_key
            if version_key(path, source_hash) != expected_version:
                raise ValueError("Watched recording changed after selection; refresh to start its new version.")
        key = digest({"source": source_hash, "input_mode": mode,
                      "reference_path": reference_path, **compute_identity})
        identities[job.index] = source_hash
        return key, {"path": path, "sha256": source_hash, "input_mode": mode,
                     "source_path": reference_path, "verified_stat": observed}

    def prepare(identity, context):
        context.check_cancelled()
        selected_jobs = [job for job in coordinator.jobs
                         if same_source(job, identity) and not job.cancelled]
        context.consumer_jobs = [{"job_id": job.job_id, "attempt_id": job.attempt_id} for job in selected_jobs]
        for job in selected_jobs:
            lifecycle.assert_live(settings, job.lifecycle_token)
        def bind(session_id, source_id):
            for job in selected_jobs:
                job.lifecycle_token = lifecycle.bind_source(settings, job.lifecycle_token, session_id,
                                                             source_id, Path(job.path).name)
        context.emit({"stage": "preparing"})
        with context.span("media_inspection"):
            info = inspect_media(settings, identity["path"], context=context)
        if info["sha256"] != identity["sha256"] or source_stat(identity["path"]) != identity["verified_stat"]:
            raise ValueError("Selected file changed during preparation.")
        infos[(identity["sha256"], identity["input_mode"], identity["source_path"])] = info
        if independent:
            for job in coordinator.jobs:
                if same_source(job, identity) and not job.cancelled:
                    coordinator.emit({"type": "metadata", "job_id": job.job_id,
                                      "attempt_id": job.attempt_id, "index": job.index,
                                      "state": job.state, "stage": "preparing",
                                      "duration_seconds": info["duration_seconds"],
                                      "model": resolved["asr"]["model"]})
        context.check_cancelled()
        with context.span("cache_lookup_and_prepare"):
            return resolve_source(settings, identity["path"], info, resolved, runtime, model,
                                  retry_failed=retry_failed, context=context, prepare_only=True, on_source=bind,
                                  ownership="external_referenced" if identity["input_mode"] == "referenced" else "managed",
                                  verified_stat=identity["verified_stat"],
                                  **({"force": force, "on_import": lambda at: info.update(imported_at=at)}
                                     if independent else {}))

    def execute(work, context):
        return finish_source(settings, work, resolved, runtime, model, context)

    def validate_selection(job, result):
        with telemetry.span("selected_source_recheck", job_id=job.job_id):
            if sha256_file(Path(job.path).expanduser().absolute()) != identities[job.index]:
                raise ValueError("Selected file changed during processing; original results remain preserved.")
        if independent and not job.cancelled:
            from .direct import publish_result
            entry = make_entry(job)
            attach_result(entry, job, result)
            with telemetry.span("direct_result_publish", job_id=job.job_id):
                summary = publish_result(settings, entry, result.value[1], resolved, runtime, model,
                                         info=infos.get(info_key(job)))
            published[job.index] = summary
            telemetry.mark("source_artifact_ready", job_id=job.job_id, index=job.index)
            telemetry.mark("first_readable_report", job_id=job.job_id, index=job.index)

    def make_entry(job):
        path = Path(job.path).expanduser().absolute()
        entry = {"position": job.index + 1, "job_id": job.job_id, "attempt_id": job.attempt_id,
                 "lifecycle_token": job.lifecycle_token,
                 "filename": path.name, "selected_path": str(path),
                 "state": job.state, "sha256": identities.get(job.index), "duration_seconds": None,
                 "duplicate_of": job.duplicate_of}
        if not independent:
            entry["recording_time"] = recording_time(path.name)
        info = infos.get(info_key(job))
        if info:
            entry.update(size_bytes=info["size_bytes"], duration_seconds=info["duration_seconds"])
            if info.get("media_decode"):
                entry["media_decode"] = info["media_decode"]
            if independent and info.get("imported_at"):
                entry["imported_at"] = info["imported_at"]
        return entry

    def attach_result(entry, job, outcome_result):
        result = copy.deepcopy(outcome_result.value[0])
        if independent:
            result["generated_in_current_invocation"] = not result.get("reused_transcript", False)
        if job.duplicate_of is not None:
            result["reused_transcript"] = True
        entry.update(state=result.get("state", "completed"), transcript=result)
        if not independent and result.get("recording_time", {}).get("provenance") != "filename":
            entry["recording_time"] = result.get("recording_time", entry["recording_time"])

    prior_events = coordinator.events
    def terminal_result_event(value):
        # Enrich only the coordinator's real terminal event. A cancellation that
        # wins while publication finishes never receives a false completed event.
        if (value.get("type") == "file" and value.get("state") in {"completed", "review_required"}
                and value.get("index") in published):
            summary = published[value["index"]]
            value = {**value, "result": summary, "result_id": summary["result_id"],
                     "duration_seconds": summary.get("duration_seconds"), "model": summary.get("model")}
        if prior_events:
            prior_events(value)

    if independent:
        coordinator.events = terminal_result_event
    try:
        outcomes = coordinator.run(identify, prepare, execute, validate_result=validate_selection)
    finally:
        coordinator.events = prior_events
    entries, contents = [], []
    for outcome in outcomes:
        job = outcome.job
        path = Path(job.path).expanduser().absolute()
        entry = make_entry(job)
        segments = []
        if job.cancelled or isinstance(outcome.error, OperationCancelled):
            entry["state"] = "cancelled"
            entry.update(reason=job.reason, restart_required=job.restart_required, drained=job.drained)
        elif outcome.error is not None:
            error = outcome.error
            entry.update(state="failed", failure_category=type(error).__name__,
                         failure_message="This file could not be validated or transcribed. Check that the recording is intact, then retry. Originals and completed results were preserved.")
            if isinstance(error, MediaError):
                entry["failure_message"] = str(error)
            elif (isinstance(error, (ResourceAllocationError, MemoryError))
                  or isinstance(error, OSError) and error.errno == errno.ENOMEM):
                entry["failure_message"] = "Decoder could not allocate memory. Completed files are preserved; retry this file explicitly when resources are available."
            elif isinstance(error, FileNotFoundError):
                entry["failure_message"] = "File is missing or unavailable. Reconnect its drive or choose it again."
            if isinstance(error, TimelineError):
                entry.update(failure_message=str(error), timeline_audit=error.audit,
                             preserved_decoder=error.provenance)
        else:
            segments = outcome.result.value[1]
            attach_result(entry, job, outcome.result)
        entries.append(entry)
        contents.append(segments)
        if progress:
            progress(f"{job.index + 1} of {len(paths)}: {path.name} — {entry['state']}")
    if independent:
        counts = {state: sum(e["state"] == state for e in entries)
                  for state in ("completed", "review_required", "failed", "cancelled")}
        state = ("partial" if counts["failed"] or counts["cancelled"] else
                 "review_required" if counts["review_required"] else "completed")
        result = {"state": state, "selected": len(entries), **counts,
                  "results": [{**published[job.index], "job_id": job.job_id, "index": job.index,
                               "attempt_id": job.attempt_id}
                              for job in coordinator.jobs
                              if job.state in {"completed", "review_required"} and job.index in published],
                  "reused_transcripts": sum(e.get("transcript", {}).get("reused_transcript", False) for e in entries)}
        result["interrupted"] = [{key: entry.get(key) for key in
                                  ("job_id", "attempt_id", "state", "reason", "restart_required", "drained")}
                                 for entry in entries if entry["state"] == "cancelled"]
    else:
        with telemetry.span("report_export_and_publish"), lifecycle.lock(settings):
            for job in coordinator.jobs:
                lifecycle.assert_live(settings, job.lifecycle_token)
            result = _publish_report(settings, entries, contents, resolved, runtime, model, groups, coordinator)
        telemetry.mark("first_readable_report")
    telemetry.mark("batch_complete")
    audio_seconds = sum(entry.get("duration_seconds") or 0 for entry in entries)
    log_root = Path(settings["roots"].get("log", str(Path(settings["roots"]["data"]) / ".performance")))
    performance_path = log_root / "performance" / (coordinator.batch_id + ".json")
    metrics_written = _publish_batch_metrics(settings, coordinator,
                                           lambda: telemetry.write(performance_path, audio_seconds=audio_seconds))
    result.update(execution_batch_id=coordinator.batch_id, execution=coordinator.policy,
                  performance_record=str(performance_path) if metrics_written else None)
    if not metrics_written:
        result["performance_record_skipped"] = "recording_deleted_before_final_telemetry"
    return result


def _publish_report(settings, entries, contents, resolved, runtime, model, groups, coordinator):
    grouping = normalize_groups(entries, groups)
    stable_entries = copy.deepcopy(entries)
    for entry in stable_entries:
        entry.pop("job_id", None)
        entry.pop("attempt_id", None)
        entry.pop("lifecycle_token", None)
        if "transcript" in entry:
            entry["transcript"].pop("reused_transcript", None)
    fingerprint = digest({"version": EXPORT_VERSION, "entries": stable_entries,
                          "effective_settings": effective_settings(resolved), "groups": grouping})
    exports = Path(settings["roots"]["data"]) / "exports"
    exports.mkdir(parents=True, exist_ok=True)
    completed = sum(e["state"] == "completed" for e in entries)
    review = sum(e["state"] == "review_required" for e in entries)
    failed = sum(e["state"] == "failed" for e in entries)
    cancelled = sum(e["state"] == "cancelled" for e in entries)
    status = "partial" if failed or cancelled else ("review_required" if review else "completed")
    quality = {"status": "incomplete" if failed or cancelled else ("review_required" if review else "passed_checks"),
               "accuracy_verified": False, "reasons": (["missing_transcripts"] if failed or cancelled else []) + (["flagged_transcripts"] if review else [])}
    reused_count = sum(e.get("transcript", {}).get("reused_transcript", False) for e in entries)
    for mpath in export_manifests(settings["roots"]["data"]):
        try:
            old = read_doc(mpath)
            batch_id = validate_id(old.get("batch_id"), "report ID")
            if batch_id != mpath.parent.name:
                continue
        except (OSError, ValueError):
            # An unrelated damaged export is preserved, not a reason to lose this report.
            continue
        report = mpath.parent / "transcript-report.md"
        if (old.get("fingerprint") == fingerprint and report.is_file()
                and not report_is_trashed(settings["roots"]["data"], batch_id)
                and sha256_file(report) == old.get("report_sha256")):
            coordinator.telemetry.cache("report", True)
            return {"state": status, "report": str(report), "batch_id": mpath.parent.name,
                    "selected": len(entries), "completed": completed, "failed": failed, "cancelled": cancelled,
                    "review_required": review, "quality": quality, "groups": grouping,
                    "reused_transcripts": reused_count, "reused_report": True}
    coordinator.telemetry.cache("report", False)
    batch_id = new_id("batch")
    destination = export_directory(settings["roots"]["data"], grouping)
    destination.mkdir(parents=True, exist_ok=True)
    staging, final = destination / ("." + batch_id + ".pending"), destination / batch_id
    staging.mkdir(mode=0o700)
    report = staging / "transcript-report.md"
    created_at = now()
    with report.open("x", encoding="utf-8") as handle:
        for index, line in enumerate(report_lines(entries, contents, resolved["asr"]["language"], created_at, grouping)):
            if index:
                handle.write("\n")
            handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())
    write_json(staging / "manifest.json", {
        "schema_version": EXPORT_VERSION, "batch_id": batch_id, "fingerprint": fingerprint,
        "created_at": created_at, "state": status, "selected": len(entries), "completed": completed,
        "failed": failed, "cancelled": cancelled, "language": resolved["asr"]["language"], "accuracy_acceptance": "pending",
        "review_required": review, "quality": quality, "groups": grouping,
        "ordered_sources": entries, "resolved_config": resolved,
        "execution": coordinator.policy, "execution_batch_id": coordinator.batch_id,
        "model_sha256": model["sha256"], "runtime_sha256": runtime["runtime"]["sha256"],
        "duplicate_policy": "Retain all selected entries; reuse the same transcript for identical audio.",
        "timestamp_basis": "source-relative; restart for every numbered source",
        "text_policy": "All stored ASR segments, in order; Markdown escaping only; no LLM or text editing.",
        "report_sha256": sha256_file(report)})
    os.rename(staging, final)
    descriptor = os.open(destination, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    rebuild_index(settings)
    return {"state": status, "report": str(final / report.name), "batch_id": batch_id,
            "selected": len(entries), "completed": completed, "failed": failed, "cancelled": cancelled,
            "review_required": review, "quality": quality, "groups": grouping,
            "reused_transcripts": reused_count, "reused_report": False}
