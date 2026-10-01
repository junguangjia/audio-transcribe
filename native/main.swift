import Cocoa
import UniformTypeIdentifiers

let reorderType = NSPasteboard.PasteboardType("local.audio-transcribe.queue-row")

func fileURLs(_ pasteboard: NSPasteboard) -> [URL] {
    (pasteboard.readObjects(forClasses: [NSURL.self], options: [.urlReadingFileURLsOnly: true]) as? [URL]) ?? []
}

final class DropTarget: NSView {
    var accept: (([URL]) -> Bool)?
    var enabled = true
    var hovering = false { didSet { needsDisplay = true } }
    override init(frame: NSRect) {
        super.init(frame: frame)
        registerForDraggedTypes([.fileURL])
        setAccessibilityLabel("将录音拖到这里，或点击导入录音")
    }
    required init?(coder: NSCoder) { fatalError() }
    override func draw(_ dirtyRect: NSRect) {
        let rect = bounds.insetBy(dx: 1, dy: 1)
        let shape = NSBezierPath(roundedRect: rect, xRadius: 12, yRadius: 12)
        (hovering ? NSColor.controlAccentColor.withAlphaComponent(0.10) : NSColor.controlBackgroundColor).setFill()
        shape.fill()
        (hovering ? NSColor.controlAccentColor : NSColor.separatorColor).setStroke()
        shape.lineWidth = hovering ? 2 : 1
        shape.setLineDash([6, 4], count: 2, phase: 0)
        shape.stroke()
    }
    override func draggingEntered(_ sender: NSDraggingInfo) -> NSDragOperation {
        hovering = enabled && !fileURLs(sender.draggingPasteboard).isEmpty
        return hovering ? .copy : []
    }
    override func draggingExited(_ sender: NSDraggingInfo?) { hovering = false }
    override func performDragOperation(_ sender: NSDraggingInfo) -> Bool {
        hovering = false
        return enabled && (accept?(fileURLs(sender.draggingPasteboard)) ?? false)
    }
}

