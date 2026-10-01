import Foundation

@main struct TaskTests {
    static func main() {
        let paths = ["/tmp/synthetic-a.wav", "/tmp/synthetic-b.wav"]
        let saved = [["path": paths[0], "state": "processing"], ["path": paths[1], "state": "review_required", "message": "Synthetic review"]]
        let restored = QueuePersistence.restore(paths, saved: saved)
        assert(restored[0].state == "waiting" && restored[0].message != nil)
        assert(restored[1].state == "review_required" && restored[1].message == "Synthetic review")
        let copy = QueuePersistence.restore(paths, saved: QueuePersistence.snapshot(restored))
        assert(copy.map { $0.state } == restored.map { $0.state })
        let migrated = QueuePersistence.restore(paths, saved: [], reportEntries: [
            ["selected_path": paths[0], "state": "review_required"], ["selected_path": paths[1], "state": "completed"]])
        assert(migrated.map { $0.state } == ["review_required", "completed"])
        let changed = QueuePersistence.restore(Array(paths.reversed()), saved: saved)
        assert(changed.allSatisfy { $0.state == "waiting" })
        let unknown = QueuePersistence.restore([paths[0]], saved: [["path": paths[0], "state": "untrusted-state"]])
        assert(unknown[0].state == "waiting")
        let entries: [[String: Any]] = [["selected_path": paths[0]], ["selected_path": paths[1]]]
        assert(QueuePersistence.reportMatches(paths, entries: entries))
        assert(!QueuePersistence.reportMatches(Array(paths.reversed()), entries: entries))
        assert(!QueuePersistence.reportMatches(paths, entries: [entries[0]]))
        let finished = QueuePersistence.snapshot(migrated)
        assert(QueuePersistence.savedTaskFinished(paths, saved: finished, markedFinished: true))
        assert(!QueuePersistence.savedTaskFinished(paths, saved: saved, markedFinished: true))
        assert(!QueuePersistence.savedTaskFinished(Array(paths.reversed()), saved: finished, markedFinished: true))
        assert(copy.map { $0.id } == restored.map { $0.id })
        let collision = UUID().uuidString
        let duplicates = QueuePersistence.restore([paths[0], paths[0]], saved: [
            ["path": paths[0], "state": "waiting", "job_id": collision],
            ["path": paths[0], "state": "waiting", "job_id": collision]])
        assert(Set(duplicates.map { $0.id }).count == 2)
        let queue = RecordingQueue(); queue.items = restored
        queue.move(IndexSet(integer: 1), to: 0)
        assert(queue.items.map { $0.id } == [restored[1].id, restored[0].id])
        progressTests(items: restored)
        admissionTests(items: restored)
        print("Task persistence tests passed: resumed work stays pending; completed/review states survive reopening; legacy report matching is exact.")
    }

