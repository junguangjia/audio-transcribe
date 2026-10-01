"""Central, source-relative benchmarks. Hypotheses stay in local review files."""
from __future__ import annotations

import copy
import contextlib
import difflib
import json
from pathlib import Path
import shutil
import time

from .audio import slice_pcm16
from .engine import (decode, decoder_segments, derivative, digest, load_runtime,
                     model_identity, new_id, now, output_hashes, verify_hashes, transform_identity,
                     verified_artifact, assembly_identity)
from .evaluation import timestamp, word_error_rate
from .storage import (locate_session, read_doc, session_lock, sha256_file, write_json,
                      write_yaml, validate_session)

EXPECTED_SOURCE_SHA256 = "97efb490c10fc2a066d7cca60bfa17e984e4bcd5b88c811322491cf8c7aab132"


def run_benchmark(settings: dict, session_path: Path, resolved: dict, *, intervals=None,
                  pilot=False, force=False) -> dict:
    from .execution import ExecutionOwner, OperationContext, cancellable_session_lock
    with ExecutionOwner(settings) as owner:
        context = OperationContext(owner)
        with cancellable_session_lock(session_path, context):
            return _run_benchmark_locked(settings, session_path, resolved, intervals=intervals,
                                         pilot=pilot, force=force, context=context)


