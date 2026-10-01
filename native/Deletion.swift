import Foundation

enum DeletionPresentation {
    static func bytes(_ value: Any?) -> String {
        guard let number = value as? NSNumber, number.doubleValue.isFinite, number.doubleValue >= 0 else { return "未知" }
        return ByteCountFormatter.string(fromByteCount: Int64(min(number.doubleValue, Double(Int64.max / 2))), countStyle: .file)
    }
    static func detail(_ plan: [String: Any]) -> String {
        let selected = plan["selected"] as? [[String: Any]] ?? []
        var lines = selected.map { "• " + ($0["filename"] as? String ?? "所选录音") + "（\($0["variant_count"] as? Int ?? 1) 个转录版本）" }
        let remove = plan["remove"] as? [String: Any] ?? [:]
        lines += ["", "预计移除：\(remove["item_count"] as? Int ?? 0) 项，逻辑大小 \(bytes(remove["logical_bytes"]))，分配大小 \(bytes(remove["allocated_bytes"]))。"]
        let retained = plan["retained_shared"] as? [[String: Any]] ?? []
        if !retained.isEmpty {
            lines += ["", "以下共享数据仍被其他录音或历史报告使用，将保留："]
            lines += retained.map { "• \($0["reason"] as? String ?? "其他保留结果仍需要")（\(bytes($0["logical_bytes"]))）" }
        }
        lines += ["", "外部原录音、已导出的文件、共享模型、程序和其他录音不会删除。此操作在应用内无法撤销。实际磁盘可用空间变化可能与上述大小不同。"]
        if let warnings = plan["warnings"] as? [String], !warnings.isEmpty { lines += [""] + warnings }
        return lines.joined(separator: "\n")
    }
    static func resultIDs(_ plan: [String: Any]) -> Set<String> {
        Set((plan["selected"] as? [[String: Any]] ?? []).flatMap { $0["result_ids"] as? [String] ?? [] })
    }
    static func jobIDs(_ selection: [String: Any]) -> Set<UUID> {
        Set((selection["jobs"] as? [[String: Any]] ?? []).compactMap { ($0["job_id"] as? String).flatMap(UUID.init(uuidString:)) })
    }
    static func includingActiveJobs(_ plan: [String: Any], selection: [String: Any]) -> [String: Any] {
        var selection = selection
        var jobs = selection["jobs"] as? [[String: Any]] ?? []
        // Only the backend can associate an active attempt with this recording's
        // managed scope. Never infer this relationship from a name or file hash.
        for active in plan["active_jobs"] as? [[String: Any]] ?? [] {
            guard let value = active["job_id"] as? String, let id = UUID(uuidString: value) else { continue }
            if let index = jobs.firstIndex(where: { ($0["job_id"] as? String).flatMap(UUID.init(uuidString:)) == id }) {
                jobs[index].merge(active) { _, current in current }
            } else { jobs.append(active) }
        }
        selection["jobs"] = jobs
        return selection
    }
    static func isDeleteShortcut(keyCode: UInt16, command: Bool, otherModifiers: Bool, listFocused: Bool) -> Bool {
        keyCode == 51 && command && !otherModifiers && listFocused
    }
}
