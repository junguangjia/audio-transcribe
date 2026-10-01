"""On-demand watched-folder reconciliation, without a persistent background worker.

Native FSEvents supplies change notifications and direct-child snapshots. This
module records tiny metadata observations so unchanged files are not rehashed
on each event. A metadata match is only a candidate; new or changed bytes are
hashed before they can enter the ready set and again before result publication.
"""
from __future__ import annotations

import hashlib
import copy
import json
from pathlib import Path
import time

from .storage import _file_lock, read_doc, sha256_file, source_stat, write_json
from . import lifecycle

_STABLE_SECONDS = 0.75
_INVALID_GRACE_SECONDS = 5.0
_EXTENSIONS = {"wav", "wave", "m4a", "mp3", "flac", "aac", "aiff", "aif", "aifc", "ogg", "oga", "opus", "mp4", "mov"}


def _root(settings):
    return Path(settings["roots"]["data"]).expanduser().resolve() / "watch"


def _load(settings):
    path = _root(settings) / "discovery.json"
    if not path.exists():
        return {"schema_version": 1, "files": {}, "ignored": {}}
    value = read_doc(path)
    if value.get("schema_version") != 1 or not isinstance(value.get("files"), dict) or not isinstance(value.get("ignored"), dict):
        raise ValueError("Watched-folder discovery metadata is invalid; no source was admitted.")
    return value


def _save(settings, value):
    write_json(_root(settings) / "discovery.json", value, overwrite=True)


def _locked(settings):
    root = _root(settings)
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink() or (root / ".lock").is_symlink():
        raise ValueError("Watched-folder metadata cannot be a symbolic link.")
    return _file_lock(root / ".lock", blocking=True)


def version_key(path, sha256):
    return hashlib.sha256(json.dumps([str(Path(path).expanduser().resolve()), sha256], ensure_ascii=False).encode()).hexdigest()


def _metadata_key(path, stat):
    return hashlib.sha256(json.dumps([str(path), stat], sort_keys=True).encode()).hexdigest()


def _record_index(records):
    by_hash = {}
    for identity, record in records.items():
        if record["kind"] == "new":
            digest = record["saved"]["document"]["source"].get("sha256")
        elif record["kind"] == "source":
            digest = record["source"].get("sha256")
        else:
            continue
        if digest:
            by_hash.setdefault(digest, []).append((identity, record))
    for entries in by_hash.values():
        entries.sort(key=lambda item: (item[1]["summary"].get("generated_at") or "", item[0]), reverse=True)
    return by_hash


def _result_for_hash(settings, path, digest, index):
    from .direct import _validate_saved_run
    from .engine import verify_completed_run, verify_review_run
    for identity, record in index.get(digest, []):
        if record["summary"]["filename"] != path.name:
            continue
        if record["kind"] == "new":
            saved = record["saved"]
            if saved["document"]["source"].get("ownership") == "external_referenced":
                from .storage import locate_session
                try:
                    session_path = locate_session(settings["roots"]["data"], saved["internal"]["session_id"])
                    session = read_doc(session_path / "session.yaml")
                    source = next(s for s in session["sources"] if s["id"] == saved["internal"]["source_id"])
                    if source.get("external_path") != str(path.resolve()):
                        continue
                except (OSError, ValueError, KeyError, StopIteration):
                    continue
            if saved["document"]["source"].get("sha256") != digest:
                continue
            if _validate_saved_run(settings, saved, copy.deepcopy(saved["document"])):
                return identity
        elif record["kind"] == "source" and record["source"].get("sha256") == digest:
            if verify_completed_run(record["run_path"]) or verify_review_run(record["run_path"]):
                return identity
    return None


def _cached_result_for_hash(settings, path, digest, identity):
    """Validate one prior link without scanning the entire result library."""
    if not isinstance(identity, str) or not identity:
        return None
    from .direct import _record_for_identity
    record = _record_for_identity(settings, identity)
    if record is None:
        return None
    return _result_for_hash(settings, path, digest, {digest: [(identity, record)]})


