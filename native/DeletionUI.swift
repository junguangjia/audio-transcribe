import Cocoa

final class RecordingTable: NSTableView {
    var deleteSelection: (() -> Void)?
    private var rowHeightInvalidationPending = false
    override func setFrameSize(_ newSize: NSSize) {
        let widthChanged = newSize.width != frame.width
        super.setFrameSize(newSize)
        guard widthChanged, !rowHeightInvalidationPending else { return }
        rowHeightInvalidationPending = true
        // Coalesce resizing and wait until AppKit leaves its delegate/layout
        // callbacks before asking it to remeasure wrapped filenames.
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            self.rowHeightInvalidationPending = false
            if self.numberOfRows > 0 {
                self.noteHeightOfRows(withIndexesChanged: IndexSet(integersIn: 0..<self.numberOfRows))
            }
        }
    }
    override func keyDown(with event: NSEvent) {
        let flags = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
        if DeletionPresentation.isDeleteShortcut(keyCode: event.keyCode, command: flags.contains(.command), otherModifiers: !flags.intersection([.shift, .option, .control]).isEmpty, listFocused: window?.firstResponder === self) {
            deleteSelection?(); return
        }
        super.keyDown(with: event)
    }
    override func menu(for event: NSEvent) -> NSMenu? {
        let row = self.row(at: convert(event.locationInWindow, from: nil))
        guard row >= 0 else { return nil }
        if !selectedRowIndexes.contains(row) { selectRowIndexes(IndexSet(integer: row), byExtendingSelection: false) }
        let menu = NSMenu()
        let item = NSMenuItem(title: "删除或忽略所选录音…", action: #selector(deleteContext), keyEquivalent: "")
        item.target = self; menu.addItem(item); return menu
    }
    @objc private func deleteContext() { deleteSelection?() }
}