    static func progressTests(items: [Recording]) {
        for (stage, label) in [("waiting_for_memory", "等待可用资源"),
                               ("waiting_for_preparation", "等待音频准备"),
                               ("waiting_for_import", "等待导入锁")] {
            var job = JobProgress(id: UUID(), index: 0, observedAt: 0)
            job.stage = stage
            assert(job.detail(now: 3) == label + "\n用时 00:03")
        }
        var batch = BatchProgress(items: items, now: 0)
        let ids = items.map { $0.id }; let attempts = [UUID().uuidString, UUID().uuidString]
        func event(_ index: Int, _ seq: Int, _ state: String, _ stage: String, percent: Int? = nil,
                   attempt: String? = nil, batchID: UUID? = nil) -> [String: Any] {
            var value: [String: Any] = ["type": stage == "queued" || JobProgress.terminalStates.contains(state) ? "file" : "progress",
                "batch_id": (batchID ?? batch.id).uuidString, "job_id": ids[index].uuidString,
                "attempt_id": attempt ?? attempts[index], "index": index, "seq": seq,
                "state": state, "stage": stage, "elapsed": Double(seq)]
            if let percent { value["percent"] = percent }; return value
        }
        assert(batch.accept(event(0, 1, "waiting", "queued"), currentIDs: ids, now: 1))
        assert(batch.accept(event(1, 2, "waiting", "queued"), currentIDs: ids, now: 2))
        assert(!batch.accept(event(0, 30, "processing", "transcribing", attempt: UUID().uuidString), currentIDs: ids, now: 3))
        assert(!batch.accept(event(0, 30, "processing", "transcribing", batchID: UUID()), currentIDs: ids, now: 3))
        var wrongIndex = event(0, 30, "processing", "transcribing"); wrongIndex["index"] = 1
        assert(!batch.accept(wrongIndex, currentIDs: ids, now: 3))
        assert(!batch.accept(event(0, 30, "processing", "transcribing"), currentIDs: Array(ids.reversed()), now: 3))
        assert(batch.accept(event(0, 3, "processing", "preparing"), currentIDs: ids, now: 3))
        assert(batch.accept(event(0, 4, "processing", "transcribing", percent: 50), currentIDs: ids, now: 4))
        assert(batch.accept(event(1, 5, "processing", "preparing"), currentIDs: ids, now: 5))
        assert(batch.jobs[ids[0]]?.stage == "transcribing" && batch.jobs[ids[1]]?.stage == "preparing")
        assert(batch.accept(event(1, 6, "processing", "transcribing", percent: 100), currentIDs: ids, now: 6))
        assert(batch.jobs.values.filter { $0.stage == "transcribing" }.count == 2)
        assert(batch.summary(now: 6).contains("已结束 0/2")) // Decoder 100% is not completion.
        assert(batch.accept(event(1, 7, "review_required", "finalizing"), currentIDs: ids, now: 7))
        assert(batch.jobs[ids[1]]?.elapsed(now: 100) == 7)
        assert(batch.jobs[ids[0]]?.state == "processing") // B can finish before A.
        assert(batch.accept(event(0, 8, "processing", "transcribing", percent: 20), currentIDs: ids, now: 8))
        assert(batch.jobs[ids[0]]?.percent == 50)
        batch.requestCancellation(jobID: ids[0])
        assert(batch.jobs[ids[0]]?.state == "processing" && batch.jobs[ids[0]]?.cancellationRequested == true)
        assert(batch.jobs[ids[1]]?.cancellationRequested == false)
        assert(batch.accept(event(0, 9, "cancelled", "cancelled"), currentIDs: ids, now: 9))
        assert(!batch.accept(event(0, 10, "completed", "finalizing"), currentIDs: ids, now: 10))
        assert(batch.jobs[ids[0]]?.state == "cancelled")
        assert(batch.accept(["type": "result", "batch_id": batch.id.uuidString, "seq": 11], currentIDs: ids, now: 11))
        assert(batch.summary(now: 11) == batch.summary(now: 100))
        assert(!batch.accept(event(0, 12, "processing", "transcribing"), currentIDs: ids, now: 12))

        let oldBatchID = batch.id; let oldAttempt = attempts[0]
        batch = BatchProgress(items: items, now: 20)
        let retryAttempt = UUID().uuidString
        assert(!batch.accept(event(0, 20, "processing", "transcribing", batchID: oldBatchID), currentIDs: ids, now: 20))
        assert(batch.accept(event(0, 1, "waiting", "queued", attempt: retryAttempt), currentIDs: ids, now: 21))
        assert(!batch.accept(event(0, 2, "processing", "transcribing", attempt: oldAttempt), currentIDs: ids, now: 22))
        assert(batch.accept(event(0, 2, "completed", "finalizing", attempt: retryAttempt), currentIDs: ids, now: 22))
        assert(!batch.accept(event(0, 1, "waiting", "queued", attempt: retryAttempt), currentIDs: ids, now: 23))
        batch.requestCancellation()
        batch.finishUnsettled(state: "cancelled", now: 24)
        assert(batch.jobs[ids[0]]?.state == "completed" && batch.jobs[ids[1]]?.state == "cancelled")
        assert(batch.jobs[ids[1]]?.elapsed(now: 50) == 4)
        print("Per-job progress tests passed: concurrent stages, B-before-A completion, real progress only, batch/index/attempt/sequence rejection, cancellation immunity, retry identity and frozen timers.")
    }

