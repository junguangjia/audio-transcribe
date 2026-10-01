"""Small explicit CLI and native file-picker workflow."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import re
import signal
import shutil
import subprocess
import sys
import select
import threading

from .audio import inspect_audio
from .media import inspect_media
from .benchmark import evaluate_reference, run_benchmark
from .config import create_profile, list_profiles, load_settings, resolve_config
from .engine import load_runtime, now, run_session, verify_completed_run
from .export import build_independent, build_report, natural_order
from .library import library_request
from .scheduler import BatchCoordinator
from .execution import ExecutionOwner, OperationContext
from .storage import (archive_session, import_sources, locate_session, read_doc,
                      restore_session, session_lock, validate_id, write_json, write_yaml)


def parser():
    root = argparse.ArgumentParser(prog="audio-transcribe", description="Local session-based English transcription; no cloud inference.")
    root.add_argument("--settings", help="Machine-local JSON/YAML settings path (or AUDIO_TRANSCRIBE_SETTINGS).")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="Inspect configured roots and local runtime without importing audio.")
    configure = commands.add_parser("configure", help="Persist an explicitly chosen storage root once.")
    configure.add_argument("--data-root", required=True)
    configure.add_argument("--storage-mode", choices=["local_alternative", "explicit_synced", "verified_local"], required=True)
    transcribe = commands.add_parser("transcribe", help="Import originals and transcribe, or reuse verified completed work.")
    transcribe.add_argument("files", nargs="*")
    transcribe.add_argument("--session", help="Process a managed session by stable ID.")
    transcribe.add_argument("--speaker", help="Manual profile ID; use '-' to clear a session assignment.")
    transcribe.add_argument("--capture", help="Manual profile ID; use '-' to clear a session assignment.")
    transcribe.add_argument("--glossary", help="Manual glossary ID; use '-' for empty vocabulary.")
    transcribe.add_argument("--model", choices=["large-v3", "large-v3-turbo"])
    transcribe.add_argument("--channel", choices=["mean", "left", "right"])
    transcribe.add_argument("--force", action="store_true", help="Create a new run; never replace earlier raw ASR.")
    transcribe.add_argument("--separate", action="store_true", help="Explicitly import a separate session even if bytes already exist.")
    transcribe.add_argument("--order-confirmed", action="store_true", help="Use multi-file positional argument order as explicitly confirmed audio order.")
    transcribe.add_argument("--import-only", action="store_true", help="Import for inspection/pilot without inference.")
    report = commands.add_parser("report", help="Transcribe/reuse selected files separately and compile one complete Markdown report.")
    report.add_argument("files", nargs="+")
    report.add_argument("--order-confirmed", action="store_true", help="Use the explicitly confirmed positional file order.")
    for kind in ("speaker", "capture", "glossary"):
        report.add_argument("--" + kind)
    report.add_argument("--model", choices=["large-v3", "large-v3-turbo"])
    report.add_argument("--channel", choices=["mean", "left", "right"])
    report.add_argument("--retry-failed", action="store_true", help="Retry failed inference while reusing intact successful sources.")
    report.add_argument("--execution", choices=["auto", "serial", "pipeline"], help="Auto uses the measured preset with resource admission; serial uses one worker.")
    report.add_argument("--asr-workers", type=int, choices=range(1, 10), help="Explicit capacity from 1 to 9; actual admission remains resource-bounded.")
    app_report = commands.add_parser("app-report", help="Native app bridge: ordered request JSON in, progress JSON lines out.")
    app_report.add_argument("--request", required=True)
    app_library = commands.add_parser("app-library", help="Local report library JSON bridge; no transcription or network access.")
    app_library.add_argument("--request", required=True)
    app_transcribe = commands.add_parser("app-transcribe", help="Native app bridge: independent recordings with immediately readable results.")
    app_transcribe.add_argument("--request", required=True)
    app_results = commands.add_parser("app-results", help="Local independent-result JSON bridge; no transcription or network access.")
    app_results.add_argument("--request", required=True)
    for name in ("benchmark", "pilot"):
        bench = commands.add_parser(name, help="Run an explicit bounded comparison." if name == "benchmark" else "Measure a short primary-model pilot before full transcription.")
        bench.add_argument("session_id")
        bench.add_argument("--interval", action="append", help="SOURCE_ID:START_SECONDS:END_SECONDS; repeat for each exact interval.")
        bench.add_argument("--speaker")
        bench.add_argument("--capture")
        bench.add_argument("--glossary")
        bench.add_argument("--force", action="store_true")
    for name in ("archive", "restore", "inspect"):
        cmd = commands.add_parser(name)
        cmd.add_argument("session_id")
    accept = commands.add_parser("accept", help="Record your explicit accuracy acceptance after listening/review.")
    accept.add_argument("session_id")
    accept.add_argument("run_id")
    profiles = commands.add_parser("profile")
    profile_cmds = profiles.add_subparsers(dest="profile_command", required=True)
    for name in ("create", "list"):
        p = profile_cmds.add_parser(name)
        p.add_argument("kind", choices=["speaker", "capture", "glossary"])
        if name == "create":
            p.add_argument("id")
            p.add_argument("--label")
    reference = commands.add_parser("evaluate", help="Evaluate a supplied reference for one exact benchmark clip; unverified drafts never produce WER.")
    reference.add_argument("benchmark_id")
    reference.add_argument("evaluation_id")
    reference.add_argument("clip_id")
    reference.add_argument("reference")
    reference.add_argument("--verified", action="store_true", help="Assert that this is human-verified text for exactly this clip.")
    commands.add_parser("launch", help="Native file chooser and optional profiles; cancellation is a clean exit.")
    return root


def emit(value):
    print(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False), flush=True)


def require_storage(settings):
    if not settings["storage_approved"]:
        raise ValueError("Data storage is not approved. Check Documents/iCloud, then run configure --data-root PATH --storage-mode local_alternative (or explicit_synced after consent). No audio was imported.")


def configure_storage(settings, data_root, storage_mode):
    path = Path(settings["settings_path"])
    if path.exists():
        raise FileExistsError("Settings already exist; they were preserved. Use an explicit alternate --settings path for a deliberate configuration change.")
    roots = dict(settings["roots"])
    roots["data"] = str(Path(data_root).expanduser().resolve())
    if Path(roots["data"]).is_relative_to(Path(roots["code"])):
        raise ValueError("Permanent audio must not live inside CODE_ROOT.")
    value = {"schema_version": 1, "roots": roots, "runtime": {}, "models": {},
             "storage_approval": {"data_root": roots["data"], "mode": storage_mode, "approved_at": now()}}
    write_json(path, value)
    emit({"settings": str(path), "roots": roots, "storage_approval": value["storage_approval"]})


def doctor(settings):
    result = {"settings": settings["settings_path"], "settings_exists": settings["settings_exists"],
              "roots": settings["roots"], "storage_approved": settings["storage_approved"],
              "process_architecture": platform.machine(), "python": sys.version.split()[0],
              "free_bytes": shutil.disk_usage(Path(settings["roots"]["code"])).free,
              "network": "No application network operations. Downloads are a separate explicit installer.",
              "legacy_migration": "No automatic legacy deletion or relocation."}
    try:
        runtime = load_runtime(settings)
        result.update({"runtime": runtime["runtime"], "hardware": runtime.get("hardware"),
                       "models": runtime["models"]})
    except (OSError, ValueError) as error:
        result["runtime_problem"] = str(error)
    emit(result)


def assigned_config(settings, args, session=None):
    overrides = {}
    if getattr(args, "model", None):
        overrides["asr"] = {"model": args.model}
    if getattr(args, "channel", None):
        overrides["preprocessing"] = {"channel": args.channel}
    selected = {}
    for kind in ("speaker", "capture", "glossary"):
        explicit = getattr(args, kind, None)
        selected[kind] = (session or {}).get("profiles", {}).get(kind) if explicit is None else explicit
        if selected[kind] == "-":
            selected[kind] = None
    return resolve_config(settings["roots"]["data"], **selected, overrides=overrides)


def confirm_cli_order(files):
    if not sys.stdin.isatty():
        raise ValueError("Multiple files require --order-confirmed in the intended positional order, or use the native launcher.")
    print("Concatenated audio order (unknown real-world gaps will not be inferred):", flush=True)
    for i, path in enumerate(files, 1):
        print(f"{i}. {path}", flush=True)
    if input("Use this exact source order? [y/N] ").strip().lower() not in {"y", "yes"}:
        return False
    return True


def transcribe(settings, args):
    def waiting(detail):
        print("Waiting for AudioTranscribe execution ownership; Ctrl+C cancels this task.", file=sys.stderr, flush=True)
    with ExecutionOwner(settings, progress=waiting) as owner:
        return _transcribe_owned(settings, args, OperationContext(owner))


def _transcribe_owned(settings, args, context):
    require_storage(settings)
    if args.session:
        if args.files or args.separate:
            raise ValueError("Use --session or input files, not both.")
        session_path = locate_session(settings["roots"]["data"], args.session)
        session = read_doc(session_path / "session.yaml")
        reused = True
        resolved = assigned_config(settings, args, session)
    else:
        resolved = assigned_config(settings, args)
        if not args.files:
            raise ValueError("Select at least one audio file, provide --session ID, or run launch.")
        if len(args.files) > 1 and not args.order_confirmed:
            if not confirm_cli_order(args.files):
                return {"cancelled": True}
            args.order_confirmed = True
        # Validate every input before copying anything into permanent session storage.
        for path in args.files:
            inspect_media(settings, Path(path).expanduser(), context=context)
        session_path, session, reused = import_sources(settings["roots"]["data"], args.files,
                                                      separate=args.separate, order_confirmed=args.order_confirmed)
    emit({"session_id": session["id"], "session_path": str(session_path), "sources": len(session["sources"]),
          "existing_session": reused, "profiles": resolved["selected_profiles"],
          "model": resolved["asr"]["model"], "preprocessing": resolved["preprocessing"],
          "glossary_terms": len(resolved["glossary"]["terms"]), "accuracy_acceptance": "pending"})
    if args.import_only:
        with session_lock(session_path):
            session["profiles"] = resolved["selected_profiles"]
            write_yaml(session_path / "session.yaml", session, overwrite=True)
        return {"session_id": session["id"], "path": str(session_path), "state": "imported", "reused": reused}
    return run_session(settings, session_path, resolved, force=args.force, context=context)


def native_dialog(code_root: Path, payload: dict) -> dict:
    result = subprocess.run(["/usr/bin/osascript", "-l", "JavaScript", str(code_root / "scripts" / "native-dialog.js"),
                             json.dumps(payload, ensure_ascii=False)], capture_output=True, text=True)
    if result.returncode:
        # Never echo dialog stderr: it may contain user input.
        raise RuntimeError("Native dialog failed or macOS denied permission; no files were imported.")
    value = json.loads(result.stdout)
    if not isinstance(value, dict):
        raise ValueError("Unexpected native dialog response.")
    return value


def report_command(settings, args):
    require_storage(settings)
    paths = list(args.files)
    if len(paths) > 1 and not args.order_confirmed:
        if not sys.stdin.isatty():
            raise ValueError("Multiple files require --order-confirmed in the desired argument order, or use the native launcher.")
        paths = natural_order(paths)
        print("Proposed file order (timestamps restart for each file):", flush=True)
        for i, path in enumerate(paths, 1):
            print(f"{i}. {path}", flush=True)
        answer = input("Enter y to confirm, row numbers in the desired order (e.g. 2,1,3), or Enter to cancel: ").strip()
        if not answer:
            return {"cancelled": True, "imported": False}
        if answer.lower() not in {"y", "yes"}:
            try:
                order = [int(p) for p in answer.replace(",", " ").split()]
            except ValueError:
                raise ValueError("Order must contain each displayed row number once.") from None
            if sorted(order) != list(range(1, len(paths) + 1)):
                raise ValueError("Order must contain each displayed row number once.")
            paths = [paths[n - 1] for n in order]
    resolved = assigned_config(settings, args)
    execution = {}
    if getattr(args, "execution", None):
        execution["mode"] = args.execution
    if getattr(args, "asr_workers", None):
        execution["asr_workers"] = args.asr_workers
        execution.setdefault("mode", "pipeline")
    return build_report(settings, paths, resolved, progress=lambda message: print(message, flush=True),
                        **({"retry_failed": True} if getattr(args, "retry_failed", False) else {}),
                        **({"execution": execution} if execution else {}))


def launch(settings):
    require_storage(settings)
    data_root, code_root = Path(settings["roots"]["data"]), Path(settings["roots"]["code"])
    payload = {"action": "choose", "profiles": {
        kind: [{"id": p["id"], "label": p.get("label")} for p in list_profiles(data_root, kind)]
        for kind in ("speaker", "capture", "glossary")}}
    selection = native_dialog(code_root, payload)
    if selection.get("cancelled"):
        return {"cancelled": True, "imported": False}
    for profile in selection.get("new_profiles", []):
        create_profile(data_root, profile["kind"], profile["id"], profile.get("label"))
    profiles = selection["profiles"]
    args = argparse.Namespace(files=selection["files"], session=None, force=False, separate=False,
                              order_confirmed=True, import_only=False, model=None, channel=None, **profiles)
    result = report_command(settings, args)
    if result.get("report"):
        subprocess.run(["/usr/bin/open", "-R", result["report"]], check=True)
    return result


def app_report_command(settings, request_path):
    """No terminal dialogs, no transcript text on the native UI protocol."""
    output_lock = threading.Lock()
    def event(value):
        with output_lock:
            print(json.dumps(value, ensure_ascii=False, allow_nan=False), flush=True)
    coordinator = None
    reader = None
    try:
        request = read_doc(Path(request_path))
        paths = request.get("files")
        if not isinstance(paths, list) or not paths or any(not isinstance(p, str) or not Path(p).is_absolute() for p in paths):
            raise ValueError("Choose at least one local recording.")
        coordinator = BatchCoordinator(settings, paths, execution=request.get("execution"),
                                       items=request.get("items"), batch_id=request.get("batch_id"), events=event,
                                       submitted_monotonic=request.get("submitted_monotonic"))
        if request.get("protocol_version") == 2:
            coordinator.start()
            reader = ControlReader(coordinator, sys.stdin)
            reader.start()
        result = build_report(settings, paths, resolve_config(Path(settings["roots"]["data"])), events=event,
                              retry_failed=request.get("retry_failed") is True,
                              coordinator=coordinator,
                              **({"groups": request["groups"]} if "groups" in request else {}))
        coordinator.emit({"type": "result", **result, "report_id": result.get("batch_id")})
        return 1 if result["state"] == "partial" else 0
    except KeyboardInterrupt:
        if coordinator:
            coordinator.cancel_batch()
        (coordinator.emit if coordinator else event)({"type": "cancelled", "state": "cancelled", "stage": "cancelled",
                "message": "Cancelled. Completed files are preserved. Click Transcribe to resume."})
        return 130
    except (ValueError, OSError, RuntimeError, KeyError, TypeError):
        (coordinator.emit if coordinator else event)({"type": "error", "state": "failed", "stage": "finished",
                "message": "Processing could not start or finish. Check the files and available disk space, then retry. Local logs are preserved."})
        return 2
    finally:
        if reader:
            reader.close()


def app_transcribe_command(settings, request_path):
    """Normal app workflow: independent files, explicit model, no grouping UI."""
    output_lock = threading.Lock()
    def event(value):
        with output_lock:
            print(json.dumps(value, ensure_ascii=False, allow_nan=False), flush=True)
    coordinator = None
    reader = None
    try:
        require_storage(settings)
        request = read_doc(Path(request_path))
        if not isinstance(request, dict):
            raise ValueError("Invalid transcription request.")
        allowed = {"protocol_version", "batch_id", "submitted_monotonic", "files", "items",
                   "model", "force", "retry_failed", "execution", "experimental_parallel", "thermal"}
        if set(request) - allowed:
            raise ValueError("Normal transcription accepts independent files only.")
        paths = request.get("files")
        if not isinstance(paths, list) or not paths or any(not isinstance(p, str) or not Path(p).is_absolute() for p in paths):
            raise ValueError("Choose at least one local recording.")
        for flag in ("force", "retry_failed", "experimental_parallel"):
            if flag in request and type(request[flag]) is not bool:
                raise ValueError("Transcription options must be boolean.")
        model = request.get("model")
        if model is not None and model not in {"large-v3", "large-v3-turbo"}:
            raise ValueError("Choose an installed transcription model.")
        execution = request.get("execution", {})
        experimental = request.get("experimental_parallel") is True
        if not isinstance(execution, dict):
            raise ValueError("Execution preference must be a mapping.")
        execution = dict(execution)
        if experimental:
            execution.setdefault("mode", "pipeline")
            execution.setdefault("asr_workers", 2)
        resolved = resolve_config(Path(settings["roots"]["data"]),
                                  overrides={"asr": {"model": model}} if model else None)
        coordinator = BatchCoordinator(settings, paths, execution=execution, items=request.get("items"),
                                       batch_id=request.get("batch_id"), events=event,
                                       submitted_monotonic=request.get("submitted_monotonic"))
        items = request.get("items")
        input_modes = ([item.get("input_mode", "managed") for item in items]
                       if items is not None else ["managed"] * len(paths))
        if any(mode not in {"managed", "referenced"} for mode in input_modes):
            raise ValueError("Choose managed or referenced input for each recording.")
        expected_version_keys = ([item.get("watched_version_key") for item in items]
                                 if items is not None else [None] * len(paths))
        for mode, key in zip(input_modes, expected_version_keys):
            if key is not None and (mode != "referenced" or not isinstance(key, str)
                                    or re.fullmatch(r"[0-9a-f]{64}", key) is None):
                raise ValueError("Watched source version must be a verified referenced-file token.")
        # Older native clients may include ``thermal``. It is intentionally
        # ignored; temperature observations cannot affect normal execution.
        if request.get("protocol_version") == 2:
            coordinator.start()
            reader = ControlReader(coordinator, sys.stdin)
            reader.start()
        result = build_independent(settings, paths, resolved, events=event,
                                   retry_failed=request.get("retry_failed") is True,
                                   force=request.get("force") is True, coordinator=coordinator,
                                   experimental_parallel=experimental, input_modes=input_modes,
                                   expected_version_keys=expected_version_keys)
        coordinator.emit({"type": "result", **result})
        if result["cancelled"] and not result["failed"]:
            return 130
        return 1 if result["state"] == "partial" else 0
    except KeyboardInterrupt:
        if coordinator:
            coordinator.cancel_batch()
        (coordinator.emit if coordinator else event)({"type": "cancelled", "state": "cancelled", "stage": "cancelled",
                "message": "Cancelled. Completed results are preserved. Resume remaining recordings when ready."})
        return 130
    except (ValueError, OSError, RuntimeError, KeyError, TypeError):
        (coordinator.emit if coordinator else event)({"type": "error", "state": "failed", "stage": "finished",
                "message": "Processing could not start or finish. Check the files and available disk space, then retry. Completed results are preserved."})
        return 2
    finally:
        if reader:
            reader.close()


class ControlReader:
    """Finite, local stdin control channel. EOF means the owning UI went away."""
    def __init__(self, coordinator, stream):
        self.coordinator, self.stream = coordinator, stream
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._run, name="audio-controls", daemon=True)

    def start(self):
        self.thread.start()

    def _run(self):
        pending = b""
        try:
            descriptor = self.stream.fileno()
            while not self.stopped.is_set():
                readable, _, _ = select.select([descriptor], [], [], 0.25)
                if not readable:
                    continue
                chunk = os.read(descriptor, 65536)
                if not chunk:
                    if not self.stopped.is_set():
                        self.coordinator.cancel_batch()
                    return
                pending += chunk
                if len(pending) > 65536:
                    self.coordinator.cancel_batch()
                    return
                lines = pending.split(b"\n")
                pending = lines.pop()
                for line in lines:
                    try:
                        self.coordinator.control(json.loads(line))
                    except (ValueError, TypeError, UnicodeError):
                        continue
        except (OSError, ValueError, AttributeError):
            if not self.stopped.is_set():
                self.coordinator.cancel_batch()

    def close(self):
        self.stopped.set()
        self.thread.join(timeout=1)


def app_library_command(settings, request_path):
    """Read requested local text only on this separate, explicit library channel."""
    try:
        emit(library_request(settings, read_doc(Path(request_path))))
        return 0
    except (ValueError, OSError, RuntimeError, KeyError, TypeError):
        emit({"type": "error", "message": "The local library request could not be completed. Check the selected report or recording metadata."})
        return 2


def app_results_command(settings, request_path):
    """Explicit read/export/label channel, separate from progress events."""
    from .direct import direct_request
    try:
        emit(direct_request(settings, read_doc(Path(request_path))))
        return 0
    except (ValueError, OSError, RuntimeError, KeyError, TypeError):
        emit({"type": "error", "message": "The result request could not be completed. Check the selected result or export location."})
        return 2


def main(argv=None):
    os.umask(0o077)
    def interrupted(signum, frame):
        raise KeyboardInterrupt()
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, interrupted)
    args = parser().parse_args(argv)
    try:
        settings = load_settings(args.settings)
        if args.command == "doctor":
            doctor(settings)
            return 0
        if args.command == "configure":
            configure_storage(settings, args.data_root, args.storage_mode)
            return 0
        require_storage(settings)
        if args.command == "app-report":
            return app_report_command(settings, args.request)
        if args.command == "app-library":
            return app_library_command(settings, args.request)
        if args.command == "app-transcribe":
            return app_transcribe_command(settings, args.request)
        if args.command == "app-results":
            return app_results_command(settings, args.request)
        if args.command == "transcribe":
            result = transcribe(settings, args)
        elif args.command == "report":
            result = report_command(settings, args)
        elif args.command in {"pilot", "benchmark"}:
            session = locate_session(settings["roots"]["data"], args.session_id)
            intervals = None
            if args.interval:
                intervals = []
                for value in args.interval:
                    parts = value.split(":")
                    if len(parts) != 3:
                        raise ValueError("Intervals use SOURCE_ID:START_SECONDS:END_SECONDS.")
                    intervals.append((parts[0], float(parts[1]), float(parts[2])))
            result = run_benchmark(settings, session, assigned_config(settings, args, read_doc(session / "session.yaml")), intervals=intervals,
                                   pilot=args.command == "pilot", force=args.force)
        elif args.command == "profile":
            if args.profile_command == "create":
                result = {"created": str(create_profile(settings["roots"]["data"], args.kind, args.id, args.label))}
            else:
                result = {"profiles": [{"id": p["id"], "label": p.get("label")} for p in list_profiles(settings["roots"]["data"], args.kind)]}
        elif args.command == "archive":
            result = {"path": str(archive_session(settings["roots"]["data"], args.session_id))}
        elif args.command == "restore":
            result = {"path": str(restore_session(settings["roots"]["data"], args.session_id))}
        elif args.command == "inspect":
            path = locate_session(settings["roots"]["data"], args.session_id)
            session = read_doc(path / "session.yaml")
            current_path = path / "transcript" / "current.json"
            result = {"session_id": session["id"], "path": str(path), "source_count": len(session["sources"]),
                      "profiles": session["profiles"], "processing_status": session["processing_status"],
                      "current": read_doc(current_path) if current_path.exists() else None}
        elif args.command == "accept":
            validate_id(args.run_id, "run ID")
            path = locate_session(settings["roots"]["data"], args.session_id)
            with session_lock(path):
                if not verify_completed_run(path / "transcript" / args.run_id):
                    raise ValueError("Only an intact technically completed run can be accepted.")
                current_path = path / "transcript" / "current.json"
                current = read_doc(current_path)
                current.update({"accepted_run_id": args.run_id, "accepted_at": now(),
                                "acceptance_provenance": "explicit user accept command"})
                current["accuracy_acceptance"] = "accepted" if current.get("latest_successful_run_id") == args.run_id else "pending"
                write_json(current_path, current, overwrite=True)
            result = {"accepted_run_id": args.run_id, "current": str(current_path)}
        elif args.command == "evaluate":
            result = evaluate_reference(settings, args.benchmark_id, args.evaluation_id, args.clip_id,
                                        Path(args.reference).expanduser(), verified=args.verified)
        elif args.command == "launch":
            result = launch(settings)
        else:
            raise ValueError("Unknown operation.")
        emit(result)
        return 1 if result.get("state") == "partial" else 0
    except KeyboardInterrupt:
        print("Interrupted. Completed checkpoints and originals are preserved; rerun the same command to resume.", file=sys.stderr)
        return 130
    except (ValueError, OSError, RuntimeError, KeyError) as error:
        print(f"AudioTranscribe: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
