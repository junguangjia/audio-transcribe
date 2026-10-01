import Foundation

@main struct DirectTests {
    static func main() {
        for ext in DirectImport.extensions {
            assert(DirectImport.supportedFile(URL(fileURLWithPath: "/tmp/.local/测试 录音." + ext.uppercased())))
        }
        assert(!DirectImport.supportedFile(URL(fileURLWithPath: "/tmp/notes.txt")))
        assert(!DirectImport.supportedFile(URL(fileURLWithPath: "/tmp/extensionless")))
        assert(!DirectImport.supportedFile(URL(string: "https://example.invalid/audio.wav")!))
        assert(DirectSettings.model(nil) == "large-v3-turbo")
        assert(DirectSettings.model("large-v3") == "large-v3")
        assert(DirectSettings.model("unexpected") == "large-v3-turbo")
        assert(DirectSettings.format(nil) == "md")
        assert(DirectSettings.executionMode(nil) == "auto")
        assert(DirectSettings.executionMode("unexpected") == "auto")
        assert(DirectSettings.executionMode("serial") == "serial")
        assert(DirectSettings.executionTitle(nil) == "自动并行")
        assert(DirectSettings.executionTitle("serial") == "逐个处理")
        assert(DirectSettings.exportName(filename:"录音 chapter 1.wav",format:"md") == "录音 chapter 1_transcript.md")
        assert(DirectSettings.exportDestination(URL(fileURLWithPath: "/tmp/测试 transcript"), format: "md").lastPathComponent == "测试 transcript.md")
        assert(DirectSettings.exportDestination(URL(fileURLWithPath: "/tmp/transcript.MD"), format: "md").lastPathComponent == "transcript.MD")
        assert(DirectSettings.exportDestination(URL(fileURLWithPath: "/tmp/transcript.v2"), format: "json").lastPathComponent == "transcript.v2.json")
        assert(DirectResult.timestamp(-0.001) == "−00:00:00.001")
        assert(DirectResult.timestamp(3601.125) == "01:00:01.125")
        assert(DirectResult.timestamp(.nan) == "时间未知")
        let fullText = "[00:00:00]\nComplete source text\n\n[00:00:03]\nFinal sentence."
        let result = DirectResult(value:["result_id":"synthetic-result","readable_text":fullText,"plain_text":"No timestamps","segments":[["start_seconds":0.0,"end_seconds":3.0,"text":"Complete source text"]],"state":"review_required","available_formats":["md","txt","json"],"source_sha256":"synthetic-hidden-metadata"])
        assert(result.text == fullText && !result.text.contains("synthetic-hidden"))
        let opaque = DirectResult(value:["legacy":true,"copy_allowed":false,"readable_text":"Internal report details","copy_text":""])
        assert(!opaque.copyAllowed)
        let safe = DirectResult(value:["readable_text":"header plus transcript","copy_text":"transcript only","copy_allowed":true])
        assert(safe.copyAllowed && safe.copyText == "transcript only")
        assert(result.warning != nil && !result.formats.contains("srt"))
        let queue = RecordingQueue(); queue.add([URL(fileURLWithPath:"/tmp/测试 audio b.wav"), URL(fileURLWithPath:"/tmp/audio a.wav")])
        for i in queue.items.indices { queue.items[i].autoStart = true }
        let watchedVersion = String(repeating:"a",count:64)
        queue.items[0].inputMode = "referenced"; queue.items[0].watchedVersionKey = watchedVersion
        let originals = queue.items
        assert(DirectQueue.nextSubmission(queue.items).map{$0.id} == originals.map{$0.id})
        let submissionID = UUID()
        for mode in [nil, "auto", "serial"] as [String?] {
            let payload = DirectQueue.submissionPayload(originals,batchID:submissionID,submitted:123.5,executionMode:mode)
            assert(payload["files"] as? [String] == originals.map{$0.url.path})
            assert(payload["batch_id"] as? String == submissionID.uuidString)
            assert(payload["submitted_monotonic"] as? Double == 123.5)
            assert(payload["execution"] as? [String:String] == ["mode": mode ?? "auto"])
            assert(payload["experimental_parallel"] == nil)
            let identities = payload["items"] as! [[String:Any]]
            assert(identities.map{$0["job_id"] as! String} == originals.map{$0.id.uuidString})
            assert(identities.map{$0["index"] as! Int} == [0,1])
            assert(identities.map{$0["input_mode"] as! String} == ["referenced", "managed"])
            assert(identities[0]["watched_version_key"] as? String == watchedVersion)
            assert(identities[1]["watched_version_key"] == nil)
            let decoded = try! JSONSerialization.jsonObject(with: JSONSerialization.data(withJSONObject:payload)) as! [String:Any]
            assert(decoded["execution"] as? [String:String] == ["mode": mode ?? "auto"])
        }
        // Single inputs still use the selected policy; the coordinator limits effective workers to one.
        let singlePayload = DirectQueue.submissionPayload([originals[0]],batchID:UUID(),submitted:123.5,executionMode:nil)
        assert((singlePayload["files"] as! [String]).count == 1)
        assert(singlePayload["execution"] as? [String:String] == ["mode":"auto"])
        var next = Recording(url:URL(fileURLWithPath:"/tmp/different model.wav")); next.model = "large-v3"; next.autoStart = true
        queue.items.append(next)
        var forced = Recording(url:originals[0].url); forced.autoStart = true; forced.force = true
        queue.items.append(forced)
        assert(DirectQueue.nextSubmission(queue.items).map{$0.id} == originals.map{$0.id})
        queue.items[0].state = "completed"; queue.items[0].resultID = "retained-result"; queue.items[0].duration = 123.45
        queue.items[1].state = "cancelled"; queue.items[1].autoStart = false
        assert(DirectQueue.nextSubmission(queue.items).map{$0.id} == [next.id])
        let restored = QueuePersistence.restore(queue.items.map{$0.url.path},saved:QueuePersistence.snapshot(queue.items))
        assert(restored.map{$0.id} == queue.items.map{$0.id})
        assert(restored[0].resultID == "retained-result" && restored[0].duration == 123.45)
        assert(restored[0].inputMode == "referenced" && restored[0].watchedVersionKey == watchedVersion)
        assert(restored[2].model == "large-v3" && restored[3].force)
        assert(restored.allSatisfy{!$0.autoStart}) // Reopening does not silently launch work.
        assert(DirectQueue.nextSubmission(restored).isEmpty)
        assert(DirectQueue.idleSummary(restored)?.contains("完成 1") == true)
        assert(DirectQueue.idleSummary(queue.items) == nil) // Pending work must retain its live status.
        var checking = restored[0]; checking.state = "checking"
        assert(DirectQueue.idleSummary([checking]) == nil)
        assert(DirectQueue.idleSummary([restored[0]])?.contains("正在检查") == false)
        var retry = originals[0]; retry.state = "failed"; retry.batchID = UUID().uuidString; retry.submittedIndex = 0
        let fresh = DirectQueue.resuming(retry)
        assert(fresh.force && fresh.id == retry.id && fresh.autoStart && fresh.batchID == nil && fresh.submittedIndex == nil)
        retry.state = "cancelled"; retry.force = false
        assert(!DirectQueue.resuming(retry).force)
        retry.force = true
        assert(DirectQueue.resuming(retry).force)
        var batch = BatchProgress(items:originals,now:0)
        let ids = originals.map{$0.id}; let attempt = UUID().uuidString
        var metadata:[String:Any] = ["type":"metadata","batch_id":batch.id.uuidString,"job_id":ids[0].uuidString,"attempt_id":attempt,"index":0,"seq":2,"duration_seconds":9.5,"model":"large-v3-turbo"]
        assert(!batch.accept(metadata,currentIDs:ids,now:1)) // Metadata cannot bind an attempt.
        let queued:[String:Any] = ["type":"file","batch_id":batch.id.uuidString,"job_id":ids[0].uuidString,"attempt_id":attempt,"index":0,"seq":1,"state":"waiting","stage":"queued","elapsed":0.0]
        assert(batch.accept(queued,currentIDs:ids,now:1))
        assert(batch.accept(metadata,currentIDs:ids,now:2))
        assert(batch.jobs[ids[0]]?.state == "waiting")
        metadata["seq"] = 3; metadata["attempt_id"] = UUID().uuidString
        assert(!batch.accept(metadata,currentIDs:ids,now:3))
        metadata["attempt_id"] = attempt; metadata["index"] = 1
        assert(!batch.accept(metadata,currentIDs:ids,now:3))
        metadata["index"] = 0
        assert(!batch.accept(metadata,currentIDs:ids + [next.id],now:3))
        // Caller freezes submitted IDs, so later pending imports never shift active indexes.
        assert(batch.accept(metadata,currentIDs:batch.submittedIDs,now:3))
        print("Direct workflow tests passed: defaults, Unicode export naming, exact readable copy, raw signed timestamps, independent pending submissions, model/force separation, resume persistence and metadata identity guards.")
    }
}
