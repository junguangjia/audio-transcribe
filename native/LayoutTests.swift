import Cocoa

// These tests use the production AppKit view tree and isolated preferences.
// The executable has no backend configuration, so bridge calls cannot launch
// helpers. Folder restoration only watches a test-owned empty directory.
@main
struct LayoutTests {
    static var failures: [String] = []
    static var measurements: [[String: Any]] = []
    static let tolerance: CGFloat = 2

    static func check(_ condition: @autoclosure () -> Bool, _ message: String) {
        if !condition() { failures.append(message) }
    }

    static func descendants(_ view: NSView) -> [NSView] {
        [view] + view.subviews.flatMap(descendants)
    }

    static func stack(containing item: NSView, in root: NSView) -> NSStackView? {
        descendants(root).compactMap { $0 as? NSStackView }
            .first { $0.views.contains { $0 === item } }
    }

    static func settle(_ controller: MainController) {
        for _ in 0..<5 {
            RunLoop.main.run(until: Date(timeIntervalSinceNow: 0.015))
            controller.window?.contentView?.layoutSubtreeIfNeeded()
        }
    }

    static func frame(_ view: NSView, in root: NSView) -> NSRect {
        view.convert(view.bounds, to: root)
    }

    static func enclosed(_ child: NSView, in parent: NSView, _ description: String) {
        guard !child.isHiddenOrHasHiddenAncestor else { return }
        let actual = frame(child, in: parent)
        check(actual.minX >= -tolerance && actual.maxX <= parent.bounds.width + tolerance,
              "\(description): horizontal overflow \(NSStringFromRect(actual)) in \(NSStringFromRect(parent.bounds))")
        check(actual.minY >= -tolerance && actual.maxY <= parent.bounds.height + tolerance,
              "\(description): vertical overflow \(NSStringFromRect(actual)) in \(NSStringFromRect(parent.bounds))")
    }

