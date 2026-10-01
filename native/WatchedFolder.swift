import Cocoa
import CoreServices
import Darwin

struct WatchedFileSnapshot: Equatable {
    let path: String
    let filename: String
    let version: String
    let stable: Bool
    let unavailableReason: String?
}

/// A running-app-only FSEvents observer. Events are hints; every notification
/// reconciles the direct children of the selected directory. File metadata is
/// gathered away from the main thread, and stability is observed twice before
/// a candidate is offered to the independent backend verifier.
final class WatchedFolderMonitor {
    var onSnapshot: (([WatchedFileSnapshot], String, Set<String>) -> Void)?
    private(set) var folder: URL?
    private var streamSession: StreamSession?
    private var streamState: StreamState = .connecting
    private var generation = 0
    private var timer: Timer?
    private var pollTimer: Timer?
    private let pollInterval: TimeInterval
    private let slowThreshold: TimeInterval
    private let scanTimeout: TimeInterval
    private var lastSeen: [String: String] = [:]
    private var lastSeenAt: [String: TimeInterval] = [:]
    private var enumerationRunning = false
    private var refreshPending = false
    private var scanAdmissionDelayed = false
    private var pendingChangedPaths = Set<String>()
    private var pendingForceFull = false
    private var lastPublished: [WatchedFileSnapshot] = []
    private var lastScanHealthy = false
    private var scanTimedOut = false
    private var scanAttempt = 0

    private let unreadableFolderStatus = "无法读取监控文件夹；请检查 macOS 桌面访问提示，或重新选择文件夹。"

    // Injectable so a test can hold FSEventStreamCreate in flight while the
    // main thread switches roots. Production always uses the system API.
    typealias StreamFactory = (URL, FSEventStreamCallback, inout FSEventStreamContext) -> FSEventStreamRef?
    typealias SnapshotFactory = (URL) -> (files: [WatchedFileSnapshot], state: String)
    private let streamFactory: StreamFactory
    private let snapshotFactory: SnapshotFactory

    init(streamFactory: @escaping StreamFactory = { url, callback, context in
        let flags = FSEventStreamCreateFlags(kFSEventStreamCreateFlagFileEvents | kFSEventStreamCreateFlagWatchRoot)
        return FSEventStreamCreate(kCFAllocatorDefault, callback, &context,
                                   [url.path] as CFArray,
                                   FSEventStreamEventId(kFSEventStreamEventIdSinceNow), 0.25, flags)
    }, snapshotFactory: @escaping SnapshotFactory = WatchedFolderMonitor.snapshot,
       pollInterval: TimeInterval = 10, slowThreshold: TimeInterval = 5,
       scanTimeout: TimeInterval = 15) {
        self.streamFactory = streamFactory
        self.snapshotFactory = snapshotFactory
        self.pollInterval = pollInterval
        self.slowThreshold = slowThreshold
        self.scanTimeout = scanTimeout
    }

    private enum StreamState {
        case connecting
        case slow
        case active
        case failed(String)
    }

    /// FSEventStreamCreate can block in File Provider's `open` for minutes.
    /// Each generation owns its own serial queue, so an old blocked root does
    /// not prevent a newly selected root from connecting. Every CoreServices
    /// stream operation and security-scope operation stays on that queue.
    private final class StreamSession {
        weak var owner: WatchedFolderMonitor?
        let folder: URL
        let generation: Int
        let queue: DispatchQueue
        let factory: StreamFactory
        private let cancellationLock = NSLock()
        private var cancelled = false
        private var stream: FSEventStreamRef?
        private var scheduled = false
        private var started = false
        private var securityAccess = false

        init(owner: WatchedFolderMonitor, folder: URL, generation: Int, factory: @escaping StreamFactory) {
            self.owner = owner; self.folder = folder; self.generation = generation; self.factory = factory
            queue = DispatchQueue(label: "local.audio-transcribe.watched-fsevents.\(generation).\(UUID().uuidString)", qos: .utility)
        }

        private var isCancelled: Bool {
            cancellationLock.lock(); defer { cancellationLock.unlock() }
            return cancelled
        }