def discover(settings, request):
    folder_raw = request.get("folder")
    paths = request.get("paths")
    if not isinstance(folder_raw, str) or not Path(folder_raw).is_absolute() or not isinstance(paths, list) or len(paths) > 256:
        raise ValueError("Choose a watched folder and a bounded list of its direct files.")
    folder = Path(folder_raw).expanduser().resolve()
    if folder.is_relative_to(Path(settings["roots"]["data"]).expanduser().resolve()):
        raise ValueError("Watch a recording folder outside application-managed data.")
    if not folder.is_dir():
        return {"items": [], "ready_count": 0, "folder_state": "unavailable"}
    if any(not isinstance(raw, str) or not Path(raw).is_absolute() or Path(raw).parent.resolve() != folder for raw in paths):
        raise ValueError("Discovery accepts only direct children of the selected folder.")
    if len(set(paths)) != len(paths):
        raise ValueError("Discovery paths must be unique.")
    changed = request.get("changed_paths", [])
    if not isinstance(changed, list) or any(raw not in paths for raw in changed):
        raise ValueError("Changed paths must be among the discovered files.")
    changed = set(changed)
    with _locked(settings):
        saved = _load(settings)
        previous = {raw: saved["files"].get(raw) for raw in paths}
        ignored = set(saved["ignored"])
    ignored.update(lifecycle.state(settings).get("ignored_source_versions", {}))
    now = time.time()
    items = []
    updates = {}
    index = None
    for raw in paths:
        path = Path(raw)
        base = {"path": raw, "filename": path.name}
        old = previous[raw] if isinstance(previous[raw], dict) else {}
        if path.suffix.lower().lstrip(".") not in _EXTENSIONS or path.name.startswith(".") or path.name.endswith((".tmp", ".partial", ".download")):
            items.append({**base, "state": "unavailable", "message": "Temporary or unsupported file."})
            continue
        try:
            observed = source_stat(path)
        except (OSError, ValueError) as error:
            items.append({**base, "state": "unavailable", "message": str(error)})
            continue
        provisional_key = _metadata_key(path, observed)
        # Legacy cache entries lack ctime_ns. They intentionally fail this
        # comparison, settle again, and receive a new full-byte hash.
        if old.get("stat") != observed:
            updates[raw] = {"stat": observed, "first_seen": now}
            items.append({**base, "state": "copying", "version_key": provisional_key,
                          "message": "Waiting for a stable file observation."})
            continue
        if raw not in changed and not old.get("revalidate") and old.get("sha256") and old.get("version_key"):
            key = old["version_key"]
            if key in ignored:
                # A deleted/ignored source version must not be resurrected by
                # an old cache entry, even if a result once referred to it.
                result_id = None
                state = "ignored"
            else:
                # Publishing a result does not mutate the tiny watched-source
                # cache. A second app bundle ID has no native queue history, so
                # reconcile this metadata candidate with the current, visible
                # result library before offering it as a new recording. This
                # validates the exact external path and saved run; it never
                # re-hashes the original audio.
                result_id = _cached_result_for_hash(settings, path, old["sha256"], old.get("result_id"))
                if result_id is None:
                    if index is None:
                        from .direct import _records
                        index = _record_index(_records(settings))
                    result_id = _result_for_hash(settings, path, old["sha256"], index)
                state = "processed_candidate" if result_id else "ready"
            if result_id != old.get("result_id"):
                updates[raw] = {**old, "result_id": result_id}
            items.append({**base, "state": state, "version_key": key,
                          "result_id": result_id,
                          "verification": "metadata_candidate",
                          "message": "Previously verified bytes; metadata is unchanged. Revalidated before processing."})
            continue
        if not old.get("revalidate") and now - old.get("first_seen", 0) < _STABLE_SECONDS:
            items.append({**base, "state": "copying", "version_key": provisional_key,
                          "message": "Waiting for the recording to finish copying."})
            continue
        if path.suffix.lower() in {".wav", ".wave"}:
            try:
                from .audio import _parse
                _parse(path)
            except (OSError, ValueError) as error:
                settled = now - old.get("first_seen", now) >= _INVALID_GRACE_SECONDS
                items.append({**base, "state": "failed" if settled else "copying",
                              "version_key": provisional_key,
                              "message": ("WAV is still invalid; retry after replacing or repairing the file: "
                                          if settled else "WAV header is not complete or readable yet: ") + str(error)})
                continue
        try:
            digest = sha256_file(path)
            if source_stat(path) != observed:
                updates[raw] = {"stat": source_stat(path), "first_seen": now}
                items.append({**base, "state": "copying", "version_key": provisional_key,
                              "message": "File changed while verifying; waiting for stability."})
                continue
            key = version_key(path, digest)
            if index is None:
                from .direct import _records
                index = _record_index(_records(settings))
            result_id = _result_for_hash(settings, path, digest, index)
            state = "ignored" if key in ignored else "processed" if result_id else "ready"
            updates[raw] = {"stat": observed, "first_seen": old.get("first_seen", now),
                            "sha256": digest, "version_key": key, "result_id": result_id}
            items.append({**base, "state": state, "version_key": key,
                          "source_sha256": digest, "result_id": result_id})
        except (OSError, ValueError) as error:
            items.append({**base, "state": "unavailable", "version_key": provisional_key,
                          "message": str(error)})
    if updates:
        with _locked(settings):
            saved = _load(settings)
            saved["files"].update(updates)
            _save(settings, saved)
    return {"items": items, "ready_count": sum(item["state"] == "ready" for item in items),
            "folder_state": "enabled"}


def ignore(settings, request, *, restore=False):
    raw = request.get("path")
    key = request.get("version_key")
    if not isinstance(raw, str) or not Path(raw).is_absolute() or not isinstance(key, str):
        raise ValueError("Choose one previously discovered source version.")
    with _locked(settings):
        saved = _load(settings)
        entry = saved["files"].get(raw)
        if not isinstance(entry, dict) or entry.get("version_key") != key:
            raise ValueError("Source version changed; refresh before changing discovery status.")
        if restore:
            saved["ignored"].pop(key, None)
            # A deletion may have removed the result named by this metadata
            # cache. Keep the version token for retry, but verify its bytes and
            # current result linkage before exposing it as ready or processed.
            entry["revalidate"] = True
        else:
            saved["ignored"][key] = {"path": raw}
        _save(settings, saved)
    if restore:
        with lifecycle.lock(settings):
            state = lifecycle.state(settings)
            state["ignored_source_versions"].pop(key, None)
            lifecycle.save(settings, state)
    return {"path": raw, "version_key": key, "state": "ready" if restore else "ignored",
            "verification": "metadata_candidate" if restore else "explicit_ignore"}
