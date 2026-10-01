import Foundation
import Darwin

/// Watches only the source currently open in the reader. The file descriptor
/// notices in-place writes; the parent descriptor catches atomic replacement
/// and rename even after the original inode is gone. Neither observer reads
/// audio or survives the application process.
final class SelectedSourceMonitor {
    private struct Stamp: Equatable {
        let device: UInt64
        let inode: UInt64
        let size: Int64
        let seconds: Int64
        let nanoseconds: Int64
        let changedSeconds: Int64
        let changedNanoseconds: Int64

        init?(verified: [String: Any]) {
            guard let device = verified["device"] as? NSNumber,
                  let inode = verified["inode"] as? NSNumber,
                  let size = verified["size_bytes"] as? NSNumber,
                  let modified = verified["mtime_ns"] as? NSNumber,
                  let changed = verified["ctime_ns"] as? NSNumber else { return nil }
            self.device = device.uint64Value
            self.inode = inode.uint64Value
            self.size = size.int64Value
            let m = Self.splitNanoseconds(modified.int64Value)
            let c = Self.splitNanoseconds(changed.int64Value)
            self.seconds = m.0
            self.nanoseconds = m.1
            self.changedSeconds = c.0
            self.changedNanoseconds = c.1
        }

        private static func splitNanoseconds(_ value: Int64) -> (Int64, Int64) {
            let unit: Int64 = 1_000_000_000
            let quotient = value / unit
            let remainder = value % unit
            return remainder < 0 ? (quotient - 1, remainder + unit) : (quotient, remainder)
        }

        init?(_ path: String) {
            var info = stat()
            let descriptor = Darwin.open(path, O_EVTONLY)
            guard descriptor >= 0 else { return nil }
            defer { Darwin.close(descriptor) }
            guard Darwin.fstat(descriptor, &info) == 0,
                  (info.st_mode & mode_t(S_IFMT)) == mode_t(S_IFREG) else { return nil }
            device = UInt64(info.st_dev)
            inode = UInt64(info.st_ino)
            size = info.st_size
            seconds = Int64(info.st_mtimespec.tv_sec)
            nanoseconds = Int64(info.st_mtimespec.tv_nsec)
            changedSeconds = Int64(info.st_ctimespec.tv_sec)
            changedNanoseconds = Int64(info.st_ctimespec.tv_nsec)
        }
    }

    var onChange: ((String) -> Void)?
    private var path: String?
    private var stamp: Stamp?
    private var fileEvents: DispatchSourceFileSystemObject?
    private var directoryEvents: DispatchSourceFileSystemObject?
    private var generation = 0
    var isWatching: Bool { path != nil && fileEvents != nil && directoryEvents != nil }

    func matchesVerifiedSource() -> Bool {
        guard isWatching, let path, let stamp else { return false }
        return Stamp(path) == stamp
    }

    func stop() {
        generation += 1
        fileEvents?.cancel(); directoryEvents?.cancel()
        fileEvents = nil; directoryEvents = nil
        path = nil; stamp = nil
    }

    @discardableResult func watch(_ sourcePath: String, verifiedStamp: [String: Any]) -> Bool {
        let canonical = URL(fileURLWithPath: sourcePath).standardizedFileURL.path
        guard let expected = Stamp(verified: verifiedStamp) else { stop(); return false }
        if path == canonical, isWatching {
            if stamp == expected && matchesVerifiedSource() { return true }
            stop()
        }
        stop()
        path = canonical
        let token = generation
        let fileFD = Darwin.open(canonical, O_EVTONLY)
        if fileFD >= 0 {
            let source = DispatchSource.makeFileSystemObjectSource(
                fileDescriptor: fileFD, eventMask: [.write, .delete, .rename, .revoke, .attrib], queue: .main)
            source.setEventHandler { [weak self] in
                guard let self, self.generation == token, self.path == canonical else { return }
                self.onChange?(canonical)
            }
            source.setCancelHandler { Darwin.close(fileFD) }
            fileEvents = source; source.resume()
        }
        let parent = URL(fileURLWithPath: canonical).deletingLastPathComponent().path
        let directoryFD = Darwin.open(parent, O_EVTONLY)
        if directoryFD >= 0 {
            let source = DispatchSource.makeFileSystemObjectSource(
                fileDescriptor: directoryFD, eventMask: [.write, .delete, .rename, .revoke, .attrib], queue: .main)
            source.setEventHandler { [weak self] in
                guard let self, self.generation == token, self.path == canonical else { return }
                let latest = Stamp(canonical)
                if latest != self.stamp { self.onChange?(canonical) }
            }
            source.setCancelHandler { Darwin.close(directoryFD) }
            directoryEvents = source; source.resume()
        }
        // Arm both observers before comparing the current file with the
        // backend's post-hash identity. A replacement before arming cannot
        // silently become the monitor's trusted baseline.
        if !isWatching || Stamp(canonical) != expected { stop(); return false }
        stamp = expected
        return true
    }

    deinit { stop() }
}