        func begin() {
            queue.async { [self] in
                guard !isCancelled else { return }
                // A blocked File Provider request cannot safely be cancelled.
                // Bound the number of such outstanding system calls per app.
                guard StreamCreationLimit.acquire() else {
                    notifyFailure("文件夹监控连接繁忙；稍后自动重试，也可重新选择文件夹。")
                    DispatchQueue.main.asyncAfter(deadline: .now() + 1) { [weak owner] in
                        guard let owner, owner.accepts(self) else { return }
                        self.begin()
                    }
                    return
                }
                defer { StreamCreationLimit.release() }
                securityAccess = folder.startAccessingSecurityScopedResource()
                if isCancelled { finishSecurityAccess(); return }
                var context = FSEventStreamContext(version: 0, info: Unmanaged.passUnretained(self).toOpaque(),
                                                   retain: nil, release: nil, copyDescription: nil)
                let callback: FSEventStreamCallback = { _, context, count, rawPaths, flags, _ in
                    guard let context else { return }
                    let session = Unmanaged<StreamSession>.fromOpaque(context).takeUnretainedValue()
                    var paths = Set<String>()
                    let pointers = rawPaths.assumingMemoryBound(to: UnsafePointer<CChar>.self)
                    var forceFull = false
                    for index in 0..<Int(count) {
                        paths.insert(String(cString: pointers[index]))
                        let flag = flags[index]
                        if flag & FSEventStreamEventFlags(kFSEventStreamEventFlagMustScanSubDirs |
                            kFSEventStreamEventFlagUserDropped | kFSEventStreamEventFlagKernelDropped |
                            kFSEventStreamEventFlagRootChanged) != 0 { forceFull = true }
                    }
                    session.notifyEvents(paths, forceFull: forceFull)
                }
                // This is the potentially unbounded OS call. Cancellation and
                // generation are checked again immediately when it returns.
                let created = factory(folder, callback, &context)
                guard let created else {
                    finishSecurityAccess()
                    if !isCancelled { notifyFailure("文件夹监控不可用，请刷新或重新选择文件夹。") }
                    return
                }
                if isCancelled {
                    // Invalidate is only valid after a stream is scheduled.
                    FSEventStreamRelease(created)
                    finishSecurityAccess()
                    return
                }
                stream = created
                FSEventStreamSetDispatchQueue(created, queue)
                scheduled = true
                if isCancelled {
                    teardown()
                    return
                }
                guard FSEventStreamStart(created) else {
                    teardown()
                    if !isCancelled { notifyFailure("文件夹监控不可用，请刷新或重新选择文件夹。") }
                    return
                }
                started = true
                if isCancelled { teardown(); return }
                DispatchQueue.main.async { [weak owner] in
                    owner?.streamStarted(session: self)
                }
            }
        }

        func stop() {
            cancellationLock.lock(); cancelled = true; cancellationLock.unlock()
            queue.async { [self] in teardown() }
        }

        private func teardown() {
            if let stream {
                if started { FSEventStreamStop(stream) }
                if scheduled { FSEventStreamInvalidate(stream) }
                FSEventStreamRelease(stream)
            }
            stream = nil; started = false; scheduled = false
            finishSecurityAccess()
        }

        private func finishSecurityAccess() {
            if securityAccess { folder.stopAccessingSecurityScopedResource() }
            securityAccess = false
        }

        private func notifyFailure(_ message: String) {
            DispatchQueue.main.async { [weak owner] in
                owner?.streamFailed(session: self, message: message)
            }
        }

        private func notifyEvents(_ paths: Set<String>, forceFull: Bool) {
            DispatchQueue.main.async { [weak owner] in
                owner?.streamEvents(session: self, paths: paths, forceFull: forceFull)
            }
        }
    }

    private enum StreamCreationLimit {
        private static let lock = NSLock()
        private static var pending = 0
        private static let maximum = 3

        static func acquire() -> Bool {
            lock.lock(); defer { lock.unlock() }
            guard pending < maximum else { return false }
            pending += 1
            return true
        }

        static func release() {
            lock.lock(); defer { lock.unlock() }
            pending -= 1
        }
    }

    private enum SnapshotLimit {
        private static let lock = NSLock()
        private static var pending = 0
        private static let maximum = 3

        static func acquire() -> Bool {
            lock.lock(); defer { lock.unlock() }
            guard pending < maximum else { return false }
            pending += 1
            return true
        }

        static func release() {
            lock.lock(); defer { lock.unlock() }
            pending -= 1
        }
    }

    deinit { stop() }

    func start(_ url: URL) {
        stop()
        let selected = url.standardizedFileURL
        folder = selected
        generation += 1
        streamState = .connecting
        let session = StreamSession(owner: self, folder: selected, generation: generation, factory: streamFactory)
        streamSession = session
        session.begin()
        // An early scan can populate the UI while File Provider connects. A
        // second scan after subscription closes the startup event gap.
        scheduleRefresh(after: 0)
        let expected = generation
        DispatchQueue.main.asyncAfter(deadline: .now() + slowThreshold) { [weak self] in
            guard let self, self.generation == expected, self.folder?.path == selected.path,
                  case .connecting = self.streamState else { return }
            self.streamState = .slow
            self.startPolling()
            self.onSnapshot?(self.lastPublished, self.fallbackStatus(), [])
        }
    }