    @discardableResult
    static func measure(_ controller: MainController, name: String, size: NSSize) -> [String: Any] {
        guard let window = controller.window, let root = window.contentView,
              let listScroll = controller.table.enclosingScrollView,
              let transcriptScroll = controller.reader.transcript.enclosingScrollView,
              let footer = stack(containing: controller.status, in: root),
              let cleanup = stack(containing: controller.cleanupStatus, in: root) else {
            fatalError("Production layout is missing an expected region")
        }
        let selectedBefore = controller.table.selectedRowIndexes
        let readerTitleBefore = controller.reader.titleField.stringValue
        window.setContentSize(size)
        settle(controller)
        check(controller.table.selectedRowIndexes == selectedBefore, "\(name): resizing changed selected recordings")
        check(controller.reader.titleField.stringValue == readerTitleBefore, "\(name): resizing changed reader selection")
        let splitFrame = frame(controller.outerSplit, in: root)
        let footerFrame = frame(footer, in: root)
        let cleanupFrame = frame(cleanup, in: root)
        let activeCleanup = !controller.cleanupStatus.isHidden || !controller.cleanupRetry.isHidden || !controller.cleanupCancel.isHidden
        let reservedCleanup = activeCleanup ? cleanupFrame.height : 0
        let gap = splitFrame.minY - footerFrame.maxY - reservedCleanup
        let readerStack = stack(containing: controller.reader.titleField, in: root)!
        let primaryActions = stack(containing: controller.reader.copy, in: root)!
        let secondaryActions = stack(containing: controller.reader.retry, in: root)!
        let audioControls = stack(containing: controller.reader.seek, in: root)!
        let folderControls = stack(containing: controller.startAll, in: root)!
        let card = descendants(controller.contentSplit.subviews[1]).compactMap { $0 as? NSBox }
            .first { $0.boxType == .custom }!
        let metric: [String: Any] = [
            "case": name, "width": root.bounds.width, "height": root.bounds.height,
            "requested_width": size.width, "requested_height": size.height,
            "split_height": splitFrame.height, "list_scroll_height": listScroll.bounds.height,
            "transcript_scroll_height": transcriptScroll.bounds.height,
            "footer_height": footerFrame.height, "cleanup_height": cleanupFrame.height,
            "cleanup_visible": activeCleanup, "unused_vertical_gap": gap,
            "reader_height": controller.reader.view.bounds.height,
            "list_width": controller.contentSplit.subviews[0].frame.width,
            "detail_width": controller.contentSplit.subviews[1].frame.width,
            "watch_card_height": card.bounds.height,
            "folder_controls_height": folderControls.bounds.height,
            "primary_actions_height": primaryActions.bounds.height,
            "secondary_actions_height": secondaryActions.bounds.height,
            "audio_controls_height": audioControls.bounds.height,
            "reader_stack_height": readerStack.bounds.height,
            "reader_title_height": controller.reader.titleField.bounds.height,
            "reader_warning_height": controller.reader.warning.isHidden ? 0 : controller.reader.warning.bounds.height,
            "reader_labels_height": controller.reader.labelsStack.isHidden ? 0 : controller.reader.labelsStack.bounds.height,
            "cleanup_status_height": controller.cleanupStatus.isHidden ? 0 : controller.cleanupStatus.bounds.height,
            "cleanup_retry_height": controller.cleanupRetry.isHidden ? 0 : controller.cleanupRetry.bounds.height,
            "cleanup_cancel_height": controller.cleanupCancel.isHidden ? 0 : controller.cleanupCancel.bounds.height,
            "footer_status_height": controller.status.bounds.height,
            "footer_delete_height": controller.deleteButton.bounds.height
        ]
        measurements.append(metric)
        check(abs(root.bounds.width - size.width) <= tolerance,
              "\(name): content forced requested width \(size.width) to \(root.bounds.width)")
        // Simultaneously displaying labels, a multi-line warning, a notice,
        // and cleanup controls has a real minimum fitting height. AppKit may
        // grow that smallest window; clipping these controls would be worse.
        if name == "all-accessories-minimum" {
            check(root.bounds.height >= size.height - tolerance && root.bounds.height <= 850,
                  "\(name): expanded fitting height \(root.bounds.height) is outside the tested normal window")
        } else {
            check(abs(root.bounds.height - size.height) <= tolerance,
                  "\(name): content forced requested height \(size.height) to \(root.bounds.height)")
        }
        check(abs(splitFrame.maxY - root.bounds.maxY) <= tolerance, "\(name): split does not reach top")
        check(gap >= -tolerance && gap <= 12, "\(name): \(gap)pt unused gap between content and footer")
        check(footerFrame.height <= 64, "\(name): footer expanded to \(footerFrame.height)pt")
        check(cleanupFrame.height <= (activeCleanup ? 100 : 12), "\(name): cleanup expanded to \(cleanupFrame.height)pt")
        for child in [controller.cleanupStatus, controller.cleanupRetry, controller.cleanupCancel] {
            enclosed(child, in: cleanup, "\(name) cleanup \(type(of: child))")
            if !child.isHiddenOrHasHiddenAncestor {
                check(child.bounds.height >= child.intrinsicContentSize.height - tolerance,
                      "\(name): cleanup \(type(of: child)) clipped vertically to \(child.bounds.height)pt")
            }
        }
        for child in [controller.status, controller.deleteButton, controller.resume, controller.cancel] {
            enclosed(child, in: footer, "\(name) footer \(type(of: child))")
            if !child.isHiddenOrHasHiddenAncestor {
                check(child.bounds.height >= child.intrinsicContentSize.height - tolerance,
                      "\(name): footer \(type(of: child)) clipped vertically to \(child.bounds.height)pt")
            }
        }
        for (index, pane) in controller.outerSplit.subviews.enumerated() {
            check(abs(pane.bounds.height - splitFrame.height) <= tolerance, "\(name): outer pane \(index) height differs")
            enclosed(pane, in: controller.outerSplit, "\(name) outer pane \(index)")
        }
        for (index, pane) in controller.contentSplit.subviews.enumerated() {
            check(abs(pane.bounds.height - splitFrame.height) <= tolerance, "\(name): content pane \(index) height differs")
            enclosed(pane, in: controller.contentSplit, "\(name) content pane \(index)")
        }
        enclosed(listScroll, in: controller.contentSplit.subviews[0], "\(name) list")
        enclosed(transcriptScroll, in: controller.reader.view, "\(name) transcript")
        enclosed(controller.reader.view, in: controller.contentSplit.subviews[1], "\(name) reader")
        for button in [controller.openFolder, controller.changeFolder, controller.refreshFolder, controller.startAll] {
            enclosed(button, in: controller.contentSplit.subviews[1], "\(name) folder control \(button.title)")
            check(button.bounds.width >= button.intrinsicContentSize.width - tolerance,
                  "\(name): folder button title compressed: \(button.title)")
        }
        for child in [controller.reader.titleField, controller.reader.metadata, controller.reader.warning,
                      controller.reader.copy, controller.reader.export, controller.reader.finder,
                      controller.reader.retry, controller.reader.locate, controller.reader.delete,
                      controller.reader.play, controller.reader.seek, controller.reader.clock,
                      controller.reader.labelsButton, controller.reader.labelsStack] {
            enclosed(child, in: controller.reader.view, "\(name) reader \(type(of: child))")
        }
        for (title, toolbar) in [("primary actions", primaryActions), ("secondary actions", secondaryActions),
                                  ("audio controls", audioControls), ("folder controls", folderControls)] {
            check(toolbar.bounds.height <= 40, "\(name): \(title) expanded to \(toolbar.bounds.height)pt")
        }
        let visibleReaderViews = readerStack.views.filter { !$0.isHiddenOrHasHiddenAncestor }
            .map { ($0, frame($0, in: readerStack)) }.sorted { $0.1.minY < $1.1.minY }
        for pair in zip(visibleReaderViews, visibleReaderViews.dropFirst()) {
            let internalGap = pair.1.1.minY - pair.0.1.maxY
            check(internalGap >= -tolerance && internalGap <= 16,
                  "\(name): reader has \(internalGap)pt gap between \(type(of: pair.0.0)) and \(type(of: pair.1.0))")
        }
        check(controller.reader.view.bounds.height > 0 && transcriptScroll.bounds.height > 0,
              "\(name): reader is collapsed")
        check(controller.process == nil && controller.listRequest == nil && controller.searchRequest == nil && controller.discoveryRequests.isEmpty,
              "\(name): harness unexpectedly started application services")
        if let index = CommandLine.arguments.firstIndex(of: "--snapshots"),
           CommandLine.arguments.indices.contains(index + 1),
           ["completed-minimum", "completed-normal", "completed-tall", "all-accessories-normal"].contains(name),
           let bitmap = root.bitmapImageRepForCachingDisplay(in: root.bounds) {
            root.cacheDisplay(in: root.bounds, to: bitmap)
            let directory = URL(fileURLWithPath: CommandLine.arguments[index + 1], isDirectory: true)
            try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            // Offscreen NSView caching leaves unpainted areas transparent.
            // Composite onto white so black text remains visible in renderers.
            let canvas = NSImage(size: root.bounds.size)
            canvas.lockFocus()
            NSColor.white.setFill(); NSBezierPath(rect: NSRect(origin: .zero, size: root.bounds.size)).fill()
            bitmap.draw(in: NSRect(origin: .zero, size: root.bounds.size))
            canvas.unlockFocus()
            if let tiff = canvas.tiffRepresentation,
               let flattened = NSBitmapImageRep(data: tiff),
               let data = flattened.representation(using: .png, properties: [:]) {
                try? data.write(to: directory.appendingPathComponent("synthetic-harness-\(name).png"), options: .atomic)
            }
        }
        return metric
    }