final class MainController: NSWindowController, NSWindowDelegate, NSTableViewDataSource, NSTableViewDelegate, NSSearchFieldDelegate {
    let queue = RecordingQueue()
    let table = RecordingTable()
    let reader = DirectResultView(), client = DirectClient()
    let drop = DropTarget()
    let outerSplit = NSSplitView(), contentSplit = NSSplitView()
    let outerSplitController = NSSplitViewController(), contentSplitController = NSSplitViewController()
    let search = NSSearchField(), sortPicker = NSPopUpButton(), extraFilterPicker = NSPopUpButton()
    let watchPath = NSTextField(labelWithString: "尚未选择文件夹")
    let watchState = NSTextField(labelWithString: "未启用")
    let sidebarFolderState = NSTextField(labelWithString: "未启用")
    let openFolder = NSButton(title: "打开文件夹", target: nil, action: nil)
    let changeFolder = NSButton(title: "更改…", target: nil, action: nil)
    let refreshFolder = NSButton(title: "刷新", target: nil, action: nil)
    let startAll = NSButton(title: "开始全部新录音（0）", target: nil, action: nil)
    let watcher = WatchedFolderMonitor()
    var watchedItems: [String: WatchedDiscovery] = [:]
    // Keep a deleted watched version out of the one-click queue until the
    // durable watch_ignore request has completed (or has been retried).
    var watchDeletionFences: [String: String] = [:]
    var watchIgnorePending = Set<String>()
    var watchIgnoreErrors: [String: String] = [:]
    var watchedSnapshots: [String: WatchedFileSnapshot] = [:]
    var watchedFirstSeen: [String: TimeInterval] = [:]
    var discoveredVersions: [String: String] = [:]
    var discoveringPaths: [(String, String)] = []
    var discoveryInFlight = 0
    var discoveryRequests: [UUID: DirectRequest] = [:]
    var watchGeneration = 0
    var discoveryQueuedPaths = Set<String>()
    var discoveryForces = Set<String>()
    var discoveryRetryCounts: [String: Int] = [:]
    var watchObservers: [NSObjectProtocol] = []
    var splitObservers: [NSObjectProtocol] = []
    var watchStatus = "未启用"
    var manualVerifyActive = false
    var manualVerifyRemaining = Set<String>()
    var manualVerifyTotal = 0
    var manualVerifyUnready = 0
    var visibleRows: [RecordingRowID] = []
    var selectedRowIdentity: RecordingRowID?
    var preferredSelectionRowID: RecordingRowID?
    var suppressSelectionUpdates = false
    var rowSignatures: [RecordingRowID: String] = [:]
    var rowCounts: [RecordingFilter: Int] = [:]
    var sidebarButtons: [RecordingFilter: NSButton] = [:]
    var selectedFilter = RecordingFilter.all
    var sortMode = RecordingSort.importedNewest
    var searchedResultIDs: Set<String>?
    var searchGeneration = 0
    var searchTimer: Timer?
    var searchRequest: DirectRequest?
    let status = NSTextField(wrappingLabelWithString: "导入即开始转录，每个录音生成独立结果。")
    let cancel = NSButton(title: "取消全部", target: nil, action: nil)
    let resume = NSButton(title: "继续未完成任务", target: nil, action: nil)
    let deleteButton = NSButton(title: "删除所选…", target: nil, action: nil)
    let cleanupStatus = NSTextField(wrappingLabelWithString: "")
    let cleanupRetry = NSButton(title: "重试清理", target: nil, action: nil)
    let cleanupCancel = NSButton(title: "取消删除", target: nil, action: nil)
    var pendingDeletion: [String: Any]?
    var deletionBusy = false
    var deletingJobIDs = Set<UUID>(), deletedJobIDs = Set<UUID>()
    var deletingResultIDs = Set<String>(), deletedResultIDs = Set<String>()
    var listRequest: DirectRequest?
    var listRefreshQueued = false
    let defaults: UserDefaults
    var results: [DirectResult] = []
    var resultGeneration = 0
    var selectedJobID: UUID?
    var process: Process?
    var batchProgress: BatchProgress?
    var stdoutBuffer = Data()
    var controlInput: FileHandle?
    var receivedTerminalEvent = false, cancelRequested = false, quitting = false
    var cancelRequestedAt: TimeInterval?, lastProtocolEventAt: TimeInterval?
    var cancellationFallbackSent = false
    var progressTimer: Timer?
    var duplicatePrompts: [(UUID, DirectResult)] = []
    var showingDuplicate = false
    var running: Bool { process != nil }
    init(defaults: UserDefaults = .standard, startServices: Bool = true) {
        self.defaults = defaults
        // A task-private QA bundle can exercise both semantic appearances
        // without changing the user's system appearance or the Test bundle.
        if Bundle.main.object(forInfoDictionaryKey: "AudioTranscribeSettingsPath") != nil,
           let appearance = UserDefaults.standard.string(forKey: "QAForceAppearance") {
            NSApp.appearance = NSAppearance(named: appearance == "dark" ? .darkAqua : .aqua)
        }
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1450, height: 850), styleMask: [.titled,.closable,.miniaturizable,.resizable], backing: .buffered, defer: false)
        window.title = Bundle.main.object(forInfoDictionaryKey: "CFBundleDisplayName") as? String ?? "AudioTranscribe"
        window.minSize = NSSize(width: 1150, height: 650)
        if startServices { window.setFrameAutosaveName("AudioTranscribeThreePaneWindow") }
        super.init(window: window); window.delegate = self
        sortMode = RecordingSort(rawValue: defaults.integer(forKey: "ThreePaneSort")) ?? .importedNewest
        queue.items = QueuePersistence.restore(defaults.stringArray(forKey: "SelectedFiles") ?? [], saved: defaults.array(forKey: "QueueState") as? [[String:String]] ?? [])
        buildView()
        reader.onRetranscribe = { [weak self] url, model, inputMode in self?.enqueueAgain(url, model: model, inputMode: inputMode) }
        reader.onChanged = { [weak self] in self?.refreshResults() }
        reader.onDelete = { [weak self] in
            guard let self else { return }
            if let id = self.reader.result?.id { self.planDeletion(self.deletionSelection(resultID: id)) }
            else if let id = self.selectedJobID, let row = self.visibleRows.firstIndex(of: .job(id)) { self.table.selectRowIndexes(IndexSet(integer: row), byExtendingSelection: false); self.deleteSelected() }
            else { self.deleteSelected() }
        }
        table.deleteSelection = { [weak self] in self?.deleteSelected() }
        watcher.onSnapshot = { [weak self] snapshot, state, changed in self?.handleWatchedSnapshot(snapshot, state: state, changedPaths: changed) }
        if startServices { restoreDeletion() }
        refresh()
        if startServices { refreshResults(); restoreWatchedFolder() }
        window.center()
    }
    required init?(coder: NSCoder) { fatalError() }
    func buildView() {
        buildThreePane()
    }
    func persist() {
        defaults.set(queue.items.map{$0.url.path},forKey:"SelectedFiles"); defaults.set(QueuePersistence.snapshot(queue.items),forKey:"QueueState")
        // Legacy LastReport is intentionally retained for the previous app.
    }
    func refresh() {
        rebuildRows(); cancel.isHidden = !running && !queue.items.contains{$0.autoStart || $0.state == "checking"}
        cancel.isEnabled = !cancelRequested
        deleteButton.isEnabled = !deletionBusy && pendingDeletion == nil
        resume.isHidden = !queue.items.contains{!$0.deletionPending && ["waiting","failed","cancelled"].contains($0.state) && !$0.autoStart && batchProgress?.jobs[$0.id]?.isTerminal != false}
    }
    func refreshResults() {
        if listRequest != nil { listRefreshQueued = true; return }
        resultGeneration += 1; let current = resultGeneration
        listRequest = client.request(["action":"list"]) { [weak self] response in
            guard let self, self.resultGeneration == current else { return }
            self.listRequest = nil
            switch response {
            case .success(let value):
                self.results = (value["results"] as? [[String:Any]] ?? []).map{DirectResult(value:$0)}.filter { !self.deletedResultIDs.contains($0.id) }
                self.rebuildRows()
            case .failure(let error): self.status.stringValue = "历史结果暂时不可用：\(error.localizedDescription)"
            }
            if self.listRefreshQueued { self.listRefreshQueued = false; self.refreshResults() }
        }
    }
    @discardableResult func add(_ urls: [URL]) -> Bool {
        var busyPaths = Set(queue.items.filter { ["checking", "waiting", "processing"].contains($0.state) && !$0.deletionPending }
            .map { $0.url.standardizedFileURL.path })
        let accepted = urls.filter { url in
            guard DirectImport.supportedFile(url),
                  (try? url.resourceValues(forKeys: [.isDirectoryKey]).isDirectory) != true else { return false }
            return busyPaths.insert(url.standardizedFileURL.path).inserted
        }
        guard !accepted.isEmpty else { status.stringValue = "请选择支持的音频或视频文件。"; return false }
        let previous = queue.items.count; queue.add(accepted)
        let model = DirectSettings.model(defaults.string(forKey:"DefaultModel"))
        for index in previous..<queue.items.count {
            queue.items[index].state = "checking"; queue.items[index].model = model
            let path = queue.items[index].url.standardizedFileURL.path
            queue.items[index].watchedVersionKey = WatchedQueueIdentity.manualImportVersion(
                path: path, discovery: watchedItems[path])
        }
        prepareSubmissionSelection(queue.items[previous].id)
        status.stringValue = "正在检查录音，随后自动开始转录…"; persist(); refresh()
        for item in queue.items[previous...] {
            client.request(["action":"lookup", "path":item.url.path]) { [weak self] response in
                guard let self, let index = self.queue.items.firstIndex(where: { $0.id == item.id && $0.state == "checking" && !$0.deletionPending }) else { return }
                switch response {
                case .failure(let error):
                    self.queue.items[index].state = "failed"; self.queue.items[index].message = "文件检查失败：\(error.localizedDescription)"
                case .success(let value):
                    if let existing = (value["existing"] as? [[String:Any]])?.first {
                        self.duplicatePrompts.append((item.id, DirectResult(value: existing)))
                    } else { self.queue.items[index].state = "waiting"; self.queue.items[index].autoStart = true }
                }
                self.persist(); self.refresh(); self.startNext(); self.presentDuplicate()
            }
        }
        window?.makeKeyAndOrderFront(nil); return true
    }
    func presentDuplicate() {
        guard !showingDuplicate, !duplicatePrompts.isEmpty, let window else { return }
        let (id,existing) = duplicatePrompts.removeFirst()
        guard let index = queue.items.firstIndex(where:{$0.id == id && $0.state == "checking" && !$0.deletionPending}) else { presentDuplicate(); return }
        showingDuplicate = true
        let alert = NSAlert(); alert.messageText = "这份录音已有结果"; alert.informativeText = "\(queue.items[index].url.lastPathComponent)\n检测到完全相同的文件内容。"
        alert.addButton(withTitle:"打开已有结果"); alert.addButton(withTitle:"重新转录"); alert.addButton(withTitle:"取消")
        alert.beginSheetModal(for:window) { [weak self] response in
            guard let self else { return }; self.showingDuplicate = false
            if let index = self.queue.items.firstIndex(where:{$0.id == id && $0.state == "checking" && !$0.deletionPending}) {
                if response == .alertFirstButtonReturn {
                    self.attachExistingResult(existing, at: index)
                } else if response == .alertSecondButtonReturn { self.queue.items[index].state = "waiting"; self.queue.items[index].autoStart = true; self.queue.items[index].force = true }
                else { self.queue.items[index].state = "cancelled" }
            }
            self.persist(); self.refresh(); self.startNext(); self.presentDuplicate()
        }
    }
    func attachExistingResult(_ existing: DirectResult, at index: Int) {
        queue.items[index].state = existing.state; queue.items[index].resultID = existing.id
        queue.items[index].duration = existing.duration
        queue.items[index].model = existing.value["model"] as? String ?? queue.items[index].model
        prepareSubmissionSelection(queue.items[index].id)
    }
    func enqueueAgain(_ url: URL, model: String, inputMode: String) {
        var item = Recording(url:url); item.model = DirectSettings.model(model); item.force = true; item.autoStart = true
        item.inputMode = inputMode == "referenced" ? "referenced" : "managed"
        queue.items.append(item); prepareSubmissionSelection(item.id)
        persist(); refresh(); startNext()
    }
    @objc func chooseFiles() {
        guard let window else { return }
        let panel = NSOpenPanel(); panel.title = "导入录音"; panel.prompt = "导入并转录"
        panel.canChooseFiles = true; panel.canChooseDirectories = false; panel.allowsMultipleSelection = true
        panel.beginSheetModal(for:window) { [weak self] response in if response == .OK { self?.add(panel.urls) } }
    }
    @objc func showSettings() {
        guard let window else { return }
        let alert = NSAlert(); alert.messageText = "设置"; alert.informativeText = "处理方式用于下一批任务。监控文件夹的原录音留在原处；手动导入使用托管副本。"
        alert.addButton(withTitle:"保存"); alert.addButton(withTitle:"取消")
        let model = NSPopUpButton(); model.addItems(withTitles:DirectSettings.models); model.selectItem(withTitle:DirectSettings.model(defaults.string(forKey:"DefaultModel")))
        let execution = NSPopUpButton(); execution.addItems(withTitles:DirectSettings.executionModes.map { DirectSettings.executionTitle($0) })
        execution.selectItem(at:DirectSettings.executionModes.firstIndex(of:DirectSettings.executionMode(defaults.string(forKey:"ExecutionMode"))) ?? 0)
        execution.setAccessibilityLabel("处理方式")
        let format = NSPopUpButton(); format.addItems(withTitles:DirectSettings.formats); format.selectItem(withTitle:DirectSettings.format(defaults.string(forKey:"DefaultExportFormat")))
        let directory = NSTextField(string:defaults.string(forKey:"DefaultExportDirectory") ?? ""); directory.placeholderString = "留空使用系统保存位置"
        let stack = NSStackView(); stack.orientation = .vertical; stack.alignment = .leading; stack.spacing = 8
        for (name,control) in [("处理方式",execution as NSView),("默认模型",model as NSView),("默认导出格式",format as NSView),("导出位置（完整路径）",directory as NSView)] {
            stack.addArrangedSubview(NSTextField(labelWithString:name)); stack.addArrangedSubview(control); control.widthAnchor.constraint(equalToConstant:350).isActive = true
        }
        stack.frame = NSRect(x:0,y:0,width:350,height:250); alert.accessoryView = stack
        alert.beginSheetModal(for:window) { [weak self] response in
            guard let self, response == .alertFirstButtonReturn else { return }
            self.defaults.set(model.titleOfSelectedItem,forKey:"DefaultModel"); self.defaults.set(format.titleOfSelectedItem,forKey:"DefaultExportFormat")
            self.defaults.set(execution.indexOfSelectedItem == 1 ? "serial" : "auto",forKey:"ExecutionMode")
            let path = directory.stringValue.trimmingCharacters(in:.whitespacesAndNewlines)
            self.defaults.set(path.isEmpty ? nil : (path as NSString).expandingTildeInPath,forKey:"DefaultExportDirectory")
        }
    }
    func numberOfRows(in tableView: NSTableView) -> Int { visibleRows.count }
    func tableView(_ tableView: NSTableView, heightOfRow row: Int) -> CGFloat {
        guard visibleRows.indices.contains(row) else { return 84 }
        let width = max(220, tableView.bounds.width - 78)
        let name = filename(for: visibleRows[row]) as NSString
        let rect = name.boundingRect(with: NSSize(width: width, height: 300),
                                     options: [.usesLineFragmentOrigin],
                                     attributes: [.font: NSFont.systemFont(ofSize: 13, weight: .medium)])
        return max(84, min(158, ceil(rect.height) + 48))
    }
    func tableView(_ tableView: NSTableView, viewFor column: NSTableColumn?, row: Int) -> NSView? {
        guard visibleRows.indices.contains(row) else { return nil }
        let identity = visibleRows[row]
        let cell = NSTableCellView()
        let name = filename(for: identity)
        let icon = NSImageView(image: NSImage(systemSymbolName: "waveform", accessibilityDescription: "录音")!)
        icon.contentTintColor = .controlAccentColor
        icon.translatesAutoresizingMaskIntoConstraints = false; cell.addSubview(icon)
        let title = NSTextField(wrappingLabelWithString: name)
        title.font = .systemFont(ofSize: 13, weight: .medium)
        title.maximumNumberOfLines = 0; title.lineBreakMode = .byWordWrapping
        title.translatesAutoresizingMaskIntoConstraints = false; cell.addSubview(title)
        let information = (duration(for: identity).map { JobProgress.duration($0) } ?? "时长待检查") + " · " + model(for: identity)
        let sub = NSTextField(labelWithString: information)
        sub.font = .systemFont(ofSize: 11); sub.textColor = .secondaryLabelColor
        sub.lineBreakMode = .byTruncatingTail; sub.translatesAutoresizingMaskIntoConstraints = false; cell.addSubview(sub)
        let statusField = NSTextField(labelWithString: stateDetail(for: identity))
        statusField.font = .systemFont(ofSize: 11)
        statusField.textColor = state(for: identity) == "review_required" ? .systemOrange : .secondaryLabelColor
        statusField.lineBreakMode = .byTruncatingTail; statusField.translatesAutoresizingMaskIntoConstraints = false; cell.addSubview(statusField)
        cell.toolTip = name + "\n" + information + "\n" + statusField.stringValue
        let action = NSButton(title: "查看", target: self, action: #selector(rowAction(_:)))
        action.bezelStyle = .rounded; action.translatesAutoresizingMaskIntoConstraints = false
        switch identity {
        case .job(let id):
            action.identifier = NSUserInterfaceItemIdentifier("job|" + id.uuidString)
            if let item = queue.items.first(where: { $0.id == id }) {
                let progress = running ? batchProgress?.jobs[id] : nil
                action.title = item.resultID != nil ? "查看" : progress?.isTerminal == false ? "取消" :
                    (["failed", "cancelled"].contains(item.state) || (item.state == "waiting" && !item.autoStart) ? "重试" : "取消")
                action.isEnabled = !item.deletionPending &&
                    !(progress?.cancellationRequested == true && progress?.isTerminal == false)
            }
        case .result(let id): action.identifier = NSUserInterfaceItemIdentifier("result|" + id)
        case .watched(let path, let version):
            action.identifier = NSUserInterfaceItemIdentifier("watched|" + path + "|" + version)
            let state = self.state(for: identity)
            action.title = state == "ready" ? "开始" : state == "ignored" ? "重新加入" :
                watchIgnoreErrors[path] != nil ? "重试忽略" : "刷新"
            action.isEnabled = state == "ready" || state == "ignored" || state == "failed" || state == "unavailable"
        }
        cell.addSubview(action)
        NSLayoutConstraint.activate([icon.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: 12),
            icon.topAnchor.constraint(equalTo: cell.topAnchor, constant: 14),
            icon.widthAnchor.constraint(equalToConstant: 28), icon.heightAnchor.constraint(equalToConstant: 28),
            title.leadingAnchor.constraint(equalTo: icon.trailingAnchor, constant: 10),
            title.trailingAnchor.constraint(equalTo: cell.trailingAnchor, constant: -10),
            title.topAnchor.constraint(equalTo: cell.topAnchor, constant: 8),
            sub.leadingAnchor.constraint(equalTo: title.leadingAnchor),
            sub.trailingAnchor.constraint(equalTo: cell.trailingAnchor, constant: -88),
            sub.topAnchor.constraint(equalTo: title.bottomAnchor, constant: 3),
            statusField.leadingAnchor.constraint(equalTo: title.leadingAnchor),
            statusField.trailingAnchor.constraint(equalTo: sub.trailingAnchor),
            statusField.topAnchor.constraint(equalTo: sub.bottomAnchor, constant: 3),
            statusField.bottomAnchor.constraint(lessThanOrEqualTo: cell.bottomAnchor, constant: -7),
            action.trailingAnchor.constraint(equalTo: cell.trailingAnchor, constant: -8),
            action.bottomAnchor.constraint(equalTo: cell.bottomAnchor, constant: -8)])
        return cell
    }
    func simpleProgress(_ job: JobProgress) -> String {
        if job.cancellationRequested && !job.isTerminal { return "正在取消…" }
        if job.restartRequired { return "已中断 · 需重新开始" }
        if job.isTerminal { return DirectResult.stateLabel(job.state) }
        let label: String
        switch job.stage {
        case "checking","importing": label = "正在导入"
        case "preparing": label = "正在准备音频"
        case "waiting_for_memory": label = batchProgress?.admission?.reasonLabel ?? "等待可用资源"
        case "waiting_for_execution", "waiting_for_ownership": label = "等待其他批次结束"
        case "transcribing": label = "正在转录" + (job.percent.map{" \($0)%"} ?? "")
        case "validating","finalizing": label = "正在保存结果"
        default: label = "等待转录"
        }
        return label + " · " + JobProgress.duration(job.elapsed(now:ProcessInfo.processInfo.systemUptime))
    }
    func tableViewSelectionDidChange(_ notification: Notification) {
        guard let selected = notification.object as? NSTableView, selected === table else { return }
        if suppressSelectionUpdates { return }
        guard visibleRows.indices.contains(selected.selectedRow) else {
            if selectedRowIdentity == nil { return }
            selectedRowIdentity = nil
            selectedJobID = nil; reader.clearSelection(); return
        }
        let identity = visibleRows[selected.selectedRow]
        if selectedRowIdentity == identity { return }
        selectedRowIdentity = identity
        switch identity {
        case .job(let id):
            guard let item = queue.items.first(where: { $0.id == id }) else { return }
            selectedJobID = id
            if item.deletionPending { reader.clearForDeletion() }
            else if let resultID = item.resultID { open(resultID, jobID: id) }
            else { reader.showPending(item) }
        case .result(let id): open(id, jobID: nil)
        case .watched(let path, let version):
            guard let item = watchedItems[path] else { return }
            guard item.versionKey == version else { return }
            selectedJobID = nil
            var pending = Recording(url: URL(fileURLWithPath: path)); pending.state = state(for: .watched(path, version))
            pending.message = item.message ?? stateDetail(for: .watched(path, version))
            reader.showPending(pending)
            reader.delete.title = "忽略此版本…"
            reader.delete.isEnabled = state(for: .watched(path, version)) == "ready"
        }
    }
    func tableView(_ tableView:NSTableView, validateDrop info:NSDraggingInfo, proposedRow:Int, proposedDropOperation:NSTableView.DropOperation) -> NSDragOperation { fileURLs(info.draggingPasteboard).isEmpty ? [] : .copy }
    func tableView(_ tableView:NSTableView, acceptDrop info:NSDraggingInfo, row:Int, dropOperation:NSTableView.DropOperation) -> Bool { add(fileURLs(info.draggingPasteboard)) }
    func open(_ id: String, jobID: UUID?) { guard !deletingResultIDs.contains(id), !deletedResultIDs.contains(id) else { return }; selectedJobID = jobID; defaults.set(id,forKey:"LastDirectResult"); reader.load(id) }
    @discardableResult func activateVisibleRow(_ identity: RecordingRowID) -> Bool {
        guard let row = visibleRows.firstIndex(of: identity) else { return false }
        // A button inside an NSTableView row does not reliably select that row.
        // Route it through the same selection handler as a row click so the
        // highlighted recording, reader and deletion target stay aligned.
        selectedRowIdentity = nil
        table.selectRowIndexes(IndexSet(integer: row), byExtendingSelection: false)
        tableViewSelectionDidChange(Notification(name: NSTableView.selectionDidChangeNotification, object: table))
        return selectedRowIdentity == identity
    }
    @objc func rowAction(_ button:NSButton) {
        guard let key = button.identifier?.rawValue else { return }
        if key.hasPrefix("result|") { activateVisibleRow(.result(String(key.dropFirst(7)))); return }
        if key.hasPrefix("watched|") {
            let payload = String(key.dropFirst(8))
            guard let separator = payload.lastIndex(of: "|") else { return }
            let path = String(payload[..<separator]), version = String(payload[payload.index(after: separator)...])
            if let item = watchedItems[path], item.versionKey == version,
               activateVisibleRow(.watched(path, version)) {
                let effective = state(for: .watched(path, version))
                if effective == "ready" { startWatched(paths: [path]) }
                else if effective == "ignored" { readdWatchedVersion(path: path, versionKey: item.versionKey) }
                else if watchIgnoreErrors[path] != nil { ignoreWatchedVersion(path: path, versionKey: version) }
                else { discoveredVersions.removeValue(forKey: path); watcher.refresh() }
            }
            return
        }
        guard key.hasPrefix("job|"), let id = UUID(uuidString: String(key.dropFirst(4))),
              let index = queue.items.firstIndex(where: { $0.id == id }), activateVisibleRow(.job(id)) else { return }
        let item = queue.items[index]
        guard !item.deletionPending else { return }
        if item.resultID != nil { return }
        if let job = batchProgress?.jobs[item.id], running, !job.isTerminal { cancelJob(job); return }
        if ["checking","waiting"].contains(item.state) && (item.autoStart || item.state == "checking") { queue.items[index].state = "cancelled"; queue.items[index].autoStart = false }
        else { queue.items[index] = DirectQueue.resuming(item); selectedJobID = item.id; reader.showPending(queue.items[index]) }
        persist(); refresh(); startNext()
    }
    @objc func resumeAll() {
        for index in queue.items.indices where !queue.items[index].deletionPending && ["waiting","failed","cancelled"].contains(queue.items[index].state) && batchProgress?.jobs[queue.items[index].id]?.isTerminal != false {
            queue.items[index] = DirectQueue.resuming(queue.items[index])
        }
        persist(); refresh(); startNext()
    }
    func startNext() {
        guard !running, !quitting else { return }
        let items = DirectQueue.nextSubmission(queue.items)
        guard !items.isEmpty else {
            if let summary = DirectQueue.idleSummary(queue.items) { status.stringValue = summary }
            return
        }
        // On Darwin both systemUptime and Python monotonic use mach_absolute_time.
        let submitted = ProcessInfo.processInfo.systemUptime
        do {
            guard let codeRoot = Bundle.main.object(forInfoDictionaryKey:"AudioTranscribeCodeRoot") as? String else { throw CocoaError(.fileNoSuchFile) }
            let base = URL(fileURLWithPath:codeRoot)
            let logRoot = (Bundle.main.object(forInfoDictionaryKey: "AudioTranscribeLogRoot") as? String).map { URL(fileURLWithPath: $0, isDirectory: true) }
                ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Logs/AudioTranscribe", isDirectory: true)
            let job = logRoot.appendingPathComponent("app/job-" + UUID().uuidString, isDirectory: true)
            try FileManager.default.createDirectory(at:job,withIntermediateDirectories:true,attributes:[.posixPermissions:0o700])
            let request = job.appendingPathComponent("request.json"); let batch = BatchProgress(items:items,now:submitted)
            let payload = DirectQueue.submissionPayload(items,batchID:batch.id,submitted:submitted,
                executionMode:defaults.string(forKey:"ExecutionMode"))
            try JSONSerialization.data(withJSONObject:payload).write(to:request,options:.atomic)
            let log = job.appendingPathComponent("backend.log"); FileManager.default.createFile(atPath:log.path,contents:nil,attributes:[.posixPermissions:0o600])
            let errorHandle = try FileHandle(forWritingTo:log)
            let child = Process(), output = Pipe(), input = Pipe()
            child.executableURL = base.appendingPathComponent(".venv/bin/python"); child.currentDirectoryURL = base
            child.arguments = ["-m","audio_transcribe","app-transcribe","--request",request.path]; child.environment = DirectSettings.environment()
            child.standardInput = input; child.standardOutput = output; child.standardError = errorHandle
            stdoutBuffer.removeAll(); receivedTerminalEvent = false; cancelRequested = false; batchProgress = batch
            cancelRequestedAt = nil; lastProtocolEventAt = nil; cancellationFallbackSent = false
            for (index,item) in items.enumerated() { if let position = queue.items.firstIndex(where:{$0.id == item.id}) {
                queue.items[position].state = "waiting"; queue.items[position].message = nil; queue.items[position].submittedIndex = index
                queue.items[position].batchID = batch.id.uuidString; queue.items[position].attemptID = nil; queue.items[position].autoStart = false; queue.items[position].restartRequired = false
            } }
            try child.run(); process = child; controlInput = input.fileHandleForWriting; try? input.fileHandleForReading.close()
            persist(); refresh()
            DispatchQueue.global(qos:.utility).async { [weak self] in
                while true { let data = output.fileHandleForReading.availableData; if data.isEmpty { break }; DispatchQueue.main.async { self?.consume(data) } }
                child.waitUntilExit(); try? errorHandle.close()
                DispatchQueue.main.async {
                    guard let self else { return }; self.closeControlInput(); self.process = nil; self.progressTimer?.invalidate(); self.progressTimer = nil
                    if !self.receivedTerminalEvent { self.batchProgress?.finishUnsettled(state:self.cancelRequested ? "cancelled" : "failed",now:ProcessInfo.processInfo.systemUptime); self.applyJobStates(); self.status.stringValue = "处理已停止，已完成的结果保留。可继续未完成任务。" }
                    self.persist(); self.refresh(); self.refreshResults()
                    if self.pendingDeletion?["state"] as? String == "busy" { self.commitDeletion() }
                    if self.quitting { NSApp.reply(toApplicationShouldTerminate:true) } else if self.pendingDeletion == nil { self.startNext() }
                }
            }
            progressTimer = Timer.scheduledTimer(withTimeInterval:1,repeats:true) { [weak self] _ in self?.refreshProgress() }
            status.stringValue = "正在处理 \(items.count) 个录音；每份结果完成后即可查看。"
        } catch {
            closeControlInput(); process = nil; batchProgress = nil
            for item in items { if let index = queue.items.firstIndex(where:{$0.id == item.id}) { queue.items[index].state = "failed"; queue.items[index].autoStart = false; queue.items[index].message = "未能启动本地转录，请重试。" } }
            status.stringValue = "未能启动本地转录，请重试。"; persist(); refresh()
        }
    }
    func consume(_ data:Data) {
        stdoutBuffer.append(data)
        while let newline = stdoutBuffer.firstIndex(of:10) {
            let line = stdoutBuffer.subdata(in:0..<newline); stdoutBuffer.removeSubrange(0...newline)
            guard let value = try? JSONSerialization.jsonObject(with:line) as? [String:Any], let type = value["type"] as? String, let ids = batchProgress?.submittedIDs else { continue }
            let now = ProcessInfo.processInfo.systemUptime
            guard batchProgress?.accept(value,currentIDs:ids,now:now) == true else { continue }
            lastProtocolEventAt = now; applyJobStates()
            if ["file","metadata"].contains(type), let raw = value["job_id"] as? String, let id = UUID(uuidString:raw), let index = queue.items.firstIndex(where:{$0.id == id && !$0.deletionPending}) {
                if let duration = value["duration_seconds"] as? Double, duration.isFinite, duration >= 0 { queue.items[index].duration = duration }
                if let model = value["model"] as? String { queue.items[index].model = model }
                if let resultValue = value["result"] as? [String:Any] { applyResult(DirectResult(value:resultValue),index:index) }
                else if let resultID = value["result_id"] as? String { queue.items[index].resultID = resultID; if selectedRowIdentity == .job(id) { open(resultID,jobID:id) } }
            }
            if type == "result" {
                receivedTerminalEvent = true
                for result in value["results"] as? [[String:Any]] ?? [] {
                    // Completion events are authoritative. The summary may repeat them.
                    if let raw = result["job_id"] as? String, let id = UUID(uuidString:raw), let index = queue.items.firstIndex(where:{$0.id == id}) { applyResult(DirectResult(value:result),index:index) }
                }
                batchProgress?.finishUnsettled(state:cancelRequested ? "cancelled" : "failed",now:now); applyJobStates()
                status.stringValue = "本次处理结束：完成 \(value["completed"] as? Int ?? 0)，需复核 \(value["review_required"] as? Int ?? 0)，失败 \(value["failed"] as? Int ?? 0)，取消 \(value["cancelled"] as? Int ?? 0)。"
            } else if type == "cancelled" || type == "error" {
                receivedTerminalEvent = true; batchProgress?.finishUnsettled(state:type == "cancelled" ? "cancelled" : "failed",now:now); applyJobStates()
                status.stringValue = value["message"] as? String ?? "任务已停止，可重试。"
            }
            if let selectedJobID, let item = queue.items.first(where:{$0.id == selectedJobID}), !item.deletionPending, item.resultID == nil {
                reader.metadata.stringValue = (item.duration.map { JobProgress.duration($0) } ?? "时长待检查") + " · " + item.model + " · " + DirectResult.stateLabel(item.state)
                if ["failed", "cancelled"].contains(item.state) { reader.showPending(item) }
            }
            persist(); refresh()
            if type == "admission" { refreshProgress() }
        }
    }
    func applyResult(_ result:DirectResult,index:Int) {
        guard !result.id.isEmpty, !queue.items[index].deletionPending, !deletedResultIDs.contains(result.id), !deletingResultIDs.contains(result.id) else { return }
        let previously = queue.items[index].resultID
        queue.items[index].resultID = result.id; queue.items[index].duration = result.duration
        queue.items[index].model = result.value["model"] as? String ?? queue.items[index].model
        if previously != result.id && selectedRowIdentity == .job(queue.items[index].id) { open(result.id,jobID:queue.items[index].id) }
        if let existing = results.firstIndex(where: { $0.id == result.id }) { results[existing] = result }
        else { results.insert(result, at: 0) }
        rebuildRows()
    }
    func applyJobStates() {
        guard let batch = batchProgress else { return }
        for index in queue.items.indices { guard queue.items[index].batchID == batch.id.uuidString, let job = batch.jobs[queue.items[index].id], job.index == queue.items[index].submittedIndex else { continue }
            queue.items[index].state = job.state; queue.items[index].message = job.message; queue.items[index].attemptID = job.attemptID; queue.items[index].restartRequired = job.restartRequired
        }
    }
    func refreshProgress() {
        guard running, !receivedTerminalEvent, let batch = batchProgress else { return }
        let now = ProcessInfo.processInfo.systemUptime
        let done = batch.jobs.values.filter{$0.isTerminal}.count
        status.stringValue = cancelRequested ? "正在取消；已完成的结果保留。" : "已结束 \(done)/\(batch.jobs.count) · 用时 \(JobProgress.duration(now - batch.startedAt))"
        if !cancelRequested, let admission = batch.admissionDetail { status.stringValue += "\n" + admission }
        rebuildRows()
        if cancelRequested, let requested = cancelRequestedAt, now - max(requested,lastProtocolEventAt ?? requested) >= 30, !cancellationFallbackSent, let process, process.isRunning {
            cancellationFallbackSent = true; closeControlInput(); process.terminate()
            status.stringValue = "正在停止无响应的当前任务并等待清理。"
        }
    }
    func sendControl(_ value:[String:Any]) {
        guard let controlInput else { return }
        do { var data = try JSONSerialization.data(withJSONObject:value); data.append(10); try controlInput.write(contentsOf:data) } catch { closeControlInput() }
    }
    func closeControlInput() { try? controlInput?.close(); controlInput = nil }
    func cancelJob(_ job:JobProgress) {
        guard let batch = batchProgress, !job.isTerminal, !job.cancellationRequested else { return }
        var value:[String:Any] = ["action":"cancel_job","batch_id":batch.id.uuidString,"job_id":job.id.uuidString]
        if let attempt = job.attemptID { value["attempt_id"] = attempt }
        batchProgress?.requestCancellation(jobID:job.id); sendControl(value); refresh()
    }
    @objc func cancelWork() {
        for index in queue.items.indices where queue.items[index].autoStart || (queue.items[index].state == "checking" && batchProgress?.jobs[queue.items[index].id] == nil) { queue.items[index].autoStart = false; queue.items[index].state = "cancelled" }
        if running, !cancelRequested, let batch = batchProgress { cancelRequested = true; cancelRequestedAt = ProcessInfo.processInfo.systemUptime; batchProgress?.requestCancellation(); sendControl(["action":"cancel_batch","batch_id":batch.id.uuidString]) }
        persist(); refresh()
    }
    func prepareForQuit() {
        watcher.stop()
        for observer in watchObservers {
            NSWorkspace.shared.notificationCenter.removeObserver(observer)
            NotificationCenter.default.removeObserver(observer)
        }
        watchObservers.removeAll()
        for observer in splitObservers { NotificationCenter.default.removeObserver(observer) }
        splitObservers.removeAll()
        reader.stopAudio(); reader.selectedSourceMonitor.stop()
        reader.client.cancelReadOnly(); client.cancelReadOnly()
    }
    func windowShouldClose(_ sender:NSWindow) -> Bool { if deletionBusy { cleanupStatus.stringValue = "正在确认清理状态，请稍后关闭窗口。"; return false }; if running { status.stringValue = "请先取消当前任务，再关闭窗口。"; return false }; return true }
}

