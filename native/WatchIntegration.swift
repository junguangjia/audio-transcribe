import Cocoa

extension MainController {
    func persistWatchDeletionFences() {
        defaults.set(watchDeletionFences, forKey: "PendingWatchIgnores")
        defaults.synchronize()
    }

    func restoreWatchedFolder() {
        if let pending = defaults.dictionary(forKey: "PendingWatchIgnores") as? [String: String] {
            watchDeletionFences = pending
            watchIgnorePending = Set(pending.keys)
            for (path, version) in pending { ignoreWatchedVersion(path: path, versionKey: version) }
        }
        // This local app is not sandboxed. A file-provider Desktop folder can
        // block bookmark resolution on the main thread during app startup.
        // The saved path is sufficient here; the watcher checks access and
        // availability on its own queue.
        var location: URL?
        if let path = defaults.string(forKey: "WatchedFolderPath"), !path.isEmpty {
            location = URL(fileURLWithPath: path, isDirectory: true)
        }
        if let location { configureWatchedFolder(location) }
        else { watchStatus = "未启用"; updateWatchedCard() }
        let workspace = NSWorkspace.shared.notificationCenter
        watchObservers.append(workspace.addObserver(forName: NSWorkspace.didWakeNotification, object: nil, queue: .main) { [weak self] _ in self?.watcher.refresh() })
        watchObservers.append(NotificationCenter.default.addObserver(forName: NSApplication.didBecomeActiveNotification, object: nil, queue: .main) { [weak self] _ in self?.watcher.refresh() })
    }

    func configureWatchedFolder(_ url: URL) {
        let selected = url.standardizedFileURL
        watchGeneration += 1
        discoveryRequests.values.forEach { $0.cancel() }; discoveryRequests.removeAll()
        watcher.stop(); watchedItems.removeAll(); watchedSnapshots.removeAll(); watchedFirstSeen.removeAll()
        discoveredVersions.removeAll(); discoveringPaths.removeAll(); discoveryQueuedPaths.removeAll()
        discoveryForces.removeAll(); discoveryRetryCounts.removeAll(); discoveryInFlight = 0
        watchScanInProgress = true
        defaults.set(selected.path, forKey: "WatchedFolderPath")
        watchStatus = "正在检查"; updateWatchedCard(); rebuildRows()
        watcher.start(selected)
    }

    @objc func chooseWatchedFolder() {
        guard let window else { return }
        let panel = NSOpenPanel(); panel.title = "选择监控文件夹"; panel.prompt = "监控此文件夹"
        panel.canChooseFiles = false; panel.canChooseDirectories = true; panel.allowsMultipleSelection = false
        panel.directoryURL = watcher.folder ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Desktop", isDirectory: true)
        panel.beginSheetModal(for: window) { [weak self] response in
            guard response == .OK, let url = panel.url else { return }
            self?.configureWatchedFolder(url)
        }
    }

    @objc func openWatchedFolder() {
        guard let folder = watcher.folder else { return }
        NSWorkspace.shared.open(folder)
    }

    @objc func refreshWatchedFolder() {
        refreshWatchedFolder(forceVerify: NSApp.currentEvent?.modifierFlags.contains(.option) == true)
    }

    func refreshWatchedFolder(forceVerify: Bool) {
        guard watcher.folder != nil else { return }
        // A manual refresh also retries previous backend failures. Dataless
        // placeholders are retried only once the OS reports them as local.
        for (path, item) in watchedItems where ["failed", "unavailable"].contains(item.state) &&
            watchedSnapshots[path]?.unavailableReason == nil {
            discoveredVersions.removeValue(forKey: path)
            discoveryRetryCounts.removeValue(forKey: path)
        }
        updateWatchedCard()
        watcher.refresh(forceVerify: forceVerify)
    }

    func needsWatchedDiscovery(_ file: WatchedFileSnapshot) -> Bool {
        file.unavailableReason == nil &&
            (discoveredVersions[file.path] != file.version || discoveryForces.contains(file.path))
    }