    func stop() {
        timer?.invalidate(); timer = nil
        pollTimer?.invalidate(); pollTimer = nil
        generation += 1
        streamSession?.stop()
        streamSession = nil; streamState = .connecting; folder = nil
        lastSeen.removeAll(); lastSeenAt.removeAll(); pendingChangedPaths.removeAll(); pendingForceFull = false
        lastPublished.removeAll(); lastScanHealthy = false
        scanTimedOut = false; scanAttempt = 0
        enumerationRunning = false; refreshPending = false; scanAdmissionDelayed = false
    }

    private func accepts(_ session: StreamSession) -> Bool {
        generation == session.generation && streamSession === session && folder?.path == session.folder.path
    }

    private func streamStarted(session: StreamSession) {
        guard accepts(session) else { return }
        streamState = .active
        // The post-subscription metadata scan closes the event gap by version
        // comparison; unchanged files do not need backend re-hashing. Keep a
        // fallback poll until that scan confirms the root is actually usable.
        scheduleRefresh(after: 0)
    }

    private func streamFailed(session: StreamSession, message: String) {
        guard accepts(session) else { return }
        streamState = .failed(message)
        startPolling()
        onSnapshot?(lastPublished, scanTimedOut ? unreadableFolderStatus :
            (lastScanHealthy ? fallbackStatus() : message), [])
    }

    private func streamEvents(session: StreamSession, paths: Set<String>, forceFull: Bool) {
        guard accepts(session) else { return }
        pendingChangedPaths.formUnion(paths)
        pendingForceFull = pendingForceFull || forceFull
        scheduleRefresh(after: 0.35)
    }

    func refresh(forceVerify: Bool = false) {
        if forceVerify { pendingForceFull = true }
        scheduleRefresh(after: 0)
    }

    var usingPolling: Bool { pollTimer != nil }

    private func startPolling() {
        guard folder != nil, pollTimer == nil else { return }
        let polling = Timer(timeInterval: pollInterval, repeats: true) { [weak self] _ in
            self?.refresh()
        }
        polling.tolerance = min(1, pollInterval * 0.1)
        pollTimer = polling
        RunLoop.main.add(polling, forMode: .common)
    }

    private func stopPolling() {
        pollTimer?.invalidate()
        pollTimer = nil
    }

    private func fallbackStatus() -> String {
        if scanTimedOut { return unreadableFolderStatus }
        return lastScanHealthy ? "已启用（定时检查）" : "文件夹监控连接较慢；等待首次检查。"
    }

    private func scheduleRefresh(after delay: TimeInterval) {
        guard folder != nil else { return }
        timer?.invalidate()
        let scheduled = Timer(timeInterval: delay, repeats: false) { [weak self] _ in self?.enumerate() }
        timer = scheduled
        RunLoop.main.add(scheduled, forMode: .common)
    }