extension MainController {
    func deletionSelection(resultID: String? = nil) -> [String: Any] {
        if let resultID { return ["result_ids": [resultID], "jobs": []] }
        let selected = table.selectedRowIndexes.compactMap { visibleRows.indices.contains($0) ? visibleRows[$0] : nil }
        let rows = selected.compactMap { identity -> Recording? in
            guard case .job(let id) = identity else { return nil }
            return queue.items.first { $0.id == id }
        }
        let jobs: [[String: Any]] = rows.map { item in
            var ref: [String: Any] = ["job_id": item.id.uuidString, "path": item.url.path]
            if let batch = item.batchID { ref["batch_id"] = batch }
            if let attempt = item.attemptID { ref["attempt_id"] = attempt }
            return ref
        }
        let results = selected.compactMap { identity -> String? in
            if case .result(let id) = identity { return id }
            return nil
        }
        return ["result_ids": Array(Set(rows.compactMap { $0.resultID } + results)), "jobs": jobs]
    }
    @objc func deleteSelected() {
        let watched = table.selectedRowIndexes.compactMap { index -> (String, String)? in
            guard visibleRows.indices.contains(index), case .watched(let path, let version) = visibleRows[index],
                  watchedItems[path]?.versionKey == version, state(for: visibleRows[index]) == "ready" else { return nil }
            return (path, version)
        }
        let selection = deletionSelection()
        let hasOwned = !(selection["result_ids"] as? [String] ?? []).isEmpty ||
            !(selection["jobs"] as? [[String: Any]] ?? []).isEmpty
        if !watched.isEmpty && hasOwned {
            status.stringValue = "请分别选择新录音和已有任务，再执行删除或忽略。"
            return
        }
        if !watched.isEmpty {
            guard let window else { return }
            let alert = NSAlert()
            alert.messageText = "忽略所选 \(watched.count) 份新录音？"
            alert.informativeText = "原录音留在文件夹中；只忽略当前文件版本。以后可点击“重新加入”，文件内容改变也会作为新版本出现。"
            alert.addButton(withTitle: "取消")
            alert.addButton(withTitle: "忽略当前版本")
            alert.beginSheetModal(for: window) { [weak self] response in
                guard let self, response == .alertSecondButtonReturn else { return }
                for (path, version) in watched where self.watchedItems[path]?.versionKey == version {
                    self.ignoreWatchedVersion(path: path, versionKey: version)
                }
            }
            return
        }
        planDeletion(selection)
    }
    func planDeletion(_ selection: [String: Any]) {
        guard pendingDeletion == nil, !deletionBusy else { status.stringValue = "请先完成或取消当前清理操作。"; return }
        guard !(selection["result_ids"] as? [String] ?? []).isEmpty || !(selection["jobs"] as? [[String: Any]] ?? []).isEmpty else { return }
        deletionBusy = true; deleteButton.isEnabled = false
        var request = selection; request["action"] = "delete_plan"
        client.request(request) { [weak self] response in
            guard let self else { return }; self.deletionBusy = false; self.deleteButton.isEnabled = true
            switch response {
            case .success(let plan): self.confirmDeletion(plan, selection: selection)
            case .failure(let error): self.cleanupStatus.stringValue = "无法检查删除范围：\(error.localizedDescription)"; self.cleanupStatus.isHidden = false
            }
        }
    }
    func confirmDeletion(_ plan: [String: Any], selection: [String: Any]) {
        guard let window, let token = plan["plan_token"] as? String, !token.isEmpty else { return }
        let selection = DeletionPresentation.includingActiveJobs(plan, selection: selection)
        let active = plan["stop_required"] as? Bool == true || DeletionPresentation.jobIDs(selection).contains { batchProgress?.jobs[$0]?.isTerminal == false && running }
        let alert = NSAlert(); alert.alertStyle = .warning
        alert.messageText = active ? "停止并永久删除所选录音？" : "永久删除所选录音？"
        alert.informativeText = "只删除所选录音独占的应用内数据。请核对范围和保留项目。"
        let cancel = alert.addButton(withTitle: "取消"); cancel.keyEquivalent = "\r"
        let remove = alert.addButton(withTitle: active ? "停止并删除" : "永久删除"); remove.keyEquivalent = ""; remove.hasDestructiveAction = true
        let text = NSTextView(frame: NSRect(x: 0, y: 0, width: 530, height: 225)); text.isEditable = false; text.isSelectable = true
        text.font = .systemFont(ofSize: 12); text.string = DeletionPresentation.detail(plan); text.textContainerInset = NSSize(width: 8, height: 8)
        let scroll = NSScrollView(frame: text.frame); scroll.documentView = text; scroll.hasVerticalScroller = true; text.isVerticallyResizable = true
        text.autoresizingMask = [.width]; text.textContainer?.widthTracksTextView = true; alert.accessoryView = scroll
        alert.beginSheetModal(for: window) { [weak self] response in
            guard let self else { return }
            guard response == .alertSecondButtonReturn else { self.abandonPendingDeletion(); return }
            let existing = self.pendingDeletion?["operation_id"] as? String
            self.pendingDeletion = ["operation_id": existing ?? UUID().uuidString, "plan_token": token, "selection": selection,
                                    "result_ids": Array(DeletionPresentation.resultIDs(plan).union(selection["result_ids"] as? [String] ?? [])), "state": "ready"]
            self.fenceDeletion()
            self.commitDeletion()
        }
    }
    func fenceDeletion() {
        guard let operation = pendingDeletion, let selection = operation["selection"] as? [String: Any] else { return }
        deletingJobIDs = DeletionPresentation.jobIDs(selection)
        deletingResultIDs = Set(operation["result_ids"] as? [String] ?? [])
        resultGeneration += 1; listRequest?.cancel(); listRequest = nil; listRefreshQueued = false
        for index in queue.items.indices where deletingJobIDs.contains(queue.items[index].id) || queue.items[index].resultID.map(deletingResultIDs.contains) == true {
            deletingJobIDs.insert(queue.items[index].id); queue.items[index].autoStart = false
            queue.items[index].deletionPending = true
            if let job = batchProgress?.jobs[queue.items[index].id], running, !job.isTerminal { cancelJob(job) }
        }
        if let id = reader.result?.id, deletingResultIDs.contains(id) { reader.clearForDeletion() }
        if let selectedJobID, deletingJobIDs.contains(selectedJobID) { reader.clearForDeletion() }
        cleanupStatus.stringValue = "正在停止所选任务并等待数据释放；其他录音继续。"; cleanupStatus.isHidden = false
        persistDeletion(); persist(); refresh()
    }
    func persistDeletion() {
        if let pendingDeletion, let data = try? JSONSerialization.data(withJSONObject: pendingDeletion) { defaults.set(data, forKey: "PendingDeletion") }
        else { defaults.removeObject(forKey: "PendingDeletion") }
    }
    func restoreDeletion() {
        guard let data = defaults.data(forKey: "PendingDeletion"), let value = try? JSONSerialization.jsonObject(with: data) as? [String: Any], let id = value["operation_id"] as? String, UUID(uuidString: id) != nil else { return }
        pendingDeletion = value; fenceDeletion()
        cleanupStatus.stringValue = "正在核对上次删除状态…"; cleanupRetry.isHidden = false; cleanupCancel.isHidden = true
        checkDeletionStatus()
    }
    func checkDeletionStatus() {
        guard let operation = pendingDeletion else { return }
        performDeletionRequest(["action": "delete_status", "operation_id": operation["operation_id"]!])
    }
    @objc func retryDeletion() {
        guard let operation = pendingDeletion else { return }
        if ["cleanup_needed", "purging"].contains(operation["state"] as? String ?? "") { performDeletionRequest(["action": "delete_retry", "operation_id": operation["operation_id"]!]) }
        else if operation["state"] as? String == "unconfirmed" { checkDeletionStatus() }
        else { commitDeletion() }
    }
    func commitDeletion() {
        guard let operation = pendingDeletion, !deletionBusy else { return }
        performDeletionRequest(["action": "delete_commit", "operation_id": operation["operation_id"]!, "plan_token": operation["plan_token"]!,
                                "selection": operation["selection"]!, "confirmed": true, "stop_selected": true])
    }
    func performDeletionRequest(_ request: [String: Any]) {
        guard !deletionBusy else { return }; deletionBusy = true; cleanupRetry.isEnabled = false
        client.request(request) { [weak self] response in
            guard let self else { return }; self.deletionBusy = false; self.cleanupRetry.isEnabled = true
            switch response {
            case .failure(let error):
                self.pendingDeletion?["state"] = "unconfirmed"; self.persistDeletion()
                self.cleanupStatus.stringValue = "删除状态待核对：\(error.localizedDescription)"; self.cleanupRetry.isHidden = false; self.cleanupCancel.isHidden = true
            case .success(let value): self.handleDeletionResult(value)
            }
        }
    }
    func handleDeletionResult(_ value: [String: Any]) {
        guard pendingDeletion != nil else { return }
        let state = value["state"] as? String ?? "cleanup_needed"
        pendingDeletion?["state"] = state; persistDeletion()
        if state == "completed" {
            let removed = Set(value["removed_result_ids"] as? [String] ?? []).union(deletingResultIDs)
            let jobs = Set((value["removed_job_ids"] as? [String] ?? []).compactMap(UUID.init(uuidString:))).union(deletingJobIDs)
            let selectedIDs = Set(table.selectedRowIndexes.compactMap { visibleRows.indices.contains($0) ? visibleRows[$0] : nil })
            var ignored: [String: String] = [:]
            for item in queue.items where jobs.contains(item.id) || item.resultID.map(removed.contains) == true {
                if let key = item.watchedVersionKey { ignored[item.url.path] = key }
            }
            for item in watchedItems.values where item.resultID.map(removed.contains) == true {
                ignored[item.path] = item.versionKey
            }
            for (path, key) in ignored {
                watchDeletionFences[path] = key
                watchIgnorePending.insert(path)
            }
            if !ignored.isEmpty { persistWatchDeletionFences() }
            deletedResultIDs.formUnion(removed); deletedJobIDs.formUnion(jobs)
            queue.items.removeAll { jobs.contains($0.id) || $0.resultID.map(removed.contains) == true }
            results.removeAll { removed.contains($0.id) }
            for (path, key) in ignored { ignoreWatchedVersion(path: path, versionKey: key) }
            rebuildRows()
            table.selectRowIndexes(IndexSet(visibleRows.indices.filter { selectedIDs.contains(visibleRows[$0]) }), byExtendingSelection: false)
            if let id = defaults.string(forKey: "LastDirectResult"), removed.contains(id) { defaults.removeObject(forKey: "LastDirectResult") }
            cleanupStatus.stringValue = "删除已验证完成。已移除 \(DeletionPresentation.bytes(value["removed_logical_bytes"]))；共享依赖和外部文件保留。"
            pendingDeletion = nil; deletingJobIDs.removeAll(); deletingResultIDs.removeAll(); persistDeletion(); persist()
            cleanupRetry.isHidden = true; cleanupCancel.isHidden = true; refresh(); refreshResults(); startNext()
        } else if state == "unknown" {
            cleanupStatus.stringValue = "尚未开始移除。可以取消删除，或重试并重新检查范围。"; cleanupRetry.isHidden = false; cleanupCancel.isHidden = false
        } else if state == "plan_changed", let plan = value["plan"] as? [String: Any], let selection = pendingDeletion?["selection"] as? [String: Any] {
            cleanupStatus.stringValue = "停止任务后删除范围发生变化，请重新核对。"
            confirmDeletion(plan, selection: selection)
        } else {
            let reason = value["reason"] as? String ?? ""
            cleanupStatus.stringValue = state == "busy" ? (reason == "active_execution" ? "等待当前处理释放文件，已完成结果仍可阅读和导出。" : "暂不能清理：请关闭旧版 AudioTranscribe 或其他正在使用这些数据的窗口，再重试。") : "仍有项目未清理。点击重试清理；此时不会显示为删除成功。"
            if let message = value["message"] as? String { cleanupStatus.toolTip = message }
            if let remaining = value["remaining"] as? [[String: Any]] { cleanupStatus.toolTip = remaining.compactMap { $0["reason"] as? String }.joined(separator: "\n") }
            cleanupRetry.isHidden = false; cleanupCancel.isHidden = ["cleanup_needed", "purging"].contains(state)
        }
    }
    @objc func abandonPendingDeletion() {
        guard !deletionBusy, !["cleanup_needed", "purging", "unconfirmed"].contains(pendingDeletion?["state"] as? String ?? "") else { return }
        for index in queue.items.indices { queue.items[index].deletionPending = false }
        pendingDeletion = nil; deletingJobIDs.removeAll(); deletingResultIDs.removeAll(); persistDeletion(); persist()
        cleanupStatus.isHidden = true; cleanupRetry.isHidden = true; cleanupCancel.isHidden = true; refresh(); refreshResults()
    }
}
