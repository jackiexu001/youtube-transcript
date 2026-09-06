import AppKit
import Darwin
import Foundation
import Security

private let appTitle = "YouTube Transcript"

final class AdaptiveBorderScrollView: NSScrollView {
    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        updateBorderColor()
    }

    override func viewDidChangeEffectiveAppearance() {
        super.viewDidChangeEffectiveAppearance()
        updateBorderColor()
    }

    private func updateBorderColor() {
        effectiveAppearance.performAsCurrentDrawingAppearance {
            layer?.borderColor = NSColor.separatorColor.cgColor
        }
    }
}

final class CaptionAppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate, NSTextFieldDelegate {
    private var window: NSWindow!
    private var channelField: NSTextField!
    private var batchField: NSTextField!
    private var archivePathField: NSTextField!
    private var proxyField: NSTextField!
    private var aiKeyField: NSSecureTextField!
    private var aiProviderPopup: NSPopUpButton!
    private var saveAPIKeyButton: NSButton!
    private var getAPIKeyButton: NSButton!
    private var aiEnabledButton: NSButton!
    private var chooseArchiveButton: NSButton!
    private var startButton: NSButton!
    private var openButton: NSButton!
    private var pauseButton: NSButton!
    private var stopButton: NSButton!
    private var closeButton: NSButton!
    private var statusLabel: NSTextField!
    private var progressBar: NSProgressIndicator!
    private var logView: NSTextView!
    private var processedValueLabel: NSTextField!
    private var positionValueLabel: NSTextField!
    private var elapsedValueLabel: NSTextField!
    private var currentVideoLabel: NSTextField!

