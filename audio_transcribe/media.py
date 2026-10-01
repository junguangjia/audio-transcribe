"""Local, lossless working decode before the existing WAV pipeline.

Original media is always imported unchanged. Non-WAV decoding keeps source
channels/rate, measures complete decoded frames, and records its own identity.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
import signal
import subprocess
import uuid
import time
import contextlib

from .audio import inspect_audio
from .storage import _file_lock, read_doc, sha256_file, write_json
from .execution import cancellable_file_lock

EXTENSIONS = {"wav", "wave", "m4a", "mp3", "flac", "aac", "aiff", "aif", "aifc", "ogg", "oga", "opus", "mp4", "mov"}
DECODE_VERSION = "first-audio-original-rate-float32-v1"


class MediaError(ValueError):
    """Safe, user-facing decoding errors; never raw decoder log contents."""


def decoder_identity():
    root = Path(__file__).resolve().parents[1]
    manifest = root / "native" / "media-runtime.json"
    if not manifest.is_file():
        raise MediaError("The local media decoder is unavailable. Run scripts/build-app.py to install it.")
    result = read_doc(manifest)
    binary = root / result["relative_path"]
    if not binary.is_file() or sha256_file(binary) != result["sha256"]:
        raise MediaError("The local media decoder is missing or changed. Rebuild AudioTranscribe before retrying.")
    return {**result, "path": str(binary)}


def decode_media(settings, path, *, context=None):
    """Return an intact cached PCM working file and technical provenance."""
    if context:
        context.check_cancelled()
    path = Path(path).expanduser().absolute()
    if not path.is_file():
        raise MediaError("File is missing or unavailable. Reconnect its drive or choose it again.")
    if path.suffix.lower().lstrip(".") not in EXTENSIONS:
        raise MediaError("Unsupported file type. Choose a supported audio file or an MP4/MOV containing audio.")
    original_hash = sha256_file(path)
    runtime = decoder_identity()
    identity = {"source_sha256": original_hash, "decoder_sha256": runtime["sha256"], "version": DECODE_VERSION}
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    cache = Path(settings["roots"]["cache"]) / "decoded-media"
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / key
    lock_path = cache / (key + ".lock")
    with (cancellable_file_lock(lock_path, context, stage="waiting_for_preparation")
          if context else _file_lock(lock_path)):
        from .lifecycle import assert_decoded_cache_available
        assert_decoded_cache_available(settings, key)
        receipt = target / "decode.json"
        wav = target / "audio.wav"
        if receipt.is_file():
            saved = read_doc(receipt)
            if saved.get("identity") != identity or not wav.is_file() or sha256_file(wav) != saved["decoded_sha256"]:
                raise MediaError("A saved working audio copy failed its integrity check. Original media was preserved.")
            if sha256_file(path) != original_hash:
                raise MediaError("File changed while being checked. Wait until recording/copying finishes, then retry.")
            return wav, saved
        stage = cache / ("." + key + "-" + uuid.uuid4().hex + ".pending")
        stage.mkdir(mode=0o700)
        args = [runtime["path"], "-hide_banner", "-nostdin", "-xerror", "-err_detect", "explode",
                "-protocol_whitelist", "file,pipe", "-format_whitelist", "wav,mov,mp3,flac,aac,aiff,ogg",
                "-i", str(path), "-map", "0:a:0", "-vn", "-sn", "-dn", "-map_metadata", "-1",
                "-c:a", "pcm_f32le", "-rf64", "auto", "-f", "wav", str(stage / "audio.wav")]
        command = args
        if Path("/usr/bin/sandbox-exec").exists():
            command = ["/usr/bin/sandbox-exec", "-p", "(version 1)(allow default)(deny network*)"] + args
        write_json(stage / "invocation.json", {"args": args, "identity": identity})
        with context.heavy("audio_decode") if context else contextlib.nullcontext():
            child = None
            with (stage / "decoder.log").open("wb") as log:
                try:
                    if context:
                        child = context.owner.spawn(command, stdout=log, stderr=log, output_dir=stage)
                        with context.span("audio_decode"):
                            while child.poll() is None:
                                context.check_cancelled()
                                time.sleep(0.2)
                    else:
                        child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                                 start_new_session=True)
                        child.wait()
                    if child.returncode:
                        raise MediaError("Audio could not be decoded. The file may be damaged, encrypted, unsupported, or contain no audio track.")
                except BaseException:
                    if context and child is not None:
                        with contextlib.suppress(Exception):
                            child.cancel()
                    elif child is not None and child.poll() is None:
                        os.killpg(child.pid, signal.SIGTERM)
                        try:
                            child.wait(timeout=8)
                        except subprocess.TimeoutExpired:
                            os.killpg(child.pid, signal.SIGKILL)
                            child.wait()
                    raise
        try:
            if context:
                from .compute import run_audio_operation
                decoded = run_audio_operation(settings, context, "inspect", stage / "audio.wav")
            else:
                decoded = inspect_audio(stage / "audio.wav")
        except ValueError:
            raise MediaError("Decoded audio could not be validated. This backend currently requires RIFF/WAVE working files below the RIFF size limit.") from None
        if sha256_file(path) != original_hash:
            raise MediaError("File changed during decoding. Wait until recording/copying finishes, then retry.")
        saved = {"identity": identity, "decoder": runtime, "decoded_sha256": decoded["sha256"],
                 "decoded_frames": decoded["complete_frames"], "sample_rate": decoded["sample_rate"],
                 "channels": decoded["channels"], "duration_seconds": decoded["duration_seconds"],
                 "duration_basis": "complete decoded audio frames / decoded sample rate",
                 "audio_stream": "first audio stream (0:a:0)", "original_size_bytes": path.stat().st_size,
                 "original_extension": path.suffix.lower(), "original_sha256": original_hash,
                 "resampled": False, "gain_applied": False, "video_decoded": False}
        log_text = (stage / "decoder.log").read_text(errors="replace")
        container = re.search(r"Input #0, (.+?), from ", log_text)
        codec = re.search(r"Stream #0:\d+[^\n]*?Audio: ([A-Za-z0-9_]+)", log_text)
        saved["detected_container"] = container.group(1) if container else "unavailable"
        saved["detected_audio_codec"] = codec.group(1) if codec else "unavailable"
        write_json(stage / "decode.json", saved)
        os.rename(stage, target)
        return wav, saved


def working_source(settings, path, *, context=None):
    path = Path(path).expanduser()
    if path.suffix.lower() in {".wav", ".wave"}:
        return path, None
    return decode_media(settings, path, context=context)


def inspect_media(settings, path, *, context=None):
    working, media = working_source(settings, path, context=context)
    if context:
        from .compute import run_audio_operation
        info = run_audio_operation(settings, context, "inspect", working)
    else:
        info = inspect_audio(working)
    if media:
        info.update(sha256=media["original_sha256"], size_bytes=media["original_size_bytes"], media_decode=media)
    return info
