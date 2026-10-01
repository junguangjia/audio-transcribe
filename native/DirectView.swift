import Cocoa
import AVFoundation
import UniformTypeIdentifiers

final class DirectResultView: NSViewController, NSTextViewDelegate {
    let client = DirectClient()
    let titleField = NSTextField(labelWithString: "转录结果")
    let metadata = NSTextField(labelWithString: "导入录音后，完整正文会在这里显示。")
    let warning = NSTextField(wrappingLabelWithString: "")
    let notice = NSTextField(wrappingLabelWithString: "")
    let transcript = NSTextView()
    let copy = NSButton(title: "复制全文", target: nil, action: nil)
    let export = NSButton(title: "导出…", target: nil, action: nil)
    let finder = NSButton(title: "在访达中显示", target: nil, action: nil)
    let delete = NSButton(title: "删除…", target: nil, action: nil)
    let retry = NSButton(title: "重新转录…", target: nil, action: nil)
    let locate = NSButton(title: "定位原录音…", target: nil, action: nil)
    let play = NSButton(image: NSImage(systemSymbolName: "play.fill", accessibilityDescription: "播放 / 暂停")!, target: nil, action: nil)
    let seek = NSSlider(value: 0, minValue: 0, maxValue: 1, target: nil, action: nil)
    let clock = NSTextField(labelWithString: "00:00 / 00:00")
    let labelsButton = NSButton(title: "添加标签（可选）", target: nil, action: nil)
    let labelsStack = NSStackView()
    let course = NSTextField(), speaker = NSTextField(), event = NSTextField()
    var result: DirectResult?
    var displayedExport: URL?
    var player: AVPlayer?
    var timeObserver: Any?
    var statusObserver: NSKeyValueObservation?
    var endedObserver: NSObjectProtocol?
    var generation = 0
    var onRetranscribe: ((URL, String, String) -> Void)?
    var onChanged: (() -> Void)?
    var onDelete: (() -> Void)?
    var readRequest: DirectRequest?
    var sourceVerifyRequest: DirectRequest?
    var sourceVerificationSerial = 0
    var externalSourcePath: String?
    let selectedSourceMonitor = SelectedSourceMonitor()
    var loadingID: String?
    var effectiveDuration: Double {
        let measured = player?.currentItem?.duration.seconds ?? .nan
        return measured.isFinite && measured > 0 ? measured : result?.duration ?? 0
    }
    override func loadView() {
        view = NSView()
        selectedSourceMonitor.onChange = { [weak self] path in self?.invalidateExternalSource(at: path) }
        let stack = NSStackView(); stack.orientation = .vertical; stack.alignment = .leading; stack.spacing = 10
        stack.translatesAutoresizingMaskIntoConstraints = false; view.addSubview(stack)
        NSLayoutConstraint.activate([stack.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: 20), stack.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -20), stack.topAnchor.constraint(equalTo: view.topAnchor, constant: 18), stack.bottomAnchor.constraint(equalTo: view.bottomAnchor, constant: -16)])
        titleField.font = .systemFont(ofSize: 20, weight: .semibold)
        titleField.lineBreakMode = .byWordWrapping; titleField.maximumNumberOfLines = 2
        titleField.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        metadata.font = .systemFont(ofSize: 12); metadata.textColor = .secondaryLabelColor
        warning.font = .systemFont(ofSize: 12); warning.textColor = .systemOrange; warning.isHidden = true
        notice.font = .systemFont(ofSize: 11); notice.textColor = .secondaryLabelColor; notice.isHidden = true
        for field in [titleField, metadata, warning] { stack.addArrangedSubview(field); field.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true }
        copy.target = self; copy.action = #selector(copyAll)
        export.target = self; export.action = #selector(exportResult)
        finder.target = self; finder.action = #selector(showFinder)
        retry.target = self; retry.action = #selector(retranscribe)
        locate.target = self; locate.action = #selector(locateOriginal)
        delete.target = self; delete.action = #selector(deleteResult)
        for button in [copy, export, finder, retry, locate, delete] { button.bezelStyle = .rounded; button.isEnabled = false }
        let primaryActions = NSStackView(views: [copy, export, finder, NSView()]); primaryActions.spacing = 6
        let secondaryActions = NSStackView(views: [retry, locate, NSView(), delete]); secondaryActions.spacing = 6
        stack.addArrangedSubview(primaryActions); primaryActions.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
        stack.addArrangedSubview(secondaryActions); secondaryActions.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
        play.target = self; play.action = #selector(togglePlay); play.bezelStyle = .rounded
        seek.target = self; seek.action = #selector(sliderChanged); seek.isContinuous = true
        clock.font = .monospacedDigitSystemFont(ofSize: 11, weight: .regular)
        let audio = NSStackView(views: [play, seek, clock]); audio.spacing = 10; audio.alignment = .centerY
        stack.addArrangedSubview(audio); audio.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
        play.isEnabled = false; seek.isEnabled = false
        let scroll = NSScrollView(); scroll.hasVerticalScroller = true; scroll.borderType = .bezelBorder
        transcript.isEditable = false; transcript.isSelectable = true; transcript.isRichText = true; transcript.delegate = self
        transcript.usesFindPanel = true; transcript.isVerticallyResizable = true; transcript.isHorizontallyResizable = false
        transcript.autoresizingMask = [.width]; transcript.textContainer?.widthTracksTextView = true
        transcript.textContainerInset = NSSize(width: 14, height: 12)
        transcript.font = .systemFont(ofSize: 14); transcript.string = "将录音拖入窗口，或点击“导入录音…”。\n\n每个文件生成独立结果，完成后即可阅读、复制或导出。"
        scroll.documentView = transcript
        stack.addArrangedSubview(scroll); scroll.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
        scroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 180).isActive = true
        scroll.setContentHuggingPriority(.defaultLow, for: .vertical)
        labelsButton.target = self; labelsButton.action = #selector(toggleLabels); labelsButton.bezelStyle = .rounded; labelsButton.isEnabled = false
        stack.addArrangedSubview(labelsButton)
        labelsStack.orientation = .vertical; labelsStack.alignment = .leading; labelsStack.spacing = 6; labelsStack.isHidden = true
        for (field, placeholder) in [(course, "课程 / Course"), (speaker, "说话人 / Speaker"), (event, "主题 / Event")] {
            field.placeholderString = placeholder; labelsStack.addArrangedSubview(field)
            field.widthAnchor.constraint(equalTo: labelsStack.widthAnchor).isActive = true
        }
        let save = NSButton(title: "保存标签", target: self, action: #selector(saveLabels)); save.bezelStyle = .rounded
        labelsStack.addArrangedSubview(save); stack.addArrangedSubview(labelsStack)
        labelsStack.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
        stack.addArrangedSubview(notice); notice.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
    }
    func showNotice(_ text: String) { notice.stringValue = text; notice.isHidden = text.isEmpty }
    func setPlainTranscript(_ text: String) {
        let attributes: [NSAttributedString.Key: Any] = [.font: NSFont.systemFont(ofSize: 14), .foregroundColor: NSColor.labelColor]
        transcript.typingAttributes = attributes
        transcript.textStorage?.setAttributedString(NSAttributedString(string: text, attributes: attributes))
    }
    @objc func deleteResult() { onDelete?() }
    func clearSelection() {
        _ = view; generation += 1; sourceVerificationSerial += 1; externalSourcePath = nil; selectedSourceMonitor.stop()
        readRequest?.cancel(); readRequest = nil; sourceVerifyRequest?.cancel(); sourceVerifyRequest = nil; loadingID = nil; stopAudio(); result = nil; displayedExport = nil
        titleField.stringValue = "选择录音查看转录"; titleField.toolTip = nil; metadata.stringValue = ""
        setPlainTranscript("当前列表没有可显示的录音。请选择其他分类，或导入新的录音。")
        for button in [copy, export, finder, retry, locate, delete, play, labelsButton] { button.isEnabled = false }
        seek.isEnabled = false; clock.stringValue = "00:00 / 00:00"; warning.isHidden = true; labelsStack.isHidden = true; showNotice("")
    }
    func clearForDeletion() {
        _ = view; generation += 1; sourceVerificationSerial += 1; externalSourcePath = nil; selectedSourceMonitor.stop()
        readRequest?.cancel(); readRequest = nil; sourceVerifyRequest?.cancel(); sourceVerifyRequest = nil; loadingID = nil; stopAudio(); result = nil; displayedExport = nil
        titleField.stringValue = "正在清理所选录音"; titleField.toolTip = nil; metadata.stringValue = ""; setPlainTranscript("清理结果将在列表下方显示。其他录音仍可阅读和导出。")
        for button in [copy, export, finder, retry, locate, delete, play, labelsButton] { button.isEnabled = false }; seek.isEnabled = false; warning.isHidden = true; labelsStack.isHidden = true; showNotice("")
    }
    func showPending(_ item: Recording) {
        readRequest?.cancel(); readRequest = nil; sourceVerifyRequest?.cancel(); sourceVerifyRequest = nil; sourceVerificationSerial += 1; externalSourcePath = nil; selectedSourceMonitor.stop(); loadingID = nil
        _ = view; generation += 1; stopAudio(); result = nil; displayedExport = nil
        delete.title = "删除…"
        titleField.stringValue = item.url.lastPathComponent; titleField.toolTip = item.url.lastPathComponent
        metadata.stringValue = (item.duration.map { JobProgress.duration($0) } ?? "时长待检查") + " · " + item.model + " · " + DirectResult.stateLabel(item.state)
        setPlainTranscript(item.message ?? (["failed", "cancelled"].contains(item.state) ? "此任务尚未生成结果。点击任务旁的继续按钮。" : "正在处理这份录音。完成后将自动显示完整转录。"))
        for button in [copy, export, finder, retry, locate, labelsButton, play] { button.isEnabled = false }
        delete.isEnabled = !item.deletionPending
        seek.isEnabled = false; clock.stringValue = "00:00 / 00:00"
        warning.isHidden = true; labelsStack.isHidden = true; showNotice("")
    }
    func load(_ id: String) {
        if loadingID == id { return }
        readRequest?.cancel(); sourceVerifyRequest?.cancel(); sourceVerifyRequest = nil; sourceVerificationSerial += 1; externalSourcePath = nil; selectedSourceMonitor.stop(); loadingID = id
        _ = view; generation += 1; let current = generation
        stopAudio(); result = nil; displayedExport = nil
        for button in [copy, export, finder, retry, locate, delete, labelsButton] { button.isEnabled = false }
        play.isEnabled = false; seek.isEnabled = false
        titleField.stringValue = "正在读取结果…"; titleField.toolTip = nil; metadata.stringValue = ""; setPlainTranscript("")
        warning.isHidden = true; labelsStack.isHidden = true; showNotice("")
        readRequest = client.request(["action": "read", "result_id": id, "defer_source_verification": true]) { [weak self] response in
            guard let self, self.generation == current else { return }
            self.readRequest = nil; self.loadingID = nil
            switch response {
            case .success(let value):
                self.display(DirectResult(value: value))
                if value["source_integrity"] as? String == "pending" { self.verifySource(id, generation: current) }
            case .failure(let error): self.titleField.stringValue = "暂时无法读取结果"; self.showNotice(error.localizedDescription)
            }
        }
    }
    func verifySource(_ id: String, generation current: Int, autoplay: Bool = false, resumeAt: Double = 0) {
        sourceVerifyRequest?.cancel()
        sourceVerificationSerial += 1
        let serial = sourceVerificationSerial
        sourceVerifyRequest = client.request(["action": "verify_source", "result_id": id]) { [weak self] response in
            guard let self, self.generation == current, self.sourceVerificationSerial == serial, self.result?.id == id else { return }
            self.sourceVerifyRequest = nil
            var updated = self.result!.value
            switch response {
            case .success(let verification):
                updated["audio_path"] = verification["audio_path"]
                updated["source_integrity"] = verification["source_integrity"] ?? "verification_failed"
                updated["verified_source_stat"] = verification["verified_source_stat"] ?? NSNull()
                if let message = verification["quality_message"] { updated["quality_message"] = message }
            case .failure:
                updated["audio_path"] = NSNull()
                updated["source_integrity"] = "verification_failed"
                updated["verified_source_stat"] = NSNull()
            }
            self.stopAudio()
            self.display(DirectResult(value: updated), preservingScroll: true)
            if autoplay, self.result?.value["source_integrity"] as? String == "verified", self.player != nil,
               self.selectedSourceMonitor.matchesVerifiedSource() {
                if resumeAt.isFinite && resumeAt > 0 { self.seekTo(resumeAt) }
                self.player?.play()
                self.updateAudio(self.player?.currentTime().seconds ?? 0)
            }
        }
    }
    func invalidateExternalSource(at path: String) {
        guard let current = result, current.value["source_ownership"] as? String == "external_referenced",
              externalSourcePath == URL(fileURLWithPath: path).standardizedFileURL.path else { return }
        sourceVerifyRequest?.cancel(); sourceVerificationSerial += 1
        selectedSourceMonitor.stop(); stopAudio()
        var updated = current.value
        updated["audio_path"] = NSNull(); updated["source_integrity"] = "pending"; updated["verified_source_stat"] = NSNull()
        display(DirectResult(value: updated), preservingScroll: true)
        verifySource(current.id, generation: generation)
    }
    func display(_ incoming: DirectResult, preservingScroll: Bool = false) {
        let scrollPoint = preservingScroll ? transcript.enclosingScrollView?.contentView.bounds.origin : nil
        let selection = preservingScroll ? transcript.selectedRange() : NSRange(location: 0, length: 0)
        let preserveLabelDraft = preservingScroll && result?.id == incoming.id
        var value = incoming
        if value.value["source_ownership"] as? String == "external_referenced", let path = value.audioPath {
            externalSourcePath = URL(fileURLWithPath: path).standardizedFileURL.path
            if value.value["source_integrity"] as? String == "verified" {
                let verifiedStamp = value.value["verified_source_stat"] as? [String: Any] ?? [:]
                if !selectedSourceMonitor.watch(path, verifiedStamp: verifiedStamp)
                    || !selectedSourceMonitor.matchesVerifiedSource() {
                    selectedSourceMonitor.stop()
                    var revoked = value.value
                    revoked["audio_path"] = NSNull()
                    revoked["verified_source_stat"] = NSNull()
                    revoked["source_integrity"] = "verification_failed"
                    value = DirectResult(value: revoked)
                }
            } else { selectedSourceMonitor.stop() }
        } else {
            selectedSourceMonitor.stop()
        }
        result = value; delete.title = "删除…"; delete.isEnabled = true; titleField.stringValue = value.filename
        metadata.stringValue = [value.duration.map { JobProgress.duration($0) } ?? "时长未知", value.model, DirectResult.stateLabel(value.state)].joined(separator: " · ")
        titleField.toolTip = value.filename
        warning.stringValue = value.warning ?? ""; warning.isHidden = value.warning == nil
        let body = NSMutableAttributedString()
        let paragraph = NSMutableParagraphStyle(); paragraph.paragraphSpacing = 7; paragraph.lineSpacing = 3
        let style: [NSAttributedString.Key: Any] = [.font: NSFont.systemFont(ofSize: 14), .foregroundColor: NSColor.labelColor, .paragraphStyle: paragraph]
        if !value.segments.isEmpty {
            for segment in value.segments {
                let seconds = segment["start_seconds"] as? Double
                let end = segment["end_seconds"] as? Double
                let validSeek = seconds?.isFinite == true && seconds! >= 0 && seconds! <= (value.duration ?? .greatestFiniteMagnitude)
                let quality = ((value.value["provenance"] as? [String: Any])?["transcription"] as? [String: Any])?["quality"] as? [String: Any]
                let validTime = validSeek && end?.isFinite == true && end! >= seconds! && end! <= (value.duration ?? .greatestFiniteMagnitude) && segment["timestamp_valid"] as? Bool != false && quality?["timestamp_valid"] as? Bool != false
                let stamp = "[" + (seconds.map(DirectResult.timestamp) ?? "时间未知") + (end.map { " – " + DirectResult.timestamp($0) } ?? "") + "]"
                var timestampStyle = style; timestampStyle[.font] = NSFont.monospacedDigitSystemFont(ofSize: 12, weight: .medium)
                timestampStyle[.foregroundColor] = validTime ? NSColor.linkColor : NSColor.systemOrange
                if validSeek, value.audioPath != nil { timestampStyle[.link] = URL(string: "atranscribe-seek:\(seconds!)")! }
                body.append(NSAttributedString(string: stamp + (validTime ? "\n" : "（时间未验证）\n"), attributes: timestampStyle))
                body.append(NSAttributedString(string: (segment["text"] as? String ?? "") + "\n\n", attributes: style))
            }
        } else { body.append(NSAttributedString(string: value.text, attributes: style)) }
        transcript.textStorage?.setAttributedString(body)
        if preservingScroll, selection.location <= body.length,
           selection.length <= body.length - selection.location { transcript.setSelectedRange(selection) }
        if let scrollPoint, let scroll = transcript.enclosingScrollView {
            scroll.contentView.scroll(to: scrollPoint); scroll.reflectScrolledClipView(scroll.contentView)
        } else { transcript.scrollToBeginningOfDocument(nil) }
        copy.isEnabled = value.copyAllowed; export.isEnabled = !value.formats.isEmpty; finder.isEnabled = value.markdownPath != nil
        retry.isEnabled = value.audioPath != nil
        locate.isEnabled = value.value["source_integrity"] as? String == "missing_or_changed" &&
            value.value["source_ownership"] as? String == "external_referenced"
        labelsButton.isEnabled = value.value["labels_editable"] as? Bool ?? !value.legacy
        if !preserveLabelDraft {
            let labels = value.value["user_labels"] as? [String: Any] ?? [:]
            course.stringValue = labels["course"] as? String ?? ""; speaker.stringValue = labels["speaker"] as? String ?? ""; event.stringValue = labels["event"] as? String ?? ""
            labelsStack.isHidden = true
        }
        labelsButton.title = [course, speaker, event].allSatisfy { $0.stringValue.isEmpty } ? "添加标签（可选）" : "编辑标签（可选）"
        let referenced = value.value["source_ownership"] as? String == "external_referenced"
        if let path = value.audioPath, FileManager.default.fileExists(atPath: path),
           !referenced || selectedSourceMonitor.matchesVerifiedSource() {
            let item = AVPlayerItem(url: URL(fileURLWithPath: path)); let currentPlayer = AVPlayer(playerItem: item); player = currentPlayer
            play.isEnabled = true; seek.isEnabled = true; seek.doubleValue = 0
            let audioGeneration = generation
            timeObserver = currentPlayer.addPeriodicTimeObserver(forInterval: CMTime(seconds: 0.25, preferredTimescale: 600), queue: .main) { [weak self] time in
                guard let self, self.generation == audioGeneration, self.player?.currentItem === item else { return }
                self.updateAudio(time.seconds)
            }
            statusObserver = item.observe(\.status, options: [.new]) { [weak self] item, _ in
                DispatchQueue.main.async { guard let self, self.generation == audioGeneration, self.player?.currentItem === item else { return }; if item.status == .failed { self.showNotice("无法播放此音频，可在访达中打开原文件核对。"); self.play.isEnabled = false; self.seek.isEnabled = false } }
            }
            endedObserver = NotificationCenter.default.addObserver(forName: AVPlayerItem.didPlayToEndTimeNotification, object: item, queue: .main) { [weak self] _ in guard let self, self.generation == audioGeneration, self.player?.currentItem === item else { return }; self.updateAudio(self.effectiveDuration) }
            updateAudio(0)
        } else {
            clock.stringValue = "音频不可用"
            if referenced, value.audioPath != nil { showNotice("无法监控原录音变化；播放已停用。请重新选择录音，或改用托管导入。") }
        }
    }
    func stopAudio() {
        player?.pause()
        if let timeObserver { player?.removeTimeObserver(timeObserver) }
        if let endedObserver { NotificationCenter.default.removeObserver(endedObserver) }
        timeObserver = nil; endedObserver = nil; statusObserver = nil; player = nil
        seek.doubleValue = 0; seek.maxValue = 1; clock.stringValue = "00:00 / 00:00"
        play.image = NSImage(systemSymbolName: "play.fill", accessibilityDescription: "播放 / 暂停")
    }
    func updateAudio(_ seconds: Double) {
        let current = seconds.isFinite ? max(0, seconds) : 0, total = effectiveDuration
        seek.maxValue = max(1, total); seek.doubleValue = min(current, seek.maxValue)
        clock.stringValue = JobProgress.duration(current) + " / " + JobProgress.duration(total)
        play.image = NSImage(systemSymbolName: player?.rate == 0 ? "play.fill" : "pause.fill", accessibilityDescription: "播放 / 暂停")
    }
    func seekTo(_ seconds: Double) {
        guard seconds.isFinite, seconds >= 0, let player else { return }
        let safe = min(seconds, max(0, effectiveDuration))
        player.seek(to: CMTime(seconds: safe, preferredTimescale: 600), toleranceBefore: .zero, toleranceAfter: .zero)
        updateAudio(safe)
    }
    @objc func togglePlay() {
        guard let player else { return }
        if player.rate != 0 { player.pause() }
        else if let result, result.value["source_ownership"] as? String == "external_referenced" {
            let elapsed = player.currentTime().seconds
            let resumeAt = elapsed.isFinite && elapsed < effectiveDuration ? max(0, elapsed) : 0
            stopAudio()
            var updated = result.value
            updated["audio_path"] = NSNull(); updated["source_integrity"] = "pending"; updated["verified_source_stat"] = NSNull()
            display(DirectResult(value: updated), preservingScroll: true)
            verifySource(result.id, generation: generation, autoplay: true, resumeAt: resumeAt)
            return
        } else {
            if player.currentTime().seconds >= effectiveDuration && effectiveDuration > 0 { seekTo(0) }
            player.play()
        }
        updateAudio(player.currentTime().seconds)
    }
    @objc func sliderChanged() { seekTo(seek.doubleValue) }
    func textView(_ textView: NSTextView, clickedOnLink link: Any, at charIndex: Int) -> Bool {
        guard let url = link as? URL, url.scheme == "atranscribe-seek", let value = Double(url.absoluteString.replacingOccurrences(of: "atranscribe-seek:", with: "")) else { return true }
        seekTo(value); return true
    }
    @objc func copyAll() {
        guard let result, result.copyAllowed else { return }
        NSPasteboard.general.clearContents(); NSPasteboard.general.setString(result.copyText, forType: .string)
        showNotice("已复制完整转录正文。")
    }
    @objc func showFinder() {
        if let url = displayedExport ?? result?.markdownPath.map({ URL(fileURLWithPath: $0) }) { NSWorkspace.shared.activateFileViewerSelecting([url]) }
    }
    @objc func toggleLabels() { labelsStack.isHidden.toggle() }
    @objc func saveLabels() {
        guard let result else { return }; let current = generation
        let fields = ["course": course.stringValue, "speaker": speaker.stringValue, "event": event.stringValue]
        client.request(["action":"labels", "result_id":result.id, "user_labels":fields]) { [weak self] response in
            guard let self, self.generation == current else { return }
            switch response {
            case .success: self.showNotice("标签已保存，将包含在之后导出的文件中。"); self.labelsStack.isHidden = true; self.labelsButton.title = "编辑标签（可选）"; self.onChanged?()
            case .failure(let error): self.showNotice(error.localizedDescription)
            }
        }
    }
    @objc func retranscribe() {
        guard let result, let path = result.audioPath, let window = view.window else { return }
        let inputMode = result.value["source_ownership"] as? String == "external_referenced" ? "referenced" : "managed"
        let alert = NSAlert(); alert.messageText = "重新转录这份录音"; alert.informativeText = "保留已有结果，并为这份录音生成一份新结果。"
        alert.addButton(withTitle: "Turbo（默认）"); alert.addButton(withTitle: "large-v3"); alert.addButton(withTitle: "取消")
        alert.beginSheetModal(for: window) { [weak self] response in
            if response == .alertFirstButtonReturn { self?.onRetranscribe?(URL(fileURLWithPath: path), "large-v3-turbo", inputMode) }
            else if response == .alertSecondButtonReturn { self?.onRetranscribe?(URL(fileURLWithPath: path), "large-v3", inputMode) }
        }
    }
    @objc func locateOriginal() {
        guard let result, locate.isEnabled, let window = view.window else { return }
        let panel = NSOpenPanel(); panel.title = "定位原录音"; panel.prompt = "验证此文件"
        panel.canChooseFiles = true; panel.canChooseDirectories = false; panel.allowsMultipleSelection = false
        panel.beginSheetModal(for: window) { [weak self] response in
            guard let self, response == .OK, let url = panel.url,
                  self.result?.id == result.id else { return }
            let current = self.generation
            self.client.request(["action": "locate_source", "result_id": result.id, "path": url.path]) { [weak self] response in
                guard let self, self.generation == current, self.result?.id == result.id else { return }
                switch response {
                case .success(let value): self.stopAudio(); self.display(DirectResult(value: value)); self.showNotice("原录音已验证并重新关联。")
                case .failure(let error): self.showNotice("无法关联此文件：\(error.localizedDescription)")
                }
            }
        }
    }
    var exportPanel: NSSavePanel?
    var formatPicker: NSPopUpButton?
    @objc func exportFormatChanged() {
        guard let panel = exportPanel, let format = formatPicker?.selectedItem?.representedObject as? String else { return }
        panel.nameFieldStringValue = URL(fileURLWithPath: panel.nameFieldStringValue).deletingPathExtension().lastPathComponent + "." + format
    }
    @objc func exportResult() {
        guard let result, let window = view.window else { return }
        let current = generation
        let panel = NSSavePanel(); let picker = NSPopUpButton()
        for format in result.formats { picker.addItem(withTitle: ["md":"Markdown", "txt":"纯文本 TXT", "srt":"字幕 SRT", "json":"结构化 JSON"][format]!); picker.lastItem?.representedObject = format }
        let preferred = DirectSettings.format(UserDefaults.standard.string(forKey: "DefaultExportFormat"))
        let selected = result.formats.contains(preferred) ? preferred : result.formats.first ?? "md"
        picker.selectItem(at: result.formats.firstIndex(of: selected) ?? 0)
        picker.target = self; picker.action = #selector(exportFormatChanged)
        let accessory = NSStackView(views: [NSTextField(labelWithString: "格式："), picker]); accessory.spacing = 8
        panel.accessoryView = accessory; panel.title = "导出转录"; panel.prompt = "导出"
        panel.message = result.formats.contains("srt") ? "选择导出位置。已有文件不会被覆盖。" : "选择导出位置。此结果没有可验证的字幕时间戳，暂不提供 SRT。"
        panel.nameFieldStringValue = DirectSettings.exportName(filename: result.filename, format: selected)
        panel.canCreateDirectories = true; panel.isExtensionHidden = false
        if let path = UserDefaults.standard.string(forKey: "DefaultExportDirectory") { panel.directoryURL = URL(fileURLWithPath: path) }
        exportPanel = panel; formatPicker = picker
        panel.beginSheetModal(for: window) { [weak self] response in
            guard let self else { return }; defer { self.exportPanel = nil; self.formatPicker = nil }
            guard response == .OK, let chosen = panel.url, let format = picker.selectedItem?.representedObject as? String else { return }
            let url = DirectSettings.exportDestination(chosen, format: format)
            self.client.request(["action":"export", "result_id":result.id, "format":format, "path":url.path]) { [weak self] response in
                guard let self, self.generation == current, self.result?.id == result.id else { return }
                switch response {
                case .success(let value):
                    self.displayedExport = URL(fileURLWithPath: value["path"] as? String ?? url.path)
                    self.finder.isEnabled = true; self.showNotice("已导出 \(url.lastPathComponent)。点击“在访达中显示”查看。")
                case .failure(let error): self.showNotice("导出未完成：\(error.localizedDescription) 请使用新的文件名或位置。")
                }
            }
        }
    }
}
