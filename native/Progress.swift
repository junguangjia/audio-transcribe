import Foundation
import CoreFoundation

/// An admission decision is a scheduler observation, not a measured GPU count.
struct WorkerAdmission {
    let requested: Int
    let admitted: Int
    let reason: String

    init?(_ event: [String: Any]) {
        func integer(_ key: String) -> Int? {
            guard let value = event[key] as? NSNumber, CFGetTypeID(value) != CFBooleanGetTypeID(),
                  value.doubleValue.isFinite, value.doubleValue.rounded(.towardZero) == value.doubleValue,
                  (0...9).contains(value.doubleValue) else { return nil }
            return value.intValue
        }
        guard event["stage"] as? String == "asr_admission",
              let requested = integer("requested_workers"), (1...9).contains(requested),
              let admitted = integer("admitted_workers"), admitted <= requested,
              let reason = event["reason"] as? String, !reason.isEmpty else { return nil }
        self.requested = requested; self.admitted = admitted; self.reason = reason
    }

    var reasonLabel: String {
        switch reason {
        case "first_worker_progress": return "开始处理"
        case "worker_limit": return "转录槽位已满"
        case "headroom_verified": return "资源估算允许并行"
        case "awaiting_swap_trend": return "正在确认内存变化"
        case "pressure_critical": return "内存压力高，先逐个处理"
        case "active_swapouts": return "交换活动增加，先逐个处理"
        case "insufficient_reclaimable_estimate": return "可用资源估算不足，先逐个处理"
        case "prior_allocation_failure": return "分配内存失败，先逐个处理"
        case "process_telemetry_unavailable": return "进程内存信息不足，先逐个处理"
        case "decoder_rss_unavailable": return "等待解码进程内存数据"
        case "pressure_unknown", "telemetry_unavailable", "swap_trend_unknown", "model_cost_unknown":
            return "资源信息不足，先逐个处理"
        default: return "等待可用资源"
        }
    }
    var detail: String { "并发准入 \(admitted)/\(requested) · " + reasonLabel }
}

struct JobProgress {
    let id: UUID
    let index: Int
    var attemptID: String?
    var state = "waiting"
    var stage = "queued"
    var percent: Int?
    var message: String?
    var cancellationRequested = false
    var restartRequired = false
    var elapsedBase: TimeInterval = 0
    var observedAt: TimeInterval
    var isTerminal: Bool { Self.terminalStates.contains(state) }
    static let terminalStates: Set<String> = ["completed", "review_required", "failed", "cancelled"]
    static let states: Set<String> = ["waiting", "checking", "processing", "completed", "review_required", "failed", "cancelled"]

    func elapsed(now: TimeInterval) -> TimeInterval {
        elapsedBase + (isTerminal ? 0 : max(0, now - observedAt))
    }
    func detail(now: TimeInterval) -> String {
        let label: String
        if cancellationRequested && !isTerminal { label = "正在取消…" }
        else if isTerminal {
            label = ["completed": "已完成", "review_required": "已生成 · 需复核", "failed": "失败 · 可重试", "cancelled": "已取消"][state]!
        } else {
            switch stage {
            case "waiting_for_execution", "waiting_for_ownership": label = "等待其他批次结束"
            case "checking", "importing": label = "正在检查与导入"
            case "preparing": label = "正在准备音频"
            case "prepared", "waiting_for_slot", "waiting_for_engine": label = "等待转录槽位"
            case "waiting_for_session": label = "等待同一录音的任务"
            case "waiting_for_memory": label = "等待可用资源"
            case "waiting_for_preparation": label = "等待音频准备"
            case "waiting_for_resource": label = "等待处理资源"
            case "waiting_for_import": label = "等待导入锁"
            case "transcribing": label = percent.map { "正在转录 \($0)%" } ?? "正在转录"
            case "validating", "finalizing": label = "正在检查与保存"
            default: label = "等待处理"
            }
        }
        return label + "\n用时 " + Self.duration(elapsed(now: now))
    }
    static func duration(_ value: TimeInterval) -> String {
        let seconds = Int(min(Double(Int.max / 2), max(0, value)))
        return seconds >= 3600
            ? String(format: "%d:%02d:%02d", seconds / 3600, seconds / 60 % 60, seconds % 60)
            : String(format: "%02d:%02d", seconds / 60, seconds % 60)
    }
}

