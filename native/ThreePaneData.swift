import Cocoa

extension MainController {
    func prepareSubmissionSelection(_ id: UUID) {
        // Explicit submissions reveal and select their new job through the
        // table's normal selection handler, keeping reader actions aligned.
        preferredSelectionRowID = .job(id)
        selectedRowIdentity = nil
        selectedFilter = .all
        searchTimer?.invalidate(); searchGeneration += 1
        searchRequest?.cancel(); searchRequest = nil; searchedResultIDs = nil
        search.stringValue = ""
    }

    func isAlreadyQueued(_ item: WatchedDiscovery) -> Bool {
        queue.items.contains { $0.url.standardizedFileURL.path == item.path &&
            !$0.deletionPending && ($0.watchedVersionKey == item.versionKey ||
                (["checking", "waiting", "processing"].contains($0.state) && $0.resultID == nil)) }
    }

    func isWatchDeletionFenced(_ item: WatchedDiscovery) -> Bool {
        watchDeletionFences[item.path] == item.versionKey
    }

    func state(for row: RecordingRowID) -> String {
        switch row {
        case .job(let id): return queue.items.first { $0.id == id }?.state ?? "unavailable"
        case .result(let id): return results.first { $0.id == id }?.state ?? "unavailable"
        case .watched(let path, let version):
            guard let item = watchedItems[path] else { return "unavailable" }
            if watchDeletionFences[path] == version {
                if watchIgnorePending.contains(path) { return "copying" }
                if watchIgnoreErrors[path] != nil { return "failed" }
                return "ignored"
            }
            return item.state
        }
    }

    func filename(for row: RecordingRowID) -> String {
        switch row {
        case .job(let id): return queue.items.first { $0.id == id }?.url.lastPathComponent ?? "录音"
        case .result(let id): return results.first { $0.id == id }?.filename ?? "转录结果"
        case .watched(let path, _): return watchedItems[path]?.filename ?? URL(fileURLWithPath: path).lastPathComponent
        }
    }

    func duration(for row: RecordingRowID) -> Double? {
        switch row {
        case .job(let id): return queue.items.first { $0.id == id }?.duration
        case .result(let id): return results.first { $0.id == id }?.duration
        case .watched: return nil
        }
    }

    func model(for row: RecordingRowID) -> String {
        switch row {
        case .job(let id): return queue.items.first { $0.id == id }?.model ?? ""
        case .result(let id): return results.first { $0.id == id }?.model ?? ""
        case .watched: return DirectSettings.model(defaults.string(forKey: "DefaultModel"))
        }
    }

    func stateDetail(for row: RecordingRowID) -> String {
        switch row {
        case .job(let id):
            guard let item = queue.items.first(where: { $0.id == id }) else { return "不可用" }
            if item.deletionPending { return "等待清理完成" }
            if running, let progress = batchProgress?.jobs[id] { return simpleProgress(progress) }
            return DirectResult.stateLabel(item.state)
        case .result(let id):
            guard let result = results.first(where: { $0.id == id }) else { return "不可用" }
            return DirectResult.stateLabel(result.state)
        case .watched(let path, _):
            guard let item = watchedItems[path] else { return "不可用" }
            if isWatchDeletionFenced(item) {
                if watchIgnorePending.contains(path) { return "正在完成监控忽略…" }
                if let error = watchIgnoreErrors[path] { return "监控忽略失败，可重试：" + error }
                return "已忽略 · 可重新加入"
            }
            switch item.state {
            case "ready": return "新录音 · 等待开始"
            case "copying": return "正在复制，稍后可用"
            case "ignored": return "已忽略 · 可重新加入"
            case "unavailable": return item.message ?? "暂时不可用"
            case "failed": return item.message ?? "文件检查失败"
            default: return item.message ?? "正在检查"
            }
        }
    }

    func rowTimestamp(_ row: RecordingRowID) -> TimeInterval {
        switch row {
        case .job(let id): return queue.items.first { $0.id == id }?.createdAt ?? 0
        case .result(let id):
            guard let raw = results.first(where: { $0.id == id })?.value["generated_at"] as? String else { return 0 }
            let fractional = ISO8601DateFormatter(); fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
            return (fractional.date(from: raw) ?? ISO8601DateFormatter().date(from: raw))?.timeIntervalSince1970 ?? 0
        case .watched(let path, _): return watchedFirstSeen[path] ?? 0
        }
    }

    func sortRows(_ rows: inout [RecordingRowID]) {
        rows.sort { left, right in
            switch sortMode {
            case .importedNewest:
                let a = rowTimestamp(left), b = rowTimestamp(right)
                if a != b { return a > b }
            case .filename:
                let compared = filename(for: left).localizedStandardCompare(filename(for: right))
                if compared != .orderedSame { return compared == .orderedAscending }
            case .durationLongest:
                let a = duration(for: left) ?? -1, b = duration(for: right) ?? -1
                if a != b { return a > b }
            case .status:
                let a = state(for: left), b = state(for: right)
                if a != b { return a < b }
            }
            return String(describing: left) < String(describing: right)
        }
    }

