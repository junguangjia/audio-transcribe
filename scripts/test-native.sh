#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
TEST_BUILD=$(mktemp -d "${TMPDIR:-/tmp}/audio-transcribe-native-tests.XXXXXX")
trap 'rm -rf "$TEST_BUILD"' EXIT HUP INT TERM
/usr/bin/xcrun swiftc -swift-version 5 -module-cache-path "$TEST_BUILD/module-cache" \
  "$ROOT/native/Queue.swift" "$ROOT/native/Progress.swift" "$ROOT/native/Library.swift" \
  "$ROOT/native/QueueTests.swift" -o "$TEST_BUILD/native-tests"
"$TEST_BUILD/native-tests"
/usr/bin/xcrun swiftc -swift-version 5 -module-cache-path "$TEST_BUILD/module-cache" \
  "$ROOT/native/Queue.swift" "$ROOT/native/Progress.swift" "$ROOT/native/TaskTests.swift" -o "$TEST_BUILD/task-tests"
"$TEST_BUILD/task-tests"
/usr/bin/xcrun swiftc -swift-version 5 -module-cache-path "$TEST_BUILD/module-cache" \
  "$ROOT/native/Queue.swift" "$ROOT/native/Progress.swift" "$ROOT/native/Direct.swift" \
  "$ROOT/native/DirectTests.swift" -o "$TEST_BUILD/direct-tests"
"$TEST_BUILD/direct-tests"

/usr/bin/xcrun swiftc -swift-version 5 -module-cache-path "$TEST_BUILD/module-cache" \
  "$ROOT/native/Queue.swift" "$ROOT/native/Progress.swift" "$ROOT/native/Direct.swift" \
  "$ROOT/native/Deletion.swift" "$ROOT/native/RepairTests.swift" -o "$TEST_BUILD/repair-tests"
"$TEST_BUILD/repair-tests"
/usr/bin/xcrun swiftc -swift-version 5 -module-cache-path "$TEST_BUILD/module-cache" \
  "$ROOT/native/ThreePaneModel.swift" "$ROOT/native/ThreePaneModelTests.swift" \
  -o "$TEST_BUILD/three-pane-tests"
"$TEST_BUILD/three-pane-tests"
/usr/bin/xcrun swiftc -swift-version 5 -module-cache-path "$TEST_BUILD/module-cache" \
  "$ROOT/native/SelectedSourceMonitor.swift" "$ROOT/native/SelectedSourceMonitorTests.swift" \
  -o "$TEST_BUILD/selected-source-monitor-tests"
"$TEST_BUILD/selected-source-monitor-tests"
/usr/bin/xcrun swiftc -swift-version 5 -module-cache-path "$TEST_BUILD/module-cache" \
  "$ROOT/native/Queue.swift" "$ROOT/native/Progress.swift" "$ROOT/native/Direct.swift" \
  "$ROOT/native/WatchedFolder.swift" "$ROOT/native/WatchedFolderTests.swift" \
  -o "$TEST_BUILD/watched-folder-tests"
"$TEST_BUILD/watched-folder-tests"