    static func admissionTests(items: [Recording]) {
        var batch = BatchProgress(items: items, now: 0)
        let ids = items.map{$0.id}
        func decision(_ seq: Int, _ reason: String, admitted: Int = 1) -> [String:Any] {
            ["type":"admission", "stage":"asr_admission", "batch_id":batch.id.uuidString,
             "seq":seq, "requested_workers":2, "admitted_workers":admitted, "reason":reason]
        }
        let states = batch.jobs.values.map{$0.state}.sorted()
        assert(batch.accept(decision(1,"first_worker_progress"),currentIDs:ids,now:1))
        assert(batch.admissionDetail == nil) // Normal first-worker startup is not a fallback warning.
        assert(batch.accept(decision(2,"awaiting_swap_trend"),currentIDs:ids,now:2))
        assert(batch.admissionDetail == "并发准入 1/2 · 正在确认内存变化")
        var available = decision(3,"headroom_verified",admitted:2)
        available["resource_evidence"] = ["pressure":"warning"]
        let wire = try! JSONSerialization.jsonObject(with: JSONSerialization.data(withJSONObject:available)) as! [String:Any]
        assert(batch.accept(wire,currentIDs:ids,now:3))
        assert(batch.admission?.admitted == 2 && batch.admission?.requested == 2)
        assert(batch.admissionDetail == "并发准入 2/2 · 资源估算允许并行")
        assert(batch.admissionDetail?.contains("逐个") == false) // Warning is not a serial-only rule.
        assert(batch.jobs.values.map{$0.state}.sorted() == states)
        assert(!batch.accept(decision(2,"pressure_critical"),currentIDs:ids,now:4))
        var foreign = decision(4,"pressure_critical"); foreign["batch_id"] = UUID().uuidString
        assert(!batch.accept(foreign,currentIDs:ids,now:4))
        assert(!batch.accept(decision(4,"pressure_critical"),currentIDs:Array(ids.reversed()),now:4))
        for (key,value) in [("requested_workers",0 as Any), ("requested_workers",10 as Any),
                            ("requested_workers",true as Any), ("requested_workers",1.5 as Any),
                            ("admitted_workers",-1 as Any), ("admitted_workers",3 as Any),
                            ("reason","" as Any), ("stage","wrong" as Any)] {
            var malformed = decision(4,"pressure_critical"); malformed[key] = value
            let wire = try! JSONSerialization.jsonObject(with: JSONSerialization.data(withJSONObject:malformed)) as! [String:Any]
            assert(!batch.accept(wire,currentIDs:ids,now:4))
        }
        assert(batch.admission?.reason == "headroom_verified")
        assert(batch.accept(decision(4,"process_telemetry_unavailable"),currentIDs:ids,now:4))
        assert(batch.admissionDetail?.contains("进程内存信息不足") == true)
        assert(batch.accept(decision(5,"prior_allocation_failure"),currentIDs:ids,now:5))
        assert(batch.admissionDetail?.contains("分配内存失败") == true)
        assert(batch.accept(decision(6,"new_backend_reason"),currentIDs:ids,now:6))
        assert(batch.admission?.reason == "new_backend_reason") // Preserve token without exposing unexplained internals.
        assert(batch.admissionDetail?.contains("new_backend_reason") == false)
        var parallel = decision(7,"headroom_verified",admitted:3)
        parallel["requested_workers"] = 4
        assert(batch.accept(parallel,currentIDs:ids,now:7))
        assert(batch.admission?.requested == 4 && batch.admission?.admitted == 3)
        assert(batch.admissionDetail == "并发准入 3/4 · 资源估算允许并行")
        batch.requestCancellation()
        assert(batch.admissionDetail == nil)
        batch.finishUnsettled(state:"cancelled",now:7)
        assert(!batch.accept(decision(8,"headroom_verified",admitted:2),currentIDs:ids,now:8))
        assert(batch.admissionDetail == nil)

        batch = BatchProgress(items:[items[0]],now:0)
        var single = decision(1,"first_worker_progress"); single["requested_workers"] = 1
        assert(batch.accept(single,currentIDs:batch.submittedIDs,now:1))
        assert(batch.admissionDetail == nil)

        let third = Recording(url:URL(fileURLWithPath:"/tmp/synthetic-c.wav"))
        batch = BatchProgress(items:items + [third],now:0)
        let threeIDs = batch.submittedIDs, attempt = UUID().uuidString
        let queued: [String:Any] = ["type":"file", "batch_id":batch.id.uuidString, "seq":1,
            "job_id":threeIDs[0].uuidString, "attempt_id":attempt, "index":0,
            "state":"waiting", "stage":"queued", "elapsed":0.0]
        assert(batch.accept(queued,currentIDs:threeIDs,now:1))
        assert(batch.accept(decision(2,"headroom_verified",admitted:2),currentIDs:threeIDs,now:2))
        assert(batch.admissionDetail?.contains("2/2") == true)
        var completed = queued; completed["seq"] = 3; completed["state"] = "completed"
        completed["stage"] = "finished"; completed["elapsed"] = 3.0
        assert(batch.accept(completed,currentIDs:threeIDs,now:3))
        assert(batch.jobs.values.filter { !$0.isTerminal }.count == 2)
        assert(batch.admission == nil && batch.admissionDetail == nil)
        assert(batch.accept(decision(4,"awaiting_swap_trend"),currentIDs:threeIDs,now:4))
        assert(batch.admissionDetail == "并发准入 1/2 · 正在确认内存变化")
        assert(batch.accept(decision(5,"decoder_rss_unavailable"),currentIDs:threeIDs,now:5))
        assert(batch.admissionDetail == "并发准入 1/2 · 等待解码进程内存数据")
        print("Admission UI tests passed: exact counts and reasons, Warning with two admitted, malformed/stale/foreign event rejection, terminal admission clearing, decoder RSS waits, cancellation and single-file suppression.")
    }
}
