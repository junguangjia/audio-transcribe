import Foundation

enum DirectImport {
    static let extensions: Set<String> = ["wav", "wave", "m4a", "mp3", "flac", "aac", "aiff", "aif", "aifc", "ogg", "oga", "opus", "mp4", "mov"]
    static func supportedFile(_ url: URL) -> Bool {
        url.isFileURL && extensions.contains(url.pathExtension.lowercased())
    }
}

/// User-facing defaults are isolated by the application's bundle identifier.
enum DirectSettings {
    static let models = ["large-v3-turbo", "large-v3"]
    static let formats = ["md", "txt", "srt", "json"]
    static let executionModes = ["auto", "serial"]
    static func model(_ value: String?) -> String { models.contains(value ?? "") ? value! : models[0] }
    static func format(_ value: String?) -> String { formats.contains(value ?? "") ? value! : "md" }
    static func executionMode(_ value: String?) -> String { value == "serial" ? "serial" : "auto" }
    static func executionTitle(_ value: String?) -> String {
        executionMode(value) == "serial" ? "逐个处理" : "自动并行"
    }
    static func exportName(filename: String, format: String) -> String {
        URL(fileURLWithPath: filename).deletingPathExtension().lastPathComponent + "_transcript." + Self.format(format)
    }
    static func exportDestination(_ url: URL, format: String) -> URL {
        let ext = Self.format(format)
        // The save panel does not rely on Launch Services type registration.
        // Preserve a typed basename and append the selected format if needed.
        return url.pathExtension.lowercased() == ext ? url : url.appendingPathExtension(ext)
    }
    static func environment(bundle: Bundle = .main) -> [String: String] {
        var result = ProcessInfo.processInfo.environment
        if let path = bundle.object(forInfoDictionaryKey: "AudioTranscribeSettingsPath") as? String, !path.isEmpty {
            result["AUDIO_TRANSCRIBE_SETTINGS"] = path
        }
        return result
    }
}

struct DirectResult {
    let value: [String: Any]
    var id: String { value["result_id"] as? String ?? "" }
    var filename: String { value["filename"] as? String ?? "转录结果" }
    var state: String { value["state"] as? String ?? "unknown" }
    var model: String { value["model_label"] as? String ?? value["model"] as? String ?? "未知模型" }
    var duration: Double? { (value["duration_seconds"] as? Double).flatMap { $0.isFinite && $0 >= 0 ? $0 : nil } }
    var audioPath: String? { value["audio_path"] as? String }
    var markdownPath: String? { value["markdown_path"] as? String ?? value["report"] as? String }
    var text: String { value["readable_text"] as? String ?? value["plain_text"] as? String ?? "" }
    var copyText: String { value["copy_text"] as? String ?? value["plain_text"] as? String ?? text }
    var copyAllowed: Bool { (value["copy_allowed"] as? Bool ?? !legacy) && !copyText.isEmpty }
    var segments: [[String: Any]] { value["segments"] as? [[String: Any]] ?? [] }
    var formats: [String] { (value["available_formats"] as? [String] ?? ["md", "txt"]).filter { DirectSettings.formats.contains($0) } }
    var legacy: Bool { value["legacy"] as? Bool ?? false }
    var warning: String? {
        let message = value["quality_message"] as? String
        if value["source_integrity"] as? String == "pending" {
            return "正文已可阅读；正在验证原录音，验证完成后才可播放和重新转录。"
        }
        if value["source_integrity"] as? String == "verification_failed" {
            return "原录音校验未完成；请重新选择录音重试。已保存的正文仍可阅读和导出。"
        }
        if value["source_integrity"] as? String == "missing_or_changed" {
            return "原录音已丢失或改变；播放和重新转录已停用。已保存的正文仍可阅读和导出。"
        }
        if value["integrity"] as? String == "modified" || (value["provenance"] as? [String: Any])?["integrity"] as? String == "modified" { return "结果导出后已被编辑；原有检查不适用于当前文本。" }
        if state == "review_required" || value["quality_status"] as? String == "review_required" {
            return "需复核：" + (message?.isEmpty == false ? message! : "自动检查发现可疑文本或时间戳。正文完整保留，请结合原音核对。")
        }
        if state == "failed" { return message ?? "转录失败，请重新转录。" }
        if value["integrity"] as? String == "modified" { return "结果导出后已被编辑；原有检查不适用于当前文本。" }
        return nil
    }
    static func stateLabel(_ state: String) -> String {
        ["checking":"正在检查", "waiting":"等待转录", "processing":"正在转录", "ready":"新录音 · 等待开始",
         "copying":"正在复制，稍后可用", "ignored":"已忽略 · 可重新加入", "unavailable":"暂时不可用",
         "completed":"已完成", "review_required":"已生成 · 需复核", "failed":"失败 · 可重试",
         "cancelled":"已取消"][state] ?? "历史结果"
    }
    static func timestamp(_ value: Double) -> String {
        guard value.isFinite else { return "时间未知" }
        let milliseconds = Int((min(abs(value), Double(Int.max / 2000)) * 1000).rounded())
        let total = milliseconds / 1000
        return (value < 0 ? "−" : "") + String(format: "%02d:%02d:%02d.%03d", total / 3600, total / 60 % 60, total % 60, milliseconds % 1000)
    }
    static func generatedTime(_ value: Any?) -> String {
        guard let raw = value as? String else { return "生成时间未知" }
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let date = formatter.date(from: raw) ?? ISO8601DateFormatter().date(from: raw)
        guard let date else { return raw }
        let local = DateFormatter(); local.dateFormat = "yyyy-MM-dd HH:mm"
        return local.string(from: date)
    }
}