/// A submission freezes UUID-to-index mapping. JSON lines are validated before
/// they may alter rows; completion order never changes original selection order.
struct BatchProgress {
    let id: UUID
    let submittedIDs: [UUID]
    let startedAt: TimeInterval
    private(set) var jobs: [UUID: JobProgress]
    private(set) var lastSequence = -1
    private(set) var terminal = false
    private(set) var stage = "queued"
    private(set) var cancelling = false
    private(set) var endedAt: TimeInterval?
    private(set) var drainedJobIDs = Set<UUID>()
    private(set) var admission: WorkerAdmission?

    init(id: UUID = UUID(), items: [Recording], now: TimeInterval) {
        self.id = id; submittedIDs = items.map { $0.id }; startedAt = now
        jobs = Dictionary(uniqueKeysWithValues: items.enumerated().map {
            ($0.element.id, JobProgress(id: $0.element.id, index: $0.offset, observedAt: now))
        })
    }
    mutating func accept(_ event: [String: Any], currentIDs: [UUID], now: TimeInterval) -> Bool {
        guard !terminal, currentIDs == submittedIDs,
              let batch = event["batch_id"] as? String, UUID(uuidString: batch) == id,
              let seq = event["seq"] as? Int, seq >= 0, seq > lastSequence,
              let type = event["type"] as? String else { return false }
        if type == "file" || type == "progress" {
            guard let rawID = event["job_id"] as? String, let jobID = UUID(uuidString: rawID),
                  var job = jobs[jobID], !job.isTerminal,
                  let index = event["index"] as? Int, index == job.index,
                  let attempt = event["attempt_id"] as? String, UUID(uuidString: attempt) != nil,
                  let state = event["state"] as? String, JobProgress.states.contains(state),
                  let nextStage = event["stage"] as? String, !nextStage.isEmpty,
                  let elapsed = event["elapsed"] as? Double, elapsed.isFinite, elapsed >= 0 else { return false }
            if let bound = job.attemptID {
                guard UUID(uuidString: attempt) == UUID(uuidString: bound) else { return false }
            } else {
                guard type == "file", state == "waiting", nextStage == "queued" else { return false }
                job.attemptID = attempt
            }
            // A locally requested cancellation awaits a backend terminal state.
            // The backend may already have saved a result; do not invent cancellation.
            job.elapsedBase = max(job.elapsed(now: now), elapsed); job.observedAt = now
            if nextStage != job.stage { job.percent = nil }
            job.stage = nextStage; job.state = state
            if nextStage == "transcribing", let percent = event["percent"] as? Int, (0...100).contains(percent) {
                job.percent = max(job.percent ?? 0, percent)
            }
            if let message = event["message"] as? String { job.message = message }
            if let restart = event["restart_required"] as? Bool { job.restartRequired = restart }
            jobs[jobID] = job
            if job.isTerminal { admission = nil } // A completion can release a slot before the next decision.
        } else if type == "metadata" {
            guard let rawID = event["job_id"] as? String, let jobID = UUID(uuidString: rawID),
                  let job = jobs[jobID], !job.isTerminal,
                  event["index"] as? Int == job.index,
                  let attempt = event["attempt_id"] as? String, let bound = job.attemptID,
                  UUID(uuidString: attempt) == UUID(uuidString: bound) else { return false }
        } else if type == "drained" || type == "cleanup_required" {
            guard let rawID = event["job_id"] as? String, let id = UUID(uuidString: rawID), let job = jobs[id],
                  event["index"] as? Int == job.index, let attempt = event["attempt_id"] as? String,
                  let bound = job.attemptID, UUID(uuidString: attempt) == UUID(uuidString: bound) else { return false }
            if type == "drained" { drainedJobIDs.insert(id) }
        } else if type == "admission" {
            guard let decision = WorkerAdmission(event) else { return false }
            admission = decision
        } else if type == "batch" {
            if let nextStage = event["stage"] as? String { stage = nextStage }
        } else if ["result", "cancelled", "error"].contains(type) {
            terminal = true; endedAt = now
        } else { return false }
        lastSequence = seq
        return true
    }
    mutating func requestCancellation(jobID: UUID? = nil) {
        if jobID == nil { cancelling = true }
        for id in submittedIDs where jobID == nil || jobID == id {
            guard var job = jobs[id], !job.isTerminal else { continue }
            job.cancellationRequested = true; jobs[id] = job
        }
    }
    mutating func finishUnsettled(state: String, now: TimeInterval) {
        for id in submittedIDs {
            guard var job = jobs[id], !job.isTerminal else { continue }
            job.elapsedBase = job.elapsed(now: now); job.observedAt = now
            job.state = state; job.stage = state; jobs[id] = job
        }
        terminal = true; endedAt = now
    }
    var admissionDetail: String? {
        guard !terminal, !cancelling, jobs.values.filter({ !$0.isTerminal }).count > 1,
              let admission, admission.requested > 1, admission.reason != "first_worker_progress" else { return nil }
        // This is the latest admission decision, not a measured active decoder counter.
        return admission.detail
    }
    func summary(now: TimeInterval) -> String {
        let completed = jobs.values.filter { $0.state == "completed" }.count
        let review = jobs.values.filter { $0.state == "review_required" }.count
        let failed = jobs.values.filter { $0.state == "failed" }.count
        let cancelled = jobs.values.filter { $0.state == "cancelled" }.count
        let decoding = jobs.values.filter { !$0.isTerminal && $0.stage == "transcribing" }.count
        let finished = completed + review + failed + cancelled
        let prefix = cancelling && !terminal ? "正在取消整批" : stage == "waiting_for_execution" && !terminal ? "等待其他批次结束" : "本批次"
        return "\(prefix) · 已结束 \(finished)/\(jobs.count) · 转录中 \(decoding)\n完成 \(completed) · 需复核 \(review) · 失败 \(failed) · 取消 \(cancelled) · 用时 \(JobProgress.duration(max(0, (endedAt ?? now) - startedAt)))"
    }
}