    private var process: Process?
    private var currentChannelFile: URL?
    private var isPaused = false
    private var lastChannelPage: String?
    private var processedThisRun = 0
    private var requestedBatchSize = 100
    private var finishedWithWarnings = false
    private var runStartedAt: Date?
    private var elapsedTimer: Timer?
    private let archivePathDefaultsKey = "YouTubeCaptionArchivePath"
    private let archiveBookmarkDefaultsKey = "YouTubeCaptionArchiveBookmark"
    private let proxyDefaultsKey = "YouTubeCaptionProxy"
    private let aiEnabledDefaultsKey = "YouTubeCaptionAIEnabled"
    private let aiProviderDefaultsKey = "YouTubeCaptionAIProvider"
    private let themeDefaultsKey = "YouTubeTranscriptTheme"
    private var activeAIProvider = "groq"
    private var activeTheme = "system"
    private var themeMenuItems: [NSMenuItem] = []
    private var apiKeyDrafts: [String: String] = [:]
    private var archiveAccessURL: URL?
    private let timestampFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.dateFormat = "HH:mm:ss"
        return formatter
    }()

    private lazy var projectRoot: URL = {
        if let configured = ProcessInfo.processInfo.environment["YCA_ROOT"], !configured.isEmpty {
            return URL(fileURLWithPath: configured, isDirectory: true)
        }
        return Bundle.main.bundleURL.deletingLastPathComponent()
    }()

    private lazy var archiveRoot: URL = resolveInitialArchiveRoot()

    private func resolveInitialArchiveRoot() -> URL {
        if let bookmark = UserDefaults.standard.data(forKey: archiveBookmarkDefaultsKey) {
            var isStale = false
            if let bookmarkedURL = try? URL(
                resolvingBookmarkData: bookmark,
                options: [.withSecurityScope],
                relativeTo: nil,
                bookmarkDataIsStale: &isStale
            ) {
                if bookmarkedURL.startAccessingSecurityScopedResource() {
                    archiveAccessURL = bookmarkedURL
                }
                if isStale {
                    saveArchiveBookmark(for: bookmarkedURL)
                }
                UserDefaults.standard.set(bookmarkedURL.path, forKey: archivePathDefaultsKey)
                return bookmarkedURL
            }
            UserDefaults.standard.removeObject(forKey: archiveBookmarkDefaultsKey)
        }
        if let saved = UserDefaults.standard.string(forKey: archivePathDefaultsKey), !saved.isEmpty {
            return URL(fileURLWithPath: saved, isDirectory: true)
        }
        let legacy = projectRoot.appendingPathComponent("archive", isDirectory: true)
        if FileManager.default.fileExists(atPath: legacy.appendingPathComponent("index.html").path) {
            UserDefaults.standard.set(legacy.path, forKey: archivePathDefaultsKey)
            return legacy
        }
        let documents = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask).first
            ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Documents", isDirectory: true)
        return documents.appendingPathComponent("YouTube 字幕学习档案", isDirectory: true)
    }

    private func saveArchiveBookmark(for url: URL) {
        if archiveAccessURL?.standardizedFileURL != url.standardizedFileURL {
            archiveAccessURL?.stopAccessingSecurityScopedResource()
            archiveAccessURL = nil
        }
        if let bookmark = try? url.bookmarkData(
            options: [.withSecurityScope],
            includingResourceValuesForKeys: nil,
            relativeTo: nil
        ) {
            UserDefaults.standard.set(bookmark, forKey: archiveBookmarkDefaultsKey)
        }
        if archiveAccessURL == nil, url.startAccessingSecurityScopedResource() {
            archiveAccessURL = url
        }
    }

    private func verifyArchiveIsWritable() throws {
        try FileManager.default.createDirectory(at: archiveRoot, withIntermediateDirectories: true)
        let probe = archiveRoot.appendingPathComponent(
            ".youtube-caption-write-test-\(ProcessInfo.processInfo.processIdentifier)"
        )
        defer { try? FileManager.default.removeItem(at: probe) }
        try Data("write-test".utf8).write(to: probe, options: .atomic)
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        migrateLegacyPreferencesIfNeeded()
        activeTheme = normalizedTheme(UserDefaults.standard.string(forKey: themeDefaultsKey) ?? "system")
        applyTheme(activeTheme, persist: false)
        buildMainMenu()
        buildWindow()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        showInstallerCleanupNoticeIfNeeded()
    }

    private func migrateLegacyPreferencesIfNeeded() {
        let migrationKey = "YouTubeTranscriptDidMigrateLegacyPreferences"
        let defaults = UserDefaults.standard
        guard !defaults.bool(forKey: migrationKey),
              let legacy = UserDefaults(suiteName: "com.youtube-caption.archive") else { return }
        let keys = [
            archivePathDefaultsKey,
            archiveBookmarkDefaultsKey,
            proxyDefaultsKey,
            aiEnabledDefaultsKey,
            aiProviderDefaultsKey,
            themeDefaultsKey,
        ]
        for key in keys where defaults.object(forKey: key) == nil {
            if let value = legacy.object(forKey: key) {
                defaults.set(value, forKey: key)
            }
        }
        defaults.set(true, forKey: migrationKey)
    }

    private func showInstallerCleanupNoticeIfNeeded() {
        let noticeVersion = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "current"
        let noticeKey = "YouTubeCaptionDidShowInstallerCleanupNotice-\(noticeVersion)"
        guard !UserDefaults.standard.bool(forKey: noticeKey) else { return }
        let appPath = Bundle.main.bundleURL.standardizedFileURL.path
        let installed = appPath.hasPrefix("/Applications/")
            || appPath.hasPrefix(FileManager.default.homeDirectoryForCurrentUser
                .appendingPathComponent("Applications", isDirectory: true).path + "/")
        guard installed else { return }

        DispatchQueue.main.asyncAfter(deadline: .now() + 0.8) { [weak self] in
            guard let self else { return }
            let alert = NSAlert()
            alert.alertStyle = .informational
            alert.messageText = "应用已经安装完成"
            alert.informativeText = "现在可以推出“YouTube Transcript”安装磁盘，并删除“下载”文件夹里的 DMG 安装包。\n\n如果搜索时看到两个同名图标，通常是安装磁盘尚未推出；如果还看到“开始抓取 YouTube 字幕”，请在“应用程序”文件夹中删除这个旧版本。删除安装包和旧应用不会影响已经生成的字幕档案。"
            alert.addButton(withTitle: "知道了")
            alert.addButton(withTitle: "打开应用程序文件夹")
            alert.addButton(withTitle: "打开下载文件夹")
            alert.beginSheetModal(for: self.window) { response in
                UserDefaults.standard.set(true, forKey: noticeKey)
                if response == .alertSecondButtonReturn,
                   let applications = FileManager.default.urls(for: .applicationDirectory, in: .localDomainMask).first {
                    NSWorkspace.shared.open(applications)
                } else if response == .alertThirdButtonReturn,
                   let downloads = FileManager.default.urls(for: .downloadsDirectory, in: .userDomainMask).first {
                    NSWorkspace.shared.open(downloads)
                }
            }
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        guard let process, process.isRunning else { return true }

        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = "字幕仍在抓取"
        alert.informativeText = "现在关闭会停止本次任务。已经成功保存的视频不会丢失，下次输入同一频道会自动继续。"
        alert.addButton(withTitle: "继续抓取")
        alert.addButton(withTitle: "停止并关闭")
        if alert.runModal() == .alertSecondButtonReturn {
            if isPaused {
                setProcessPaused(false)
            }
            process.terminate()
            return true
        }
        return false
    }

    private func buildMainMenu() {
        let main = NSMenu()
        @discardableResult
        func addAction(_ menu: NSMenu, _ title: String, _ action: Selector, _ key: String = "") -> NSMenuItem {
            let item = menu.addItem(withTitle: title, action: action, keyEquivalent: key)
            item.target = self
            return item
        }

        let appItem = NSMenuItem()
        let appMenu = NSMenu(title: appTitle)
        addAction(appMenu, "关于 \(appTitle)", #selector(showAboutPanel))
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "隐藏 \(appTitle)", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(withTitle: "隐藏其他应用", action: #selector(NSApplication.hideOtherApplications(_:)), keyEquivalent: "h").keyEquivalentModifierMask = [.command, .option]
        appMenu.addItem(withTitle: "显示全部", action: #selector(NSApplication.unhideAllApplications(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "退出 \(appTitle)", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        main.addItem(appItem)

        let fileItem = NSMenuItem()
        let fileMenu = NSMenu(title: "文件")
        addAction(fileMenu, "选择档案文件夹…", #selector(chooseArchiveFolder), "o")
        addAction(fileMenu, "打开字幕档案", #selector(openResults), "r")
        fileMenu.addItem(.separator())
        fileMenu.addItem(withTitle: "关闭窗口", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        fileItem.submenu = fileMenu
        main.addItem(fileItem)

        let taskItem = NSMenuItem()
        let taskMenu = NSMenu(title: "任务")
        let start = addAction(taskMenu, "开始抓取", #selector(startCapture), "\r")
        start.keyEquivalentModifierMask = [.command]
        addAction(taskMenu, "暂停或继续", #selector(togglePause), "p")
        addAction(taskMenu, "停止当前任务", #selector(stopCapture), ".")
        taskItem.submenu = taskMenu
        main.addItem(taskItem)

        let settingsItem = NSMenuItem()
        let settingsMenu = NSMenu(title: "设置")
        addAction(settingsMenu, "网络代理…", #selector(configureNetworkProxy), ",")
        addAction(settingsMenu, "保存当前 API Key", #selector(saveSelectedAPIKey))
        settingsMenu.addItem(.separator())
        let themeItem = NSMenuItem(title: "主题", action: nil, keyEquivalent: "")
        let themeMenu = NSMenu(title: "主题")
        themeMenuItems = [
            makeThemeMenuItem(title: "跟随系统（System）", value: "system"),
            makeThemeMenuItem(title: "浅色（Light）", value: "light"),
            makeThemeMenuItem(title: "深色（Dark）", value: "dark"),
        ]
        themeMenuItems.forEach { themeMenu.addItem($0) }
        themeItem.submenu = themeMenu
        settingsMenu.addItem(themeItem)
        updateThemeMenuChecks()
        settingsItem.submenu = settingsMenu
        main.addItem(settingsItem)

        let helpItem = NSMenuItem()
        let helpMenu = NSMenu(title: "帮助")
        addAction(helpMenu, "使用说明", #selector(openUserGuide), "?")
        addAction(helpMenu, "为什么搜索到多个应用？", #selector(showDuplicateAppsHelp))
        helpMenu.addItem(.separator())
        addAction(helpMenu, "获取 Groq API Key", #selector(openGroqAPIKeyPage))
        addAction(helpMenu, "获取 OpenAI API Key", #selector(openOpenAIAPIKeyPage))
        helpItem.submenu = helpMenu
        main.addItem(helpItem)

        NSApp.mainMenu = main
    }

    @objc private func showAboutPanel() {
        NSApp.orderFrontStandardAboutPanel(nil)
    }

    @objc private func openUserGuide() {
        if let guide = Bundle.main.url(forResource: "MAC_DOWNLOAD_GUIDE", withExtension: "txt") {
            NSWorkspace.shared.open(guide)
        } else {
            showAlert(title: "使用说明", message: "输入频道主页、选择本次抓取数量；如需为无字幕视频生成 AI 字幕，请选择服务并填写对应 API Key。")
        }
    }

    @objc private func showDuplicateAppsHelp() {
        showAlert(
            title: "为什么会看到多个应用？",
            message: "安装后尚未推出 DMG 时，macOS 会同时搜索到“应用程序”中的正式副本和安装磁盘中的临时副本。推出安装磁盘并删除下载的 DMG 即可。\n\n如果还看到“开始抓取 YouTube 字幕”，那是旧版本，可以从“应用程序”文件夹删除；字幕档案不会被删除。"
        )
    }

    private func normalizedTheme(_ value: String) -> String {
        ["system", "light", "dark"].contains(value) ? value : "system"
    }

    private func makeThemeMenuItem(title: String, value: String) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: #selector(changeTheme(_:)), keyEquivalent: "")
        item.target = self
        item.representedObject = value
        return item
    }

    private func updateThemeMenuChecks() {
        themeMenuItems.forEach { item in
            item.state = (item.representedObject as? String) == activeTheme ? .on : .off
        }
    }

    private func applyTheme(_ theme: String, persist: Bool = true) {
        activeTheme = normalizedTheme(theme)
        switch activeTheme {
        case "light":
            NSApp.appearance = NSAppearance(named: .aqua)
        case "dark":
            NSApp.appearance = NSAppearance(named: .darkAqua)
        default:
            NSApp.appearance = nil
        }
        if persist {
            UserDefaults.standard.set(activeTheme, forKey: themeDefaultsKey)
        }
        updateThemeMenuChecks()
        window?.contentView?.needsDisplay = true
    }

    @objc private func changeTheme(_ sender: NSMenuItem) {
        guard let theme = sender.representedObject as? String else { return }
        applyTheme(theme)
    }

    private func buildWindow() {
        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 960, height: 890),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = appTitle
        window.minSize = NSSize(width: 820, height: 790)
        window.center()
        window.delegate = self
        window.isReleasedWhenClosed = false

        guard let content = window.contentView else { return }
        content.wantsLayer = true
        content.layer?.backgroundColor = nil

        let brandIcon = NSImageView()
        if let iconURL = Bundle.main.url(forResource: "AppIcon", withExtension: "icns") {
            brandIcon.image = NSImage(contentsOf: iconURL)
        } else {
            brandIcon.image = NSImage(
                systemSymbolName: "play.rectangle.fill",
                accessibilityDescription: appTitle
            )?.withSymbolConfiguration(NSImage.SymbolConfiguration(pointSize: 38, weight: .bold))
            brandIcon.contentTintColor = .labelColor
        }
        brandIcon.imageScaling = .scaleProportionallyUpOrDown
        brandIcon.translatesAutoresizingMaskIntoConstraints = false

        let title = label(appTitle, size: 30, weight: .bold)
        let header = NSStackView(views: [brandIcon, title])
        header.orientation = .horizontal
        header.alignment = .centerY
        header.spacing = 14
        header.translatesAutoresizingMaskIntoConstraints = false

        let form = NSVisualEffectView()
        form.material = .contentBackground
        form.blendingMode = .withinWindow
        form.state = .active
        form.wantsLayer = true
        form.layer?.cornerRadius = 12
        form.layer?.masksToBounds = true
        form.translatesAutoresizingMaskIntoConstraints = false

        let channelLabel = label("频道主页", size: 13, weight: .semibold)
        channelField = NSTextField()
        channelField.placeholderString = "例如：https://www.youtube.com/@xxxx"
        channelField.font = .systemFont(ofSize: 14)
        channelField.controlSize = .large
        channelField.usesSingleLineMode = true
        channelField.lineBreakMode = .byTruncatingMiddle
        channelField.focusRingType = .default
        channelField.translatesAutoresizingMaskIntoConstraints = false

        let batchLabel = label("本次数量", size: 13, weight: .semibold)
        batchField = NSTextField(string: "100")
        batchField.font = .monospacedDigitSystemFont(ofSize: 14, weight: .regular)
        batchField.alignment = .center
        batchField.placeholderString = "100"
        batchField.controlSize = .large
        batchField.translatesAutoresizingMaskIntoConstraints = false
        let batchUnit = label("条视频 · 0 表示全部", size: 13, color: .secondaryLabelColor)

        let batchRow = NSStackView(views: [batchField, batchUnit])
        batchRow.orientation = .horizontal
        batchRow.alignment = .centerY
        batchRow.spacing = 8
        batchRow.translatesAutoresizingMaskIntoConstraints = false

        let archiveLabel = label("保存地址", size: 13, weight: .semibold)
        archivePathField = NSTextField(labelWithString: archiveRoot.path)
        archivePathField.font = .systemFont(ofSize: 13)
        archivePathField.textColor = .secondaryLabelColor
        archivePathField.lineBreakMode = .byTruncatingMiddle
        archivePathField.usesSingleLineMode = true
        archivePathField.translatesAutoresizingMaskIntoConstraints = false
        chooseArchiveButton = NSButton(title: "选择…", target: self, action: #selector(chooseArchiveFolder))
        chooseArchiveButton.bezelStyle = .rounded
        chooseArchiveButton.controlSize = .regular
        let archiveRow = NSStackView(views: [archivePathField, chooseArchiveButton])
        archiveRow.orientation = .horizontal
        archiveRow.alignment = .centerY
        archiveRow.spacing = 10
        archiveRow.translatesAutoresizingMaskIntoConstraints = false

        let aiLabel = label("AI 字幕", size: 13, weight: .semibold)
        activeAIProvider = UserDefaults.standard.string(forKey: aiProviderDefaultsKey) ?? "groq"
        if !["groq", "openai"].contains(activeAIProvider) { activeAIProvider = "groq" }
        aiProviderPopup = NSPopUpButton(frame: .zero, pullsDown: false)
        aiProviderPopup.addItems(withTitles: ["Groq（推荐，速度快）", "OpenAI Whisper"])
        aiProviderPopup.selectItem(at: activeAIProvider == "openai" ? 1 : 0)
        aiProviderPopup.target = self
        aiProviderPopup.action = #selector(aiProviderChanged)
        aiProviderPopup.controlSize = .large
        aiProviderPopup.translatesAutoresizingMaskIntoConstraints = false
        let storedAPIKey = loadAPIKey(provider: activeAIProvider)
        apiKeyDrafts[activeAIProvider] = storedAPIKey
        aiKeyField = NSSecureTextField(string: storedAPIKey)
        aiKeyField.font = .monospacedSystemFont(ofSize: 13, weight: .regular)
        aiKeyField.controlSize = .large
        aiKeyField.usesSingleLineMode = true
        aiKeyField.delegate = self
        aiKeyField.translatesAutoresizingMaskIntoConstraints = false
        saveAPIKeyButton = NSButton(title: storedAPIKey.isEmpty ? "保存 Key" : "已保存", target: self, action: #selector(saveSelectedAPIKey))
        saveAPIKeyButton.bezelStyle = .rounded
        saveAPIKeyButton.controlSize = .regular
        getAPIKeyButton = NSButton(title: "获取 API Key", target: self, action: #selector(openSelectedAPIKeyPage))
        getAPIKeyButton.bezelStyle = .rounded
        getAPIKeyButton.controlSize = .regular
        let aiCredentialRow = NSStackView(views: [aiProviderPopup, aiKeyField, saveAPIKeyButton, getAPIKeyButton])
        aiCredentialRow.orientation = .horizontal
        aiCredentialRow.alignment = .centerY
        aiCredentialRow.spacing = 8
        aiEnabledButton = NSButton(
            checkboxWithTitle: "勾选后若 YouTube 无字幕，系统将自动生成 AI 字幕（支持中文、英语等 99+ 种语言）",
            target: self,
            action: #selector(toggleAI)
        )
        aiEnabledButton.font = .systemFont(ofSize: 12.5)
        aiEnabledButton.state = UserDefaults.standard.object(forKey: aiEnabledDefaultsKey) == nil
            ? .on
            : (UserDefaults.standard.bool(forKey: aiEnabledDefaultsKey) ? .on : .off)
        let aiRow = NSStackView(views: [aiCredentialRow, aiEnabledButton])
        aiRow.orientation = .vertical
        aiRow.alignment = .leading
        aiRow.spacing = 6
        aiRow.translatesAutoresizingMaskIntoConstraints = false
        updateAIProviderUI()
        updateAIFieldState()

        proxyField = NSTextField(string: UserDefaults.standard.string(forKey: proxyDefaultsKey) ?? "")

        let formGrid = NSGridView(views: [
            [channelLabel, channelField],
            [batchLabel, batchRow],
            [archiveLabel, archiveRow],
            [aiLabel, aiRow],
        ])
        formGrid.rowSpacing = 10
        formGrid.columnSpacing = 16
        formGrid.row(at: 0).height = 30
        formGrid.row(at: 1).height = 30
        formGrid.row(at: 2).height = 30
        formGrid.row(at: 3).height = 62
        formGrid.column(at: 0).xPlacement = .leading
        formGrid.column(at: 1).xPlacement = .fill
        formGrid.setContentHuggingPriority(.required, for: .vertical)
        formGrid.setContentCompressionResistancePriority(.required, for: .vertical)
        formGrid.translatesAutoresizingMaskIntoConstraints = false
        form.addSubview(formGrid)

        startButton = NSButton(title: "开始抓取", target: self, action: #selector(startCapture))
        startButton.bezelStyle = .rounded
        startButton.controlSize = .large
        startButton.bezelColor = .labelColor
        startButton.contentTintColor = .windowBackgroundColor
        startButton.font = .systemFont(ofSize: 14, weight: .semibold)
        startButton.image = NSImage(systemSymbolName: "play.fill", accessibilityDescription: nil)
        startButton.imagePosition = .imageLeading
        startButton.keyEquivalent = "\r"

        openButton = NSButton(title: "打开结果", target: self, action: #selector(openResults))
        openButton.bezelStyle = .rounded
        openButton.controlSize = .large
        openButton.font = .systemFont(ofSize: 14, weight: .medium)
        openButton.image = NSImage(systemSymbolName: "folder", accessibilityDescription: nil)
        openButton.imagePosition = .imageLeading
        openButton.isEnabled = FileManager.default.fileExists(atPath: archiveRoot.appendingPathComponent("index.html").path)

        pauseButton = NSButton(title: "暂停", target: self, action: #selector(togglePause))
        pauseButton.bezelStyle = .rounded
        pauseButton.controlSize = .large
        pauseButton.font = .systemFont(ofSize: 14, weight: .medium)
        pauseButton.image = NSImage(systemSymbolName: "pause.fill", accessibilityDescription: nil)
        pauseButton.imagePosition = .imageLeading
        pauseButton.isEnabled = false

        stopButton = NSButton(title: "停止", target: self, action: #selector(stopCapture))
        stopButton.bezelStyle = .rounded
        stopButton.controlSize = .large
        stopButton.font = .systemFont(ofSize: 14, weight: .medium)
        stopButton.image = NSImage(systemSymbolName: "stop.fill", accessibilityDescription: nil)
        stopButton.imagePosition = .imageLeading
        stopButton.isEnabled = false

        let buttonRow = NSStackView(views: [startButton, openButton, pauseButton, stopButton])
        buttonRow.orientation = .horizontal
        buttonRow.spacing = 10
        buttonRow.translatesAutoresizingMaskIntoConstraints = false

        processedValueLabel = label("0 / —", size: 18, weight: .semibold)
        processedValueLabel.font = .monospacedDigitSystemFont(ofSize: 18, weight: .semibold)
        positionValueLabel = label("—", size: 18, weight: .semibold)
        positionValueLabel.font = .monospacedDigitSystemFont(ofSize: 18, weight: .semibold)
        elapsedValueLabel = label("00:00", size: 18, weight: .semibold)
        elapsedValueLabel.font = .monospacedDigitSystemFont(ofSize: 18, weight: .semibold)

        let metrics = NSStackView(views: [
            metricCard(title: "本次已处理", value: processedValueLabel, symbol: "checkmark.circle", tint: .systemGreen),
            metricCard(title: "频道位置", value: positionValueLabel, symbol: "list.number", tint: .systemBlue),
            metricCard(title: "运行时间", value: elapsedValueLabel, symbol: "clock", tint: .systemOrange),
        ])
        metrics.orientation = .horizontal
        metrics.distribution = .fillEqually
        metrics.spacing = 10
        metrics.translatesAutoresizingMaskIntoConstraints = false

        statusLabel = label("准备就绪", size: 13, weight: .medium, color: .secondaryLabelColor)
        statusLabel.alignment = .right
        statusLabel.translatesAutoresizingMaskIntoConstraints = false

        closeButton = NSButton(title: "关闭应用", target: self, action: #selector(closeApplication))
        closeButton.bezelStyle = .rounded
        closeButton.controlSize = .regular
        closeButton.font = .systemFont(ofSize: 13, weight: .medium)
        closeButton.image = NSImage(systemSymbolName: "xmark.circle", accessibilityDescription: nil)
        closeButton.imagePosition = .imageLeading
        closeButton.isHidden = true

        progressBar = NSProgressIndicator()
        progressBar.style = .bar
        progressBar.isIndeterminate = false
        progressBar.minValue = 0
        progressBar.maxValue = 100
        progressBar.doubleValue = 0
        progressBar.translatesAutoresizingMaskIntoConstraints = false

        let progressTitle = label("抓取进度", size: 13, weight: .semibold)
        let statusStack = NSStackView(views: [closeButton, statusLabel])
        statusStack.orientation = .vertical
        statusStack.alignment = .trailing
        statusStack.spacing = 4
        let progressHeader = NSStackView(views: [progressTitle, flexibleSpace(), statusStack])
        progressHeader.orientation = .horizontal
        progressHeader.alignment = .bottom
        progressHeader.translatesAutoresizingMaskIntoConstraints = false

        let logTitle = label("运行日志", size: 14, weight: .semibold)
        currentVideoLabel = label("", size: 12.5, color: .secondaryLabelColor)
        currentVideoLabel.lineBreakMode = .byTruncatingMiddle
        currentVideoLabel.maximumNumberOfLines = 1
        let logHeader = NSStackView(views: [logTitle])
        logHeader.orientation = .vertical
        logHeader.alignment = .leading
        logHeader.spacing = 3
        logHeader.translatesAutoresizingMaskIntoConstraints = false

        let logScroll = AdaptiveBorderScrollView()
        logScroll.hasVerticalScroller = true
        logScroll.hasHorizontalScroller = false
        logScroll.autohidesScrollers = true
        logScroll.scrollerStyle = .overlay
        logScroll.borderType = .noBorder
        logScroll.drawsBackground = false
        logScroll.wantsLayer = true
        logScroll.layer?.cornerRadius = 10
        logScroll.layer?.borderWidth = 1
        logScroll.layer?.masksToBounds = true
        logScroll.translatesAutoresizingMaskIntoConstraints = false

        logView = NSTextView(frame: NSRect(origin: .zero, size: NSSize(width: 860, height: 300)))
        logView.isEditable = false
        logView.isSelectable = true
        logView.isRichText = true
        logView.importsGraphics = false
        logView.isVerticallyResizable = true
        logView.isHorizontallyResizable = false
        logView.autoresizingMask = [.width]
        logView.minSize = NSSize(width: 0, height: 0)
        logView.maxSize = NSSize(width: CGFloat.greatestFiniteMagnitude, height: CGFloat.greatestFiniteMagnitude)
        logView.textContainer?.containerSize = NSSize(width: 0, height: CGFloat.greatestFiniteMagnitude)
        logView.textContainer?.widthTracksTextView = true
        logView.textContainer?.lineFragmentPadding = 0
        logView.drawsBackground = true
        logView.backgroundColor = .controlBackgroundColor
        logView.textColor = .labelColor
        logView.font = .monospacedSystemFont(ofSize: 12.5, weight: .regular)
        logView.textContainerInset = NSSize(width: 14, height: 12)
        logView.string = "准备就绪。输入频道主页和数量后，点击“开始抓取”。\n运行时会在这里实时显示频道读取、视频标题、字幕状态和错误提示。\n"
        logScroll.documentView = logView

        content.addSubview(header)
        content.addSubview(form)
        content.addSubview(buttonRow)
        content.addSubview(metrics)
        content.addSubview(progressHeader)
        content.addSubview(progressBar)
        content.addSubview(logHeader)
        content.addSubview(logScroll)

        NSLayoutConstraint.activate([
            brandIcon.widthAnchor.constraint(equalToConstant: 46),
            brandIcon.heightAnchor.constraint(equalToConstant: 46),
            header.topAnchor.constraint(equalTo: content.topAnchor, constant: 26),
            header.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 32),
            header.trailingAnchor.constraint(lessThanOrEqualTo: content.trailingAnchor, constant: -32),

            form.topAnchor.constraint(equalTo: header.bottomAnchor, constant: 22),
            form.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 32),
            form.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -32),
            form.heightAnchor.constraint(equalToConstant: 214),
            formGrid.topAnchor.constraint(equalTo: form.topAnchor, constant: 14),
            formGrid.leadingAnchor.constraint(equalTo: form.leadingAnchor, constant: 20),
            formGrid.trailingAnchor.constraint(equalTo: form.trailingAnchor, constant: -20),
            formGrid.bottomAnchor.constraint(equalTo: form.bottomAnchor, constant: -14),
            channelField.widthAnchor.constraint(greaterThanOrEqualToConstant: 420),
            batchField.widthAnchor.constraint(equalToConstant: 92),
            archivePathField.widthAnchor.constraint(greaterThanOrEqualToConstant: 360),
            aiProviderPopup.widthAnchor.constraint(equalToConstant: 178),
            aiKeyField.widthAnchor.constraint(greaterThanOrEqualToConstant: 285),

            buttonRow.topAnchor.constraint(equalTo: form.bottomAnchor, constant: 16),
            buttonRow.leadingAnchor.constraint(equalTo: form.leadingAnchor),

            metrics.topAnchor.constraint(equalTo: buttonRow.bottomAnchor, constant: 16),
            metrics.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 32),
            metrics.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -32),
            metrics.heightAnchor.constraint(equalToConstant: 72),

            progressHeader.topAnchor.constraint(equalTo: metrics.bottomAnchor, constant: 15),
            progressHeader.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 32),
            progressHeader.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -32),

            progressBar.topAnchor.constraint(equalTo: progressHeader.bottomAnchor, constant: 8),
            progressBar.leadingAnchor.constraint(equalTo: progressHeader.leadingAnchor),
            progressBar.trailingAnchor.constraint(equalTo: progressHeader.trailingAnchor),
            progressBar.heightAnchor.constraint(equalToConstant: 8),

            logHeader.topAnchor.constraint(equalTo: progressBar.bottomAnchor, constant: 16),
            logHeader.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 32),
            logHeader.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -32),

            logScroll.topAnchor.constraint(equalTo: logHeader.bottomAnchor, constant: 9),
            logScroll.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 32),
            logScroll.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -32),
            logScroll.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -26),
            logScroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 220),
        ])

        window.makeFirstResponder(channelField)
    }

    private func label(
        _ text: String,
        size: CGFloat,
        weight: NSFont.Weight = .regular,
        color: NSColor = .labelColor
    ) -> NSTextField {
        let field = NSTextField(labelWithString: text)
        field.font = .systemFont(ofSize: size, weight: weight)
        field.textColor = color
        field.maximumNumberOfLines = 2
        field.lineBreakMode = .byWordWrapping
        return field
    }

    private func flexibleSpace() -> NSView {
        let view = NSView()
        view.setContentHuggingPriority(.defaultLow, for: .horizontal)
        return view
    }

    private func metricCard(title: String, value: NSTextField, symbol: String, tint: NSColor) -> NSView {
        let card = NSVisualEffectView()
        card.material = .contentBackground
        card.blendingMode = .withinWindow
        card.state = .active
        card.wantsLayer = true
        card.layer?.cornerRadius = 10
        card.layer?.masksToBounds = true

        let icon = NSImageView()
        icon.image = NSImage(systemSymbolName: symbol, accessibilityDescription: title)?
            .withSymbolConfiguration(NSImage.SymbolConfiguration(pointSize: 14, weight: .semibold))
        icon.contentTintColor = tint
        icon.translatesAutoresizingMaskIntoConstraints = false

        let titleLabel = label(title, size: 12, weight: .medium, color: .secondaryLabelColor)
        let titleRow = NSStackView(views: [icon, titleLabel])
        titleRow.orientation = .horizontal
        titleRow.alignment = .centerY
        titleRow.spacing = 6

        let stack = NSStackView(views: [titleRow, value])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 5
        stack.translatesAutoresizingMaskIntoConstraints = false
        card.addSubview(stack)

        NSLayoutConstraint.activate([
            icon.widthAnchor.constraint(equalToConstant: 16),
            icon.heightAnchor.constraint(equalToConstant: 16),
            stack.leadingAnchor.constraint(equalTo: card.leadingAnchor, constant: 15),
            stack.trailingAnchor.constraint(lessThanOrEqualTo: card.trailingAnchor, constant: -15),
            stack.centerYAnchor.constraint(equalTo: card.centerYAnchor),
        ])
        return card
    }

    private func keychainService(provider: String) -> String {
        "com.youtube-transcript.\(provider)-api-key.v2"
    }

    private func providerDisplayName(_ provider: String) -> String {
        provider == "openai" ? "OpenAI" : "Groq"
    }

    private func providerKeyPrefix(_ provider: String) -> String {
        provider == "openai" ? "sk-" : "gsk_"
    }

    private func providerKeyURL(_ provider: String) -> URL {
        URL(string: provider == "openai"
            ? "https://platform.openai.com/api-keys"
            : "https://console.groq.com/keys")!
    }

    private func loadAPIKey(provider: String) -> String {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: keychainService(provider: provider),
            kSecAttrAccount as String: NSUserName(),
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var result: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &result) == errSecSuccess,
              let data = result as? Data else { return "" }
        return String(data: data, encoding: .utf8) ?? ""
    }

    @discardableResult
    private func saveAPIKey(_ key: String, provider: String) -> OSStatus {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: keychainService(provider: provider),
            kSecAttrAccount as String: NSUserName(),
        ]
        let attributes: [String: Any] = [
            kSecValueData as String: Data(key.utf8),
        ]
        let status = SecItemUpdate(query as CFDictionary, attributes as CFDictionary)
        if status != errSecItemNotFound { return status }
        var item = query
        attributes.forEach { item[$0.key] = $0.value }
        return SecItemAdd(item as CFDictionary, nil)
    }

    private func updateAIFieldState() {
        aiKeyField?.isEnabled = aiEnabledButton?.state == .on
        aiProviderPopup?.isEnabled = aiEnabledButton?.state == .on
        saveAPIKeyButton?.isEnabled = aiEnabledButton?.state == .on
        getAPIKeyButton?.isEnabled = aiEnabledButton?.state == .on
    }

    private func updateAIProviderUI() {
        let name = providerDisplayName(activeAIProvider)
        aiKeyField?.placeholderString = "输入 \(name) API Key（\(providerKeyPrefix(activeAIProvider))…）"
        getAPIKeyButton?.toolTip = "前往 \(name) 官方平台获取 API Key"
    }

    @objc private func aiProviderChanged() {
        let previous = activeAIProvider
        let previousKey = aiKeyField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        apiKeyDrafts[previous] = previousKey
        activeAIProvider = aiProviderPopup.indexOfSelectedItem == 1 ? "openai" : "groq"
        UserDefaults.standard.set(activeAIProvider, forKey: aiProviderDefaultsKey)
        let storedOrDraft = apiKeyDrafts[activeAIProvider] ?? loadAPIKey(provider: activeAIProvider)
        apiKeyDrafts[activeAIProvider] = storedOrDraft
        aiKeyField.stringValue = storedOrDraft
        setAPIKeySavedAppearance(!storedOrDraft.isEmpty)
        updateAIProviderUI()
    }

    func controlTextDidChange(_ notification: Notification) {
        guard let field = notification.object as? NSTextField, field === aiKeyField else { return }
        apiKeyDrafts[activeAIProvider] = field.stringValue
        setAPIKeySavedAppearance(false)
    }

    private func setAPIKeySavedAppearance(_ saved: Bool) {
        saveAPIKeyButton?.title = saved ? "已保存" : "保存 Key"
        saveAPIKeyButton?.contentTintColor = saved ? .systemGreen : .controlTextColor
    }

    private func validAPIKey(_ key: String, provider: String) -> Bool {
        key.hasPrefix(providerKeyPrefix(provider)) && key.count >= 20
    }

    @objc private func saveSelectedAPIKey() {
        let key = aiKeyField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        let providerName = providerDisplayName(activeAIProvider)
        guard validAPIKey(key, provider: activeAIProvider) else {
            showAlert(
                title: "API Key 格式不正确",
                message: "请粘贴完整的、以 \(providerKeyPrefix(activeAIProvider)) 开头的 \(providerName) API Key。"
            )
            window.makeFirstResponder(aiKeyField)
            return
        }
        let status = saveAPIKey(key, provider: activeAIProvider)
        guard status == errSecSuccess else {
            setAPIKeySavedAppearance(false)
            showAlert(
                title: "无法保存 API Key",
                message: "macOS 钥匙串未能保存密钥（错误码 \(status)）。本次仍可使用，但下次打开应用需要重新输入。"
            )
            return
        }
        apiKeyDrafts[activeAIProvider] = key
        setAPIKeySavedAppearance(true)
        appendEvent("\(providerName) API Key 已安全保存到 macOS 钥匙串。", color: .systemGreen, weight: .medium)
    }

    @objc private func openSelectedAPIKeyPage() {
        NSWorkspace.shared.open(providerKeyURL(activeAIProvider))
    }

    @objc private func openGroqAPIKeyPage() {
        NSWorkspace.shared.open(providerKeyURL("groq"))
    }

    @objc private func openOpenAIAPIKeyPage() {
        NSWorkspace.shared.open(providerKeyURL("openai"))
    }

    @objc private func toggleAI() {
        let enabled = aiEnabledButton.state == .on
        UserDefaults.standard.set(enabled, forKey: aiEnabledDefaultsKey)
        updateAIFieldState()
        if enabled && aiKeyField.stringValue.isEmpty {
            window.makeFirstResponder(aiKeyField)
        }
    }

    @objc private func startCapture() {
        guard process?.isRunning != true else { return }

        let pasted = channelField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        let channel = pasted.removingPercentEncoding ?? pasted
        let manualProxy = proxyField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        let aiEnabled = aiEnabledButton.state == .on
        let aiAPIKey = aiKeyField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !channel.isEmpty else {
            showAlert(title: "还没有输入频道", message: "请先粘贴 YouTube 博主主页链接。")
            return
        }
        guard let batchSize = Int(batchField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)), batchSize >= 0 else {
            showAlert(title: "数量格式不正确", message: "请输入 0 或正整数，例如 50、100。")
            return
        }
        let providerName = providerDisplayName(activeAIProvider)
        let keyPrefix = providerKeyPrefix(activeAIProvider)
        if aiEnabled && !validAPIKey(aiAPIKey, provider: activeAIProvider) {
            showAlert(
                title: "需要 \(providerName) API Key",
                message: "要为没有 YouTube 字幕的视频生成 AI 字幕，请输入完整的、以 \(keyPrefix) 开头的 \(providerName) API Key；或者取消勾选 AI 字幕功能。"
            )
            window.makeFirstResponder(aiKeyField)
            return
        }

        guard let backend = resolveBackend() else {
            showAlert(title: "应用文件不完整", message: "应用包中缺少字幕抓取后端，请重新下载应用。")
            return
        }

        var keychainWarning: String?
        do {
            do {
                try verifyArchiveIsWritable()
            } catch {
                showAlert(
                    title: "需要选择档案文件夹",
                    message: "macOS 尚未允许应用写入当前保存地址。请重新选择这个文件夹（已有内容不会丢失），然后再次点击开始抓取。"
                )
                chooseArchiveFolder()
                return
            }
            let channelFile = FileManager.default.temporaryDirectory
                .appendingPathComponent("youtube-caption-channel-\(ProcessInfo.processInfo.processIdentifier).txt")
            try channel.write(to: channelFile, atomically: true, encoding: .utf8)
            currentChannelFile = channelFile

            channelField.stringValue = channel
            if manualProxy.contains("@") {
                UserDefaults.standard.removeObject(forKey: proxyDefaultsKey)
            } else {
                UserDefaults.standard.set(manualProxy, forKey: proxyDefaultsKey)
            }
            UserDefaults.standard.set(aiEnabled, forKey: aiEnabledDefaultsKey)
            UserDefaults.standard.set(activeAIProvider, forKey: aiProviderDefaultsKey)
            if aiEnabled {
                let keychainStatus = saveAPIKey(aiAPIKey, provider: activeAIProvider)
                if keychainStatus != errSecSuccess {
                    keychainWarning = "本次可以正常使用 AI 字幕，但密钥未能保存到钥匙串；下次打开应用时需要重新输入（错误码 \(keychainStatus)）。"
                    setAPIKeySavedAppearance(false)
                } else {
                    apiKeyDrafts[activeAIProvider] = aiAPIKey
                    setAPIKeySavedAppearance(true)
                }
            }
            requestedBatchSize = batchSize
            processedThisRun = 0
            finishedWithWarnings = false
            isPaused = false
            updatePauseButton()
            lastChannelPage = nil
            runStartedAt = Date()
            closeButton.isHidden = true
            processedValueLabel.stringValue = batchSize == 0 ? "0" : "0 / \(batchSize)"
            positionValueLabel.stringValue = "读取中"
            elapsedValueLabel.stringValue = "00:00"
            currentVideoLabel.stringValue = "正在连接 YouTube 并读取频道视频列表……"
            logView.textStorage?.setAttributedString(NSAttributedString())
            appendEvent("开始新任务", color: .systemBlue, weight: .semibold)
            if let keychainWarning {
                appendEvent(keychainWarning, color: .systemOrange, weight: .medium)
            }
            appendEvent("频道：\(channel)")
            appendEvent("本次最多抓取：\(batchSize == 0 ? "全部未归档视频" : "\(batchSize) 条")")
            appendEvent("保存位置：\(archiveRoot.path)", color: .secondaryLabelColor)
            if aiEnabled { appendEvent("AI 字幕服务：\(providerName)", color: .secondaryLabelColor) }
            appendEvent(manualProxy.isEmpty ? "网络代理：自动读取系统设置" : "网络代理：使用手动设置（地址已隐藏）", color: .secondaryLabelColor)
            appendEvent("正在读取频道，请稍候……", color: .secondaryLabelColor)
            startElapsedTimer()
            setRunning(true)
            statusLabel.stringValue = "正在读取频道……"
            statusLabel.textColor = .systemBlue
            progressBar.isIndeterminate = true
            progressBar.startAnimation(nil)

            let task = Process()
            task.executableURL = backend.executable
            var arguments = backend.prefixArguments + [
                "--channel-file", channelFile.path,
                "--output", archiveRoot.path,
                "--batch-size", String(batchSize),
                "--event-format", "jsonl",
                "--open-after",
            ]
            if aiEnabled {
                arguments.append(contentsOf: ["--ai-transcribe-missing", "--ai-provider", activeAIProvider, "--ai-language", "auto"])
            }
            task.arguments = arguments
            task.currentDirectoryURL = FileManager.default.temporaryDirectory

            var environment = ProcessInfo.processInfo.environment
            environment["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + (environment["PATH"] ?? "/usr/bin:/bin")
            environment["LANG"] = environment["LANG"] ?? "en_US.UTF-8"
            environment["LC_ALL"] = environment["LC_ALL"] ?? "en_US.UTF-8"
            environment["PYTHONUNBUFFERED"] = "1"
            environment["PYTHONIOENCODING"] = "utf-8"
            if !manualProxy.isEmpty {
                environment["YCA_PROXY"] = manualProxy
                environment["HTTPS_PROXY"] = manualProxy
                environment["HTTP_PROXY"] = manualProxy
            }
            if aiEnabled {
                environment["YCA_AI_API_KEY"] = aiAPIKey
            }
            task.environment = environment

            let pipe = Pipe()
            task.standardOutput = pipe
            task.standardError = pipe
            task.standardInput = FileHandle.nullDevice

            try task.run()
            process = task
            readOutput(from: pipe.fileHandleForReading, process: task)
        } catch {
            removeCurrentChannelFile()
            stopElapsedTimer()
            setRunning(false)
            statusLabel.stringValue = "启动失败"
            statusLabel.textColor = .systemRed
            closeButton.isHidden = false
            currentVideoLabel.stringValue = "任务未能启动"
            appendEvent("启动失败：\(error.localizedDescription)", color: .systemRed, weight: .semibold)
            showAlert(title: "无法开始抓取", message: error.localizedDescription)
        }
    }

    private func readOutput(from handle: FileHandle, process: Process) {
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            var buffer = Data()
            while true {
                let data = handle.availableData
                if data.isEmpty { break }
                buffer.append(data)
                while let newline = buffer.firstIndex(of: 0x0A) {
                    let lineData = buffer[..<newline]
                    let next = buffer.index(after: newline)
                    buffer.removeSubrange(..<next)
                    let line = String(decoding: lineData, as: UTF8.self).trimmingCharacters(in: .newlines)
                    DispatchQueue.main.async {
                        self?.consumeLine(line)
                    }
                }
            }
            if !buffer.isEmpty {
                let tail = String(decoding: buffer, as: UTF8.self)
                DispatchQueue.main.async { self?.consumeLine(tail) }
            }
            process.waitUntilExit()
            let exitCode = process.terminationStatus
            DispatchQueue.main.async {
                self?.finish(exitCode: exitCode)
            }
        }
    }

    private func consumeLine(_ line: String) {
        let cleaned = line.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !cleaned.isEmpty else { return }

        if consumeJSONEvent(cleaned) {
            return
        }

        let lineColor: NSColor
        if cleaned.contains("ERROR") || cleaned.contains("失败") || cleaned.contains("跳过（稍后可重试）") {
            lineColor = .systemRed
        } else if cleaned.contains("限流") || cleaned.contains("Too Many Requests") {
            lineColor = .systemOrange
        } else if cleaned.hasPrefix("完成：") || cleaned.contains("本批已处理") {
            lineColor = .systemGreen
        } else {
            lineColor = .labelColor
        }
        appendEvent(cleaned, color: lineColor)

        let marker = "正在后台打开字幕页面："
        if let range = cleaned.range(of: marker) {
            lastChannelPage = String(cleaned[range.upperBound...]).trimmingCharacters(in: .whitespacesAndNewlines)
        }

        let pattern = #"\]\s+(\d+)/(\d+)\s+(.*)$"#
        if let regex = try? NSRegularExpression(pattern: pattern),
           let match = regex.firstMatch(in: cleaned, range: NSRange(cleaned.startIndex..., in: cleaned)),
           let positionRange = Range(match.range(at: 1), in: cleaned),
           let totalRange = Range(match.range(at: 2), in: cleaned),
           let titleRange = Range(match.range(at: 3), in: cleaned),
           let position = Int(cleaned[positionRange]),
           let total = Int(cleaned[totalRange]) {
            processedThisRun += 1
            let maximum = requestedBatchSize == 0 ? max(total, 1) : max(requestedBatchSize, 1)
            let videoTitle = String(cleaned[titleRange]).trimmingCharacters(in: .whitespacesAndNewlines)
            progressBar.stopAnimation(nil)
            progressBar.isIndeterminate = false
            progressBar.maxValue = Double(maximum)
            progressBar.doubleValue = Double(min(processedThisRun, maximum))
            statusLabel.stringValue = "正在处理第 \(processedThisRun) 条（频道位置 \(position)/\(total)）"
            processedValueLabel.stringValue = requestedBatchSize == 0
                ? "\(processedThisRun)"
                : "\(processedThisRun) / \(requestedBatchSize)"
            positionValueLabel.stringValue = "\(position) / \(total)"
            currentVideoLabel.stringValue = "正在处理：\(videoTitle)"
        } else if cleaned.contains("检测到 YouTube 临时限流") {
            statusLabel.stringValue = "YouTube 暂时限流，已保存当前进度"
            statusLabel.textColor = .systemOrange
            currentVideoLabel.stringValue = "当前任务因 YouTube 临时限流暂停"
        } else if cleaned.contains("正在后台打开字幕页面") {
            currentVideoLabel.stringValue = "字幕档案已生成，正在打开结果……"
        }
    }

    private func consumeJSONEvent(_ line: String) -> Bool {
        guard let data = line.data(using: .utf8),
              let event = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let kind = event["event"] as? String else {
            return false
        }
        let message = event["message"] as? String ?? kind
        let level = event["level"] as? String ?? "info"
        let color: NSColor = level == "error" ? .systemRed
            : (level == "warning" ? .systemOrange
                : (["archive_completed", "channel_completed", "batch_completed"].contains(kind) ? .systemGreen : .labelColor))
        appendEvent(message, color: color, weight: kind == "video_progress" ? .medium : .regular)

        switch kind {
        case "network_check":
            statusLabel.stringValue = "正在检查 YouTube 连接……"
            statusLabel.textColor = .systemBlue
            currentVideoLabel.stringValue = message
        case "network_ok":
            statusLabel.stringValue = "网络连接正常"
            statusLabel.textColor = .systemGreen
            currentVideoLabel.stringValue = message
        case "network_warning", "network_retry":
            statusLabel.stringValue = "网络不稳定，正在尝试恢复……"
            statusLabel.textColor = .systemOrange
            currentVideoLabel.stringValue = message
        case "retry_queue_loaded":
            statusLabel.stringValue = "正在优先重试上次未完成内容"
            statusLabel.textColor = .systemOrange
            currentVideoLabel.stringValue = message
        case "ai_started":
            statusLabel.stringValue = "正在生成 AI 字幕……"
            statusLabel.textColor = .systemPurple
            currentVideoLabel.stringValue = message
        case "ai_progress":
            statusLabel.stringValue = "AI 字幕处理中……"
            statusLabel.textColor = .systemPurple
            currentVideoLabel.stringValue = message
        case "ai_completed":
            statusLabel.stringValue = "AI 字幕生成完成"
            statusLabel.textColor = .systemGreen
            currentVideoLabel.stringValue = message
        case "ai_key_missing":
            finishedWithWarnings = true
            statusLabel.stringValue = "需要 AI API Key"
            statusLabel.textColor = .systemRed
            currentVideoLabel.stringValue = message
        case "channel_loaded":
            let total = event["total"] as? Int ?? 0
            positionValueLabel.stringValue = total > 0 ? "0 / \(total)" : "—"
            statusLabel.stringValue = "频道列表读取完成"
            statusLabel.textColor = .systemBlue
            currentVideoLabel.stringValue = message
        case "video_progress":
            let position = event["position"] as? Int ?? 0
            let total = event["total"] as? Int ?? 0
            let processed = event["processed"] as? Int ?? (processedThisRun + 1)
            let title = event["title"] as? String ?? ""
            processedThisRun = processed
            let maximum = requestedBatchSize == 0 ? max(total, 1) : max(requestedBatchSize, 1)
            progressBar.stopAnimation(nil)
            progressBar.isIndeterminate = false
            progressBar.maxValue = Double(maximum)
            progressBar.doubleValue = Double(min(processed, maximum))
            processedValueLabel.stringValue = requestedBatchSize == 0 ? "\(processed)" : "\(processed) / \(requestedBatchSize)"
            positionValueLabel.stringValue = "\(position) / \(total)"
            statusLabel.stringValue = "正在处理第 \(processed) 条"
            statusLabel.textColor = .systemBlue
            currentVideoLabel.stringValue = "正在处理：\(title)"
        case "retry_queued":
            finishedWithWarnings = true
            statusLabel.stringValue = "单条失败，已加入待重试队列"
            statusLabel.textColor = .systemOrange
            currentVideoLabel.stringValue = message
        case "rate_limited", "channel_paused", "metadata_paused":
            finishedWithWarnings = true
            statusLabel.stringValue = "YouTube 暂时不可用，当前进度已保存"
            statusLabel.textColor = .systemOrange
            currentVideoLabel.stringValue = message
        case "channel_error":
            finishedWithWarnings = true
            statusLabel.stringValue = "无法连接或读取该频道"
            statusLabel.textColor = .systemRed
            currentVideoLabel.stringValue = message
        case "open_page":
            lastChannelPage = event["page"] as? String
            currentVideoLabel.stringValue = "字幕档案已生成，正在打开结果……"
        case "channel_completed", "archive_completed":
            currentVideoLabel.stringValue = message
        default:
            break
        }
        return true
    }

    private func finish(exitCode: Int32) {
        removeCurrentChannelFile()
        process = nil
        isPaused = false
        updatePauseButton()
        stopElapsedTimer()
        progressBar.stopAnimation(nil)
        progressBar.isIndeterminate = false
        setRunning(false)
        closeButton.isHidden = false

        if exitCode == 0 && !finishedWithWarnings {
            progressBar.maxValue = 100
            progressBar.doubleValue = 100
            statusLabel.stringValue = "任务已完成"
            statusLabel.textColor = .systemGreen
            currentVideoLabel.stringValue = "处理完成：结果已保存，可以打开字幕档案。"
            openButton.isEnabled = true
            appendEvent("任务已完成。结果已保存，可以点击“打开结果”。", color: .systemGreen, weight: .semibold)
        } else if finishedWithWarnings {
            statusLabel.stringValue = "任务已保存，部分内容待重试"
            statusLabel.textColor = .systemOrange
            currentVideoLabel.stringValue = "网络恢复后再次运行同一频道，将自动优先重试失败内容。"
            openButton.isEnabled = FileManager.default.fileExists(atPath: archiveRoot.appendingPathComponent("index.html").path)
            appendEvent("本次任务未全部完成。现有结果和待重试队列均已保存。", color: .systemOrange, weight: .semibold)
        } else {
            statusLabel.stringValue = "任务未完成"
            statusLabel.textColor = .systemRed
            currentVideoLabel.stringValue = "任务异常结束，请查看下方最后几条运行日志。"
            appendEvent("任务异常结束（错误码 \(exitCode)）。请查看上方提示。", color: .systemRed, weight: .semibold)
            NSSound.beep()
        }
    }

    @objc private func stopCapture() {
        guard let process, process.isRunning else { return }
        if isPaused {
            setProcessPaused(false)
        }
        appendEvent("正在停止当前任务……", color: .systemOrange)
        process.terminate()
        statusLabel.stringValue = "正在停止……"
        statusLabel.textColor = .systemOrange
        currentVideoLabel.stringValue = "正在安全停止，已完成的视频会保留。"
        stopButton.isEnabled = false
    }

    @objc private func togglePause() {
        guard let process, process.isRunning else { return }
        setProcessPaused(!isPaused)
        if isPaused {
            statusLabel.stringValue = "任务已暂停"
            statusLabel.textColor = .systemOrange
            currentVideoLabel.stringValue = "抓取已暂停；点击“继续”可从当前位置恢复。"
            appendEvent("任务已暂停。", color: .systemOrange, weight: .semibold)
        } else {
            statusLabel.stringValue = "正在继续抓取……"
            statusLabel.textColor = .systemBlue
            currentVideoLabel.stringValue = "任务已恢复，正在继续处理当前频道。"
            appendEvent("任务已继续。", color: .systemBlue, weight: .semibold)
        }
    }

    @objc private func closeApplication() {
        NSApp.terminate(nil)
    }

    @objc private func configureNetworkProxy() {
        let alert = NSAlert()
        alert.alertStyle = .informational
        alert.messageText = "网络代理"
        alert.informativeText = "通常保持留空，应用会自动读取 macOS 系统代理。只有需要手动指定时才填写，例如 http://127.0.0.1:7890。"
        alert.addButton(withTitle: "保存")
        alert.addButton(withTitle: "取消")
        alert.addButton(withTitle: "恢复自动读取")

        let field = NSTextField(string: proxyField.stringValue)
        field.placeholderString = "留空则自动读取系统设置"
        field.font = .monospacedSystemFont(ofSize: 13, weight: .regular)
        field.frame = NSRect(x: 0, y: 0, width: 420, height: 28)
        alert.accessoryView = field

        alert.beginSheetModal(for: window) { [weak self] response in
            guard let self else { return }
            if response == .alertThirdButtonReturn {
                self.proxyField.stringValue = ""
                UserDefaults.standard.removeObject(forKey: self.proxyDefaultsKey)
                self.appendEvent("网络代理已恢复为自动读取系统设置。", color: .systemBlue)
                return
            }
            guard response == .alertFirstButtonReturn else { return }
            let value = field.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
            self.proxyField.stringValue = value
            if value.contains("@") {
                UserDefaults.standard.removeObject(forKey: self.proxyDefaultsKey)
            } else {
                UserDefaults.standard.set(value, forKey: self.proxyDefaultsKey)
            }
            self.appendEvent(
                value.isEmpty ? "网络代理：自动读取系统设置。" : "网络代理：已保存手动设置（地址已隐藏）。",
                color: .systemBlue
            )
        }
    }

    @objc private func chooseArchiveFolder() {
        let panel = NSOpenPanel()
        panel.title = "选择字幕学习档案的保存文件夹"
        panel.prompt = "选择文件夹"
        panel.message = "已有档案不会被删除；选择原来的文件夹可继续增量更新。"
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.canCreateDirectories = true
        panel.allowsMultipleSelection = false
        panel.directoryURL = archiveRoot
        panel.beginSheetModal(for: window) { [weak self] response in
            guard response == .OK, let selected = panel.url, let self else { return }
            self.archiveRoot = selected
            UserDefaults.standard.set(selected.path, forKey: self.archivePathDefaultsKey)
            self.saveArchiveBookmark(for: selected)
            self.archivePathField.stringValue = selected.path
            self.lastChannelPage = nil
            self.openButton.isEnabled = FileManager.default.fileExists(
                atPath: selected.appendingPathComponent("index.html").path
            )
            self.appendEvent("保存地址已改为：\(selected.path)", color: .systemBlue)
        }
    }

    @objc private func openResults() {
        guard FileManager.default.fileExists(atPath: archiveRoot.appendingPathComponent("index.html").path) else {
            showAlert(title: "还没有结果", message: "请先完成一次抓取。")
            return
        }
        guard let backend = resolveBackend() else {
            showAlert(title: "无法打开结果", message: "应用包中缺少字幕抓取后端。")
            return
        }

        let task = Process()
        task.executableURL = backend.executable
        task.arguments = backend.prefixArguments + [
            "--open",
            "--output", archiveRoot.path,
            "--page", lastChannelPage ?? "index.html",
        ]
        task.currentDirectoryURL = FileManager.default.temporaryDirectory
        var environment = ProcessInfo.processInfo.environment
        environment.removeValue(forKey: "GROQ_API_KEY")
        environment.removeValue(forKey: "OPENAI_API_KEY")
        environment.removeValue(forKey: "YCA_AI_API_KEY")
        environment["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + (environment["PATH"] ?? "/usr/bin:/bin")
        task.environment = environment
        task.standardInput = FileHandle.nullDevice
        task.standardOutput = FileHandle.nullDevice
        task.standardError = FileHandle.nullDevice
        do {
            try task.run()
        } catch {
            showAlert(title: "无法打开结果", message: error.localizedDescription)
        }
    }

    private func findPython() -> String? {
        let candidates = ["/usr/bin/python3", "/opt/homebrew/bin/python3", "/usr/local/bin/python3"]
        return candidates.first { FileManager.default.isExecutableFile(atPath: $0) }
    }

    private func removeCurrentChannelFile() {
        guard let currentChannelFile else { return }
        try? FileManager.default.removeItem(at: currentChannelFile)
        self.currentChannelFile = nil
    }

    private func resolveBackend() -> (executable: URL, prefixArguments: [String])? {
        if let bundled = Bundle.main.url(forAuxiliaryExecutable: "youtube-caption-backend"),
           FileManager.default.isExecutableFile(atPath: bundled.path) {
            return (bundled, [])
        }
        guard let script = Bundle.main.url(forResource: "youtube_caption", withExtension: "py"),
              let python = findPython() else {
            return nil
        }
        return (URL(fileURLWithPath: python), ["-u", script.path])
    }

    private func setRunning(_ running: Bool) {
        startButton.isEnabled = !running
        pauseButton.isEnabled = running
        stopButton.isEnabled = running
        channelField.isEnabled = !running
        batchField.isEnabled = !running
        proxyField.isEnabled = !running
        aiKeyField.isEnabled = !running && aiEnabledButton.state == .on
        aiProviderPopup.isEnabled = !running && aiEnabledButton.state == .on
        saveAPIKeyButton.isEnabled = !running && aiEnabledButton.state == .on
        getAPIKeyButton.isEnabled = !running && aiEnabledButton.state == .on
        aiEnabledButton.isEnabled = !running
        chooseArchiveButton.isEnabled = !running
        if running { openButton.isEnabled = false }
    }

    private func setProcessPaused(_ paused: Bool) {
        guard let process, process.isRunning else { return }
        let rootPID = process.processIdentifier
        if paused {
            _ = Darwin.kill(rootPID, SIGSTOP)
            for pid in descendantPIDs(of: rootPID) {
                _ = Darwin.kill(pid, SIGSTOP)
            }
        } else {
            for pid in descendantPIDs(of: rootPID).reversed() {
                _ = Darwin.kill(pid, SIGCONT)
            }
            _ = Darwin.kill(rootPID, SIGCONT)
        }
        isPaused = paused
        updatePauseButton()
    }

    private func descendantPIDs(of parentPID: pid_t) -> [pid_t] {
        let lookup = Process()
        let output = Pipe()
        lookup.executableURL = URL(fileURLWithPath: "/usr/bin/pgrep")
        lookup.arguments = ["-P", String(parentPID)]
        lookup.standardOutput = output
        lookup.standardError = FileHandle.nullDevice
        do {
            try lookup.run()
            lookup.waitUntilExit()
        } catch {
            return []
        }

        let data = output.fileHandleForReading.readDataToEndOfFile()
        let directChildren = String(decoding: data, as: UTF8.self)
            .split(whereSeparator: \.isNewline)
            .compactMap { pid_t($0) }
        return directChildren + directChildren.flatMap { descendantPIDs(of: $0) }
    }

    private func updatePauseButton() {
        guard pauseButton != nil else { return }
        pauseButton.title = isPaused ? "继续" : "暂停"
        pauseButton.image = NSImage(
            systemSymbolName: isPaused ? "play.fill" : "pause.fill",
            accessibilityDescription: nil
        )
    }

    private func startElapsedTimer() {
        elapsedTimer?.invalidate()
        let timer = Timer(timeInterval: 1, repeats: true) { [weak self] _ in
            self?.updateElapsedTime()
        }
        elapsedTimer = timer
        RunLoop.main.add(timer, forMode: .common)
        updateElapsedTime()
    }

    private func stopElapsedTimer() {
        elapsedTimer?.invalidate()
        elapsedTimer = nil
        updateElapsedTime()
    }

    private func updateElapsedTime() {
        guard let started = runStartedAt else {
            elapsedValueLabel?.stringValue = "00:00"
            return
        }
        let totalSeconds = max(0, Int(Date().timeIntervalSince(started)))
        let hours = totalSeconds / 3600
        let minutes = (totalSeconds % 3600) / 60
        let seconds = totalSeconds % 60
        elapsedValueLabel.stringValue = hours > 0
            ? String(format: "%d:%02d:%02d", hours, minutes, seconds)
            : String(format: "%02d:%02d", minutes, seconds)
    }

    private func appendEvent(_ message: String, color: NSColor = .labelColor, weight: NSFont.Weight = .regular) {
        guard let storage = logView.textStorage else { return }
        let paragraph = NSMutableParagraphStyle()
        paragraph.alignment = .left
        paragraph.lineBreakMode = .byWordWrapping
        let prefix = "[\(timestampFormatter.string(from: Date()))]  "
        storage.append(NSAttributedString(
            string: prefix,
            attributes: [
                .font: NSFont.monospacedSystemFont(ofSize: 12, weight: .regular),
                .foregroundColor: NSColor.tertiaryLabelColor,
                .paragraphStyle: paragraph,
            ]
        ))
        storage.append(NSAttributedString(
            string: message + "\n",
            attributes: [
                .font: NSFont.monospacedSystemFont(ofSize: 12.5, weight: weight),
                .foregroundColor: color,
                .paragraphStyle: paragraph,
            ]
        ))
        logView.layoutManager?.ensureLayout(for: logView.textContainer!)
        if let scrollView = logView.enclosingScrollView {
            let clipView = scrollView.contentView
            let bottom = max(0, logView.frame.height - clipView.bounds.height)
            clipView.scroll(to: NSPoint(x: 0, y: bottom))
            scrollView.reflectScrolledClipView(clipView)
        }
    }

    private func showAlert(title: String, message: String) {
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = title
        alert.informativeText = message
        alert.addButton(withTitle: "好")
        alert.beginSheetModal(for: window)
    }
}

let app = NSApplication.shared
let delegate = CaptionAppDelegate()
app.delegate = delegate
app.run()
