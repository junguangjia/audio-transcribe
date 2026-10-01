import Foundation
import Darwin

@main struct SelectedSourceMonitorTests {
    static func identity(_ path: String) -> [String: Any] {
        let descriptor = Darwin.open(path, O_EVTONLY)
        precondition(descriptor >= 0)
        defer { Darwin.close(descriptor) }
        var info = stat()
        precondition(Darwin.fstat(descriptor, &info) == 0)
        return [
            "device": NSNumber(value: UInt64(info.st_dev)),
            "inode": NSNumber(value: UInt64(info.st_ino)),
            "size_bytes": NSNumber(value: Int64(info.st_size)),
            "mtime_ns": NSNumber(value: Int64(info.st_mtimespec.tv_sec) * 1_000_000_000 + Int64(info.st_mtimespec.tv_nsec)),
            "ctime_ns": NSNumber(value: Int64(info.st_ctimespec.tv_sec) * 1_000_000_000 + Int64(info.st_ctimespec.tv_nsec)),
        ]
    }

    static func main() throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent("atranscribe-monitor-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let original = directory.appendingPathComponent("source.wav")
        let replacement = directory.appendingPathComponent("replacement.wav")
        try Data("same bytes".utf8).write(to: original)
        let verified = identity(original.path)
        let monitor = SelectedSourceMonitor()
        precondition(monitor.watch(original.path, verifiedStamp: verified))
        precondition(monitor.matchesVerifiedSource())
        try Data("same bytes".utf8).write(to: replacement)
        _ = try FileManager.default.replaceItemAt(original, withItemAt: replacement)
        precondition(!monitor.matchesVerifiedSource())
        monitor.stop()
        precondition(!monitor.watch(original.path, verifiedStamp: verified))
        precondition(!monitor.isWatching)
        print("selected-source-monitor tests passed")
    }
}
