"""Factual recording-clock metadata; filename conventions are not assumed."""
from __future__ import annotations

from datetime import datetime, timedelta
import math
from pathlib import Path
import re
import struct

BWF_SPEC = "https://tech.ebu.ch/docs/tech/tech3285.pdf"
_ISO = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?\Z")


def _clock(value):
    if not isinstance(value, str) or not _ISO.fullmatch(value):
        raise ValueError("Recording time must be an ISO date and clock, with optional explicit offset.")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def bwf_clock(path):
    """Read only the BWF origination fields (EBU Tech 3285 section 2.3).

    These declare sequence creation time, not proof of recorder clock accuracy.
    Arbitrary ancillary metadata and filesystem dates are never substituted.
    """
    path = Path(path)
    with path.open("rb") as stream:
        header = stream.read(12)
        if len(header) != 12 or header[:4] != b"RIFF" or header[8:] != b"WAVE":
            return None
        end = struct.unpack_from("<I", header, 4)[0] + 8
        if end != path.stat().st_size:
            return None
        found = []
        offset = 12
        while offset + 8 <= end:
            stream.seek(offset)
            chunk, size = struct.unpack("<4sI", stream.read(8))
            if offset + 8 + size > end:
                return None
            if chunk == b"bext":
                if size < 602:
                    return None
                stream.seek(offset + 8 + 320)
                raw = stream.read(18)
                try:
                    date, time = raw[:10].decode("ascii"), raw[10:].decode("ascii")
                    if not re.fullmatch(r"\d{4}.\d{2}.\d{2}", date) or not re.fullmatch(r"\d{2}.\d{2}.\d{2}", time):
                        return None
                    parsed = _clock(f"{date[:4]}-{date[5:7]}-{date[8:]}T{time[:2]}:{time[3:5]}:{time[6:]}")
                    found.append(parsed.isoformat())
                except (UnicodeError, ValueError):
                    return None
            offset += 8 + size + size % 2
        # Multiple contradictory clock chunks are ambiguous; do not pick one.
        if offset == end and len(found) == 1:
            return {"value": found[0], "source": "embedded_bwf_origination", "confidence": "embedded_metadata",
                    "evidence": {"fields": ["bext.OriginationDate", "bext.OriginationTime"],
                                 "specification": BWF_SPEC, "resolution_seconds": 1,
                                 "meaning": "Embedded declaration of audio-sequence creation time; device clock accuracy not independently verified."}}
    return None


def recording_metadata(path, duration, *, source=None, session=None, legacy_time=None):
    result = {"started_at": None, "ended_at": None, "time_source": "unknown", "time_confidence": "unknown",
              "timezone": None, "timezone_status": "unknown", "end_time_basis": None}
    declared = None
    source, session = source or {}, session or {}
    explicit = source.get("recorded_at")
    if explicit is None and len(session.get("sources", [])) == 1:
        explicit = session.get("recorded_at")
    if explicit is not None:
        try:
            declared = {"value": _clock(explicit).isoformat(), "source": "explicit_source_metadata",
                        "confidence": "user_supplied", "evidence": {"field": "recorded_at", "meaning": "Explicit managed metadata; not inferred from filename or filesystem time."}}
        except ValueError:
            result["invalid_explicit_recorded_at"] = True
    if declared is None and path:
        try:
            declared = bwf_clock(path)
        except OSError:
            pass
    if declared:
        clock = _clock(declared["value"])
        ended = None
        if type(duration) in (int, float) and math.isfinite(duration) and duration >= 0:
            try:
                ended = (clock + timedelta(seconds=duration)).isoformat()
            except OverflowError:
                pass
        result.update(started_at=clock.isoformat(), ended_at=ended, time_source=declared["source"],
                      time_confidence=declared["confidence"], timezone=clock.strftime("%z") if clock.tzinfo else None,
                      timezone_status="explicit_offset" if clock.tzinfo else "unknown",
                      end_time_basis="declared start plus decoded duration" if ended else None, evidence=declared["evidence"])
    if isinstance(legacy_time, dict) and legacy_time.get("provenance") == "filename":
        result["legacy_filename_inference"] = {"value": legacy_time.get("recorded_at"),
                                                "status": "unverified_naming_convention", "used_as_recording_time": False}
    result["filename_time_policy"] = "Not parsed: no independently corroborated device naming convention is configured."
    return result