final class AppDelegate:NSObject,NSApplicationDelegate {
    lazy var controller = MainController()
    func applicationDidFinishLaunching(_ notification:Notification) {
        let menu = NSMenu(), appItem = NSMenuItem(), appMenu = NSMenu()
        appMenu.addItem(withTitle:"关于 AudioTranscribe",action:#selector(NSApplication.orderFrontStandardAboutPanel(_:)),keyEquivalent:"")
        let settings = NSMenuItem(title:"设置…",action:#selector(MainController.showSettings),keyEquivalent:","); settings.target = controller; appMenu.addItem(settings)
        appMenu.addItem(.separator()); appMenu.addItem(withTitle:"退出 AudioTranscribe",action:#selector(NSApplication.terminate(_:)),keyEquivalent:"q")
        appItem.submenu = appMenu; menu.addItem(appItem)
        let fileItem = NSMenuItem(), fileMenu = NSMenu(title:"文件")
        let choose = NSMenuItem(title:"导入录音…",action:#selector(MainController.chooseFiles),keyEquivalent:"o"); choose.target = controller; fileMenu.addItem(choose); fileItem.submenu = fileMenu; menu.addItem(fileItem)
        let editItem = NSMenuItem(), editMenu = NSMenu(title:"编辑")
        for (name,action,key) in [("剪切","cut:","x"),("复制","copy:","c"),("粘贴","paste:","v"),("全选","selectAll:","a")] { editMenu.addItem(withTitle:name,action:Selector(action),keyEquivalent:key) }
        let find = NSMenuItem(title:"查找正文",action:#selector(NSTextView.performFindPanelAction(_:)),keyEquivalent:"f"); find.tag = Int(NSFindPanelAction.showFindPanel.rawValue); find.target = controller.reader.transcript; editMenu.addItem(find)
        editItem.submenu = editMenu; menu.addItem(editItem); NSApp.mainMenu = menu
        controller.showWindow(nil); NSApp.activate(ignoringOtherApps:true)
    }
    func application(_ sender:NSApplication,openFiles filenames:[String]) { sender.reply(toOpenOrPrint:controller.add(filenames.map{URL(fileURLWithPath:$0)}) ? .success : .failure) }
    func applicationShouldHandleReopen(_ sender:NSApplication,hasVisibleWindows:Bool) -> Bool { controller.showWindow(nil); return true }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender:NSApplication) -> Bool { true }
    func applicationShouldTerminate(_ sender:NSApplication) -> NSApplication.TerminateReply { if controller.deletionBusy { controller.cleanupStatus.stringValue = "正在确认清理状态，请稍后退出。"; return .terminateCancel }; controller.prepareForQuit(); if controller.running { controller.quitting = true; controller.cancelWork(); return .terminateLater }; return .terminateNow }
}
#if !LAYOUT_TESTING
let app = NSApplication.shared
signal(SIGPIPE,SIG_IGN)
app.setActivationPolicy(.regular)
let delegate = AppDelegate(); app.delegate = delegate
app.run()
#endif
