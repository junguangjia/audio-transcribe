#!/usr/bin/env python3
"""Score a human-review JSON export without modifying the package or ASR files."""
import argparse
import hashlib
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from audio_transcribe.human_review import parse_json, score_review
from audio_transcribe.storage import write_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected", type=Path, required=True, help="Trusted package data/review.json")
    parser.add_argument("--review", type=Path, required=True, help="Explicitly exported human review JSON")
    parser.add_argument("--output", type=Path, required=True, help="New result JSON path; existing files are never overwritten")
    args = parser.parse_args()
    try:
        if args.output.exists():
            raise ValueError("Result path already exists; choose a new filename.")
        snapshots = {"trusted_package": args.expected.read_bytes(), "human_review_export": args.review.read_bytes()}
        result = score_review(parse_json(snapshots["trusted_package"]), parse_json(snapshots["human_review_export"]))
        result["input_files"] = {name: {"sha256": hashlib.sha256(content).hexdigest()}
                                 for name, content in snapshots.items()}
        write_json(args.output, result)
    except (ValueError, OSError, KeyError, TypeError) as error:
        # Validation messages identify fields, never quote transcript text.
        print(f"Could not score human review: {error}", file=sys.stderr)
        return 2
    print(f"Saved human-review evaluation: {args.output}")
    print("Unverified references remain unmeasured; aggregate scores cover verified clips only.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