struct TranscriptionProgress {
    var stage = "checking"
    var percent: Int?
    let started: TimeInterval

    mutating func update(_ event: [String: Any]) {
        guard let next = event["stage"] as? String,
              ["preparing", "waiting_for_engine", "transcribing", "validating"].contains(next) else { return }
        if stage != next { percent = nil }
        stage = next
        if let value = event["percent"] as? Int, (0...100).contains(value) {
            percent = max(percent ?? 0, value)
        }
    }
    var detail: String {
        switch stage {
        case "preparing": return "Preparing audio…"
        case "waiting_for_engine": return "Waiting for transcription engine…"
        case "transcribing": return percent.map { "Transcribing \($0)%" } ?? "Starting transcription…"
        case "validating": return "Validating transcript…"
        default: return "Checking recording…"
        }
    }
    func status(index: Int, total: Int, now: TimeInterval) -> String {
        let seconds = Int(max(0, now - started))
        let elapsed = seconds >= 3600
            ? String(format: "%d:%02d:%02d", seconds / 3600, seconds / 60 % 60, seconds % 60)
            : String(format: "%02d:%02d", seconds / 60, seconds % 60)
        return "Processing \(index + 1) of \(total) · \(detail) · Elapsed \(elapsed)\nThe report appears when all recordings finish."
    }
}