enum DirectQueue {
    static func idleSummary(_ items: [Recording]) -> String? {
        guard !items.contains(where: { $0.autoStart || ["checking", "processing"].contains($0.state) }) else { return nil }
        guard !items.isEmpty else { return "导入即开始转录，每个录音生成独立结果。" }
        let counts = ["completed", "review_required", "failed", "cancelled", "waiting"].map { state in items.filter { $0.state == state }.count }
        return "任务已更新：完成 \(counts[0]) · 需复核 \(counts[1]) · 失败 \(counts[2]) · 取消 \(counts[3]) · 待继续 \(counts[4])。点击查看结果即可阅读。"
    }
    static func resuming(_ item: Recording) -> Recording {
        var next = item
        // Explicit retry of a failed result starts fresh; ordinary interruption
        // keeps cache/checkpoint recovery. IDs remain stable across attempts.
        if next.state == "failed" { next.force = true }
        next.state = "waiting"; next.restartRequired = false; next.message = nil; next.autoStart = true
        next.batchID = nil; next.attemptID = nil; next.submittedIndex = nil
        return next
    }
    /// Internal submissions share scheduling only. Every item retains its own result.
    static func nextSubmission(_ items: [Recording]) -> [Recording] {
        guard let first = items.first(where: { $0.state == "waiting" && $0.autoStart && !$0.deletionPending }) else { return [] }
        return items.filter { $0.state == "waiting" && $0.autoStart && !$0.deletionPending && $0.model == first.model && $0.force == first.force }
    }
    static func submissionPayload(_ items: [Recording], batchID: UUID, submitted: TimeInterval,
                                  executionMode: String?) -> [String: Any] {
        precondition(!items.isEmpty)
        return ["files": items.map { $0.url.path }, "protocol_version": 2,
                "batch_id": batchID.uuidString, "submitted_monotonic": submitted,
                "items": items.enumerated().map { indexed -> [String: Any] in
                    let index = indexed.offset, item = indexed.element
                    var entry: [String: Any] = ["job_id": item.id.uuidString, "path": item.url.path, "index": index,
                                                "input_mode": item.inputMode]
                    if item.inputMode == "referenced", let version = item.watchedVersionKey {
                        entry["watched_version_key"] = version
                    }
                    return entry
                }, "model": items[0].model, "force": items[0].force, "retry_failed": false,
                "execution": ["mode": DirectSettings.executionMode(executionMode)]]
    }
}

