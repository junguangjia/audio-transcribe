"""Strict local settings and manually assigned, independently scoped profiles.

Profiles are configuration records, never identity classifiers or trained voices.
Resolved configurations contain copies and hashes of every selected document.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path

from .storage import read_doc, validate_id, write_yaml
from .product import DEFAULT_MODEL

PROFILE_DIRECTORIES = {"speaker": "speakers", "capture": "captures", "glossary": "glossaries"}


def defaults() -> dict:
    return {"asr": {"model": DEFAULT_MODEL, "language": "en", "temperature": 0, "beam_size": 5, "temperature_increment": 0.2, "threads": 4, "max_context": 0}, "preprocessing": {"channel": "mean", "candidate": "A", "peak_dbfs": -3, "max_gain_db": 30, "gain_db": None, "vad": False, "filtering": "none"}}


def execution_policy(settings=None, overrides=None, *, file_count=None):
    """Scheduling preferences are deliberately outside the ASR/cache identity."""
    result = {"mode": "auto", "asr_workers": 3, "prepare_workers": 1,
              "prepared_ahead": 1, "resource_sampling": False}
    for values in ((settings or {}).get("execution", {}), overrides or {}):
        _unknown(values, result, "execution policy")
        result.update(values)
    if result["mode"] not in {"auto", "serial", "pipeline"}:
        raise ValueError("execution.mode must be auto, serial, or pipeline.")
    _number(result["asr_workers"], 1, 9, "execution.asr_workers", integral=True)
    _number(result["prepare_workers"], 1, 4, "execution.prepare_workers", integral=True)
    _number(result["prepared_ahead"], 1, 9, "execution.prepared_ahead", integral=True)
    if type(result["resource_sampling"]) is not bool:
        raise ValueError("execution.resource_sampling must be boolean.")
    # The measured nine-file preset requests three decoders. Admission may
    # conservatively hold extra slots without delaying the first worker.
    result["effective_asr_workers"] = (1 if result["mode"] == "serial"
                                       else min(result["asr_workers"], file_count)
                                       if type(file_count) is int and file_count > 0
                                       else result["asr_workers"])
    result["effective_prepare_workers"] = 1 if result["mode"] == "serial" else result["prepare_workers"]
    result["effective_prepared_ahead"] = 1 if result["mode"] == "serial" else result["prepared_ahead"]
    result["pipeline"] = result["mode"] != "serial"
    return result


def _mapping(value, label):
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a mapping.")
    return value


def _unknown(value, allowed, label):
    _mapping(value, label)
    extra = set(value) - set(allowed)
    if extra:
        raise ValueError(f"Unknown or cross-domain field(s) in {label}: {', '.join(sorted(extra))}.")


def _string(value, label, nullable=False):
    if nullable and value is None:
        return
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string" + (" or null." if nullable else "."))


def _strings(value, label):
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{label} must be a list of strings.")


def _number(value, low, high, label, integral=False):
    if type(value) not in ((int,) if integral else (int, float)) or not low <= value <= high:
        raise ValueError(f"{label} must be {'an integer' if integral else 'a number'} between {low} and {high}.")


def _validate_asr(values):
    _unknown(values, defaults()["asr"], "ASR preferences")
    for key, value in values.items():
        if key == "model" and value not in ("large-v3", "large-v3-turbo"):
            raise ValueError("Only local large-v3 and large-v3-turbo models are supported.")
        if key == "language" and value != "en":
            raise ValueError("This implementation transcribes English with language='en'.")
        if key in ("temperature", "temperature_increment"):
            _number(value, 0, 1, key)
        if key == "beam_size":
            _number(value, 1, 100, key, integral=True)
        if key == "threads":
            _number(value, 1, 128, key, integral=True)
        if key == "max_context":
            _number(value, 0, 16384, key, integral=True)


def _validate_preprocessing(values):
    _unknown(values, defaults()["preprocessing"], "capture preprocessing")
    for key, value in values.items():
        if key == "channel" and value not in ("mean", "left", "right"):
            raise ValueError("channel must be mean, left, or right.")
        if key == "candidate" and value not in ("A", "B"):
            raise ValueError("candidate must be A or B.")
        if key == "peak_dbfs":
            _number(value, -30, -0.1, key)
        if key == "max_gain_db":
            _number(value, 0, 60, key)
        if key == "gain_db" and value is not None:
            _number(value, -60, 60, key)
        if key == "vad" and value is not False:
            raise ValueError("VAD must remain false; automatic speech removal is not supported.")
        if key == "filtering" and value != "none":
            raise ValueError("Only filtering='none' is implemented; no unvalidated filter is silently enabled.")


def _canonical_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _settings_defaults():
    home = Path.home()
    return {"schema_version": 1, "roots": {"code": str(Path(__file__).resolve().parents[1]), "data": str(home / "AudioTranscription"), "app": str(home / "Library/Application Support/AudioTranscribe"), "cache": str(home / "Library/Caches/AudioTranscribe"), "log": str(home / "Library/Logs/AudioTranscribe")}, "runtime": {}, "models": {}, "execution": {}, "storage_approval": None}


def load_settings(path=None) -> dict:
    """Read settings without creating files. Explicit path wins over environment."""
    result = _settings_defaults()
    settings_path = Path(path if path is not None else os.environ.get("AUDIO_TRANSCRIBE_SETTINGS", str(Path.home() / "Library/Application Support/AudioTranscribe/settings.json"))).expanduser().resolve()
    if settings_path.exists():
        document = read_doc(settings_path)
        _unknown(document, result, "settings")
        if type(document.get("schema_version")) is not int or document["schema_version"] != 1:
            raise ValueError("settings.schema_version must be 1.")
        roots = document.get("roots", {})
        _unknown(roots, result["roots"], "settings.roots")
        for key, value in roots.items():
            _string(value, f"settings.roots.{key}")
            if not value:
                raise ValueError(f"settings.roots.{key} cannot be empty.")
            result["roots"][key] = str(Path(value).expanduser().resolve())
        for key in ("runtime", "models"):
            if key in document:
                result[key] = copy.deepcopy(_mapping(document[key], f"settings.{key}"))
        if "execution" in document:
            execution_policy(overrides=document["execution"])
            result["execution"] = copy.deepcopy(document["execution"])
        approval = document.get("storage_approval")
        if approval is not None:
            _unknown(approval, {"data_root", "mode", "approved_at"}, "storage_approval")
            if set(approval) != {"data_root", "mode", "approved_at"}:
                raise ValueError("storage_approval requires data_root, mode, and approved_at.")
            for key in approval:
                _string(approval[key], f"storage_approval.{key}")
            if approval["mode"] not in ("local_alternative", "explicit_synced", "verified_local"):
                raise ValueError("Unrecognized storage approval mode.")
            if not approval["approved_at"]:
                raise ValueError("storage_approval.approved_at cannot be empty.")
            result["storage_approval"] = copy.deepcopy(approval)
    approval = result["storage_approval"]
    result["storage_approved"] = bool(approval and Path(approval["data_root"]).expanduser().resolve() == Path(result["roots"]["data"]).resolve())
    result["settings_path"] = str(settings_path)
    result["settings_exists"] = settings_path.is_file()
    return result


def _profile_kind(kind):
    if kind not in PROFILE_DIRECTORIES:
        raise ValueError("Profile kind must be speaker, capture, or glossary.")
    return kind


def _validate_recipe(recipe):
    allowed = {"id", "state", "applicability", "asr", "preprocessing", "model_precision", "evaluation_refs", "provenance"}
    _unknown(recipe, allowed, "validated recipe")
    validate_id(recipe.get("id"), "recipe ID")
    if recipe.get("state") not in ("provisional", "validated_for_listed_clips"):
        raise ValueError("Recipe state must be provisional or validated_for_listed_clips.")
    applicability = recipe.get("applicability", {})
    _unknown(applicability, {"speaker_id", "capture_id", "glossary_sha256", "apply_to_matching_profiles"}, "recipe applicability")
    for key in ("speaker_id", "capture_id"):
        validate_id(applicability.get(key), key)
    if type(applicability.get("apply_to_matching_profiles", False)) is not bool:
        raise ValueError("Recipe apply_to_matching_profiles must be a boolean.")
    digest = applicability.get("glossary_sha256")
    if not isinstance(digest, str) or len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError("Recipe applicability requires the exact glossary SHA-256.")
    if recipe.get("model_precision") != "f16":
        raise ValueError("Validated recipes currently require model_precision='f16'.")
    _validate_asr(recipe.get("asr", {}))
    _validate_preprocessing(recipe.get("preprocessing", {}))
    _strings(recipe.get("evaluation_refs", []), "recipe evaluation_refs")
    if recipe.get("state") == "validated_for_listed_clips" and not recipe.get("evaluation_refs"):
        raise ValueError("A validated recipe requires evaluation references; an unmeasured preference is provisional.")
    if "provenance" in recipe:
        _string(recipe["provenance"], "recipe provenance")


def validate_profile(document: dict, kind: str) -> dict:
    _profile_kind(kind)
    common = {"schema_version", "kind", "id", "label", "language", "notes"}
    specific = {"speaker": {"terms", "asr", "calibration_state", "provenance", "evaluation_refs", "recipes"}, "capture": {"device", "placement", "room", "preprocessing", "evidence", "applicability"}, "glossary": {"terms", "provenance", "revision"}}
    _unknown(document, common | specific[kind], f"{kind} profile")
    if type(document.get("schema_version")) is not int or document["schema_version"] != 1:
        raise ValueError("Profile schema_version must be 1.")
    if document.get("kind") != kind:
        raise ValueError(f"Expected a {kind} profile kind.")
    validate_id(document.get("id"), f"{kind} profile ID")
    for key in ("label", "language", "notes", "device", "placement", "room", "provenance", "revision"):
        if key in document:
            _string(document[key], key, nullable=key in ("label", "language", "device", "placement", "room"))
    if document.get("language") not in (None, "en"):
        raise ValueError("Profile language must be en or null.")
    if "terms" in document:
        _strings(document["terms"], "terms")
        if any(not term.strip() or len(term) > 200 or "\n" in term for term in document["terms"]) or len(document["terms"]) > 100:
            raise ValueError("Use at most 100 short, nonempty contextual glossary terms (200 characters each).")
    if kind == "speaker":
        _validate_asr(document.get("asr", {}))
        if document.get("calibration_state", "uncalibrated") not in ("uncalibrated", "provisional", "validated_for_listed_clips"):
            raise ValueError("Invalid speaker calibration_state.")
        _strings(document.get("evaluation_refs", []), "evaluation_refs")
        recipes = document.get("recipes", [])
        if not isinstance(recipes, list):
            raise ValueError("recipes must be a list.")
        for recipe in recipes:
            _validate_recipe(recipe)
    elif kind == "capture":
        _validate_preprocessing(document.get("preprocessing", {}))
        _strings(document.get("evidence", []), "capture evidence")
        _strings(document.get("applicability", []), "capture applicability")
    return document


def load_profile(data_root, kind, profile_id) -> dict:
    _profile_kind(kind)
    validate_id(profile_id, "profile ID")
    directory = Path(data_root) / "profiles" / PROFILE_DIRECTORIES[kind]
    matches = [directory / (profile_id + suffix) for suffix in (".yaml", ".yml", ".json") if (directory / (profile_id + suffix)).is_file()]
    if not matches:
        raise FileNotFoundError(f"Unknown {kind} profile: {profile_id}. Create it explicitly or leave it unassigned.")
    if len(matches) != 1:
        raise ValueError(f"Multiple files define {kind} profile {profile_id}; retain a single definition.")
    document = validate_profile(read_doc(matches[0]), kind)
    if document["id"] != profile_id:
        raise ValueError("Profile filename and document ID disagree.")
    return document


def list_profiles(data_root, kind) -> list[dict]:
    _profile_kind(kind)
    directory = Path(data_root) / "profiles" / PROFILE_DIRECTORIES[kind]
    ids = sorted({path.stem for path in directory.glob("*") if path.suffix in (".json", ".yaml", ".yml")})
    return [load_profile(data_root, kind, profile_id) for profile_id in ids]


def create_profile(data_root, kind, profile_id, label=None) -> Path:
    _profile_kind(kind)
    validate_id(profile_id, "profile ID")
    document = {"schema_version": 1, "kind": kind, "id": profile_id, "label": label, "language": "en", "notes": ""}
    if kind == "speaker":
        document.update(terms=[], asr={}, calibration_state="uncalibrated", provenance="Manually created; no voice matching or model training.", evaluation_refs=[], recipes=[])
    elif kind == "capture":
        document.update(device=None, placement=None, room=None, preprocessing={}, evidence=[], applicability=[])
    else:
        document.update(terms=[], provenance="No vocabulary supplied.", revision="1")
    validate_profile(document, kind)
    directory = Path(data_root) / "profiles" / PROFILE_DIRECTORIES[kind]
    if any((directory / (profile_id + suffix)).exists() for suffix in (".json", ".yaml", ".yml")):
        raise FileExistsError(f"Profile {profile_id} already exists; existing data was preserved.")
    path = directory / (profile_id + ".yaml")
    write_yaml(path, document)
    return path


def resolve_config(data_root, speaker=None, capture=None, glossary=None, overrides=None) -> dict:
    result = defaults()
    result["origins"] = {f"{group}.{field}": "application_defaults" for group in ("asr", "preprocessing") for field in result[group]}
    result["selected_profiles"] = {"speaker": speaker, "capture": capture, "glossary": glossary}
    result["profile_snapshots"] = {}
    documents = {}
    for kind, profile_id in result["selected_profiles"].items():
        if profile_id is not None:
            document = load_profile(data_root, kind, profile_id)
            documents[kind] = document
            result["profile_snapshots"][kind] = {"id": profile_id, "sha256": _canonical_hash(document), "document": copy.deepcopy(document)}
    terms = []
    for kind in ("speaker", "glossary"):
        for term in documents.get(kind, {}).get("terms", []):
            if term not in terms:
                terms.append(term)
    result["glossary"] = {"id": glossary, "terms": terms, "sha256": _canonical_hash(terms), "revision": documents.get("glossary", {}).get("revision"), "term_sources": {kind: list(documents.get(kind, {}).get("terms", [])) for kind in ("speaker", "glossary")}}

    def apply(group, values, origin):
        for key, value in values.items():
            result[group][key] = copy.deepcopy(value)
            result["origins"][f"{group}.{key}"] = origin

    apply("asr", documents.get("speaker", {}).get("asr", {}), f"speaker:{speaker}")
    apply("preprocessing", documents.get("capture", {}).get("preprocessing", {}), f"capture:{capture}")
    applicable = []
    if speaker is not None and capture is not None:
        for recipe in documents.get("speaker", {}).get("recipes", []):
            scope = recipe["applicability"]
            if recipe["state"] == "validated_for_listed_clips" and scope.get("apply_to_matching_profiles") is True and scope["speaker_id"] == speaker and scope["capture_id"] == capture and scope["glossary_sha256"] == result["glossary"]["sha256"]:
                applicable.append(recipe)
    if len(applicable) > 1:
        raise ValueError("Multiple validated recipes match this scope; select one applicable recipe in the profile.")
    result["applied_recipe"] = None
    if applicable:
        recipe = applicable[0]
        for group in ("asr", "preprocessing"):
            apply(group, recipe.get(group, {}), f"validated_recipe:{recipe['id']}")
        result["applied_recipe"] = copy.deepcopy(recipe)
    overrides = {} if overrides is None else overrides
    _unknown(overrides, {"asr", "preprocessing", "language"}, "run overrides")
    if "language" in overrides:
        if overrides["language"] != "en":
            raise ValueError("Only English transcription is supported.")
        if "language" in overrides.get("asr", {}) and overrides["asr"]["language"] != overrides["language"]:
            raise ValueError("Conflicting language overrides.")
        result["asr"]["language"] = overrides["language"]
        result["origins"]["asr.language"] = "explicit_override"
    for group, validator in (("asr", _validate_asr), ("preprocessing", _validate_preprocessing)):
        values = overrides.get(group, {})
        validator(values)
        apply(group, values, "explicit_override")
    preprocessing = result["preprocessing"]
    if preprocessing["gain_db"] is not None and preprocessing["gain_db"] > preprocessing["max_gain_db"]:
        raise ValueError("gain_db exceeds the resolved max_gain_db policy.")
    result["schema_version"] = 1
    return result
