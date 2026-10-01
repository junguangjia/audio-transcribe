"""Strict WAV inspection and timeline-preserving local working derivatives.

All level measurements are digital sample measurements, not speech or SNR
estimates. A null dBFS measurement denotes exact digital silence. Only the final
write quantizes samples. No operation writes to an input file.
"""

from __future__ import annotations

import hashlib
import math
import os
from pathlib import Path
import struct
import wave

import numpy as np
from scipy import ndimage, signal


OUTPUT_RATE = 16_000
_BLOCK_SAMPLES = 1_048_576
_RESAMPLE_BLOCK_SAMPLES = 262_144
_RESAMPLE_MAX_OUTPUTS = 65_536
_KNOWN_ORPHAN_SHA256 = "97efb490c10fc2a066d7cca60bfa17e984e4bcd5b88c811322491cf8c7aab132"
_KNOWN_ORPHAN_DATA_SIZE = 251_461_588
_KNOWN_ORPHAN_FILE_SIZE = 251_461_632
_SUPPORTED = {(1, 16): "PCM16", (1, 24): "PCM24", (1, 32): "PCM32",
              (3, 32): "IEEE_FLOAT32", (3, 64): "IEEE_FLOAT64"}


class AudioError(ValueError):
    """An input or processing policy cannot be handled safely."""


def _hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _fingerprint(path):
    info = Path(path).stat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _db(value):
    return None if value <= 0 else float(20 * math.log10(value))


def _correlation(sums, squares, cross, count):
    variance = squares - sums * sums / count
    denominator = math.sqrt(max(0.0, variance[0]) * max(0.0, variance[1]))
    if denominator == 0:
        return None
    return float(np.clip((cross - sums[0] * sums[1] / count) / denominator, -1, 1))