    static func syntheticResult(review: Bool = false) -> DirectResult {
        let segments: [[String: Any]] = (0..<80).map { index in
            ["start_seconds": Double(index * 10), "end_seconds": Double(index * 10 + 9),
             "text": "Synthetic layout fixture \(index + 1). This is generated test text for checking transcript resizing and scrolling."]
        }
        return DirectResult(value: ["result_id": "layout-fixture", "filename": "Synthetic_course_recording_with_a_deliberately_long_filename_for_layout_validation_20260929.wav",
            "model": "large-v3-turbo", "state": review ? "review_required" : "completed",
            "duration_seconds": 900.0, "segments": segments,
            "plain_text": "Synthetic test transcript", "quality_message": "Synthetic review warning. Verify a long explanation wraps inside the reader without widening the window or hiding controls. 合成测试提示，用于验证多行文字与窗口尺寸变化。",
            "labels_editable": true])
    }

    static func controllerRegressions() throws {
        precondition(Bundle.main.object(forInfoDictionaryKey: "AudioTranscribeCodeRoot") == nil)
        let suite = "AudioTranscribe.ControllerTests.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }
        let controller = MainController(defaults: defaults, startServices: false)
        // Exercise submission UI without starting a transcription batch.
        controller.quitting = true
        defer { controller.prepareForQuit(); controller.window?.orderOut(nil) }
        controller.sortMode = .filename
        var original = Recording(url: URL(fileURLWithPath: "/synthetic-layout/a-original.wav"))
        original.state = "failed"
        controller.queue.items = [original]
        controller.selectedFilter = .failed
        controller.search.stringValue = "a-original"
        controller.refresh()
        check(controller.activateVisibleRow(.job(original.id)), "Import fixture could not select original row")
        check(controller.add([URL(fileURLWithPath: "/synthetic-layout/z-imported.wav")]), "Synthetic import was rejected")
        controller.window?.orderOut(nil)
        func selectedJobIsAligned(_ item: Recording, context: String) {
            let row = controller.table.selectedRow
            check(controller.visibleRows.indices.contains(row) && controller.visibleRows[row] == .job(item.id), "\(context): highlighted row differs from submitted job")
            check(controller.selectedRowIdentity == .job(item.id) && controller.selectedJobID == item.id, "\(context): selection identities differ")
            check(controller.reader.titleField.stringValue == item.url.lastPathComponent, "\(context): reader differs from submitted job")
            let jobs = controller.deletionSelection()["jobs"] as? [[String: Any]] ?? []
            check(jobs.count == 1 && jobs.first?["job_id"] as? String == item.id.uuidString, "\(context): list deletion targets another job")
            check(controller.selectedFilter == .all && controller.search.stringValue.isEmpty, "\(context): submitted job remains hidden by filter or search")
        }
        selectedJobIsAligned(controller.queue.items.last!, context: "manual import")
        controller.selectedFilter = .failed; controller.search.stringValue = "a-original"; controller.refresh()
        controller.enqueueAgain(URL(fileURLWithPath: "/synthetic-layout/z-retry.wav"), model: "large-v3-turbo", inputMode: "managed")
        selectedJobIsAligned(controller.queue.items.last!, context: "retranscription")

        for duplicateAlreadySelected in [false, true] {
            controller.reader.clearSelection()
            var other = Recording(url: URL(fileURLWithPath: "/synthetic-layout/a-other-import.wav"))
            var duplicate = Recording(url: URL(fileURLWithPath: "/synthetic-layout/z-duplicate.wav"))
            other.state = "checking"; duplicate.state = "checking"
            controller.queue.items = [other, duplicate]; controller.results = []; controller.refresh()
            check(controller.activateVisibleRow(.job(duplicateAlreadySelected ? duplicate.id : other.id)), "Duplicate fixture could not select row")
            controller.attachExistingResult(DirectResult(value: ["result_id": "existing-result", "filename": "z-duplicate.wav", "state": "completed"]), at: 1)
            controller.refresh()
            check(controller.selectedRowIdentity == .job(duplicate.id) && controller.visibleRows[controller.table.selectedRow] == .job(duplicate.id), "Duplicate result did not select its import row")
            check(controller.selectedJobID == duplicate.id && controller.reader.loadingID == "existing-result", "Duplicate result left a pending or unrelated reader")
            check(controller.deletionSelection()["result_ids"] as? [String] == ["existing-result"], "Duplicate result has an unrelated deletion target")
        }

        let watchedPath = "/synthetic-layout/watched.wav", version = String(repeating: "a", count: 64)
        let watchedID = RecordingRowID.watched(watchedPath, version)
        // Cover full result payloads and metadata events carrying only an ID.
        // Selected jobs still open on completion; a watched selection stays put.
        for metadataOnly in [false, true] {
            for selectCompletingJob in [false, true] {
                controller.reader.clearSelection(); controller.batchProgress = nil
                let background = Recording(url: URL(fileURLWithPath: "/synthetic-layout/background.wav"))
                controller.queue.items = [background]; controller.results = []
                controller.watchedItems = [watchedPath: WatchedDiscovery(["path": watchedPath, "filename": "watched.wav", "state": "ready", "version_key": version])!]
                controller.refresh()
                let selected = selectCompletingJob ? RecordingRowID.job(background.id) : watchedID
                check(controller.activateVisibleRow(selected), "Completion fixture could not select row")
                let resultID = "synthetic-completion"
                if metadataOnly {
                    let batch = BatchProgress(items: [background], now: 0), attempt = UUID().uuidString
                    controller.batchProgress = batch
                    var event: [String: Any] = ["type": "file", "batch_id": batch.id.uuidString,
                        "job_id": background.id.uuidString, "attempt_id": attempt, "index": 0,
                        "seq": 1, "state": "waiting", "stage": "queued", "elapsed": 0.0]
                    var data = try JSONSerialization.data(withJSONObject: event); data.append(10); controller.consume(data)
                    event["type"] = "metadata"; event["seq"] = 2; event["result_id"] = resultID
                    data = try JSONSerialization.data(withJSONObject: event); data.append(10); controller.consume(data)
                } else {
                    controller.applyResult(DirectResult(value: ["result_id": resultID, "filename": "background.wav", "state": "completed"]), index: 0)
                }
                check(controller.queue.items[0].resultID == resultID, "Completion fixture did not apply result")
                check(controller.selectedRowIdentity == selected && controller.visibleRows[controller.table.selectedRow] == selected, "Completion changed highlighted identity")
                if selectCompletingJob {
                    check(controller.reader.loadingID == resultID && controller.selectedJobID == background.id, "Selected completion did not open its result")
                } else {
                    check(controller.reader.titleField.stringValue == "watched.wav" && controller.reader.readRequest == nil && controller.selectedJobID == nil, "Background completion replaced the watched reader")
                }
            }
        }

        controller.reader.clearSelection(); controller.batchProgress = nil
        let filename = String(repeating: "A long synthetic recording name ", count: 6) + ".wav"
        let long = Recording(url: URL(fileURLWithPath: "/synthetic-layout/" + filename))
        controller.queue.items = [long]; controller.results = []; controller.watchedItems = [:]; controller.refresh()
        controller.window?.setContentSize(NSSize(width: 1800, height: 1000)); settle(controller)
        controller.contentSplit.setPosition(900, ofDividerAt: 0); settle(controller)
        controller.table.reloadData(); controller.activateVisibleRow(.job(long.id)); settle(controller)
        let wideHeight = controller.table.rect(ofRow: 0).height
        controller.contentSplit.setPosition(345, ofDividerAt: 0); settle(controller)
        let narrowHeight = controller.table.rect(ofRow: 0).height
        check(narrowHeight > wideHeight && abs(narrowHeight - controller.tableView(controller.table, heightOfRow: 0)) <= tolerance, "Narrow divider left cached long-filename row height")
        controller.contentSplit.setPosition(900, ofDividerAt: 0); settle(controller)
        check(abs(controller.table.rect(ofRow: 0).height - wideHeight) <= tolerance, "Wide divider left expanded long-filename row height")
        selectedJobIsAligned(long, context: "divider resize")
        check(controller.process == nil, "Controller regressions started an ASR process")
    }