    private func pendingWatchedItem(_ file: WatchedFileSnapshot, state: String, message: String? = nil) -> WatchedDiscovery? {
        var value: [String: Any] = ["path": file.path, "filename": file.filename,
                                   "state": state, "version_key": file.version]
        if let message { value["message"] = message }
        return WatchedDiscovery(value)
    }

    func handleWatchedSnapshot(_ files: [WatchedFileSnapshot], state: String, changedPaths: Set<String>) {
        guard let folder = watcher.folder else { return }
        watchScanInProgress = watcher.checkingDirectory
        watchStatus = state
        let current = Dictionary(uniqueKeysWithValues: files.map { ($0.path, $0) })
        let missing = Set(watchedSnapshots.keys).subtracting(current.keys)
        let changedVersions = Set(files.compactMap { file -> String? in
            guard let old = watchedSnapshots[file.path], old.version != file.version else { return nil }
            return file.path
        })
        for path in changedPaths.union(missing).union(changedVersions) {
            reader.invalidateExternalSource(at: path)
        }
        for path in missing {
            watchedItems.removeValue(forKey: path)
            discoveredVersions.removeValue(forKey: path)
            watchedFirstSeen.removeValue(forKey: path)
            discoveryForces.remove(path)
            discoveryRetryCounts.removeValue(forKey: path)
        }
        for path in changedVersions { discoveryRetryCounts.removeValue(forKey: path) }
        watchedSnapshots = current
        discoveryForces.formUnion(files.lazy.filter { $0.unavailableReason == nil && changedPaths.contains($0.path) }.map(\.path))
        let now = Date().timeIntervalSince1970
        for file in files {
            watchedFirstSeen[file.path] = watchedFirstSeen[file.path] ?? now
            if let reason = file.unavailableReason {
                watchedItems[file.path] = pendingWatchedItem(file, state: "unavailable", message: reason)
                discoveredVersions[file.path] = file.version
                discoveryForces.remove(file.path)
            } else if needsWatchedDiscovery(file), !file.stable {
                watchedItems[file.path] = pendingWatchedItem(file, state: "copying")
            }
        }
        enqueueWatchedDiscovery()
        drainDiscovery(folder: folder)
        rebuildRows()
    }

    private func enqueueWatchedDiscovery() {
        let candidates = watchedSnapshots.values.filter {
            $0.stable && needsWatchedDiscovery($0) &&
                !discoveryQueuedPaths.contains($0.path) && discoveryRetryCounts[$0.path] == nil
        }
        for file in candidates {
            discoveringPaths.append((file.path, file.version))
            discoveryQueuedPaths.insert(file.path)
            watchedItems[file.path] = pendingWatchedItem(file, state: "watch_queued")
        }
        if !candidates.isEmpty {
            // New arrivals should be checked ahead of the startup backlog.
            discoveringPaths.sort {
                let lhs = watchedFirstSeen[$0.0] ?? 0
                let rhs = watchedFirstSeen[$1.0] ?? 0
                return lhs == rhs ? $0.0 < $1.0 : lhs > rhs
            }
        }
    }

