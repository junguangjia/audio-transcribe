import Foundation

enum RecordingFilter: Int, CaseIterable {
    case all, processing, waiting, completed, review, failed, cancelled

    var title: String {
        switch self {
        case .all: return "全部任务"
        case .processing: return "处理中"
        case .waiting: return "等待中"
        case .completed: return "已完成"
        case .review: return "需要复核"
        case .failed: return "失败"
        case .cancelled: return "已取消"
        }
    }
    var symbol: String {
        switch self {
        case .all: return "tray.full"
        case .processing: return "clock.arrow.circlepath"
        case .waiting: return "clock"
        case .completed: return "checkmark.circle.fill"
        case .review: return "exclamationmark.triangle.fill"
        case .failed: return "xmark.circle.fill"
        case .cancelled: return "minus.circle"
        }
    }
    func includes(_ state: String) -> Bool {
        switch self {
        case .all: return true
        case .processing: return ["checking", "processing", "transcribing"].contains(state)
        case .waiting: return ["ready", "copying", "waiting", "unavailable"].contains(state)
        case .completed: return ["completed", "review_required"].contains(state)
        case .review: return state == "review_required"
        case .failed: return state == "failed"
        case .cancelled: return state == "cancelled"
        }
    }
}

enum RecordingSort: Int, CaseIterable {
    case importedNewest, filename, durationLongest, status
    var title: String {
        switch self {
        case .importedNewest: return "按导入时间"
        case .filename: return "按文件名"
        case .durationLongest: return "按时长"
        case .status: return "按状态"
        }
    }
}

enum RecordingRowID: Hashable {
    case job(UUID)
    case result(String)
    case watched(String, String)
}

enum RecordingSelection {
    static func indexes(in rows: [RecordingRowID], preserving selected: Set<RecordingRowID>,
                        watchedPaths: Set<String>, preferring preferred: RecordingRowID?) -> IndexSet {
        if let preferred, let index = rows.firstIndex(of: preferred) { return IndexSet(integer: index) }
        return IndexSet(rows.indices.filter { index in
            if selected.contains(rows[index]) { return true }
            if case .watched(let path, _) = rows[index] { return watchedPaths.contains(path) }
            return false
        })
    }
}

struct WatchedDiscovery {
    let path: String
    let filename: String
    let state: String
    let versionKey: String
    let resultID: String?
    let message: String?
    init?(_ value: [String: Any]) {
        guard let path = value["path"] as? String,
              let filename = value["filename"] as? String,
              let state = value["state"] as? String,
              let versionKey = value["version_key"] as? String else { return nil }
        self.path = path; self.filename = filename; self.state = state
        self.versionKey = versionKey; resultID = value["result_id"] as? String
        message = value["message"] as? String
    }
}

enum WatchedQueueIdentity {
    static func manualImportVersion(path: String, discovery: WatchedDiscovery?) -> String? {
        guard let discovery, discovery.path == path, discovery.state == "ready",
              discovery.versionKey.range(of: "^[0-9a-f]{64}$", options: .regularExpression) != nil
        else { return nil }
        return discovery.versionKey
    }
}

enum WatchedDiscoveryRetry {
    static let interval: TimeInterval = 1.2
    // Seven observations span at least 7.2 seconds from the first backend
    // check, allowing its five-second invalid-WAV grace period to expire.
    static let maxAttempts = 7
    static func shouldSchedule(after attempt: Int) -> Bool { attempt < maxAttempts }
}
