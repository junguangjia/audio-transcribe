#!/bin/bash
set -euo pipefail
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
TEST_BUILD=$(mktemp -d "${TMPDIR:-/tmp}/audio-transcribe-layout-tests.XXXXXX")
trap 'rm -rf "$TEST_BUILD"' EXIT HUP INT TERM
sources=()
for file in "$ROOT"/native/*.swift; do
    case "$file" in
        *Tests.swift) ;;
        */main.swift)
            # Swift parses top-level expressions even in inactive #if branches
            # under -parse-as-library. Omit only the marked app entry point.
            awk '/^#if !LAYOUT_TESTING$/ {skip=1; next} skip && /^#endif$/ {skip=0; next} !skip' "$file" > "$TEST_BUILD/ApplicationController.swift"
            sources+=("$TEST_BUILD/ApplicationController.swift") ;;
        *) sources+=("$file");;
    esac
done
/usr/bin/xcrun swiftc -swift-version 5 -D LAYOUT_TESTING -parse-as-library \
    -module-cache-path "$TEST_BUILD/module-cache" "${sources[@]}" \
    "$ROOT/native/LayoutTests.swift" -o "$TEST_BUILD/layout-tests"
"$TEST_BUILD/layout-tests" "$@"
