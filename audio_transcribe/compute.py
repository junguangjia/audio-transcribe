"""Cancellable ownership boundary around the unchanged numerical audio code.

NumPy/SciPy calls cannot be interrupted promptly inside a Python thread. A
short-lived child uses the existing guardian, global lock and cancellation
protocol; no chunking, resampling or decoder policy changes are introduced.
"""
from __future__ import annotations

import contextlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time


def run_audio_operation(settings, context, operation, source, *, output=None, policy=None):
    from .audio import inspect_audio, prepare_audio
    from .storage import read_doc, write_json
    if operation not in {"inspect", "prepare"}:
        raise ValueError("Unknown audio computation.")
    if context is None:
        return (inspect_audio(source) if operation == "inspect" else
                prepare_audio(source, output, policy=policy))
    scratch = Path(settings["roots"].get("cache", str(Path(settings["roots"]["data"]) / ".cache"))) / "compute"
    scratch.mkdir(parents=True, exist_ok=True)
    with context.heavy("preprocess"), tempfile.TemporaryDirectory(prefix="operation-", dir=scratch) as directory:
        directory = Path(directory)
        request = {"operation": operation, "source": str(Path(source).absolute()),
                   "job_id": context.job_id, "attempt_id": context.attempt_id,
                   "output": str(Path(output).absolute()) if output is not None else None,
                   "policy": policy}
        write_json(directory / "request.json", request)
        child = None
        with (directory / "result.json").open("wb") as result, (directory / "worker.log").open("wb") as log:
            try:
                context.check_cancelled()
                child = context.owner.spawn([sys.executable, str(Path(__file__).resolve()),
                                             str(directory / "request.json")],
                                            stdout=result, stderr=log, output_dir=directory)
                while child.poll() is None:
                    context.check_cancelled()
                    time.sleep(0.2)
                context.check_cancelled()
                if child.returncode:
                    raise ValueError("Audio preparation could not finish; the original was preserved.")
            except BaseException:
                if child is not None:
                    with contextlib.suppress(Exception):
                        child.cancel()
                raise
        value = read_doc(directory / "result.json")
        if value.get("ok") is not True:
            # Only the bounded audio validator's deliberate errors are returned.
            raise ValueError(value.get("message", "Audio validation failed."))
        return value["value"]


def _worker(request_path):
    # Executed by filename so launch CWD and inherited PYTHONPATH are irrelevant.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from audio_transcribe.audio import AudioError, inspect_audio, prepare_audio
    value = json.loads(Path(request_path).read_text())
    try:
        if value["operation"] == "inspect":
            result = inspect_audio(value["source"])
        elif value["operation"] == "prepare":
            result = prepare_audio(value["source"], value["output"], policy=value["policy"])
        else:
            raise ValueError("Unknown audio operation.")
        print(json.dumps({"ok": True, "value": result}, allow_nan=False), flush=True)
    except AudioError as error:
        print(json.dumps({"ok": False, "message": str(error)}, allow_nan=False), flush=True)


if __name__ == "__main__":
    _worker(sys.argv[1])
