import Foundation

@main struct RepairTests {
    static func main() throws {
        for focused in [true, false] {
            assert(DeletionPresentation.isDeleteShortcut(keyCode: 51, command: true, otherModifiers: false, listFocused: focused) == focused)
        }
        assert(!DeletionPresentation.isDeleteShortcut(keyCode: 51, command: false, otherModifiers: false, listFocused: true))
        assert(!DeletionPresentation.isDeleteShortcut(keyCode: 51, command: true, otherModifiers: true, listFocused: true))
        let id = UUID()
        let plan: [String: Any] = ["selected": [["filename": "测试 recording.wav", "variant_count": 2, "result_ids": ["r-one", "r-two"]]],
            "remove": ["item_count": 4, "logical_bytes": 1024, "allocated_bytes": 4096],
            "retained_shared": [["reason": "Retained historical report", "logical_bytes": 100]]]
        let message = DeletionPresentation.detail(plan)
        assert(message.contains("测试 recording.wav") && message.contains("2 个转录版本"))
        assert(message.contains("Retained historical report") && message.contains("无法撤销") && message.contains("外部原录音"))
        assert(DeletionPresentation.resultIDs(plan) == ["r-one", "r-two"])
        assert(DeletionPresentation.jobIDs(["jobs": [["job_id": id.uuidString], ["job_id": "bad"]]]) == [id])
        let activeID = UUID(), aliasID = UUID()
        let expanded = DeletionPresentation.includingActiveJobs(["active_jobs": [["job_id": activeID.uuidString, "attempt_id": "new-attempt"], ["job_id": "bad"]]], selection: ["result_ids": ["r-one"], "jobs": [["job_id": activeID.uuidString, "path": "/tmp/synthetic.wav", "attempt_id": "old-attempt"]]])
        assert(DeletionPresentation.jobIDs(expanded) == [activeID] && !DeletionPresentation.jobIDs(expanded).contains(aliasID))
        assert((expanded["jobs"] as! [[String: Any]]).count == 1)
        assert((expanded["jobs"] as! [[String: Any]])[0]["attempt_id"] as? String == "new-attempt")
        assert((expanded["jobs"] as! [[String: Any]])[0]["path"] as? String == "/tmp/synthetic.wav")
        let fromResult = DeletionPresentation.includingActiveJobs(["active_jobs": [["job_id": activeID.uuidString]]], selection: ["result_ids": ["r-one"], "jobs": []])
        assert(DeletionPresentation.jobIDs(fromResult) == [activeID])
        var item = Recording(id: id, url: URL(fileURLWithPath: "/tmp/synthetic.wav")); item.autoStart = true; item.deletionPending = true; item.restartRequired = true
        assert(DirectQueue.nextSubmission([item]).isEmpty)
        let restored = QueuePersistence.restore([item.url.path], saved: QueuePersistence.snapshot([item]))[0]
        assert(restored.deletionPending && restored.restartRequired && !restored.autoStart)
        var batch = BatchProgress(items: [item], now: 0); let attempt = UUID().uuidString
        let queued: [String: Any] = ["type":"file", "batch_id":batch.id.uuidString, "seq":1, "job_id":id.uuidString, "attempt_id":attempt, "index":0, "state":"waiting", "stage":"queued", "elapsed":0.0]
        assert(batch.accept(queued, currentIDs:[id], now:0))
        let obsolete: [String: Any] = ["type":"thermal", "batch_id":batch.id.uuidString, "seq":2, "state":"serious", "capacity":0]
        assert(!batch.accept(obsolete,currentIDs:[id],now:1))
        var interrupted = queued; interrupted["seq"] = 2; interrupted["state"] = "cancelled"; interrupted["stage"] = "restart_required"; interrupted["restart_required"] = true
        assert(batch.accept(interrupted,currentIDs:[id],now:2)); assert(batch.jobs[id]?.restartRequired == true)
        var drained = queued; drained["type"] = "drained"; drained["seq"] = 3
        assert(batch.accept(drained,currentIDs:[id],now:3)); assert(batch.drainedJobIDs.contains(id))
        let cancelled = DirectRequest(cancellable:true); cancelled.cancel(); let never = Process(); never.executableURL = URL(fileURLWithPath:"/usr/bin/true")
        do { try cancelled.launch(never); assertionFailure("Cancelled read launched") } catch is CancellationError {}
        assert(!never.isRunning)
        let mutation = DirectRequest(cancellable:false); mutation.cancel(); assert(!mutation.isCancelled)
        print("Repair tests passed: focus-safe delete, explicit scope/bytes, pending/restart persistence, obsolete thermal event rejection, drain identity and read-only cancellation boundaries.")
    }
}
