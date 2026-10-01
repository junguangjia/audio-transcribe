"""Short-lived parent-death guardian for one owned ASR subprocess.

Invoked as a script, with no package imports, network access, or transcript
parsing. The coordinator's control pipe is deliberately NOT inherited by the
CLI. Both guardian and CLI retain the inherited global ownership descriptor.
"""
import argparse
import contextlib
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--control-fd", type=int, required=True)
    parser.add_argument("--lock-fd", type=int, required=True)
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--nonce", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    stopping = False
    def stop(signum, frame):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGHUP, stop)
    signal.signal(signal.SIGINT, stop)
    child = None
    reason = "completed"
    def receipt(returncode, reason):
        destination = Path(args.receipt)
        temporary = destination.with_name(".guardian-" + args.nonce + ".tmp")
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump({"nonce": args.nonce, "state": "reaped", "returncode": returncode,
                       "reason": reason, "ended_monotonic": time.monotonic()}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    try:
        # If the coordinator died before startup, do not launch work at all.
        ready, _, _ = select.select([args.control_fd], [], [], 0)
        if stopping or (ready and not os.read(args.control_fd, 1)):
            receipt(130, "cancelled_before_spawn")
            return 0
        try:
            child = subprocess.Popen(command, stdin=subprocess.DEVNULL, start_new_session=True,
                                     pass_fds=(args.lock_fd,))
        except OSError:
            receipt(127, "spawn_failed")
            return 0
        while child.poll() is None:
            readable, _, _ = select.select([args.control_fd], [], [], 0.2)
            if stopping or (readable and not os.read(args.control_fd, 1)):
                reason = "parent_cancelled_or_exited"
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(child.pid, signal.SIGKILL)
                    child.wait()
                break
        child.wait()
        receipt(child.returncode, reason)
        return 0
    finally:
        if child is not None and child.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGKILL)
            child.wait()
        os.close(args.control_fd)
        os.close(args.lock_fd)


if __name__ == "__main__":
    raise SystemExit(main())
