import Cocoa
import CoreServices

@main struct WatchedFolderTests {
    static func until(_ seconds: TimeInterval, _ condition: () -> Bool) -> Bool {
        let end = Date().addingTimeInterval(seconds)
        while Date() < end {
            if condition() { return true }
            _ = RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.05))
        }
        return condition()
    }

    static func makeStream(_ url: URL, _ callback: FSEventStreamCallback,
                           _ context: inout FSEventStreamContext) -> FSEventStreamRef? {
        let flags = FSEventStreamCreateFlags(kFSEventStreamCreateFlagFileEvents | kFSEventStreamCreateFlagWatchRoot)
        return FSEventStreamCreate(kCFAllocatorDefault, callback, &context, [url.path] as CFArray,
                                   FSEventStreamEventId(kFSEventStreamEventIdSinceNow), 0.1, flags)
    }

    static func main() throws {
        if CommandLine.arguments.count == 3 && CommandLine.arguments[1] == "--probe-folder" {
            // Read-only native probe. Report counts only; never open audio bytes
            // or print the user's filenames.
            let started = ProcessInfo.processInfo.systemUptime
            let result = WatchedFolderMonitor.snapshot(URL(fileURLWithPath: CommandLine.arguments[2], isDirectory: true))
            let unavailable = result.files.filter { $0.unavailableReason != nil }.count
            print("native-folder-probe state=\(result.state) supported=\(result.files.count) unavailable=\(unavailable) elapsed=\(String(format: "%.3f", ProcessInfo.processInfo.systemUptime - started))s")
            return
        }
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("atranscribe-watch-\(UUID().uuidString)")
        let first = root.appendingPathComponent("first", isDirectory: true)
        let second = root.appendingPathComponent("second", isDirectory: true)
        let failed = root.appendingPathComponent("failed", isDirectory: true)
        for path in [first, second, failed] { try FileManager.default.createDirectory(at: path, withIntermediateDirectories: true) }
        defer { try? FileManager.default.removeItem(at: root) }

        let posix = root.appendingPathComponent("posix", isDirectory: true)
        try FileManager.default.createDirectory(at: posix, withIntermediateDirectories: true)
        let unicodeFile = posix.appendingPathComponent("课程录音.WAV")
        try Data("a".utf8).write(to: unicodeFile)
        try Data("hidden".utf8).write(to: posix.appendingPathComponent(".hidden.wav"))
        try FileManager.default.createDirectory(at: posix.appendingPathComponent("folder.wav", isDirectory: true), withIntermediateDirectories: true)
        try FileManager.default.createSymbolicLink(at: posix.appendingPathComponent("alias.wav"), withDestinationURL: unicodeFile)
        try Data("other".utf8).write(to: posix.appendingPathComponent("other.txt"))
        let firstPosix = WatchedFolderMonitor.snapshot(posix)
        precondition(firstPosix.state == "已启用" && firstPosix.files.count == 1 &&
            firstPosix.files[0].filename == "课程录音.WAV" && firstPosix.files[0].path == unicodeFile.path,
            "POSIX enumeration must preserve Unicode and skip hidden, symlink, directory and unsupported files")
        precondition(WatchedFolderMonitor.placeholderReason(flags: UInt32(SF_DATALESS)) != nil &&
            WatchedFolderMonitor.placeholderReason(flags: 0) == nil,
            "dataless File Provider placeholders must remain unavailable")
        try Data("changed bytes".utf8).write(to: unicodeFile)
        precondition(WatchedFolderMonitor.snapshot(posix).files[0].version != firstPosix.files[0].version,
                     "metadata versions must detect a changed file without reading its bytes")

        let firstEntered = DispatchSemaphore(value: 0)
        let releaseFirst = DispatchSemaphore(value: 0)
        var publications: [(String, String, [WatchedFileSnapshot])] = []
        let monitor = WatchedFolderMonitor(streamFactory: { url, callback, context in
            if url.path == first.path {
                firstEntered.signal()
                releaseFirst.wait()
            }
            if url.path == failed.path { return nil }
            return makeStream(url, callback, &context)
        })
        monitor.onSnapshot = { files, state, _ in
            publications.append((monitor.folder?.path ?? "", state, files))
        }

        let start = ProcessInfo.processInfo.systemUptime
        monitor.start(first)
        precondition(ProcessInfo.processInfo.systemUptime - start < 0.2,
                     "a slow FSEvents create must not block the main thread")
        precondition(firstEntered.wait(timeout: .now() + 2) == .success)
        let switchStart = ProcessInfo.processInfo.systemUptime
        monitor.start(second)
        precondition(ProcessInfo.processInfo.systemUptime - switchStart < 0.2,
                     "a blocked old root must not block root switching")
        precondition(until(3) { publications.contains { $0.0 == second.path && $0.1 == "已启用" } },
                     "the new root must become active while the old create is blocked")

        // The initial scan and an FSEvents-triggered scan should both publish
        // metadata only. The monitor has no ASR/client side effects.
        let newFile = second.appendingPathComponent("new.wav")
        try Data("fixture".utf8).write(to: newFile)
        precondition(until(4) { publications.contains { $0.0 == second.path &&
            $0.2.contains { $0.path == newFile.path && $0.stable } } },
                     "a new file must be discovered and reach the stable state")

        let countBeforeOldCompletion = publications.count
        releaseFirst.signal()
        _ = until(0.5) { false }
        precondition(publications.dropFirst(countBeforeOldCompletion).allSatisfy { $0.0 == second.path },
                     "the stale first stream must never publish into the new root")

        monitor.start(failed)
        precondition(until(3) { publications.contains { $0.0 == failed.path && $0.1.contains("不可用") } },
                     "stream creation failures need a visible, retryable state")
        monitor.start(second)
        precondition(until(3) { publications.last?.0 == second.path && publications.last?.1 == "已启用" },
                     "a failed root must not prevent later recovery")

        monitor.stop()
        let stoppedCount = publications.count
        try Data("later".utf8).write(to: second.appendingPathComponent("later.wav"))
        _ = until(0.5) { false }
        precondition(publications.count == stoppedCount, "stopped streams must not publish")

        // Three old File Provider-style scans can remain stuck after rapid
        // root switches. A fourth root must keep the main thread responsive,
        // show a truthful busy state, and retry once any call returns.
        let roots = (0..<4).map { root.appendingPathComponent("snapshot-\($0)", isDirectory: true) }
        for path in roots { try FileManager.default.createDirectory(at: path, withIntermediateDirectories: true) }
        let entered = (0..<3).map { _ in DispatchSemaphore(value: 0) }
        let release = (0..<3).map { _ in DispatchSemaphore(value: 0) }
        var scanPublications: [(String, String)] = []
        let scanMonitor = WatchedFolderMonitor(streamFactory: makeStream, snapshotFactory: { url in
            if let index = roots.firstIndex(where: { $0.path == url.path }), index < 3 {
                entered[index].signal()
                release[index].wait()
            }
            return ([], "已启用")
        })
        scanMonitor.onSnapshot = { _, state, _ in
            scanPublications.append((scanMonitor.folder?.path ?? "", state))
        }
        for index in 0..<3 {
            scanMonitor.start(roots[index])
            precondition(until(2) { entered[index].wait(timeout: .now()) == .success },
                         "the simulated old scan must be in flight")
        }
        let fourthStart = ProcessInfo.processInfo.systemUptime
        scanMonitor.start(roots[3])
        precondition(ProcessInfo.processInfo.systemUptime - fourthStart < 0.2,
                     "the scan cap must not block the main thread")
        precondition(until(2) { scanPublications.contains { $0.0 == roots[3].path && $0.1.contains("读取繁忙") } },
                     "the current root needs a visible bounded-scan state")
        release[0].signal()
        precondition(until(3) { scanPublications.contains { $0.0 == roots[3].path && $0.1 == "已启用" } },
                     "the current root must automatically retry when an old scan frees capacity")
        let previousFourth = scanPublications.filter { $0.0 == roots[3].path && $0.1 == "已启用" }.count
        scanMonitor.refresh(forceVerify: true)
        precondition(until(2) { scanPublications.filter { $0.0 == roots[3].path && $0.1 == "已启用" }.count > previousFourth },
                     "manual refresh must still work after a delayed scan")
        release[1].signal(); release[2].signal()
        scanMonitor.stop()

        let connectionRoots = (0..<4).map { root.appendingPathComponent("connection-\($0)", isDirectory: true) }
        for path in connectionRoots { try FileManager.default.createDirectory(at: path, withIntermediateDirectories: true) }
        let connectionEntered = (0..<3).map { _ in DispatchSemaphore(value: 0) }
        let connectionRelease = (0..<3).map { _ in DispatchSemaphore(value: 0) }
        var connectionPublications: [(String, String)] = []
        let connectionMonitor = WatchedFolderMonitor(streamFactory: { url, callback, context in
            if let index = connectionRoots.firstIndex(where: { $0.path == url.path }), index < 3 {
                connectionEntered[index].signal()
                connectionRelease[index].wait()
            }
            return makeStream(url, callback, &context)
        })
        connectionMonitor.onSnapshot = { _, state, _ in
            connectionPublications.append((connectionMonitor.folder?.path ?? "", state))
        }
        for index in 0..<3 {
            connectionMonitor.start(connectionRoots[index])
            precondition(until(2) { connectionEntered[index].wait(timeout: .now()) == .success },
                         "the simulated system connection must be in flight")
        }
        connectionMonitor.start(connectionRoots[3])
        precondition(until(2) { connectionPublications.contains { $0.0 == connectionRoots[3].path && $0.1.contains("连接繁忙") } },
                     "the connection cap needs a visible bounded state")
        connectionRelease[0].signal()
        precondition(until(3) { connectionPublications.contains { $0.0 == connectionRoots[3].path && $0.1 == "已启用" } },
                     "the latest root must automatically connect after a stale call returns")
        connectionRelease[1].signal(); connectionRelease[2].signal()
        connectionMonitor.stop()

        let pollFolder = root.appendingPathComponent("poll", isDirectory: true)
        try FileManager.default.createDirectory(at: pollFolder, withIntermediateDirectories: true)
        let pollEntered = DispatchSemaphore(value: 0)
        let releasePollStream = DispatchSemaphore(value: 0)
        var pollPublications: [(String, [WatchedFileSnapshot], Set<String>)] = []
        let pollMonitor = WatchedFolderMonitor(streamFactory: { url, callback, context in
            pollEntered.signal()
            releasePollStream.wait()
            return makeStream(url, callback, &context)
        }, pollInterval: 0.1, slowThreshold: 0.05)
        pollMonitor.onSnapshot = { files, state, changed in
            pollPublications.append((state, files, changed))
        }
        pollMonitor.start(pollFolder)
        precondition(until(2) { pollEntered.wait(timeout: .now()) == .success })
        precondition(until(2) { pollMonitor.usingPolling &&
            pollPublications.contains { $0.0 == "已启用（定时检查）" } },
            "a blocked FSEvents stream must enable a truthful polling fallback")
        let pollFile = pollFolder.appendingPathComponent("polled.wav")
        try Data("new".utf8).write(to: pollFile)
        precondition(until(3) { pollPublications.contains { publication in
            publication.1.contains { $0.path == pollFile.path && $0.stable } } },
            "polling must discover a new direct child without FSEvents")
        precondition(pollPublications.allSatisfy { $0.2.isEmpty },
                     "ordinary polling must not force backend re-verification of unchanged files")
        releasePollStream.signal()
        precondition(until(2) { !pollMonitor.usingPolling && pollPublications.contains { $0.0 == "已启用" } },
                     "a late FSEvents connection must replace polling and rescan")
        pollMonitor.stop()
        precondition(!pollMonitor.usingPolling)

        let delayedRoot = root.appendingPathComponent("delayed-snapshot", isDirectory: true)
        let nextRoot = root.appendingPathComponent("next-snapshot", isDirectory: true)
        try FileManager.default.createDirectory(at: delayedRoot, withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: nextRoot, withIntermediateDirectories: true)
        let scanEntered = DispatchSemaphore(value: 0)
        let releaseScan = DispatchSemaphore(value: 0)
        var delayedPublications: [(String, String)] = []
        let delayedMonitor = WatchedFolderMonitor(streamFactory: makeStream, snapshotFactory: { url in
            if url.path == delayedRoot.path {
                scanEntered.signal()
                releaseScan.wait()
            }
            return WatchedFolderMonitor.snapshot(url)
        }, pollInterval: 0.05, slowThreshold: 0.05, scanTimeout: 0.15)
        delayedMonitor.onSnapshot = { _, state, _ in
            delayedPublications.append((delayedMonitor.folder?.path ?? "", state))
        }
        delayedMonitor.start(delayedRoot)
        precondition(until(2) { scanEntered.wait(timeout: .now()) == .success })
        precondition(until(2) { delayedPublications.contains { $0.0 == delayedRoot.path &&
            $0.1.contains("无法读取监控文件夹") } },
            "a blocked initial snapshot must report an actionable timeout")
        precondition(!delayedPublications.contains { $0.0 == delayedRoot.path && $0.1.hasPrefix("已启用") },
                     "a timer must not claim successful polling before a healthy scan")
        releaseScan.signal()
        precondition(until(3) { delayedPublications.contains { $0.0 == delayedRoot.path &&
            $0.1.hasPrefix("已启用") } },
            "the same monitor must recover automatically when folder access completes")

        let staleScanEntered = DispatchSemaphore(value: 0)
        let releaseStaleScan = DispatchSemaphore(value: 0)
        var nextPublications: [(String, String)] = []
        let nextMonitor = WatchedFolderMonitor(streamFactory: makeStream, snapshotFactory: { url in
            if url.path == delayedRoot.path {
                staleScanEntered.signal()
                releaseStaleScan.wait()
            }
            return WatchedFolderMonitor.snapshot(url)
        }, scanTimeout: 0.15)
        nextMonitor.onSnapshot = { _, state, _ in
            nextPublications.append((nextMonitor.folder?.path ?? "", state))
        }
        nextMonitor.start(delayedRoot)
        precondition(until(2) { staleScanEntered.wait(timeout: .now()) == .success })
        nextMonitor.start(nextRoot)
        precondition(until(3) { nextPublications.contains { $0.0 == nextRoot.path && $0.1 == "已启用" } },
                     "a new root must scan while the old snapshot remains blocked")
        _ = until(0.3) { false }
        precondition(!nextPublications.contains { $0.0 == nextRoot.path &&
            $0.1.contains("无法读取监控文件夹") },
            "an old scan timeout must not overwrite the newly selected root")
        releaseStaleScan.signal()
        delayedMonitor.stop(); nextMonitor.stop()

        let previouslyHealthyRoot = root.appendingPathComponent("healthy-then-blocked", isDirectory: true)
        try FileManager.default.createDirectory(at: previouslyHealthyRoot, withIntermediateDirectories: true)
        let retainedFile = previouslyHealthyRoot.appendingPathComponent("retained.wav")
        try Data("fixture".utf8).write(to: retainedFile)
        let postHealthyLock = NSLock()
        var blockLaterScans = false
        let postHealthyEntered = DispatchSemaphore(value: 0)
        let releasePostHealthy = DispatchSemaphore(value: 0)
        var postHealthyPublications: [(String, [WatchedFileSnapshot])] = []
        let postHealthyMonitor = WatchedFolderMonitor(streamFactory: makeStream, snapshotFactory: { url in
            postHealthyLock.lock()
            let shouldBlock = blockLaterScans
            postHealthyLock.unlock()
            if shouldBlock {
                postHealthyEntered.signal()
                releasePostHealthy.wait()
            }
            return WatchedFolderMonitor.snapshot(url)
        }, scanTimeout: 0.15)
        postHealthyMonitor.onSnapshot = { files, state, _ in
            postHealthyPublications.append((state, files))
        }
        postHealthyMonitor.start(previouslyHealthyRoot)
        precondition(until(4) { postHealthyPublications.contains { $0.0 == "已启用" &&
            $0.1.contains { $0.path == retainedFile.path && $0.stable } } },
            "the root must be healthy before the later scan is blocked")
        let healthyBeforeBlock = postHealthyPublications.filter { $0.0 == "已启用" }.count
        postHealthyLock.lock(); blockLaterScans = true; postHealthyLock.unlock()
        postHealthyMonitor.refresh()
        precondition(until(2) { postHealthyEntered.wait(timeout: .now()) == .success })
        precondition(until(2) { postHealthyPublications.contains { $0.0.contains("无法读取监控文件夹") &&
            $0.1.contains { $0.path == retainedFile.path } } },
            "a later blocked scan must warn while retaining previously published files")
        postHealthyLock.lock(); blockLaterScans = false; postHealthyLock.unlock()
        releasePostHealthy.signal()
        precondition(until(3) { postHealthyPublications.filter { $0.0 == "已启用" }.count > healthyBeforeBlock },
                     "a later blocked scan must restore the healthy state when it completes")
        postHealthyMonitor.stop()

        let recoveryRoot = root.appendingPathComponent("recovery", isDirectory: true)
        let renamedRoot = root.appendingPathComponent("recovery-moved", isDirectory: true)
        try FileManager.default.createDirectory(at: recoveryRoot, withIntermediateDirectories: true)
        var recoveryPublications: [(String, [WatchedFileSnapshot])] = []
        let recoveryMonitor = WatchedFolderMonitor(pollInterval: 0.1, slowThreshold: 5)
        recoveryMonitor.onSnapshot = { files, state, _ in
            recoveryPublications.append((state, files))
        }
        recoveryMonitor.start(recoveryRoot)
        precondition(until(3) { recoveryPublications.contains { $0.0 == "已启用" } },
                     "the temporary root must become active before rename")
        try FileManager.default.moveItem(at: recoveryRoot, to: renamedRoot)
        precondition(until(4) { recoveryMonitor.usingPolling &&
            recoveryPublications.contains { $0.0.contains("不可用") } },
            "an active root rename must trigger a bounded recovery poll")
        try FileManager.default.createDirectory(at: recoveryRoot, withIntermediateDirectories: true)
        let recoveredFile = recoveryRoot.appendingPathComponent("recovered.wav")
        try Data("recovered".utf8).write(to: recoveredFile)
        precondition(until(4) { recoveryPublications.contains { $0.0 == "已启用" &&
            $0.1.contains { $0.path == recoveredFile.path && $0.stable } } },
            "the recreated root must discover files without manual refresh")
        precondition(until(2) { !recoveryMonitor.usingPolling },
                     "polling should stop only after an active and healthy scan")
        let laterFile = recoveryRoot.appendingPathComponent("after-recovery.wav")
        try Data("later".utf8).write(to: laterFile)
        precondition(until(4) { recoveryPublications.contains { $0.1.contains { $0.path == laterFile.path && $0.stable } } },
                     "the recovered root must keep detecting new files after polling stops")
        recoveryMonitor.stop()
        print("watched-folder tests passed: POSIX metadata, dataless guard, bounded async connection/scans, polling, blocked-scan timeout, root recreation and recovery")
    }
}