struct DirectRequestError: LocalizedError {
    let message: String
    var errorDescription: String? { message }
}

/// Cancellation is restricted to our read-only helper processes. Mutations drain.
final class DirectRequest {
    private let lock = NSLock()
    private var process: Process?
    private var cancelled = false
    let cancellable: Bool
    init(cancellable: Bool) { self.cancellable = cancellable }
    var isCancelled: Bool { lock.lock(); defer { lock.unlock() }; return cancelled }
    func launch(_ child: Process) throws {
        lock.lock(); defer { lock.unlock() }
        if cancelled { throw CancellationError() }
        try child.run(); process = child
    }
    func finished() { lock.lock(); process = nil; lock.unlock() }
    func cancel() {
        guard cancellable else { return }
        lock.lock(); defer { lock.unlock() }
        cancelled = true
        if let process, process.isRunning { process.terminate() }
    }
}

final class DirectClient {
    private let requestsLock = NSLock()
    private var requests: [UUID: DirectRequest] = [:]
    func cancelReadOnly() {
        requestsLock.lock(); let active = Array(requests.values); requestsLock.unlock()
        active.forEach { $0.cancel() }
    }
    private func finished(_ id: UUID) { requestsLock.lock(); requests.removeValue(forKey: id); requestsLock.unlock() }
    @discardableResult func request(_ request: [String: Any], completion: @escaping (Result<[String: Any], Error>) -> Void) -> DirectRequest {
        let codeRoot = Bundle.main.object(forInfoDictionaryKey: "AudioTranscribeCodeRoot") as? String
        let environment = DirectSettings.environment()
        let handle = DirectRequest(cancellable: ["read", "list", "lookup", "verify_source", "discover"].contains(request["action"] as? String ?? ""))
        let requestID = UUID()
        requestsLock.lock(); requests[requestID] = handle; requestsLock.unlock()
        DispatchQueue.global(qos: .utility).async {
            defer { self.finished(requestID) }
            let result: Result<[String: Any], Error>
            do {
                guard let codeRoot else { throw DirectRequestError(message: "本地运行环境未配置。") }
                let temp = FileManager.default.temporaryDirectory.appendingPathComponent("audiotranscribe-" + UUID().uuidString)
                try FileManager.default.createDirectory(at: temp, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
                defer { try? FileManager.default.removeItem(at: temp) }
                let path = temp.appendingPathComponent("request.json")
                try JSONSerialization.data(withJSONObject: request).write(to: path, options: .atomic)
                let child = Process(); let output = Pipe()
                child.executableURL = URL(fileURLWithPath: codeRoot).appendingPathComponent(".venv/bin/python")
                child.currentDirectoryURL = URL(fileURLWithPath: codeRoot)
                child.arguments = ["-m", "audio_transcribe", "app-results", "--request", path.path]
                child.environment = environment; child.qualityOfService = .utility
                child.standardInput = FileHandle.nullDevice; child.standardOutput = output; child.standardError = FileHandle.nullDevice
                try handle.launch(child)
                let data = output.fileHandleForReading.readDataToEndOfFile(); child.waitUntilExit(); handle.finished()
                if handle.isCancelled { throw CancellationError() }
                guard let value = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                    throw DirectRequestError(message: "未能读取结果，请重试。")
                }
                guard child.terminationStatus == 0, value["error"] == nil else {
                    throw DirectRequestError(message: value["message"] as? String ?? value["error"] as? String ?? "操作未完成，请重试。")
                }
                result = .success(value)
            } catch { handle.finished(); result = .failure(error) }
            DispatchQueue.main.async { completion(result) }
        }
        return handle
    }
}