def _run_benchmark_locked(settings: dict, session_path: Path, resolved: dict, *, intervals=None,
                          pilot=False, force=False, context=None) -> dict:
    runtime = load_runtime(settings)
    session = validate_session(session_path)
    if intervals is None:
        if len(session["sources"]) != 1 or session["sources"][0]["sha256"] != EXPECTED_SOURCE_SHA256:
            raise ValueError("For this recording provide explicit --interval SOURCE_ID:START:END values in seconds.")
        source = session["sources"][0]
        intervals = [(source["id"], 90.0, 110.0)] if pilot else [
            (source["id"], 90.0, 210.0), (source["id"], 230.0, 260.0)]
    source_by_id = {s["id"]: s for s in session["sources"]}
    clips = []
    for i, (source_id, start, end) in enumerate(intervals, 1):
        if source_id not in source_by_id or not 0 <= start < end:
            raise ValueError("Benchmark intervals must refer to a managed source and increasing nonnegative seconds.")
        clips.append({"id": f"clip-{i}", "session_id": session["id"], "source_id": source_id,
                      "source_sha256": source_by_id[source_id]["sha256"],
                      "start_seconds": start, "end_seconds": end,
                      "reference_status": "not_provided", "WER": None})
    definition = {"schema_version": 1, "purpose": "pilot" if pilot else "bounded_comparison",
                  "clips": clips, "timestamp_basis": "source-relative audio seconds",
                  "reference_status": "not_provided", "accuracy_acceptance": "pending"}
    bench_id = ("pilot-" if pilot else "benchmark-") + digest(definition)[:20]
    root = Path(settings["roots"]["data"]) / "benchmarks" / bench_id
    root.mkdir(parents=True, exist_ok=True)
    if not (root / "benchmark.yaml").exists():
        write_yaml(root / "benchmark.yaml", {"id": bench_id, **definition})
    candidates = [("large-v3-A", "large-v3", "A")]
    if not pilot:
        if all(c["source_sha256"] == EXPECTED_SOURCE_SHA256 for c in clips):
            candidates.append(("large-v3-B", "large-v3", "B"))
        candidates.append(("large-v3-turbo-A", "large-v3-turbo", "A"))
    model_map = {name: model_identity(runtime, name) for name in dict.fromkeys(m for _, m, _ in candidates)}
    input_identity = {"definition": definition, "config": resolved, "models": model_map,
                      "transform_identity": transform_identity(),
                      "assembly_identity": assembly_identity(), "benchmark_implementation_sha256": sha256_file(Path(__file__)),
                      "runtime": runtime["runtime"]}
    fingerprint = digest(input_identity)
    eval_root = root / "evaluations"
    eval_root.mkdir(exist_ok=True)
    evaluation = None
    if not force:
        for path in sorted(eval_root.glob("*/manifest.json"), reverse=True):
            old = read_doc(path)
            if old.get("fingerprint") != fingerprint:
                continue
            if old.get("state") == "completed":
                hashes = old.get("output_hashes", {})
                if not verify_hashes(path.parent, hashes):
                    raise ValueError("Benchmark outputs changed; preserve and use --force for a fresh evaluation.")
                return {"benchmark_id": bench_id, "evaluation_id": path.parent.name,
                        "path": str(path.parent), "reused": True, "state": "completed"}
            if evaluation is None:
                evaluation = path.parent
    evaluation = evaluation or eval_root / new_id("evaluation")
    evaluation.mkdir(exist_ok=True)
    manifest_path = evaluation / "manifest.json"
    manifest = {"schema_version": 1, "fingerprint": fingerprint, "state": "running",
                "started_at": now(), "inputs": input_identity, "results": [],
                "reference_status": "not_provided", "WER": None,
                "preference": {"model": "large-v3", "candidate": "A", "state": "provisional",
                               "basis": "user-requested primary baseline; no measured accuracy preference"}}
    write_json(manifest_path, manifest, overwrite=manifest_path.exists())
    begun = time.monotonic()
    try:
        with contextlib.nullcontext():
            for candidate_name, model_name, policy_name in candidates:
                policy = copy.deepcopy(resolved["preprocessing"])
                policy.update({"candidate": policy_name, "gain_db": 21.0 if policy_name == "B" else None})
                if policy_name == "B" and any(c["source_sha256"] != EXPECTED_SOURCE_SHA256 for c in clips):
                    raise ValueError("Candidate B gain 21 dB is scoped to the verified quiet sample; define an evaluated policy before using B elsewhere.")
                asr = copy.deepcopy(resolved["asr"])
                asr["model"] = model_name
                for clip in clips:
                    source = source_by_id[clip["source_id"]]
                    wav_path, transform = derivative(session_path, source, policy, settings=settings, context=context)
                    output_dir = evaluation / candidate_name / clip["id"]
                    output_dir.mkdir(parents=True, exist_ok=True)
                    excerpt = output_dir / "excerpt.wav"
                    excerpt_meta = output_dir / "excerpt.json"
                    if excerpt_meta.exists():
                        slicing = read_doc(excerpt_meta)
                        if not excerpt.is_file() or sha256_file(excerpt) != slicing["output_sha256"]:
                            raise ValueError("Benchmark excerpt integrity failed; preserve and start --force.")
                    else:
                        if excerpt.exists():
                            excerpt.rename(output_dir / (new_id("interrupted-excerpt") + ".wav"))
                        slicing = slice_pcm16(wav_path, excerpt, clip["start_seconds"], clip["end_seconds"])
                        write_json(excerpt_meta, slicing)
                    metadata = decode(settings, runtime, model_map[model_name], excerpt,
                                      output_dir / "logs", asr, resolved["glossary"], context=context)
                    native = read_doc(output_dir / "logs" / "native.json")
                    segments = decoder_segments(native, clip["source_id"], metadata["input_duration_seconds"],
                                                source_start=slicing["timestamp_offset_seconds"])
                    hypothesis = "\n".join(s["text"] for s in segments)
                    result = {"candidate": candidate_name, "clip": clip, "transform": transform, "slicing": slicing,
                              "decoder": metadata, "hypothesis_path": str((output_dir / "hypothesis.txt").relative_to(evaluation)),
                              "segments": segments, "evaluation": word_error_rate(None, hypothesis),
                              "technical_term_errors": {"reviewed": False, "errors": None, "denominator": None},
                              "important_condition_errors": {"reviewed": False, "errors": None, "denominator": None}}
                    preserve_write(output_dir / "hypothesis.txt", hypothesis + "\n")
                    result_path = output_dir / "result.json"
                    if result_path.exists() and read_doc(result_path) != result:
                        result_path.rename(output_dir / (new_id("previous-result") + ".json"))
                    if not result_path.exists():
                        write_json(result_path, result)
                    manifest["results"].append({"candidate": candidate_name, "clip_id": clip["id"],
                                                "result_path": str((output_dir / "result.json").relative_to(evaluation)),
                                                "elapsed_seconds": metadata["elapsed_seconds"],
                                                "real_time_factor": metadata["real_time_factor"], "WER": None})
                    write_json(manifest_path, manifest, overwrite=True)
        write_comparison(evaluation, manifest, clips)
        manifest.update({"state": "completed", "completed_at": now(),
                         "wall_seconds_this_attempt": time.monotonic() - begun,
                         "inference_elapsed_seconds": sum(r["elapsed_seconds"] for r in manifest["results"]),
                         "output_hashes": output_hashes(evaluation)})
        write_json(manifest_path, manifest, overwrite=True)
        return {"benchmark_id": bench_id, "evaluation_id": evaluation.name,
                "path": str(evaluation), "state": "completed", "reused": False,
                "inference_elapsed_seconds": manifest["inference_elapsed_seconds"]}
    except BaseException as error:
        manifest.update({"state": "interrupted" if isinstance(error, KeyboardInterrupt) else "failed",
                         "failure_category": type(error).__name__})
        write_json(manifest_path, manifest, overwrite=True)
        raise


def preserve_write(path: Path, text: str):
    if path.exists():
        if path.read_text(encoding="utf-8") == text:
            return
        path.rename(path.with_name(new_id("previous-" + path.stem[:20]) + path.suffix))
    with path.open("x", encoding="utf-8") as handle:
        handle.write(text)