def _parse(path):
    """Read chunk boundaries, never interpreting ancillary chunks as samples."""
    path = Path(path)
    size = path.stat().st_size
    chunks = []
    fmt = None
    data = None
    with path.open("rb") as stream:
        header = stream.read(12)
        if len(header) != 12 or header[:4] != b"RIFF" or header[8:] != b"WAVE":
            raise AudioError("Expected a little-endian RIFF/WAVE file (RF64 is not supported).")
        riff_end = struct.unpack_from("<I", header, 4)[0] + 8
        if riff_end != size:
            raise AudioError(f"RIFF size mismatch: declared {riff_end} bytes, actual {size} bytes.")
        cursor = 12
        while cursor < riff_end:
            if cursor + 8 > riff_end:
                raise AudioError("Incomplete WAV chunk header.")
            stream.seek(cursor)
            chunk_id, chunk_size = struct.unpack("<4sI", stream.read(8))
            start, end = cursor + 8, cursor + 8 + chunk_size
            if end > riff_end or end + (chunk_size & 1) > riff_end:
                raise AudioError("WAV chunk extends beyond the declared container.")
            chunks.append({"id": chunk_id.decode("ascii", errors="replace"), "size_bytes": chunk_size})
            if chunk_id == b"fmt ":
                if fmt is not None or chunk_size < 16 or chunk_size > 65536:
                    raise AudioError("Missing, duplicate, or invalid WAV format chunk.")
                raw = stream.read(chunk_size)
                tag, channels, rate, byte_rate, align, bits = struct.unpack_from("<HHIIHH", raw)
                channel_mask = None
                if tag == 0xFFFE:
                    if len(raw) < 40 or struct.unpack_from("<H", raw, 16)[0] < 22:
                        raise AudioError("Incomplete WAVE_FORMAT_EXTENSIBLE format.")
                    valid_bits, channel_mask = struct.unpack_from("<HI", raw, 18)
                    if valid_bits != bits:
                        raise AudioError("Extensible WAV valid bits must equal its container bit depth.")
                    guid = raw[24:40]
                    if guid[4:] != bytes.fromhex("00001000800000aa00389b71"):
                        raise AudioError("Unsupported extensible WAV subformat GUID.")
                    tag = struct.unpack_from("<I", guid)[0]
                if (tag, bits) not in _SUPPORTED:
                    raise AudioError("Supported WAV encodings: PCM16/24/32 and IEEE float32/64.")
                if not 1 <= channels <= 64 or not 1 <= rate <= 768000:
                    raise AudioError("Unsupported WAV channel count or sample rate.")
                if align != channels * (bits // 8) or byte_rate != rate * align:
                    raise AudioError("WAV block alignment or byte rate disagrees with its format.")
                fmt = {"sample_rate": rate, "channels": channels, "bits_per_sample": bits,
                       "format_code": tag, "format": _SUPPORTED[tag, bits],
                       "block_align": align, "channel_mask": channel_mask}
            elif chunk_id == b"data":
                if data is not None:
                    raise AudioError("Multiple WAV data chunks are not supported; explicit conversion is required.")
                data = (start, chunk_size)
            cursor = end + (chunk_size & 1)
    if fmt is None or data is None:
        raise AudioError("WAV requires one format chunk and one data chunk.")
    frames, orphan = divmod(data[1], fmt["block_align"])
    if frames == 0:
        raise AudioError("WAV contains no complete audio frames.")
    return {**fmt, "size_bytes": size, "data_offset": data[0], "data_bytes": data[1],
            "complete_frames": frames, "orphan_bytes": orphan, "chunks": chunks,
            "duration_seconds": frames / fmt["sample_rate"]}


def _verify_alignment(info, digest):
    if not info["orphan_bytes"]:
        return
    matching_exception = (
        digest == _KNOWN_ORPHAN_SHA256
        and info["size_bytes"] == _KNOWN_ORPHAN_FILE_SIZE
        and info["data_bytes"] == _KNOWN_ORPHAN_DATA_SIZE
        and info["sample_rate"] == 48000 and info["channels"] == 2
        and info["format_code"] == 3 and info["bits_per_sample"] == 32
        and info["orphan_bytes"] == 4
    )
    if not matching_exception:
        raise AudioError(f"Frame misalignment: {info['orphan_bytes']} orphan byte(s). Only the exact verified supplied recording permits its four orphan bytes.")


def _read_frames(stream, info, start, end, scratch=None, decoded=None):
    stream.seek(info["data_offset"] + start * info["block_align"])
    count = end - start
    size = count * info["block_align"]
    if scratch is None:
        raw = stream.read(size)
        if len(raw) != size:
            raise AudioError("Audio changed or became incomplete during reading.")
    else:
        # Reusing the raw buffer avoids retaining one freed multi-megabyte
        # allocation per source block in macOS's process heap. All decoded
        # formats below copy to float64 before this buffer is reused.
        raw = memoryview(scratch)[:size]
        offset = 0
        while offset < size:
            received = stream.readinto(raw[offset:])
            if not received:
                raise AudioError("Audio changed or became incomplete during reading.")
            offset += received
    bits = info["bits_per_sample"]
    if decoded is not None:
        result = decoded[:count]
        target = result.reshape(-1)
    if info["format_code"] == 3:
        source = np.frombuffer(raw, dtype="<f4" if bits == 32 else "<f8")
        if decoded is None:
            result = source.astype(np.float64)
        else:
            np.copyto(target, source)
    elif bits == 24:
        octets = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        converted = (octets[:, 0] | (octets[:, 1] << 8) | (octets[:, 2] << 16))
        converted = ((converted ^ 0x800000) - 0x800000).astype(np.float64) / 8388608.0
        if decoded is None:
            result = converted
        else:
            np.copyto(target, converted)
    else:
        source = np.frombuffer(raw, dtype="<i2" if bits == 16 else "<i4")
        if decoded is None:
            result = source.astype(np.float64)
        else:
            np.copyto(target, source)
            result = target
        result /= float(2 ** (bits - 1))
    result = result.reshape(count, info["channels"])
    if not np.isfinite(result).all():
        raise AudioError(f"Non-finite audio samples in source frames {start}:{end}; no derivative was written.")
    return result


def inspect_audio(path):
    """Return JSON-safe, recomputed source diagnostics, rejecting unsafe WAVs."""
    before = _fingerprint(path)
    info = _parse(path)
    digest = _hash(path)
    _verify_alignment(info, digest)
    channels, frames = info["channels"], info["complete_frames"]
    sums, squares, peaks = (np.zeros(channels, dtype=np.float64) for _ in range(3))
    peak_frames = np.zeros(channels, dtype=np.int64)
    clips = np.zeros(channels, dtype=np.int64)
    zeros = np.zeros(channels, dtype=np.int64)
    cross = mono_square = mono_peak = 0.0
    cancellation_intervals = []
    block_frames = min(info["sample_rate"] * 5, _BLOCK_SAMPLES // channels)
    scratch = bytearray(block_frames * info["block_align"])
    decoded = np.empty((block_frames, channels), dtype=np.float64)
    # Channel-contiguous columns let argmax(axis=0) scan without allocating a
    # fresh frame-sized contiguous copy for every diagnostic block.
    absolute_buffer = np.empty(decoded.shape, dtype=np.float64, order="F")
    squared_buffer = np.empty_like(decoded)
    mono_buffer = np.empty(block_frames, dtype=np.float64)
    mono_absolute_buffer = np.empty(block_frames, dtype=np.float64)
    boolean_buffer = np.empty((block_frames, channels), dtype=np.bool_)
    with Path(path).open("rb") as stream:
        for start in range(0, frames, block_frames):
            end = min(frames, start + block_frames)
            values = _read_frames(stream, info, start, end, scratch, decoded)
            absolute = np.abs(values, out=absolute_buffer[:end - start])
            with np.errstate(over="ignore", invalid="ignore"):
                block_sums = values.sum(axis=0)
                block_squares = np.square(values, out=squared_buffer[:end - start]).sum(axis=0)
                sums += block_sums
                squares += block_squares
            if not np.isfinite(sums).all() or not np.isfinite(squares).all():
                raise AudioError("Audio magnitude exceeds the finite numeric range for safe level analysis.")
            block_peaks = absolute.max(axis=0)
            changed = block_peaks > peaks
            peak_frames[changed] = start + absolute.argmax(axis=0)[changed]
            peaks = np.maximum(peaks, block_peaks)
            clips += np.greater_equal(absolute, 1.0, out=boolean_buffer[:end - start]).sum(axis=0)
            zeros += np.equal(values, 0, out=boolean_buffer[:end - start]).sum(axis=0)
            mono = np.mean(values, axis=1, out=mono_buffer[:end - start])
            block_mono_square = float(np.dot(mono, mono))
            mono_square += block_mono_square
            mono_peak = max(mono_peak, float(np.abs(mono, out=mono_absolute_buffer[:end - start]).max()))
            if channels == 2:
                block_cross = float(np.dot(values[:, 0], values[:, 1]))
                cross += block_cross
                channel_energy = float(block_squares.mean())
                ratio = block_mono_square / channel_energy if channel_energy > 0 else 1.0
                if channel_energy > 0 and ratio < 10 ** (-12 / 10):
                    cancellation_intervals.append({
                        "start_seconds": start / info["sample_rate"],
                        "end_seconds": end / info["sample_rate"],
                        "downmix_energy_ratio_db": _db(math.sqrt(ratio)),
                        "stereo_correlation": _correlation(block_sums, block_squares, block_cross, end - start),
                    })
    if _fingerprint(path) != before:
        raise AudioError("Source changed while diagnostics were being computed; retry with a stable original.")
    channel_energy = float(squares.mean())
    ratio = mono_square / channel_energy if channel_energy else 1.0
    warnings = []
    if info["orphan_bytes"]:
        warnings.append("VERIFIED_FOUR_ORPHAN_BYTES: originals remain unchanged; derivatives use complete frames only.")
    if cancellation_intervals or (channel_energy and ratio < 10 ** (-12 / 10)):
        warnings.append("DOWNMIX_CANCELLATION: inspect the listed source intervals and select a channel when appropriate.")
    if np.any(clips):
        warnings.append("FULL_SCALE_SAMPLES: values at or above digital full scale are not evidence of recoverable analog clipping.")
    return {
        **info, "sha256": digest, "finite_samples": True,
        "channel_rms_dbfs": [_db(math.sqrt(value / frames)) for value in squares],
        "channel_peak_dbfs": [_db(value) for value in peaks],
        "channel_peak_frame": peak_frames.tolist(),
        "channel_peak_seconds": (peak_frames / info["sample_rate"]).tolist(),
        "channel_full_scale_sample_count": clips.tolist(),
        "channel_zero_sample_count": zeros.tolist(),
        "stereo_correlation": _correlation(sums, squares, cross, frames) if channels == 2 else None,
        "mean_mono_rms_dbfs": _db(math.sqrt(mono_square / frames)),
        "mean_mono_peak_dbfs": _db(mono_peak),
        "downmix_energy_ratio_db": _db(math.sqrt(ratio)),
        "cancellation_warning": bool(cancellation_intervals or (channel_energy and ratio < 10 ** (-12 / 10))),
        "cancellation_interval_count": len(cancellation_intervals),
        "representative_cancellation_intervals": cancellation_intervals[:16],
        "warnings": warnings,
        "level_interpretation": "Digital sample measurements only; not isolated speech level, SNR, identity, or intelligibility. Null dBFS means digital silence.",
    }


def _policy(value):
    defaults = {"channel": "mean", "candidate": "A", "peak_dbfs": -3.0,
                "max_gain_db": 30.0, "gain_db": None, "vad": False, "filtering": "none"}
    if value is None:
        return defaults
    if not isinstance(value, dict):
        raise AudioError("Audio policy must be a mapping.")
    extra = set(value) - set(defaults)
    if extra:
        raise AudioError(f"Unknown audio policy fields: {', '.join(sorted(extra))}")
    result = {**defaults, **value}
    if result["channel"] not in ("mean", "left", "right") or result["candidate"] not in ("A", "B"):
        raise AudioError("channel must be mean/left/right; candidate must be A/B.")
    if result["vad"] is not False or result["filtering"] != "none":
        raise AudioError("This release supports vad=false and filtering=none only; no content is gated or removed.")
    for key in ("peak_dbfs", "max_gain_db", "gain_db"):
        number = result[key]
        if key == "gain_db" and number is None:
            continue
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number):
            raise AudioError(f"{key} must be a finite number.")
        result[key] = float(number)
    if not -30 <= result["peak_dbfs"] <= -0.1:
        raise AudioError("peak_dbfs must be between -30 and -0.1 dBFS.")
    if not 0 <= result["max_gain_db"] <= 60:
        raise AudioError("max_gain_db must be between 0 and 60 dB.")
    if result["gain_db"] is not None and not -120 <= result["gain_db"] <= result["max_gain_db"]:
        raise AudioError("gain_db must be between -120 and max_gain_db.")
    return result


def _interval(info, interval):
    if interval is None:
        return 0, info["complete_frames"]
    if not isinstance(interval, (list, tuple)) or len(interval) != 2:
        raise AudioError("interval must contain source-relative start and end seconds.")
    start, end = interval
    if any(isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v) for v in interval):
        raise AudioError("Interval endpoints must be finite numbers.")
    if not 0 <= start < end <= info["duration_seconds"] + 1e-9:
        raise AudioError("Interval must be nonempty and within the complete source timeline.")
    first = int(math.floor(start * info["sample_rate"] + 0.5))
    last = min(info["complete_frames"], int(math.floor(end * info["sample_rate"] + 0.5)))
    if last <= first:
        raise AudioError("Interval contains no complete source frames after nearest-sample rounding.")
    return first, last