    func drainDiscovery(folder: URL) {
        guard watcher.folder?.path == folder.path else { return }
        while discoveryInFlight < 2, !discoveringPaths.isEmpty {
            let queued = Array(discoveringPaths.prefix(8))
            discoveringPaths.removeFirst(queued.count)
            let batch = queued.filter { path, version in
                guard let file = watchedSnapshots[path], file.version == version, file.stable,
                      needsWatchedDiscovery(file) else {
                    discoveryQueuedPaths.remove(path)
                    return false
                }
                watchedItems[path] = pendingWatchedItem(file, state: "watch_checking")
                return true
            }
            if batch.count != queued.count { enqueueWatchedDiscovery() }
            guard !batch.isEmpty else { continue }
            discoveryInFlight += 1
            let generation = watchGeneration
            let forced = batch.map(\.0).filter { discoveryForces.remove($0) != nil }
            let forcedPaths = Set(forced)
            for path in forced { discoveredVersions.removeValue(forKey: path) }
            let requestID = UUID()
            let versions = Dictionary(uniqueKeysWithValues: batch)
            discoveryRequests[requestID] = client.request(["action": "discover", "folder": folder.path,
                "paths": batch.map(\.0), "changed_paths": forced, "stable_versions": versions]) { [weak self] response in
                guard let self, self.watchGeneration == generation else { return }
                self.discoveryRequests.removeValue(forKey: requestID)
                self.discoveryInFlight -= 1
                let items: [String: WatchedDiscovery]
                let failure: String
                switch response {
                case .success(let value):
                    let rows = (value["items"] as? [[String: Any]] ?? []).compactMap { raw -> WatchedDiscovery? in
                        var row = raw
                        if row["version_key"] == nil, let path = row["path"] as? String, row["state"] as? String == "unavailable" {
                            row["version_key"] = versions[path]
                        }
                        return WatchedDiscovery(row)
                    }
                    items = Dictionary(rows.map { ($0.path, $0) }, uniquingKeysWith: { first, _ in first })
                    failure = "未能读取文件检查结果，请重试。"
                case .failure(let error):
                    items = [:]
                    failure = error.localizedDescription
                }
                var queueChanged = false
                for (path, version) in batch {
                    self.discoveryQueuedPaths.remove(path)
                    guard let file = self.watchedSnapshots[path], file.version == version,
                          file.unavailableReason == nil, !self.discoveryForces.contains(path) else { continue }
                    guard let item = items[path] else {
                        self.watchedItems[path] = self.pendingWatchedItem(file, state: "failed", message: failure)
                        self.discoveredVersions[path] = version
                        self.discoveryRetryCounts.removeValue(forKey: path)
                        continue
                    }
                    if item.state == "copying" {
                        self.scheduleWatchedRetry(file, folder: folder, generation: generation, force: forcedPaths.contains(path))
                        continue
                    }
                    self.discoveryRetryCounts.removeValue(forKey: path)
                    self.discoveredVersions[path] = version
                    if let fenced = self.watchDeletionFences[path], fenced != item.versionKey,
                       ["ready", "processed", "processed_candidate", "ignored"].contains(item.state) {
                        self.watchDeletionFences.removeValue(forKey: path)
                        self.watchIgnorePending.remove(path)
                        self.watchIgnoreErrors.removeValue(forKey: path)
                        self.persistWatchDeletionFences()
                    }
                    self.watchedItems[path] = item
                    if item.state == "ready" {
                        // Bind old incomplete queue rows to the checked bytes.
                        for index in self.queue.items.indices where self.queue.items[index].url.standardizedFileURL.path == path &&
                            self.queue.items[index].watchedVersionKey == nil &&
                            ["waiting", "checking", "processing", "failed", "cancelled"].contains(self.queue.items[index].state) {
                            self.queue.items[index].watchedVersionKey = item.versionKey
                            queueChanged = true
                        }
                    }
                }
                if queueChanged { self.persist() }
                self.enqueueWatchedDiscovery()
                self.drainDiscovery(folder: folder)
                self.rebuildRows()
            }
        }
    }

