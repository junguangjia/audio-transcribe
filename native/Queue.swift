import Foundation

struct Recording {
    let id: UUID
    let url: URL
    var state = "waiting"
    var message: String? = nil
    var submittedIndex: Int?
    var batchID: String?
    var attemptID: String?
    var resultID: String?
    var duration: Double?
    var model = "large-v3-turbo"
    var force = false
    var autoStart = false
    var deletionPending = false
    var restartRequired = false
    var inputMode = "managed"
    var watchedVersionKey: String?
    var createdAt = Date().timeIntervalSince1970

    init(id: UUID = UUID(), url: URL) { self.id = id; self.url = url }
}

final class RecordingQueue {
    var items: [Recording] = []
    func add(_ urls: [URL]) {
        let ordered = urls.filter { $0.isFileURL }.enumerated().sorted { a, b in
            let result = a.element.lastPathComponent.compare(b.element.lastPathComponent,
                options: [.numeric, .caseInsensitive], locale: Locale(identifier: "en_US_POSIX"))
            if result != .orderedSame { return result == .orderedAscending }
            if a.element.path != b.element.path { return a.element.path < b.element.path }
            return a.offset < b.offset
        }
        items += ordered.map { Recording(url: $0.element.standardizedFileURL) }
    }
    func move(_ indexes: IndexSet, to destination: Int) {
        guard !indexes.isEmpty, indexes.allSatisfy({ items.indices.contains($0) }),
              destination >= 0, destination <= items.count else { return }
        let moving = indexes.map { items[$0] }
        let insertion = destination - indexes.filter { $0 < destination }.count
        for i in indexes.reversed() { items.remove(at: i) }
        items.insert(contentsOf: moving, at: insertion)
    }
}

enum QueuePersistence {
    static let states: Set<String> = ["waiting", "checking", "processing", "completed", "review_required", "failed", "cancelled"]

    static func snapshot(_ items: [Recording]) -> [[String: String]] {
        items.map { item in
            var value = ["path": item.url.path, "state": item.state, "job_id": item.id.uuidString]
            value["model"] = item.model
            value["force"] = item.force ? "true" : "false"
            value["deletion_pending"] = item.deletionPending ? "true" : "false"
            value["restart_required"] = item.restartRequired ? "true" : "false"
            value["input_mode"] = item.inputMode
            if let key = item.watchedVersionKey { value["watched_version_key"] = key }
            value["created_at"] = String(item.createdAt)
            if let id = item.resultID { value["result_id"] = id }
            if let duration = item.duration { value["duration"] = String(duration) }
            if let message = item.message { value["message"] = message }
            if let index = item.submittedIndex { value["submitted_index"] = String(index) }
            if let batch = item.batchID { value["batch_id"] = batch }
            if let attempt = item.attemptID { value["attempt_id"] = attempt }
            return value
        }
    }

    static func reportMatches(_ paths: [String], entries: [[String: Any]]) -> Bool {
        !paths.isEmpty && entries.count == paths.count && paths.enumerated().allSatisfy {
            entries[$0.offset]["selected_path"] as? String == $0.element
        }
    }

    static func savedTaskFinished(_ paths: [String], saved: [[String: String]], markedFinished: Bool) -> Bool {
        markedFinished && !paths.isEmpty && saved.count == paths.count && paths.enumerated().allSatisfy {
            saved[$0.offset]["path"] == $0.element && ["completed", "review_required", "failed", "cancelled"].contains(saved[$0.offset]["state"] ?? "")
        }
    }

    static func restore(_ paths: [String], saved: [[String: String]], reportEntries: [[String: Any]] = []) -> [Recording] {
        var restoredIDs = Set<UUID>()
        return paths.enumerated().map { index, path in
            let record = saved.indices.contains(index) && saved[index]["path"] == path ? saved[index] : [:]
            var id = record["job_id"].flatMap(UUID.init(uuidString:)) ?? UUID()
            if restoredIDs.contains(id) { id = UUID() }
            restoredIDs.insert(id)
            var item = Recording(id: id, url: URL(fileURLWithPath: path))
            if saved.indices.contains(index), saved[index]["path"] == path {
                item.state = saved[index]["state"] ?? "waiting"
                item.message = saved[index]["message"]
                item.resultID = saved[index]["result_id"]
                item.duration = saved[index]["duration"].flatMap(Double.init).flatMap { $0.isFinite && $0 >= 0 ? $0 : nil }
                item.model = ["large-v3-turbo", "large-v3"].contains(saved[index]["model"] ?? "") ? saved[index]["model"]! : "large-v3-turbo"
                item.force = saved[index]["force"] == "true"
                item.deletionPending = saved[index]["deletion_pending"] == "true"
                item.restartRequired = saved[index]["restart_required"] == "true"
                item.inputMode = saved[index]["input_mode"] == "referenced" ? "referenced" : "managed"
                item.watchedVersionKey = saved[index]["watched_version_key"]
                item.createdAt = saved[index]["created_at"].flatMap(Double.init) ?? item.createdAt
                item.submittedIndex = saved[index]["submitted_index"].flatMap(Int.init).flatMap { $0 >= 0 ? $0 : nil }
                item.batchID = saved[index]["batch_id"].flatMap(UUID.init(uuidString:))?.uuidString
                item.attemptID = saved[index]["attempt_id"].flatMap(UUID.init(uuidString:))?.uuidString
            } else if reportEntries.indices.contains(index), reportEntries[index]["selected_path"] as? String == path {
                item.state = reportEntries[index]["state"] as? String ?? "waiting"
            }
            if !states.contains(item.state) { item.state = "waiting"; item.message = nil }
            if ["checking", "processing"].contains(item.state) {
                item.state = "waiting"
                item.message = "上次处理已中断，点击继续。"
            }
            return item
        }
    }
}