def _mono(path, info, channel):
    if channel == "right" and info["channels"] < 2:
        raise AudioError("Right-channel selection requires at least two source channels.")
    result = np.empty(info["complete_frames"], dtype=np.float64)
    block_frames = min(262144, _BLOCK_SAMPLES // info["channels"])
    scratch = bytearray(block_frames * info["block_align"])
    decoded = np.empty((block_frames, info["channels"]), dtype=np.float64)
    with Path(path).open("rb") as stream:
        for first in range(0, len(result), block_frames):
            last = min(len(result), first + block_frames)
            block = _read_frames(stream, info, first, last, scratch, decoded)
            result[first:last] = block.mean(axis=1) if channel == "mean" else block[:, 0 if channel == "left" else 1]
    return result


def _resample(values, rate):
    if rate == OUTPUT_RATE:
        return values.copy()
    divisor = math.gcd(rate, OUTPUT_RATE)
    # scipy's zero-phase polyphase low-pass filter preserves the first sample's
    # time origin; it returns ceil(input_frames * up/down) samples.
    return signal.resample_poly(values, OUTPUT_RATE // divisor, rate // divisor,
                                window=("kaiser", 5.0), padtype="constant")


def _resampled_blocks(path, info, channel, start, end):
    """Yield the old zero-padded polyphase result without a whole-file array.

    Block starts are multiples of the reduced down factor, so each local
    output has the same phase as scipy.signal.resample_poly on the complete
    selected interval. The input halo exceeds the FIR's support on both sides;
    actual interval edges retain scipy's constant-zero padding. This chunks
    storage and processing only, never Whisper decoding.
    """
    if channel == "right" and info["channels"] < 2:
        raise AudioError("Right-channel selection requires at least two source channels.")
    rate = info["sample_rate"]
    divisor = math.gcd(rate, OUTPUT_RATE)
    up, down = OUTPUT_RATE // divisor, rate // divisor
    input_frames = end - start
    output_frames = (input_frames * up + down - 1) // down
    # Keep decoded multichannel input bounded even at high sample rates.
    block_outputs = max(16, min(_RESAMPLE_MAX_OUTPUTS,
                                (_RESAMPLE_BLOCK_SAMPLES // info["channels"]) * up // down))
    # scipy's default Kaiser FIR has half_len = 10 * max(up, down) in
    # upsampled coordinates. Twice that source-domain radius is conservative
    # and covers the filter plus all phase/padding positions.
    halo = math.ceil(20 * max(up, down) / up) + 2
    scratch = bytearray()
    decoded = np.empty((0, info["channels"]), dtype=np.float64)
    with Path(path).open("rb") as stream:
        for first_output in range(0, output_frames, block_outputs):
            last_output = min(output_frames, first_output + block_outputs)
            first_input = max(0, (max(0, first_output * down // up - halo) // down) * down)
            last_input = min(input_frames, (last_output - 1) * down // up + halo + 1)
            required = (last_input - first_input) * info["block_align"]
            if len(scratch) < required:
                scratch = bytearray(required)
            if len(decoded) < last_input - first_input:
                decoded = np.empty((last_input - first_input, info["channels"]), dtype=np.float64)
            values = _read_frames(stream, info, start + first_input, start + last_input,
                                  scratch, decoded)
            mono = (values.mean(axis=1) if channel == "mean"
                    else values[:, 0 if channel == "left" else 1])
            sampled = _resample(mono, rate)
            local_first = first_output - first_input * up // down
            block = sampled[local_first:local_first + last_output - first_output]
            if len(block) != last_output - first_output:
                raise AudioError("Resampled block was incomplete.")
            yield block


def _frame_max(values, frame_size=320):
    return np.maximum.reduceat(values, np.arange(0, len(values), frame_size))


def _active_frames(values):
    starts = np.arange(0, len(values), 320)
    counts = np.minimum(320, len(values) - starts)
    rms = np.sqrt(np.add.reduceat(np.square(values), starts) / counts)
    whole_rms = float(np.sqrt(np.mean(np.square(values))))
    threshold = max(10 ** (-90 / 20), whole_rms * 10 ** (-30 / 20))
    return rms >= threshold, threshold


def _limit(values, requested_gain_db, ceiling):
    """Offline symmetric 18 ms lookahead envelope, no time shift or makeup.

    Activity is deliberately an amplitude-only proxy, not speech detection. The
    fixed gain is reduced until at most 1% of active 20 ms frames have more than
    0.1 dB limiting, including rare transients in that denominator. Separately
    record the fraction affected significantly (more than 1 dB).
    """
    magnitude = np.abs(values)
    guard = ndimage.maximum_filter1d(magnitude, size=321, mode="nearest")
    envelope = np.maximum(magnitude, ndimage.gaussian_filter1d(guard, sigma=32, mode="nearest", truncate=4))
    del guard, magnitude
    active, threshold = _active_frames(values)
    active_count = int(active.sum())
    gain_db = requested_gain_db
    if active_count:
        frame_peaks = _frame_max(envelope)[active]
        permitted = int(math.floor(active_count * 0.01))
        boundary = float(np.partition(frame_peaks, active_count - permitted - 1)[active_count - permitted - 1])
        if boundary > 0:
            gain_db = min(gain_db, 20 * math.log10(ceiling * 10 ** (0.1 / 20) / boundary) - 1e-9)
    elif not np.any(values):
        gain_db = 0.0
    gain = 10 ** (gain_db / 20)
    attenuation = np.minimum(1.0, ceiling / np.maximum(envelope * gain, np.finfo(float).tiny))
    reduction = -20 * np.log10(attenuation)
    significant_frames = _frame_max(reduction) > 1.0 + 1e-10
    limited_frames = _frame_max(reduction) > 0.1 + 1e-10
    affected_active = int(np.count_nonzero(significant_frames & active))
    limited_active = int(np.count_nonzero(limited_frames & active))
    output = values * gain * attenuation
    metrics = {
        "enabled": True, "algorithm": "offline_symmetric_peak_envelope_v1",
        "latency_samples": 0, "lookahead_seconds": 0.018,
        "peak_guard_half_width_seconds": 0.010,
        "envelope_smoothing_sigma_seconds": 0.002, "automatic_makeup_gain": False,
        "requested_gain_db": requested_gain_db, "applied_gain_db": gain_db,
        "gain_reduced_for_activity": gain_db < requested_gain_db - 1e-8,
        "activity_basis": "Amplitude-only proxy, not speech identification; 20 ms RMS >= max(-90 dBFS, whole-file RMS minus 30 dB).",
        "activity_threshold_dbfs": _db(threshold), "activity_frame_seconds": 0.020,
        "active_frames": active_count, "significantly_limited_active_frames": affected_active,
        "activity_cap_reduction_db": 0.1, "limited_active_frames": limited_active,
        "limited_active_fraction": limited_active / active_count if active_count else 0.0,
        "significant_reduction_db": 1.0,
        "significantly_limited_active_fraction": affected_active / active_count if active_count else 0.0,
        "limited_sample_fraction": float(np.mean(reduction > 1e-6)),
        "significantly_limited_sample_fraction": float(np.mean(reduction > 1.0)),
        "max_gain_reduction_db": float(reduction.max()),
        "mean_gain_reduction_db": float(reduction.mean()),
    }
    if metrics["limited_active_fraction"] > 0.01 + 1e-12:
        raise AudioError("Limiter activity bound could not be satisfied safely.")
    return output, gain_db, metrics


def _write_pcm16(path, values):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = np.rint(np.clip(values, -1, 32767 / 32768) * 32768).astype("<i2")
    # x mode is intentional: a rerun never overwrites an existing derivative.
    with path.open("xb") as stream:
        try:
            with wave.open(stream, "wb") as writer:
                writer.setnchannels(1)
                writer.setsampwidth(2)
                writer.setframerate(OUTPUT_RATE)
                writer.writeframes(pcm.tobytes())
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            path.unlink(missing_ok=True)
            raise
    return {"output_sha256": _hash(path), "output_frames": len(pcm),
            "output_sample_rate": OUTPUT_RATE, "output_duration_seconds": len(pcm) / OUTPUT_RATE,
            "output_peak_dbfs": _db(float(np.abs(pcm.astype(np.float64)).max()) / 32768),
            "output_rms_dbfs": _db(float(np.sqrt(np.mean(np.square(pcm.astype(np.float64) / 32768))))) }


def _write_pcm16_blocks(path, blocks):
    """Quantize and write bounded blocks with the existing PCM16 operation."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = 0
    peak = 0
    square_sum = 0
    # x mode and exception cleanup preserve the no-overwrite contract.
    with path.open("xb") as stream:
        try:
            with wave.open(stream, "wb") as writer:
                writer.setnchannels(1)
                writer.setsampwidth(2)
                writer.setframerate(OUTPUT_RATE)
                for values in blocks:
                    if not np.isfinite(values).all():
                        raise AudioError("Non-finite processing result; no derivative was written.")
                    pcm = np.rint(np.clip(values, -1, 32767 / 32768) * 32768).astype("<i2")
                    writer.writeframesraw(pcm.tobytes())
                    integral = pcm.astype(np.int64)
                    peak = max(peak, int(np.abs(integral).max()))
                    square_sum += int(np.square(integral).sum())
                    frames += len(pcm)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            path.unlink(missing_ok=True)
            raise
    try:
        digest = _hash(path)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return {"output_sha256": digest, "output_frames": frames,
            "output_sample_rate": OUTPUT_RATE, "output_duration_seconds": frames / OUTPUT_RATE,
            "output_peak_dbfs": _db(peak / 32768),
            "output_rms_dbfs": _db(math.sqrt(square_sum / (frames * 32768 ** 2)))}


def prepare_audio(source_path, output_path, policy=None, interval=None):
    """Create a new 16 kHz mono PCM16 derivative and return its provenance.

    An interval selects exact nearest-rounded source frames before resampling.
    Gain calibration always uses the full source. For sample-identical excerpt
    filtering, prepare a full derivative once and use slice_pcm16 afterwards.
    """
    selected = _policy(policy)
    if Path(output_path).exists():
        raise FileExistsError(f"Derivative already exists: {output_path}")
    before = _fingerprint(source_path)
    info = inspect_audio(source_path)
    start, end = _interval(info, interval)
    if selected["candidate"] == "A":
        peak = 0.0
        for block in _resampled_blocks(source_path, info, selected["channel"], 0, info["complete_frames"]):
            peak = max(peak, float(np.abs(block).max()))
    else:
        # The optional symmetric limiter remains on its original numerical
        # path until its envelope and activity statistics can be streamed with
        # the same whole-recording decisions.
        mono = _mono(source_path, info, selected["channel"])
        full = _resample(mono, info["sample_rate"])
        peak = float(np.abs(full).max())
    ceiling = math.floor(10 ** (selected["peak_dbfs"] / 20) * 32768) / 32768
    safe_gain = min(selected["max_gain_db"], 20 * math.log10(ceiling / peak)) if peak else 0.0
    is_full = start == 0 and end == info["complete_frames"]
    if selected["candidate"] == "A":
        gain_db = safe_gain if selected["gain_db"] is None else selected["gain_db"]
        if gain_db > safe_gain + 1e-9:
            raise AudioError("Candidate A explicit gain exceeds the full-source peak-safe gain.")
        gain = 10 ** (gain_db / 20)
        limiter = {"enabled": False, "latency_samples": 0, "automatic_makeup_gain": False,
                   "max_gain_reduction_db": 0.0, "limited_sample_fraction": 0.0}

        def prepared_blocks():
            for block in _resampled_blocks(source_path, info, selected["channel"], start, end):
                prepared = block * gain
                if np.abs(prepared).max() > ceiling + 1e-12:
                    raise AudioError("Interval-boundary resampling exceeds the full-source calibrated ceiling; prepare the full derivative then use slice_pcm16.")
                yield prepared

        if _fingerprint(source_path) != before or _hash(source_path) != info["sha256"]:
            raise AudioError("Source changed during preparation; no derivative was written.")
        output = _write_pcm16_blocks(output_path, prepared_blocks())
        try:
            unchanged = (_fingerprint(source_path) == before
                         and _hash(source_path) == info["sha256"])
        except OSError:
            unchanged = False
        if not unchanged:
            Path(output_path).unlink(missing_ok=True)
            raise AudioError("Source changed during preparation; no derivative was written.")
    else:
        working = full if is_full else _resample(mono[start:end], info["sample_rate"])
        del mono
        requested = selected["gain_db"]
        if requested is None:
            if info["sha256"] != _KNOWN_ORPHAN_SHA256:
                raise AudioError("Candidate B requires an explicit evaluated gain_db for other recordings; 21 dB is specific to the verified supplied sample.")
            requested = min(21.0, selected["max_gain_db"])
        full_limited, gain_db, full_metrics = _limit(full, requested, ceiling)
        if is_full:
            prepared, limiter = full_limited, full_metrics
        else:
            # The full-source activity cap is retained, and a narrower excerpt
            # may further reduce gain to keep its own activity bound conservative.
            prepared, gain_db, limiter = _limit(working, gain_db, ceiling)
            limiter["full_source_calibration"] = full_metrics
        del full_limited
        if not np.isfinite(prepared).all():
            raise AudioError("Non-finite processing result; no derivative was written.")
        if _fingerprint(source_path) != before or _hash(source_path) != info["sha256"]:
            raise AudioError("Source changed during preparation; no derivative was written.")
        output = _write_pcm16(output_path, prepared)
    duration = (end - start) / info["sample_rate"]
    return {
        "schema_version": 1, "source": info, "policy": selected,
        "input_frame_start": start, "input_frame_end": end, "input_frames": end - start,
        "source_start_seconds": start / info["sample_rate"], "source_end_seconds": end / info["sample_rate"],
        "duration_seconds": duration, "gain_db": gain_db, "full_source_peak_safe_gain_db": safe_gain,
        "gain_calibration": "Full-source selected-channel signal after resampling.",
        "full_source_resampled_peak_dbfs": _db(peak), "limiter": limiter,
        "resampler": {"implementation": "scipy.signal.resample_poly", "window": ["kaiser", 5.0],
                      "padding": "constant", "latency_samples": 0, "output_length_rule": "ceil(input_frames * 16000 / input_sample_rate)"},
        "quantization": "Final-only signed little-endian PCM16; round-to-nearest; no dither.",
        "derivative_only_discarded_orphan_bytes": info["orphan_bytes"],
        "timeline_changed": False, "duration_rounding_tolerance_seconds": 1 / OUTPUT_RATE,
        "duration_error_seconds": output["output_duration_seconds"] - duration,
        **output,
    }


def slice_pcm16(path, out, start, end):
    """Copy a nearest-sample interval of a prepared derivative, without reprocessing."""
    if Path(out).exists():
        raise FileExistsError(f"Derivative already exists: {out}")
    before = _fingerprint(path)
    info = inspect_audio(path)
    if (info["format_code"], info["bits_per_sample"], info["channels"], info["sample_rate"]) != (1, 16, 1, OUTPUT_RATE):
        raise AudioError("slice_pcm16 requires a 16 kHz mono PCM16 working derivative.")
    first, last = _interval(info, (start, end))
    with Path(path).open("rb") as stream:
        values = _read_frames(stream, info, first, last)[:, 0]
    if _fingerprint(path) != before:
        raise AudioError("Source derivative changed during slicing.")
    result = _write_pcm16(out, values)
    return {"schema_version": 1, "source_sha256": info["sha256"], "input_frame_start": first,
            "input_frame_end": last, "source_start_seconds": first / OUTPUT_RATE,
            "source_end_seconds": last / OUTPUT_RATE, "duration_seconds": (last - first) / OUTPUT_RATE,
            "timestamp_offset_seconds": first / OUTPUT_RATE, "operation": "sample_exact_slice",
            "duration_rounding_tolerance_seconds": 1 / OUTPUT_RATE, **result}