    func rebuildRows() {
        let selected = Set(table.selectedRowIndexes.compactMap { visibleRows.indices.contains($0) ? visibleRows[$0] : nil })
        let selectedWatchedPaths = Set(selected.compactMap { row -> String? in
            if case .watched(let path, _) = row { return path }; return nil
        })
        let linked = Set(queue.items.compactMap(\.resultID))
        let all = queue.items.filter { !$0.deletionPending && !deletedJobIDs.contains($0.id) }.map { RecordingRowID.job($0.id) }
            + results.filter { !linked.contains($0.id) && !deletedResultIDs.contains($0.id) && !deletingResultIDs.contains($0.id) }.map { RecordingRowID.result($0.id) }
            + watchedItems.values.filter { !["processed", "processed_candidate"].contains($0.state) && !isAlreadyQueued($0) }
                .map { RecordingRowID.watched($0.path, $0.versionKey) }
        rowCounts = Dictionary(uniqueKeysWithValues: RecordingFilter.allCases.map { filter in
            (filter, all.filter { filter.includes(state(for: $0)) }.count)
        })
        let query = search.stringValue.trimmingCharacters(in: .whitespacesAndNewlines).localizedLowercase
        var next = all.filter { selectedFilter.includes(state(for: $0)) }
        if !query.isEmpty {
            next = next.filter { row in
                if filename(for: row).localizedLowercase.contains(query) { return true }
                switch row {
                case .result(let id): return searchedResultIDs?.contains(id) ?? false
                case .job(let id):
                    guard let item = queue.items.first(where: { $0.id == id }) else { return false }
                    return item.resultID.map { searchedResultIDs?.contains($0) ?? false } ?? false
                case .watched: return false
                }
            }
        }
        sortRows(&next)
        let signatures = Dictionary(uniqueKeysWithValues: next.map { row in
            (row, filename(for: row) + "\n" + stateDetail(for: row) + "\n" + model(for: row) +
                "\n" + String(duration(for: row) ?? -1))
        })
        let changed = next != visibleRows
        visibleRows = next
        let preferredIndex = preferredSelectionRowID.flatMap { next.firstIndex(of: $0) }
        preferredSelectionRowID = nil
        if changed {
            suppressSelectionUpdates = true
            table.reloadData()
        }
        else if !next.isEmpty {
            // Only changed presentations redraw during continuous progress.
            let dirty = IndexSet(next.indices.filter { rowSignatures[next[$0]] != signatures[next[$0]] })
            if !dirty.isEmpty { table.reloadData(forRowIndexes: dirty, columnIndexes: IndexSet(integer: 0)) }
            if let selectedRowIdentity, case .watched = selectedRowIdentity,
               let selectedIndex = next.firstIndex(of: selectedRowIdentity), dirty.contains(selectedIndex) {
                self.selectedRowIdentity = nil
                tableViewSelectionDidChange(Notification(name: NSTableView.selectionDidChangeNotification, object: table))
            }
        }
        rowSignatures = signatures
        if changed || preferredIndex != nil {
            let indexes = RecordingSelection.indexes(in: next, preserving: selected,
                watchedPaths: selectedWatchedPaths, preferring: preferredIndex.map { next[$0] })
            if !indexes.isEmpty { table.selectRowIndexes(indexes, byExtendingSelection: false) }
            else if !next.isEmpty { table.selectRowIndexes(IndexSet(integer: 0), byExtendingSelection: false) }
            else { table.deselectAll(nil) }
            suppressSelectionUpdates = false
            tableViewSelectionDidChange(Notification(name: NSTableView.selectionDidChangeNotification, object: table))
        }
        updateSidebar(); updateWatchedCard()
    }

    @objc func selectFilter(_ sender: NSButton) {
        selectedFilter = RecordingFilter(rawValue: sender.tag) ?? .all
        rebuildRows()
    }

    @objc func selectExtraFilter(_ sender: NSPopUpButton) {
        selectedFilter = sender.indexOfSelectedItem == 1 ? .failed : sender.indexOfSelectedItem == 2 ? .cancelled : .all
        rebuildRows()
    }

    @objc func changeSort() {
        sortMode = RecordingSort(rawValue: sortPicker.indexOfSelectedItem) ?? .importedNewest
        defaults.set(sortMode.rawValue, forKey: "ThreePaneSort")
        rebuildRows()
    }

    func controlTextDidChange(_ notification: Notification) {
        searchTimer?.invalidate()
        searchGeneration += 1
        searchRequest?.cancel(); searchRequest = nil
        searchedResultIDs = nil
        rebuildRows()
        searchTimer = Timer.scheduledTimer(withTimeInterval: 0.3, repeats: false) { [weak self] _ in self?.performSearch() }
    }

    func performSearch() {
        let query = search.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        searchGeneration += 1; let generation = searchGeneration
        searchRequest?.cancel(); searchRequest = nil
        guard !query.isEmpty else { searchedResultIDs = nil; rebuildRows(); return }
        searchRequest = client.request(["action": "list", "query": query]) { [weak self] result in
            guard let self, self.searchGeneration == generation else { return }
            self.searchRequest = nil
            switch result {
            case .success(let value):
                self.searchedResultIDs = Set((value["results"] as? [[String: Any]] ?? []).compactMap { $0["result_id"] as? String })
                self.rebuildRows()
            case .failure: self.searchedResultIDs = []; self.rebuildRows()
            }
        }
    }

    func storeSplitWidths(_ notification: Notification) {
        guard let split = notification.object as? NSSplitView, split.subviews.count >= 2 else { return }
        let width = split.subviews[0].frame.width
        if width < 100 { return }
        if split === outerSplit { defaults.set(Double(width), forKey: "ThreePaneSidebarWidth") }
        else if split === contentSplit { defaults.set(Double(width), forKey: "ThreePaneListWidth") }
    }
}