    static func watchedFolderDefaultsRegression() throws {
        let suite = "AudioTranscribe.WatchDefaultsTests.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }
        let controller = MainController(defaults: defaults, startServices: false)
        defer { controller.prepareForQuit(); controller.window?.orderOut(nil) }
        controller.restoreWatchedFolder()
        check(controller.watcher.folder == nil && controller.watchStatus == "未启用", "Clean install configured a recording folder without user selection")
        check(defaults.string(forKey: "WatchedFolderPath") == nil, "Clean install persisted an unsolicited watched folder")
        controller.prepareForQuit()
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent("audio-transcribe-watch-defaults-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { controller.watcher.stop(); try? FileManager.default.removeItem(at: directory) }
        defaults.set(directory.path, forKey: "WatchedFolderPath")
        controller.restoreWatchedFolder()
        check(controller.watcher.folder?.standardizedFileURL == directory.standardizedFileURL, "Saved watched folder was not restored")
        check(defaults.string(forKey: "WatchedFolderPath") == directory.standardizedFileURL.path, "Saved watched folder path was replaced")
    }

    static func main() throws {
        let app = NSApplication.shared
        app.setActivationPolicy(.prohibited)
        let suite = "AudioTranscribe.LayoutTests.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }
        let controller = MainController(defaults: defaults, startServices: false)
        controller.outerSplit.autosaveName = nil
        controller.contentSplit.autosaveName = nil
        settle(controller)
        let sizes: [(String, NSSize)] = [("minimum", NSSize(width: 1150, height: 628)),
            ("normal", NSSize(width: 1450, height: 850)), ("tall", NSSize(width: 1450, height: 1100)),
            ("wide", NSSize(width: 1800, height: 1000))]
        for state in ["empty", "pending", "completed"] {
            if state == "empty" { controller.reader.clearSelection() }
            if state == "pending" {
                var a = Recording(url: URL(fileURLWithPath: "/synthetic-layout/first.wav")); a.duration = 900
                let b = Recording(url: URL(fileURLWithPath: "/synthetic-layout/second.wav"))
                controller.queue.items = [a, b]; controller.refresh()
                controller.table.selectRowIndexes(IndexSet(integer: 1), byExtendingSelection: false)
                controller.tableViewSelectionDidChange(Notification(name: NSTableView.selectionDidChangeNotification, object: controller.table))
                check(controller.reader.titleField.stringValue == controller.filename(for: controller.visibleRows[1]), "Selection does not update reader")
            }
            if state == "completed" { controller.reader.display(syntheticResult()) }
            var normal: [String: Any]?
            for (label, size) in sizes {
                let metric = measure(controller, name: "\(state)-\(label)", size: size)
                if label == "normal" { normal = metric }
                if label == "tall", let normal {
                    let splitGrowth = (metric["split_height"] as! CGFloat) - (normal["split_height"] as! CGFloat)
                    let listGrowth = (metric["list_scroll_height"] as! CGFloat) - (normal["list_scroll_height"] as! CGFloat)
                    let readerGrowth = (metric["transcript_scroll_height"] as! CGFloat) - (normal["transcript_scroll_height"] as! CGFloat)
                    check(abs(splitGrowth - 250) <= tolerance, "\(state): split absorbs only \(splitGrowth) of 250pt height growth")
                    check(abs(listGrowth - 250) <= tolerance, "\(state): list absorbs only \(listGrowth) of 250pt height growth")
                    check(abs(readerGrowth - 250) <= tolerance, "\(state): transcript absorbs only \(readerGrowth) of 250pt height growth")
                }
            }
        }
        controller.reader.display(syntheticResult(review: true))
        controller.cleanupStatus.stringValue = "Synthetic cleanup status. The original recording remains untouched; this fixture only checks the layout of wrapping status text and action buttons. 合成布局测试。"
        controller.cleanupStatus.isHidden = false; controller.cleanupRetry.isHidden = false; controller.cleanupCancel.isHidden = false
        controller.reader.labelsStack.isHidden = false
        controller.reader.notice.stringValue = "Synthetic notice for layout verification."
        controller.reader.notice.isHidden = false
        for (label, size) in sizes { measure(controller, name: "all-accessories-\(label)", size: size) }
        controller.cleanupStatus.isHidden = true; controller.cleanupRetry.isHidden = true; controller.cleanupCancel.isHidden = true
        controller.reader.labelsStack.isHidden = true; controller.reader.warning.isHidden = true; controller.reader.notice.isHidden = true
        measure(controller, name: "accessories-hidden-again", size: NSSize(width: 1450, height: 1100))
        measure(controller, name: "accessories-hidden-again-minimum", size: NSSize(width: 1150, height: 628))
        try controllerRegressions()
        try watchedFolderDefaultsRegression()
        let report: [String: Any] = ["fixture": "synthetic-production-AppKit-view-tree", "production_services_started": false,
            "passed": failures.isEmpty, "failures": failures, "measurements": measurements]
        let data = try JSONSerialization.data(withJSONObject: report, options: [.prettyPrinted, .sortedKeys])
        if let index = CommandLine.arguments.firstIndex(of: "--output"), CommandLine.arguments.indices.contains(index + 1) {
            try data.write(to: URL(fileURLWithPath: CommandLine.arguments[index + 1]), options: .atomic)
        }
        print("AppKit layout: \(measurements.count) cases; \(failures.count) failures")
        for failure in failures { print("FAIL: " + failure) }
        controller.window?.orderOut(nil)
        if !failures.isEmpty && !CommandLine.arguments.contains("--record-baseline") { exit(1) }
    }
}
