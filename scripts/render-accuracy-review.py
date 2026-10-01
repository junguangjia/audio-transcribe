#!/usr/bin/env python3
"""Render an offline human-review page without modifying review data or ASR."""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import tempfile
from urllib.parse import quote, urlsplit


def embedded_json(value):
    """JSON script elements are parsed as HTML before JavaScript reads them."""
    return (json.dumps(value, ensure_ascii=False, allow_nan=False)
            .replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def audio_url(value, package, output):
    if not isinstance(value, str) or not value or urlsplit(value).scheme:
        raise ValueError("Clip audio must be a package-relative local path.")
    path = Path(value)
    if path.is_absolute() or not (package / path).resolve().is_relative_to(package):
        raise ValueError("Clip audio must stay inside the review package.")
    resolved = (package / path).resolve()
    if not resolved.is_file():
        raise ValueError("A referenced clip audio file is missing.")
    return quote(os.path.relpath(resolved, output.parent), safe="/")


def render(review_path, output, package=None):
    review_path, output = Path(review_path).resolve(), Path(output).resolve()
    package = Path(package).resolve() if package else review_path.parent.parent
    value = json.loads(review_path.read_text(encoding="utf-8"))
    if value.get("schema_version") != 1 or not isinstance(value.get("benchmark_id"), str):
        raise ValueError("Expected a version-1 accuracy review document with benchmark_id.")
    if not isinstance(value.get("source"), dict) or not value["source"].get("sha256"):
        raise ValueError("Review source provenance is required.")
    clips = value.get("clips")
    if not isinstance(clips, list) or not clips:
        raise ValueError("Review needs at least one clip.")
    urls, identifiers = {}, set()
    for clip in clips:
        identifier = clip.get("clip_id")
        if not isinstance(identifier, str) or not identifier or identifier in identifiers:
            raise ValueError("Clip IDs must be nonempty and unique.")
        identifiers.add(identifier)
        start, end = clip.get("source_start_seconds"), clip.get("source_end_seconds")
        if (any(type(n) not in (int, float) or not math.isfinite(n) for n in (start, end))
                or not 0 <= start < end):
            raise ValueError("Each clip needs a finite, ordered original-source interval.")
        urls[identifier] = {"calibrated": audio_url(clip.get("audio_path"), package, output)}
        if clip.get("original_audio_path"):
            urls[identifier]["original"] = audio_url(clip["original_audio_path"], package, output)
    template = Path(__file__).resolve().parents[1] / "templates" / "accuracy-review.html"
    html = template.read_text(encoding="utf-8")
    for marker in ("__REVIEW_DATA__", "__AUDIO_URLS__"):
        if html.count(marker) != 1:
            raise ValueError("Review template has invalid data placeholders.")
    html = html.replace("__AUDIO_URLS__", embedded_json(urls))
    html = html.replace("__REVIEW_DATA__", embedded_json(value))
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".review-", suffix=".html", dir=output.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(html)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
    finally:
        if Path(temporary).exists():
            Path(temporary).unlink()
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("review_json", type=Path, help="Package data/review.json")
    parser.add_argument("--output", type=Path, help="Default: package/review.html")
    parser.add_argument("--package-root", type=Path, help="Default: review JSON's grandparent")
    args = parser.parse_args()
    package = args.package_root or args.review_json.resolve().parent.parent
    result = render(args.review_json, args.output or package / "review.html", package)
    print(result)


if __name__ == "__main__":
    main()