    private func scheduleWatchedRetry(_ file: WatchedFileSnapshot, folder: URL, generation: Int, force: Bool) {
        let path = file.path
        let attempts = (discoveryRetryCounts[path] ?? 0) + 1
        guard WatchedDiscoveryRetry.shouldSchedule(after: attempts) else {
            discoveryRetryCounts.removeValue(forKey: path)
            discoveredVersions[path] = file.version
            watchedItems[path] = pendingWatchedItem(file, state: "failed", message: "文件仍未稳定，请等待复制结束后重试。")
            return
        }
        discoveryRetryCounts[path] = attempts
        if force { discoveryForces.insert(path) }
        watchedItems[path] = pendingWatchedItem(file, state: "copying")
        DispatchQueue.main.asyncAfter(deadline: .now() + WatchedDiscoveryRetry.interval) { [weak self] in
            guard let self, self.watchGeneration == generation,
                  self.watchedSnapshots[path]?.version == file.version,
                  self.discoveryRetryCounts[path] == attempts else { return }
            self.discoveryRetryCounts.removeValue(forKey: path)
            // Carry retry count through dispatch until the response arrives.
            if let snapshot = self.watchedSnapshots[path], snapshot.stable, self.needsWatchedDiscovery(snapshot),
               self.discoveryQueuedPaths.insert(path).inserted {
                self.discoveryRetryCounts[path] = attempts
                self.discoveringPaths.insert((path, file.version), at: 0)
                self.watchedItems[path] = self.pendingWatchedItem(file, state: "watch_queued")
                self.drainDiscovery(folder: folder)
                self.rebuildRows()
            }
        }
    }

    @objc func startAllNew() {
        startWatched(paths: nil)
    }

    func startWatched(paths: Set<String>?) {
        let ready = watchedItems.values.filter { $0.state == "ready" &&
            $0.versionKey.range(of: "^[0-9a-f]{64}$", options: .regularExpression) != nil &&
            !isAlreadyQueued($0) && !isWatchDeletionFenced($0) &&
            (paths == nil || paths!.contains($0.path)) }
            .sorted { $0.filename.localizedStandardCompare($1.filename) == .orderedAscending }
        guard !ready.isEmpty else { return }
        let model = DirectSettings.model(defaults.string(forKey: "DefaultModel"))
        var firstJobID: UUID?
        for item in ready {
            var recording = Recording(url: URL(fileURLWithPath: item.path))
            recording.state = "waiting"; recording.autoStart = true; recording.inputMode = "referenced"
            recording.watchedVersionKey = item.versionKey; recording.model = model
            if firstJobID == nil { firstJobID = recording.id }
            queue.items.append(recording)
        }
        if let id = firstJobID {
            prepareSubmissionSelection(id)
        }
        persist(); rebuildRows(); startNext()
    }

    func ignoreWatchedVersion(path: String, versionKey: String) {
        watchDeletionFences[path] = versionKey
        watchIgnorePending.insert(path); watchIgnoreErrors.removeValue(forKey: path)
        persistWatchDeletionFences()
        rebuildRows()
        client.request(["action": "watch_ignore", "path": path, "version_key": versionKey]) { [weak self] response in
            guard let self else { return }
            guard self.watchDeletionFences[path] == versionKey else { return }
            self.watchIgnorePending.remove(path)
            switch response {
            case .success:
                self.watchIgnoreErrors.removeValue(forKey: path)
                self.discoveredVersions.removeValue(forKey: path); self.watcher.refresh()
            case .failure(let error):
                self.watchIgnoreErrors[path] = error.localizedDescription
                self.cleanupStatus.stringValue = "应用内数据已删除，监控忽略未完成。请在录音列表中点击“重试”；原录音未删除。"
                self.cleanupStatus.isHidden = false
            }
            self.rebuildRows()
        }
    }

    func readdWatchedVersion(path: String, versionKey: String) {
        client.request(["action": "watch_readd", "path": path, "version_key": versionKey]) { [weak self] response in
            guard let self else { return }
            if case .success = response {
                if self.watchDeletionFences[path] == versionKey {
                    self.watchDeletionFences.removeValue(forKey: path)
                    self.watchIgnorePending.remove(path); self.watchIgnoreErrors.removeValue(forKey: path)
                    self.persistWatchDeletionFences()
                }
                self.discoveredVersions.removeValue(forKey: path); self.watcher.refresh()
                self.rebuildRows()
            }
        }
    }
}
