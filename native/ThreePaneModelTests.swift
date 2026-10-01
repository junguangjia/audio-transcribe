import Foundation

@main struct ThreePaneModelTests {
    static func main() {
        assert(RecordingFilter.all.includes("failed"))
        assert(RecordingFilter.processing.includes("checking"))
        assert(RecordingFilter.waiting.includes("ready"))
        assert(RecordingFilter.waiting.includes("copying"))
        assert(RecordingFilter.completed.includes("completed"))
        assert(RecordingFilter.completed.includes("review_required"))
        assert(RecordingFilter.review.includes("review_required"))
        assert(!RecordingFilter.review.includes("failed"))
        assert(RecordingFilter.failed.includes("failed"))
        assert(RecordingFilter.cancelled.includes("cancelled"))
        let item = WatchedDiscovery(["path": "/tmp/recordings/Unicode 课程.wav", "filename": "Unicode 课程.wav",
                                     "state": "ready", "version_key": "synthetic-version"])
        assert(item?.filename == "Unicode 课程.wav" && item?.versionKey == "synthetic-version")
        assert(WatchedDiscovery(["filename": "missing path", "state": "ready", "version_key": "x"]) == nil)
        let verifiedVersion = String(repeating: "a", count: 64)
        let watchedPath = "/tmp/recordings/dragged.wav"
        let readyWatch = WatchedDiscovery(["path": watchedPath, "filename": "dragged.wav",
                                           "state": "ready", "version_key": verifiedVersion])
        assert(WatchedQueueIdentity.manualImportVersion(path: watchedPath, discovery: readyWatch) == verifiedVersion)
        assert(WatchedQueueIdentity.manualImportVersion(path: "/tmp/other.wav", discovery: readyWatch) == nil)
        let processedWatch = WatchedDiscovery(["path": watchedPath, "filename": "dragged.wav",
                                               "state": "processed", "version_key": verifiedVersion])
        assert(WatchedQueueIdentity.manualImportVersion(path: watchedPath, discovery: processedWatch) == nil)
        assert(WatchedQueueIdentity.manualImportVersion(path: item!.path, discovery: item) == nil)
        let started = UUID()
        let oldWatched: RecordingRowID = .watched("/tmp/z.wav", "old-version")
        let reordered: [RecordingRowID] = [.result("alphabetically-first"), .job(started)]
        assert(RecordingSelection.indexes(in: reordered, preserving: [oldWatched],
                                          watchedPaths: ["/tmp/z.wav"], preferring: .job(started)) == IndexSet(integer: 1))
        let refreshedWatched: [RecordingRowID] = [.watched("/tmp/z.wav", "new-version")]
        assert(RecordingSelection.indexes(in: refreshedWatched, preserving: [oldWatched],
                                          watchedPaths: ["/tmp/z.wav"], preferring: nil) == IndexSet(integer: 0))
        assert(WatchedDiscoveryRetry.shouldSchedule(after: 4))
        assert(WatchedDiscoveryRetry.interval * Double(WatchedDiscoveryRetry.maxAttempts - 1) > 5.0)
        assert(!WatchedDiscoveryRetry.shouldSchedule(after: WatchedDiscoveryRetry.maxAttempts))
        print("Three-pane model tests passed: review overlap, mixed states, watched-to-job selection and Unicode names.")
    }
}
