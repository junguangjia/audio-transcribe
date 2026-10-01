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
        manualVerifyActive = false; manualVerifyRemaining.removeAll(); manualVerifyTotal = 0; manualVerifyUnready = 0
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
        guard watcher.folder != nil else { return }
        manualVerifyActive = true; manualVerifyRemaining.removeAll(); manualVerifyTotal = 0; manualVerifyUnready = 0
        watchStatus = "正在核对文件…"; updateWatchedCard()
        watcher.refresh(forceVerify: true)
    }

    func updateManualVerifyProgress() {
        guard manualVerifyActive else { return }
        if manualVerifyRemaining.isEmpty {
            manualVerifyActive = false
            watchStatus = manualVerifyUnready == 0 ?
                (watcher.usingPolling ? "已启用（定时检查）" : "已启用") :
                "已核对；\(manualVerifyUnready) 个文件未就绪"
        } else {
            watchStatus = "正在核对 \(manualVerifyTotal - manualVerifyRemaining.count)/\(manualVerifyTotal)"
        }
        updateWatchedCard()
    }

    func handleWatchedSnapshot(_ files: [WatchedFileSnapshot], state: String, changedPaths: Set<String>) {
        guard let folder = watcher.folder else { return }
        if !state.hasPrefix("已启用") { manualVerifyActive = false; watchStatus = state }
        else if !manualVerifyActive { watchStatus = state }
        let current = Dictionary(uniqueKeysWithValues: files.map { ($0.path, $0) })
        if manualVerifyActive && manualVerifyTotal == 0 {
            manualVerifyRemaining = Set(files.filter { $0.unavailableReason == nil }.map(\.path))
            manualVerifyTotal = manualVerifyRemaining.count
            manualVerifyUnready = files.count - manualVerifyTotal
            updateManualVerifyProgress()
        }
        let missing = Set(watchedSnapshots.keys).subtracting(current.keys)
        let changedVersions = Set(current.compactMap { path, snapshot -> String? in
            guard let old = watchedSnapshots[path], old.version != snapshot.version else { return nil }
            return path
        })
        for path in changedPaths.union(missing).union(changedVersions) {
            reader.invalidateExternalSource(at: path)
        }
        for path in missing {
            watchedItems.removeValue(forKey: path); discoveredVersions.removeValue(forKey: path)
            watchedFirstSeen.removeValue(forKey: path); discoveryQueuedPaths.remove(path)
            discoveryRetryCounts.removeValue(forKey: path)
        }
        watchedSnapshots = current
        discoveryForces.formUnion(changedPaths.intersection(current.keys))
        for file in files {
            watchedFirstSeen[file.path] = watchedFirstSeen[file.path] ?? Date().timeIntervalSince1970
            if let reason = file.unavailableReason {
                watchedItems[file.path] = WatchedDiscovery(["path": file.path, "filename": file.filename,
                    "state": "unavailable", "version_key": file.version, "message": reason])
                discoveredVersions[file.path] = file.version
                continue
            }
            if !file.stable {
                if discoveredVersions[file.path] != file.version {
                    discoveryRetryCounts.removeValue(forKey: file.path)
                    watchedItems[file.path] = WatchedDiscovery(["path": file.path, "filename": file.filename,
                                                                 "state": "copying", "version_key": file.version,
                                                                 "message": "正在复制，稍后可用"])
                }
                continue
            }
            if (discoveredVersions[file.path] != file.version || discoveryForces.contains(file.path)), !discoveryQueuedPaths.contains(file.path) {
                discoveringPaths.append((file.path, file.version)); discoveryQueuedPaths.insert(file.path)
            }
        }
        updateWatchedCard(); rebuildRows(); drainDiscovery(folder: folder)
    }

    func drainDiscovery(folder: URL) {
        guard watcher.folder?.path == folder.path else { return }
        while discoveryInFlight < 2, !discoveringPaths.isEmpty {
            let batch = Array(discoveringPaths.prefix(3)); discoveringPaths.removeFirst(min(3, discoveringPaths.count))
            discoveryInFlight += 1
            let generation = watchGeneration
            let forced = batch.map(\.0).filter { discoveryForces.contains($0) }
            let requestID = UUID()
            discoveryRequests[requestID] = client.request(["action": "discover", "folder": folder.path, "paths": batch.map(\.0),
                            "changed_paths": forced]) { [weak self] response in
                guard let self else { return }
                self.discoveryRequests.removeValue(forKey: requestID)
                guard self.watchGeneration == generation else { return }
                self.discoveryInFlight -= 1
                guard self.watcher.folder?.path == folder.path else { return }
                for (path, version) in batch {
                    self.discoveryQueuedPaths.remove(path)
                    self.discoveryForces.remove(path)
                    guard self.watchedSnapshots[path]?.version == version else { continue }
                    switch response {
                    case .success(let value):
                        let raw = value["items"] as? [[String: Any]] ?? []
                        if let item = raw.first(where: { $0["path"] as? String == path }).flatMap(WatchedDiscovery.init) {
                            if let fenced = self.watchDeletionFences[path], fenced != item.versionKey {
                                self.watchDeletionFences.removeValue(forKey: path)
                                self.watchIgnorePending.remove(path)
                                self.watchIgnoreErrors.removeValue(forKey: path)
                                self.persistWatchDeletionFences()
                            }
                            self.watchedItems[path] = item
                            if item.state == "copying" {
                                let retries = (self.discoveryRetryCounts[path] ?? 0) + 1
                                self.discoveryRetryCounts[path] = retries
                                if WatchedDiscoveryRetry.shouldSchedule(after: retries) {
                                    DispatchQueue.main.asyncAfter(deadline: .now() + WatchedDiscoveryRetry.interval) { [weak self] in
                                        guard let self, self.watchedSnapshots[path]?.version == version,
                                              !self.discoveryQueuedPaths.contains(path) else { return }
                                        self.discoveringPaths.append((path, version)); self.discoveryQueuedPaths.insert(path)
                                        self.drainDiscovery(folder: folder)
                                    }
                                } else if self.manualVerifyRemaining.remove(path) != nil {
                                    self.manualVerifyUnready += 1; self.updateManualVerifyProgress()
                                }
                            } else {
                                self.discoveryRetryCounts.removeValue(forKey: path)
                                self.discoveredVersions[path] = version
                                if self.manualVerifyRemaining.remove(path) != nil { self.updateManualVerifyProgress() }
                                if item.state == "ready" {
                                    // Legacy incomplete queue rows predate watched-version
                                    // identities. Bind their current bytes once, so unchanged
                                    // cancelled/failed work requires the explicit Retry action.
                                    for index in self.queue.items.indices where self.queue.items[index].url.standardizedFileURL.path == path &&
                                        self.queue.items[index].watchedVersionKey == nil &&
                                        ["waiting", "checking", "processing", "failed", "cancelled"].contains(self.queue.items[index].state) {
                                        self.queue.items[index].watchedVersionKey = item.versionKey
                                    }
                                    self.persist()
                                }
                            }
                        }
                    case .failure(let error):
                        self.watchedItems[path] = WatchedDiscovery(["path": path,
                            "filename": URL(fileURLWithPath: path).lastPathComponent,
                            "state": "unavailable", "version_key": version,
                            "message": error.localizedDescription])
                        self.discoveredVersions[path] = version
                        if self.manualVerifyRemaining.remove(path) != nil {
                            self.manualVerifyUnready += 1; self.updateManualVerifyProgress()
                        }
                    }
                }
                self.rebuildRows(); self.drainDiscovery(folder: folder)
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