def write_comparison(evaluation: Path, manifest: dict, clips: list):
    if (evaluation / "review.md").exists():
        (evaluation / "review.md").rename(evaluation / (new_id("previous-review") + ".md"))
    with (evaluation / "review.md").open("w", encoding="utf-8") as handle:
        handle.write("# Bounded comparison: listening required\n\nReference status: not_provided. WER: null. All hypotheses are raw ASR. Runtime and agreement do not establish accuracy.\n\n")
        handle.write("Default preference remains provisional large-v3 / A as explicitly requested. No profile is promoted. Source times are in audio time, not wall-clock time.\n\n")
        for clip in clips:
            handle.write(f"## {clip['id']}: {clip['source_id']} {timestamp(clip['start_seconds'])}–{timestamp(clip['end_seconds'])}\n\n")
            handle.write("| Candidate | Runtime (s) | RTF | Fixed gain (dB) | WER |\n|---|---:|---:|---:|---|\n")
            results = [read_doc(evaluation / r["result_path"]) for r in manifest["results"] if r["clip_id"] == clip["id"]]
            for r in results:
                handle.write(f"| {r['candidate']} | {r['decoder']['elapsed_seconds']:.2f} | {r['decoder']['real_time_factor']:.3f} | {r['transform'].get('gain_db')} | unmeasured |\n")
            handle.write("\n### Raw hypotheses by source interval\n\n")
            # A side-by-side local HTML table complements sequential Markdown segments.
            for r in results:
                handle.write(f"#### {r['candidate']}\n\n")
                for s in r["segments"]:
                    handle.write(f"[{timestamp(s['start_seconds'])}–{timestamp(s['end_seconds'])}] {s['text']}\n\n")
            if len(results) > 1:
                baseline = (evaluation / results[0]["hypothesis_path"]).read_text()
                for other in results[1:]:
                    hypothesis = (evaluation / other["hypothesis_path"]).read_text()
                    ratio = difflib.SequenceMatcher(None, baseline.split(), hypothesis.split()).ratio()
                    handle.write(f"- {results[0]['candidate']} vs {other['candidate']}: token sequence agreement {ratio:.3f} (disagreement flag only, not accuracy). Listen to the entire referenced clip, especially differences in technical terms, quantities and conditions.\n")
                    # difflib escapes text in its HTML output. No external resources.
                    html = difflib.HtmlDiff(wrapcolumn=70).make_file(baseline.splitlines(), hypothesis.splitlines(),
                                      fromdesc=results[0]["candidate"], todesc=other["candidate"], context=False)
                    preserve_write(evaluation / f"{clip['id']}-{other['candidate']}-comparison.html", html)
            handle.write("\nProcessing metrics and limiter activity are preserved in each result.json. Whole-file RMS is not SNR or a speech-intelligibility score.\n\n")


def evaluate_reference(settings: dict, benchmark_id: str, evaluation_id: str, clip_id: str,
                       reference_path: Path, *, verified=False) -> dict:
    from .config import validate_id
    for identifier in (benchmark_id, evaluation_id, clip_id):
        validate_id(identifier)
    root = Path(settings["roots"]["data"]) / "benchmarks" / benchmark_id
    definition = read_doc(root / "benchmark.yaml")
    clip = next((c for c in definition["clips"] if c["id"] == clip_id), None)
    if clip is None:
        raise ValueError("Unknown benchmark clip.")
    session = locate_session(Path(settings["roots"]["data"]), clip["session_id"])
    current = read_doc(session / "session.yaml")
    source = next(s for s in current["sources"] if s["id"] == clip["source_id"])
    if source["sha256"] != clip["source_sha256"]:
        raise ValueError("Benchmark source identity changed.")
    ref_hash = sha256_file(reference_path)
    eval_path = root / "evaluations" / evaluation_id
    manifest = read_doc(eval_path / "manifest.json")
    if manifest.get("state") != "completed" or not verify_hashes(eval_path, manifest.get("output_hashes", {})):
        raise ValueError("Only intact completed benchmark outputs can be evaluated.")
    output = root / "references" / new_id("reference")
    output.mkdir(parents=True)
    shutil.copyfile(reference_path, output / "reference.txt")
    if sha256_file(output / "reference.txt") != ref_hash:
        raise ValueError("Reference copy integrity check failed.")
    reference = (output / "reference.txt").read_text(encoding="utf-8")
    scores = []
    for result in manifest["results"]:
        if result["clip_id"] == clip_id:
            run_root = root / "evaluations" / evaluation_id
            entry = read_doc(verified_artifact(run_root, result["result_path"], manifest["output_hashes"]))
            hypothesis_path = verified_artifact(run_root, entry["hypothesis_path"], manifest["output_hashes"])
            hypothesis = hypothesis_path.read_text()
            scores.append({"candidate": result["candidate"], "hypothesis_sha256": sha256_file(hypothesis_path),
                           **word_error_rate(reference, hypothesis, verified=verified)})
    record = {"schema_version": 1, "clip": clip, "evaluation_id": evaluation_id,
              "reference_sha256": ref_hash, "reference_status": "verified" if verified else "unverified",
              "verification_provenance": "explicit user --verified assertion" if verified else None,
              "reference_path": "reference.txt", "created_at": now(), "scores": scores}
    write_json(output / "evaluation.json", record)
    return {"path": str(output), "reference_status": record["reference_status"], "scores": scores}
