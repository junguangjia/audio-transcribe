"""Local report library and explicitly unconfirmed recording-time suggestions.

Only metadata and selected report text are read. Filesystem modification time is
never a recording date, and temporal proximity is never proof of a shared class.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
import re
import unicodedata

from .audio import _parse
from .storage import (_file_lock, locate_session, resolve_source_path, read_doc,
                      sha256_file, validate_id, validate_session, write_json)

LIBRARY_VERSION = 1
_FILENAME_CLOCK = re.compile(r"audio_(\d{6})_(\d{6})(?:_[^.]+)?\.[A-Za-z0-9]+\Z", re.ASCII)
_GAP_SECONDS = 15 * 60


def recording_time(filename, explicit=None):
    """Return a civil clock value and its provenance, without inventing a zone."""
    if explicit is not None:
        try:
            if not isinstance(explicit, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:[.]\d+)?(?:Z|[+-]\d{2}:\d{2})?", explicit):
                raise ValueError("Invalid recording timestamp")
            value = dt.datetime.fromisoformat(explicit.replace("Z", "+00:00"))
            return {"date": value.date().isoformat(), "time": value.strftime("%H:%M:%S"),
                    "recorded_at": value.isoformat(), "timezone": str(value.tzinfo) if value.tzinfo else None,
                    "provenance": "explicit_metadata", "confirmed": True}
        except ValueError:
            return {"date": None, "time": None, "recorded_at": None, "timezone": None,
                    "provenance": "invalid_explicit_metadata", "confirmed": False}
    match = _FILENAME_CLOCK.fullmatch(Path(filename).name)
    if match:
        date, clock = match.groups()
        try:
            value = dt.datetime(2000 + int(date[:2]), int(date[2:4]), int(date[4:]),
                                int(clock[:2]), int(clock[2:4]), int(clock[4:]))
            return {"date": value.date().isoformat(), "time": value.strftime("%H:%M:%S"),
                    "recorded_at": value.isoformat(), "timezone": None,
                    "provenance": "filename", "confirmed": False}
        except ValueError:
            pass
    return {"date": None, "time": None, "recorded_at": None, "timezone": None,
            "provenance": "unknown", "confirmed": False}


def clean_title(value):
    if not isinstance(value, str):
        raise ValueError("A class title must be text.")
    value = " ".join(unicodedata.normalize("NFC", value).split())
    if not value or len(value) > 160 or any(unicodedata.category(c).startswith("C") for c in value):
        raise ValueError("Use a nonempty class title of at most 160 characters.")
    return value


def path_title(value):
    value = clean_title(value)
    slug = "".join(c if c.isalnum() or c in "-_" else "-" for c in value)
    return re.sub("-+", "-", slug).strip("-_")[:72] or "recordings"


def _duration(path):
    # WAV chunk headers provide measured sample counts without reading audio.
    # Other formats stay unknown until the existing decoder records a duration.
    if path.suffix.lower() not in {".wav", ".wave"}:
        return None
    try:
        info = _parse(path)
        return info["duration_seconds"] if not info["orphan_bytes"] else None
    except (ValueError, OSError):
        return None


def propose_groups(paths, metadata=None):
    recordings, groups = [], []
    for index, value in enumerate(paths):
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise ValueError("Choose local recording paths.")
        path = Path(value)
        known = (metadata or {}).get(str(path.resolve()), {})
        recordings.append({"index": index, "filename": path.name, **recording_time(path.name, known.get("recorded_at")),
                           "duration_seconds": known.get("duration_seconds") or _duration(path)})
    for item in recordings:
        issues, join = [], False
        previous = recordings[item["index"] - 1] if item["index"] else None
        if previous and item["date"] and item["date"] == previous["date"]:
            start = dt.datetime.fromisoformat(item["recorded_at"])
            prior = dt.datetime.fromisoformat(previous["recorded_at"])
            if start.utcoffset() != prior.utcoffset():
                issues.append("Recording timezone bases differ or are unknown; continuity cannot be compared reliably.")
            elif previous["duration_seconds"] is None:
                issues.append("Previous recording duration is unavailable; continuity is unknown.")
            else:
                gap = (start - prior).total_seconds() - previous["duration_seconds"]
                if gap < 0:
                    issues.append("Recording times overlap or are out of order; confirm the device clock and grouping.")
                elif gap <= _GAP_SECONDS:
                    join = True
                    if gap > 0:
                        issues.append(f"There is a {gap:g} second gap between recordings.")
                else:
                    issues.append(f"There is a {round(gap)} second gap; a separate class is suggested.")
        if join:
            groups[-1]["indices"].append(item["index"])
            groups[-1]["issues"].extend(issues)
            groups[-1]["reason"] = "Same recording clock date and a gap of at most 15 minutes after the measured end. Clock provenance and timezone remain as shown; this suggests continuity, not class identity."
        else:
            groups.append({"indices": [item["index"]], "title": (item["time"][:5] + " recordings") if item["time"] else "Undated recordings",
                           "date": item["date"], "start_time": item["time"],
                           "status": "suggested" if item["date"] else "unknown", "confirmed": False,
                           "reason": ("Explicit recording metadata; class identity still requires confirmation." if item["provenance"] == "explicit_metadata" else "Filename clock only; recording timezone and class identity are unverified.") if item["date"] else "No reliable recording date was found.",
                           "issues": issues})
    return {"schema_version": LIBRARY_VERSION, "recordings": recordings, "groups": groups,
            "requires_confirmation": True, "timezone": None}


def normalize_groups(entries, groups=None):
    """Validate an explicit partition while retaining the selected source order."""
    if groups is None:
        clocks = [e.get("recording_time") or recording_time(e["filename"]) for e in entries]
        dates = {c["date"] for c in clocks}
        date = next(iter(dates)) if len(dates) == 1 else None
        return [{"indices": list(range(len(entries))), "title": f"{len(entries)} recording" + ("s" if len(entries) != 1 else ""),
                 "date": date, "start_time": clocks[0]["time"] if clocks else None,
                 "status": "unconfirmed", "confirmed": False,
                 "date_provenance": ("explicit_metadata" if any(c["provenance"] == "explicit_metadata" for c in clocks) else "filename") if date else "unknown"}]
    if not isinstance(groups, list) or not groups:
        raise ValueError("Confirm at least one recording group.")
    result, flattened = [], []
    for group in groups:
        indices = group.get("indices") if isinstance(group, dict) else None
        if (not isinstance(indices, list) or not indices or any(type(i) is not int for i in indices)
                or group.get("confirmed") is not True):
            raise ValueError("Every group needs explicit confirmation and recording indices.")
        flattened.extend(indices)
        date = group.get("date")
        if date is not None:
            if not isinstance(date, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
                raise ValueError("Recording dates use YYYY-MM-DD.")
            dt.date.fromisoformat(date)
        if any(i < 0 or i >= len(entries) for i in indices):
            raise ValueError("Group index is outside the recording list.")
        first_entry = entries[indices[0]]
        clock = first_entry.get("recording_time") or recording_time(first_entry["filename"])
        result.append({"indices": indices, "title": clean_title(group.get("title", "Class recordings")),
                       "date": date, "start_time": clock["time"], "status": "confirmed", "confirmed": True,
                       "date_provenance": "owner_confirmed" if date else "unknown"})
    if flattened != list(range(len(entries))):
        raise ValueError("Groups must include each selected recording exactly once in its original order.")
    return result


def export_directory(data_root, groups):
    dates = {g["date"] for g in groups}
    folder = next(iter(dates)) if len(dates) == 1 and None not in dates else ("unknown-date" if dates == {None} else "multiple-dates")
    label = groups[0]["title"] if len(groups) == 1 else f"{len(groups)}-classes"
    clock = (groups[0].get("start_time") or "").replace(":", "")
    # Joining a fully sanitized component prevents user titles escaping DATA_ROOT.
    return Path(data_root) / "exports" / folder / (((clock + "-") if clock else "") + path_title(label))


def _known_recordings(data_root):
    """Use only unambiguous managed paths; basenames are not source identities."""
    root, known = Path(data_root), {}
    paths = list((root / "sessions").glob("*/session.yaml"))
    paths += list((root / "archive").glob("[0-9][0-9][0-9][0-9]/*/session.yaml"))
    for path in paths:
        try:
            session = validate_session(path.parent)
            for source in session["sources"]:
                explicit = source.get("recorded_at")
                if explicit is None and len(session["sources"]) == 1:
                    explicit = session.get("recorded_at")
                known[str(resolve_source_path(path.parent, source))] = {"recorded_at": explicit}
        except (OSError, ValueError, KeyError):
            continue
    return known


def export_manifests(data_root):
    root = Path(data_root).resolve() / "exports"
    for path in sorted(root.rglob("manifest.json"), reverse=True):
        if (any(p.startswith(".") for p in path.relative_to(root).parts)
                or not path.resolve().is_relative_to(root.resolve())):
            continue
        yield path


def _annotations(data_root):
    path = Path(data_root) / "library" / "annotations.json"
    return read_doc(path).get("reports", {}) if path.is_file() else {}


def report_is_trashed(data_root, report_id):
    """Trash is a local visibility annotation, never deletion of saved artifacts."""
    validate_id(report_id, "report ID")
    return bool(_annotations(data_root).get(report_id, {}).get("deleted_at"))


def _view_options(view, sort="recorded"):
    if view not in {"active", "trash"}:
        raise ValueError("Library view must be active or trash.")
    if sort not in {"recent", "recorded"}:
        raise ValueError("Library sort must be recent or recorded.")


def _recent_key(report):
    # Our created_at values are UTC. Respect an explicit offset in imported
    # manifests too; malformed or zone-free values are not guessed from mtime.
    created = report.get("created_at")
    epoch = float("-inf")
    if isinstance(created, str):
        try:
            value = dt.datetime.fromisoformat(created.replace("Z", "+00:00"))
            if value.tzinfo is not None:
                epoch = value.timestamp()
        except ValueError:
            pass
    return epoch, report["id"]


def _summary(manifest, path, annotations):
    entries = manifest.get("ordered_sources", [])
    groups = manifest.get("groups") or normalize_groups(entries)
    if manifest.get("groups"):
        date_set = {g.get("date") for g in groups}
        clock_items = [{"date": g.get("date"), "time": g.get("start_time")} for g in groups]
    else:
        clock_items = [e.get("recording_time") or recording_time(e["filename"]) for e in entries]
        date_set = {clock["date"] for clock in clock_items}
    known_dates = sorted(date_set or {None}, key=lambda value: value or "", reverse=True)
    date_start_times = []
    for date in known_dates:
        clocks = [clock["time"] for clock in clock_items if clock["date"] == date and clock.get("time")]
        date_start_times.append({"date": date, "start_time": min(clocks) if clocks else None})
    title = groups[0]["title"] if len(groups) == 1 else f"{len(groups)} classes · {len(entries)} recordings"
    title = annotations.get(manifest["batch_id"], {}).get("title", title)
    quality = manifest.get("quality", {})
    review = manifest.get("review_required", 0)
    return {"id": manifest["batch_id"], "title": title, "date": known_dates[0] if len(known_dates) == 1 else None,
            "dates": known_dates or [None], "date_start_times": date_start_times,
            "start_time": groups[0].get("start_time"), "report": str(path.parent / "transcript-report.md"),
            "selected": manifest.get("selected", len(entries)), "completed": manifest.get("completed", 0), "failed": manifest.get("failed", 0),
            "review_required": review, "quality_status": quality.get("status", "review_required" if review else "unknown"),
            "accuracy_verified": quality.get("accuracy_verified", False), "state": manifest.get("state", "unknown"),
            "grouping_status": "confirmed" if all(g.get("confirmed") for g in groups) else "unconfirmed",
            "created_at": manifest.get("created_at"), "groups": groups,
            "filenames": [entry["filename"] for entry in entries],
            "deleted_at": annotations.get(manifest["batch_id"], {}).get("deleted_at")}


def _reports(data_root):
    annotations = _annotations(data_root)
    found, ambiguous = {}, set()
    exports_root = (Path(data_root) / "exports").resolve()
    for path in export_manifests(data_root):
        try:
            manifest = read_doc(path)
            validate_id(manifest.get("batch_id"), "report ID")
            report = path.parent / "transcript-report.md"
            if (manifest["batch_id"] != path.parent.name or not report.is_file()
                    or not report.resolve().is_relative_to(exports_root)):
                continue
            summary = _summary(manifest, path, annotations)
            if manifest["batch_id"] in ambiguous:
                continue
            if manifest["batch_id"] in found:
                found.pop(manifest["batch_id"])
                ambiguous.add(manifest["batch_id"])
                continue
            found[manifest["batch_id"]] = (summary, manifest, path)
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return found


def list_library(settings, query="", *, view="active", sort="recorded"):
    _view_options(view, sort)
    if not isinstance(query, str):
        raise ValueError("Search query must be text.")
    query = query.casefold().strip()
    reports = []
    available = _reports(settings["roots"]["data"])
    trash_count = sum(bool(summary.get("deleted_at")) for summary, _, _ in available.values())
    for summary, manifest, path in available.values():
        if bool(summary.get("deleted_at")) != (view == "trash"):
            continue
        searchable = " ".join([summary["title"], *(str(d or "") for d in summary["dates"]),
                                *(e["filename"] for e in manifest.get("ordered_sources", []))]).casefold()
        if query and query not in searchable:
            try:
                if query not in (path.parent / "transcript-report.md").read_text(encoding="utf-8").casefold():
                    continue
            except (OSError, UnicodeError):
                continue
        reports.append(summary)
    if sort == "recent":
        reports.sort(key=_recent_key, reverse=True)
    else:
        reports.sort(key=lambda r: (r["date"] or "", r["start_time"] or "", r["created_at"] or "", r["id"]), reverse=True)
    dates = {}
    for report in reports:
        for date in report["dates"]:
            start = next(clock["start_time"] for clock in report["date_start_times"] if clock["date"] == date)
            # A multi-date master appears under each date with that date's clock,
            # while its stable identity still opens the complete batch report.
            dated = {**report, "date": date, "start_time": start}
            dates.setdefault(date, []).append(dated)
    for dated_reports in dates.values():
        dated_reports.sort(key=_recent_key if sort == "recent" else lambda report: (report["start_time"] or "", report["created_at"] or "", report["id"]), reverse=True)
    return {"schema_version": LIBRARY_VERSION, "reports": reports, "view": view, "sort": sort,
            "active_count": len(available) - trash_count, "trash_count": trash_count,
            "dates": [{"date": date, "label": date or "Unknown recording date", "reports": dates[date]}
                      for date in sorted(dates, key=lambda d: d or "", reverse=True)]}


def rebuild_index(settings, *, locked=False):
    root = Path(settings["roots"]["data"]) / "library"
    root.mkdir(parents=True, exist_ok=True)
    def rebuild():
        snapshot = list_library(settings)
        write_json(root / "index.json", snapshot, overwrite=True)
        return snapshot
    if locked:
        return rebuild()
    with _file_lock(root / ".library.lock", blocking=True):
        return rebuild()


def read_report(settings, report_id, query="", *, view="active"):
    _view_options(view)
    if not isinstance(query, str):
        raise ValueError("Search query must be text.")
    validate_id(report_id, "report ID")
    match = _reports(settings["roots"]["data"]).get(report_id)
    if not match:
        raise ValueError("Report is not available in the local library.")
    summary, manifest, path = match
    if bool(summary.get("deleted_at")) != (view == "trash"):
        raise ValueError("Report is not available in the selected library view.")
    report_path = path.parent / "transcript-report.md"
    text = report_path.read_text(encoding="utf-8")
    intact = sha256_file(report_path) == manifest.get("report_sha256")
    sources, segments = [], []
    for index, entry in enumerate(manifest.get("ordered_sources", [])):
        transcript = entry.get("transcript", {})
        item = {"index": index, "filename": entry["filename"], **entry.get("recording_time", recording_time(entry["filename"])),
                "duration_seconds": entry.get("duration_seconds"), "state": entry.get("state"),
                "quality_status": transcript.get("quality", {}).get("status", "unknown"), "audio_path": None}
        try:
            session_path = locate_session(settings["roots"]["data"], transcript["session_id"])
            session = validate_session(session_path)
            source = next(s for s in session["sources"] if s["id"] == transcript["source_id"])
            item["audio_path"] = str(resolve_source_path(session_path, source, verify_hash=True))
            run_id = validate_id(transcript["run_id"], "run ID")
            stored = session_path / "transcript" / run_id / "transcript.json"
            if stored.resolve().is_relative_to(session_path.resolve()) and sha256_file(stored) == transcript.get("transcript_json_sha256"):
                for segment in read_doc(stored)["segments"]:
                    if segment["source_id"] == source["id"]:
                        segments.append({"source_index": index, "start_seconds": segment["start_seconds"],
                                         "end_seconds": segment["end_seconds"], "text": segment["text"]})
        except (OSError, ValueError, KeyError, StopIteration):
            item["quality_message"] = "The saved source or structured transcript is unavailable; the report is preserved."
        sources.append(item)
    matches = [{"line": i, "text": line} for i, line in enumerate(text.splitlines(), 1)
               if query and query.casefold() in line.casefold()]
    return {"schema_version": LIBRARY_VERSION, "report": summary, "markdown": text,
            "integrity": "intact" if intact else "modified", "sources": sources, "segments": segments, "matches": matches}


def library_request(settings, request):
    action = request.get("action", "list")
    if action == "plan":
        paths = request.get("files")
        if not isinstance(paths, list) or not paths:
            raise ValueError("Select recordings to suggest grouping.")
        return propose_groups(paths, _known_recordings(settings["roots"]["data"]))
    if action == "list":
        return list_library(settings, request.get("query", ""), view=request.get("view", "active"), sort=request.get("sort", "recorded"))
    if action == "read":
        return read_report(settings, request.get("report_id"), request.get("query", ""), view=request.get("view", "active"))
    if action in {"rename", "trash", "restore"}:
        report_id = validate_id(request.get("report_id"), "report ID")
        view = request.get("view", "trash" if action == "restore" else "active")
        sort = request.get("sort", "recorded")
        query = request.get("query", "")
        _view_options(view, sort)
        if not isinstance(query, str):
            raise ValueError("Search query must be text.")
        title = clean_title(request.get("title")) if action == "rename" else None
        root = Path(settings["roots"]["data"]) / "library"
        root.mkdir(parents=True, exist_ok=True)
        with _file_lock(root / ".library.lock"):
            if report_id not in _reports(settings["roots"]["data"]):
                raise ValueError("Report is not available in the local library.")
            records = _annotations(settings["roots"]["data"])
            annotation = records.setdefault(report_id, {})
            if action == "rename":
                if bool(annotation.get("deleted_at")) != (view == "trash"):
                    raise ValueError("Report is not available in the selected library view.")
                annotation["title"] = title
            elif action == "trash":
                if not annotation.get("deleted_at"):
                    annotation["deleted_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
            else:
                annotation.pop("deleted_at", None)
            write_json(root / "annotations.json", {"schema_version": LIBRARY_VERSION, "reports": records}, overwrite=True)
            rebuild_index(settings, locked=True)
            return list_library(settings, query, view=view, sort=sort)
    raise ValueError("Unknown local library action.")