    private func enumerate() {
        guard let folder else { return }
        if enumerationRunning { refreshPending = true; return }
        guard SnapshotLimit.acquire() else {
            if !scanAdmissionDelayed {
                scanAdmissionDelayed = true
                onSnapshot?(lastPublished, "文件夹读取繁忙；稍后自动重试，也可手动刷新。", [])
            }
            // Do not enqueue a waiter behind a File Provider call: a single
            // timer represents the latest root and is cancelled on stop/swap.
            scheduleRefresh(after: 1.0)
            return
        }
        scanAdmissionDelayed = false
        enumerationRunning = true
        scanAttempt += 1
        let expected = generation
        let expectedAttempt = scanAttempt
        DispatchQueue.main.asyncAfter(deadline: .now() + scanTimeout) { [weak self] in
            guard let self, self.generation == expected, self.folder?.path == folder.path,
                  self.scanAttempt == expectedAttempt, self.enumerationRunning else { return }
            self.scanTimedOut = true
            self.onSnapshot?(self.lastPublished, self.unreadableFolderStatus, [])
        }
        let changed = pendingChangedPaths
        let forceFull = pendingForceFull
        pendingChangedPaths.removeAll()
        pendingForceFull = false
        let snapshotFactory = self.snapshotFactory
        DispatchQueue.global(qos: .utility).async { [weak self] in
            let result = snapshotFactory(folder)
            SnapshotLimit.release()
            DispatchQueue.main.async {
                guard let self, self.generation == expected, self.folder?.path == folder.path else { return }
                self.enumerationRunning = false
                self.scanTimedOut = false
                let now = ProcessInfo.processInfo.systemUptime
                var output: [WatchedFileSnapshot] = []
                var nextSeen: [String: String] = [:]
                var nextSeenAt: [String: TimeInterval] = [:]
                for item in result.files {
                    let previous = self.lastSeen[item.path]
                    let firstSeenAt = previous == item.version ? self.lastSeenAt[item.path] ?? now : now
                    let stable = previous == item.version && now - firstSeenAt >= 0.8
                    output.append(WatchedFileSnapshot(path: item.path, filename: item.filename,
                                                      version: item.version, stable: stable,
                                                      unavailableReason: item.unavailableReason))
                    nextSeen[item.path] = item.version; nextSeenAt[item.path] = firstSeenAt
                }
                self.lastSeen = nextSeen; self.lastSeenAt = nextSeenAt
                self.lastPublished = output
                self.lastScanHealthy = result.state == "已启用"
                if self.lastScanHealthy {
                    if case .active = self.streamState { self.stopPolling() }
                } else {
                    // WatchRoot can report deletion/rename of an active root.
                    // Until a later scan sees it again, FSEvents alone cannot
                    // guarantee a notification for recreation at that path.
                    self.startPolling()
                }
                let changedDirect = Set(output.map(\.path)).intersection(changed)
                let state: String
                if result.state != "已启用" { state = result.state }
                else {
                    switch self.streamState {
                    case .connecting: state = self.usingPolling ? self.fallbackStatus() : "正在连接文件夹监控…"
                    case .slow: state = self.fallbackStatus()
                    case .active: state = result.state
                    case .failed(let message): state = self.lastScanHealthy ? self.fallbackStatus() : message
                    }
                }
                self.onSnapshot?(output, state, forceFull ? Set(output.map(\.path)) : changedDirect)
                if self.refreshPending { self.refreshPending = false; self.scheduleRefresh(after: 0) }
                else if output.contains(where: { !$0.stable }) { self.scheduleRefresh(after: 1.0) }
            }
        }
    }

    static func snapshot(_ folder: URL) -> (files: [WatchedFileSnapshot], state: String) {
        guard let directory = Darwin.opendir(folder.path) else {
            let code = errno
            return ([], "文件夹不可用：\(String(cString: strerror(code)))")
        }
        defer { Darwin.closedir(directory) }
        var files: [WatchedFileSnapshot] = []
        while true {
            errno = 0
            guard let entry = Darwin.readdir(directory) else {
                let code = errno
                if code != 0 { return ([], "文件夹读取失败：\(String(cString: strerror(code)))") }
                break
            }
            let name = withUnsafePointer(to: entry.pointee.d_name) { pointer in
                pointer.withMemoryRebound(to: CChar.self, capacity: Int(entry.pointee.d_namlen) + 1) {
                    String(validatingUTF8: $0)
                }
            }
            guard let name, name != ".", name != "..", !name.hasPrefix(".") else { continue }
            let path = folder.path + "/" + name
            guard DirectImport.supportedFile(URL(fileURLWithPath: path)) else { continue }
            var metadata = stat()
            guard Darwin.lstat(path, &metadata) == 0 else { continue }
            guard metadata.st_mode & mode_t(S_IFMT) == mode_t(S_IFREG),
                  metadata.st_flags & UInt32(UF_HIDDEN) == 0 else { continue }
            // SF_DATALESS is the macOS file-provider placeholder flag. Never
            // open a dataless file or hand it to backend hashing/discovery.
            let unavailableReason = placeholderReason(flags: metadata.st_flags)
            // Metadata only: device/inode/size/nanosecond times and dataless
            // state detect replacement without reading or downloading bytes.
            let version = String(metadata.st_dev) + ":" + String(metadata.st_ino) + ":" +
                String(metadata.st_size) + ":" +
                String(metadata.st_mtimespec.tv_sec) + ":" + String(metadata.st_mtimespec.tv_nsec) + ":" +
                String(metadata.st_ctimespec.tv_sec) + ":" + String(metadata.st_ctimespec.tv_nsec) + ":" +
                String(metadata.st_flags)
            files.append(WatchedFileSnapshot(path: path, filename: name, version: version, stable: false,
                                             unavailableReason: unavailableReason))
        }
        files.sort { $0.filename.localizedStandardCompare($1.filename) == .orderedAscending }
        return (files, "已启用")
    }

    static func placeholderReason(flags: UInt32) -> String? {
        flags & UInt32(SF_DATALESS) != 0 ? "iCloud 文件尚未下载，请先在访达中保留本机副本。" : nil
    }
}
