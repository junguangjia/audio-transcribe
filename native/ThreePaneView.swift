import Cocoa

final class BoundsContainingStackView: NSStackView {
    var contentEdgeInsets = NSEdgeInsetsZero {
        didSet { needsUpdateConstraints = true }
    }

    override func updateConstraints() {
        // AppKit aligns controls by alignment rects. Older rounded buttons
        // draw outside those rects, so reserve their visible frame overflow.
        // Hidden controls add no padding to the collapsed accessory row.
        let visibleInsets = views.filter { !$0.isHidden }.map(\.alignmentRectInsets)
        let top = contentEdgeInsets.top + max(0, visibleInsets.map(\.top).max() ?? 0)
        let bottom = contentEdgeInsets.bottom + max(0, visibleInsets.map(\.bottom).max() ?? 0)
        if edgeInsets.top != top || edgeInsets.bottom != bottom ||
            edgeInsets.left != contentEdgeInsets.left || edgeInsets.right != contentEdgeInsets.right {
            edgeInsets = NSEdgeInsets(top: top, left: contentEdgeInsets.left,
                                     bottom: bottom, right: contentEdgeInsets.right)
        }
        super.updateConstraints()
    }
}

extension MainController {
    func buildThreePane() {
        guard let content = window?.contentView else { return }
        content.wantsLayer = true
        let root = NSStackView(); root.orientation = .vertical; root.spacing = 0
        root.distribution = .fill
        root.translatesAutoresizingMaskIntoConstraints = false; content.addSubview(root)
        NSLayoutConstraint.activate([root.leadingAnchor.constraint(equalTo: content.leadingAnchor),
                                     root.trailingAnchor.constraint(equalTo: content.trailingAnchor),
                                     root.topAnchor.constraint(equalTo: content.topAnchor),
                                     root.bottomAnchor.constraint(equalTo: content.bottomAnchor)])

        outerSplitController.splitView = outerSplit
        outerSplit.isVertical = true; outerSplit.dividerStyle = .thin
#if !LAYOUT_TESTING
        outerSplit.autosaveName = "AudioTranscribeThreePaneOuter"
#endif
        root.addArrangedSubview(outerSplitController.view)
        outerSplitController.view.widthAnchor.constraint(equalTo: root.widthAnchor).isActive = true
        outerSplitController.view.setContentHuggingPriority(.defaultLow, for: .vertical)
        let navController = NSViewController(); navController.view = makeSidebar()
        let navItem = NSSplitViewItem(sidebarWithViewController: navController)
        navItem.minimumThickness = 190; navItem.maximumThickness = 270; navItem.canCollapse = false
        outerSplitController.addSplitViewItem(navItem)

        contentSplitController.splitView = contentSplit
        contentSplit.isVertical = true; contentSplit.dividerStyle = .thin
#if !LAYOUT_TESTING
        contentSplit.autosaveName = "AudioTranscribeThreePaneContent"
#endif
        let listController = NSViewController(); listController.view = makeRecordingPane()
        let listItem = NSSplitViewItem(viewController: listController)
        listItem.minimumThickness = 345; listItem.canCollapse = false
        contentSplitController.addSplitViewItem(listItem)
        let detailController = NSViewController(); detailController.view = makeDetailPane()
        let detailItem = NSSplitViewItem(viewController: detailController)
        detailItem.minimumThickness = 520; detailItem.canCollapse = false
        contentSplitController.addSplitViewItem(detailItem)
        let contentItem = NSSplitViewItem(viewController: contentSplitController)
        contentItem.minimumThickness = 865; contentItem.canCollapse = false
        outerSplitController.addSplitViewItem(contentItem)
        for split in [outerSplit, contentSplit] {
            splitObservers.append(NotificationCenter.default.addObserver(forName: NSSplitView.didResizeSubviewsNotification,
                object: split, queue: .main) { [weak self] note in self?.storeSplitWidths(note) })
        }

        status.font = .systemFont(ofSize: 12); status.textColor = .secondaryLabelColor
        status.lineBreakMode = .byTruncatingTail
        cancel.target = self; cancel.action = #selector(cancelWork); cancel.bezelStyle = .rounded
        resume.target = self; resume.action = #selector(resumeAll); resume.bezelStyle = .rounded
        deleteButton.target = self; deleteButton.action = #selector(deleteSelected); deleteButton.bezelStyle = .rounded
        cleanupRetry.target = self; cleanupRetry.action = #selector(retryDeletion); cleanupRetry.bezelStyle = .rounded; cleanupRetry.isHidden = true
        cleanupCancel.target = self; cleanupCancel.action = #selector(abandonPendingDeletion); cleanupCancel.bezelStyle = .rounded; cleanupCancel.isHidden = true
        cleanupStatus.font = .systemFont(ofSize: 12); cleanupStatus.textColor = .secondaryLabelColor; cleanupStatus.isHidden = true
        let cleanupSpacer = NSView()
        let idleHeight = cleanupSpacer.heightAnchor.constraint(equalToConstant: 0)
        // Prefer a collapsed empty row, but allow visible controls (compression
        // resistance 750) to give it their full height during cleanup.
        idleHeight.priority = NSLayoutConstraint.Priority(499); idleHeight.isActive = true
        let cleanup = BoundsContainingStackView(views: [cleanupStatus, cleanupSpacer, cleanupRetry, cleanupCancel]); cleanup.spacing = 8
        cleanup.contentEdgeInsets = NSEdgeInsets(top: 4, left: 12, bottom: 4, right: 12)
        // An empty horizontal spacer must not absorb the window's extra height.
        // The split view owns that space; accessory rows hug their controls.
        cleanup.setHuggingPriority(.required, for: .vertical)
        root.addArrangedSubview(cleanup); cleanup.widthAnchor.constraint(equalTo: root.widthAnchor).isActive = true
        let bottom = NSStackView(views: [status, NSView(), deleteButton, resume, cancel]); bottom.spacing = 8
        bottom.edgeInsets = NSEdgeInsets(top: 8, left: 12, bottom: 8, right: 12)
        bottom.setHuggingPriority(.required, for: .vertical)
        root.addArrangedSubview(bottom); bottom.widthAnchor.constraint(equalTo: root.widthAnchor).isActive = true

        drop.accept = { [weak self] urls in self?.add(urls) ?? false }
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            if self.defaults.object(forKey: "ThreePaneSidebarWidth") == nil {
                self.outerSplit.setPosition(225, ofDividerAt: 0)
            } else { self.outerSplit.setPosition(CGFloat(self.defaults.double(forKey: "ThreePaneSidebarWidth")), ofDividerAt: 0) }
            if self.defaults.object(forKey: "ThreePaneListWidth") == nil {
                self.contentSplit.setPosition(465, ofDividerAt: 0)
            } else { self.contentSplit.setPosition(CGFloat(self.defaults.double(forKey: "ThreePaneListWidth")), ofDividerAt: 0) }
        }
    }

    func makeSidebar() -> NSView {
        let background = NSVisualEffectView(); background.material = .sidebar; background.blendingMode = .withinWindow
        let stack = NSStackView(); stack.orientation = .vertical; stack.alignment = .leading; stack.spacing = 10
        stack.translatesAutoresizingMaskIntoConstraints = false; background.addSubview(stack)
        NSLayoutConstraint.activate([stack.leadingAnchor.constraint(equalTo: background.leadingAnchor, constant: 13),
                                     stack.trailingAnchor.constraint(equalTo: background.trailingAnchor, constant: -13),
                                     stack.topAnchor.constraint(equalTo: background.topAnchor, constant: 22),
                                     stack.bottomAnchor.constraint(lessThanOrEqualTo: background.bottomAnchor, constant: -16),
                                     background.widthAnchor.constraint(greaterThanOrEqualToConstant: 190)])
        let approvedIcon = Bundle.main.url(forResource: "AppIcon", withExtension: "icns")
            .flatMap { NSImage(contentsOf: $0) }
            ?? NSWorkspace.shared.icon(forFile: Bundle.main.bundlePath)
        let icon = NSImageView(image: approvedIcon)
        icon.imageScaling = .scaleProportionallyUpOrDown
        icon.widthAnchor.constraint(equalToConstant: 31).isActive = true
        icon.heightAnchor.constraint(equalToConstant: 31).isActive = true
        let appName = NSTextField(labelWithString:
            Bundle.main.object(forInfoDictionaryKey: "CFBundleDisplayName") as? String ?? "AudioTranscribe")
        appName.font = .systemFont(ofSize: 14, weight: .semibold); appName.lineBreakMode = .byTruncatingTail
        let identity = NSStackView(views: [icon, appName]); identity.spacing = 7; identity.alignment = .centerY
        stack.addArrangedSubview(identity); identity.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
        let spacer = NSView(); stack.addArrangedSubview(spacer); spacer.heightAnchor.constraint(equalToConstant: 9).isActive = true
        for filter in [RecordingFilter.all, .processing, .waiting, .completed, .review] {
            let button = sidebarButton(filter.title, symbol: filter.symbol, action: #selector(selectFilter(_:)))
            button.tag = filter.rawValue; sidebarButtons[filter] = button; stack.addArrangedSubview(button)
        }
        let extra = extraFilterPicker; extra.addItems(withTitles: ["更多状态", "失败", "已取消"])
        extra.target = self; extra.action = #selector(selectExtraFilter(_:)); extra.bezelStyle = .rounded
        extra.setAccessibilityLabel("筛选失败或取消的任务")
        stack.addArrangedSubview(extra); extra.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
        let separator = NSBox(); separator.boxType = .separator
        stack.addArrangedSubview(separator); separator.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
        let importButton = sidebarButton("导入录音…", symbol: "square.and.arrow.down", action: #selector(chooseFiles))
        stack.addArrangedSubview(importButton)
        let folderButton = sidebarButton("监控文件夹", symbol: "folder", action: #selector(chooseWatchedFolder))
        stack.addArrangedSubview(folderButton)
        sidebarFolderState.font = .systemFont(ofSize: 11); sidebarFolderState.textColor = .secondaryLabelColor
        sidebarFolderState.lineBreakMode = .byTruncatingMiddle
        stack.addArrangedSubview(sidebarFolderState); sidebarFolderState.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
        let settings = sidebarButton("设置", symbol: "gearshape", action: #selector(showSettings))
        stack.addArrangedSubview(settings)
        let statusSpacer = NSView(); stack.addArrangedSubview(statusSpacer)
        statusSpacer.heightAnchor.constraint(greaterThanOrEqualToConstant: 16).isActive = true
        updateSidebar()
        return background
    }

    func sidebarButton(_ title: String, symbol: String, action: Selector) -> NSButton {
        let button = NSButton(title: title, target: self, action: action)
        button.image = NSImage(systemSymbolName: symbol, accessibilityDescription: title)
        button.imagePosition = .imageLeading; button.alignment = .left
        button.bezelStyle = .recessed; button.isBordered = false
        button.font = .systemFont(ofSize: 13)
        button.heightAnchor.constraint(equalToConstant: 31).isActive = true
        button.widthAnchor.constraint(greaterThanOrEqualToConstant: 160).isActive = true
        return button
    }

    func makeRecordingPane() -> NSView {
        let pane = NSView()
        let stack = NSStackView(); stack.orientation = .vertical; stack.alignment = .leading; stack.spacing = 10
        stack.translatesAutoresizingMaskIntoConstraints = false; pane.addSubview(stack)
        NSLayoutConstraint.activate([stack.leadingAnchor.constraint(equalTo: pane.leadingAnchor, constant: 13),
                                     stack.trailingAnchor.constraint(equalTo: pane.trailingAnchor, constant: -13),
                                     stack.topAnchor.constraint(equalTo: pane.topAnchor, constant: 15),
                                     stack.bottomAnchor.constraint(equalTo: pane.bottomAnchor, constant: -10),
                                     pane.widthAnchor.constraint(greaterThanOrEqualToConstant: 345)])
        search.placeholderString = "搜索文件名、标签或正文…"; search.delegate = self
        search.setAccessibilityLabel("搜索录音和转录正文")
        sortPicker.addItems(withTitles: RecordingSort.allCases.map(\.title))
        sortPicker.selectItem(at: sortMode.rawValue); sortPicker.target = self; sortPicker.action = #selector(changeSort)
        sortPicker.setAccessibilityLabel("排序")
        let searchBar = NSStackView(views: [search, sortPicker]); searchBar.spacing = 8
        stack.addArrangedSubview(searchBar); searchBar.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
        search.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        sortPicker.widthAnchor.constraint(equalToConstant: 126).isActive = true
        let column = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("recording"))
        column.minWidth = 315; column.width = 455; table.addTableColumn(column)
        table.headerView = nil; table.rowHeight = 83; table.style = .fullWidth
        table.allowsEmptySelection = true; table.allowsMultipleSelection = true
        table.delegate = self; table.dataSource = self; table.setAccessibilityLabel("录音任务与结果")
        table.registerForDraggedTypes([.fileURL])
        table.deleteSelection = { [weak self] in self?.deleteSelected() }
        let scroll = NSScrollView(); scroll.documentView = table; scroll.hasVerticalScroller = true
        scroll.borderType = .noBorder; scroll.autohidesScrollers = true
        stack.addArrangedSubview(scroll); scroll.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
        scroll.setContentHuggingPriority(.defaultLow, for: .vertical)
        let foot = NSTextField(labelWithString: "选择录音查看结果；可拖入文件。")
        foot.font = .systemFont(ofSize: 11); foot.textColor = .secondaryLabelColor
        stack.addArrangedSubview(foot); foot.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
        return pane
    }

    func makeDetailPane() -> NSView {
        let pane = NSView()
        let stack = NSStackView(); stack.orientation = .vertical; stack.alignment = .leading; stack.spacing = 8
        stack.translatesAutoresizingMaskIntoConstraints = false; pane.addSubview(stack)
        NSLayoutConstraint.activate([stack.leadingAnchor.constraint(equalTo: pane.leadingAnchor),
                                     stack.trailingAnchor.constraint(equalTo: pane.trailingAnchor),
                                     stack.topAnchor.constraint(equalTo: pane.topAnchor),
                                     stack.bottomAnchor.constraint(equalTo: pane.bottomAnchor),
                                     pane.widthAnchor.constraint(greaterThanOrEqualToConstant: 520)])
        let card = NSBox(); card.boxType = .custom
        card.cornerRadius = 12; card.borderWidth = 1
        card.borderColor = .separatorColor; card.fillColor = .controlBackgroundColor
        let cardStack = NSStackView(); cardStack.orientation = .vertical; cardStack.alignment = .leading; cardStack.spacing = 8
        cardStack.translatesAutoresizingMaskIntoConstraints = false; card.contentView?.addSubview(cardStack)
        if let content = card.contentView {
            NSLayoutConstraint.activate([cardStack.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 14),
                                         cardStack.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -14),
                                         cardStack.topAnchor.constraint(equalTo: content.topAnchor, constant: 11),
                                         cardStack.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -11)])
        }
        let folderIcon = NSImageView(image: NSImage(systemSymbolName: "folder.fill", accessibilityDescription: "监控文件夹")!)
        folderIcon.contentTintColor = .controlAccentColor
        folderIcon.widthAnchor.constraint(equalToConstant: 28).isActive = true
        folderIcon.heightAnchor.constraint(equalToConstant: 28).isActive = true
        let folderHeading = NSTextField(labelWithString: "监控文件夹"); folderHeading.font = .systemFont(ofSize: 14, weight: .semibold)
        watchPath.font = .systemFont(ofSize: 11); watchPath.textColor = .secondaryLabelColor
        watchPath.lineBreakMode = .byTruncatingMiddle; watchPath.toolTip = watchPath.stringValue
        let folderText = NSStackView(views: [folderHeading, watchPath]); folderText.orientation = .vertical
        folderText.alignment = .leading; folderText.spacing = 2
        let header = NSStackView(views: [folderIcon, folderText, NSView(), watchState]); header.spacing = 8
        cardStack.addArrangedSubview(header); header.widthAnchor.constraint(equalTo: cardStack.widthAnchor).isActive = true
        watchState.font = .systemFont(ofSize: 11); watchState.textColor = .secondaryLabelColor
        watchState.setContentCompressionResistancePriority(.required, for: .horizontal)
        openFolder.target = self; openFolder.action = #selector(openWatchedFolder)
        changeFolder.target = self; changeFolder.action = #selector(chooseWatchedFolder)
        refreshFolder.target = self; refreshFolder.action = #selector(refreshWatchedFolder)
        startAll.target = self; startAll.action = #selector(startAllNew)
        for button in [openFolder, changeFolder, refreshFolder, startAll] { button.bezelStyle = .rounded }
        let controls = NSStackView(views: [openFolder, changeFolder, refreshFolder, NSView(), startAll]); controls.spacing = 6
        cardStack.addArrangedSubview(controls); controls.widthAnchor.constraint(equalTo: cardStack.widthAnchor).isActive = true
        stack.addArrangedSubview(card); card.widthAnchor.constraint(equalTo: stack.widthAnchor, constant: -24).isActive = true
        card.heightAnchor.constraint(greaterThanOrEqualToConstant: 90).isActive = true
        card.setContentHuggingPriority(.required, for: .vertical)
        stack.setCustomSpacing(3, after: card)
        stack.addArrangedSubview(reader.view); reader.view.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
        reader.view.setContentHuggingPriority(.defaultLow, for: .vertical)
        updateWatchedCard()
        return pane
    }

    func updateSidebar() {
        extraFilterPicker.selectItem(at: selectedFilter == .failed ? 1 : selectedFilter == .cancelled ? 2 : 0)
        for filter in [RecordingFilter.all, .processing, .waiting, .completed, .review] {
            guard let button = sidebarButtons[filter] else { continue }
            let count = rowCounts[filter] ?? 0
            button.title = "\(filter.title)    \(count)"
            if filter == .completed { button.toolTip = "包含需要复核的已生成结果；两个分类的数量有重叠。" }
            if filter == .review { button.toolTip = "正文已生成，自动检查提示需要结合原音复核。" }
            button.contentTintColor = selectedFilter == filter ? .controlAccentColor : .labelColor
            button.font = .systemFont(ofSize: 13, weight: selectedFilter == filter ? .semibold : .regular)
        }
    }

    func updateWatchedCard() {
        let folder = watcher.folder
        watchPath.stringValue = folder?.path ?? "尚未选择文件夹"
        watchPath.toolTip = folder?.path
        let ready = watchedItems.values.filter { $0.state == "ready" && !isAlreadyQueued($0) && !isWatchDeletionFenced($0) }.count
        startAll.title = "开始全部新录音（\(ready)）"
        startAll.isEnabled = ready > 0
        openFolder.isEnabled = folder != nil
        watchState.stringValue = folder == nil ? "未启用" : watchStatus
        watchState.textColor = folder != nil && watchStatus.hasPrefix("已启用") ? .systemGreen : .secondaryLabelColor
        sidebarFolderState.stringValue = folder == nil ? "未启用" : watchStatus
    }
}
