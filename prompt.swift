#!/usr/bin/env swift
import AppKit
import Foundation

struct PromptAction {
    let title: String
    let key: String
    let result: String
    let confirmation: String?
}

struct ReviewPayload: Decodable {
    let name: String
    let path: String
    let closeOnUnfocus: Bool?
    let mode: String?
    let detail: String?
    let snoozeHours: Double?
    let keepDays: Double?
    let policy: String?
    let copyText: String?
    let pending: Int?
    let headline: String?
}

final class ActionButton: NSButton {
    var result = ""
}

final class MaintenanceApp: NSObject, NSApplicationDelegate, NSWindowDelegate {
    let window = NSWindow(
        contentRect: NSRect(x: 0, y: 0, width: 640, height: 400),
        styleMask: [.titled, .closable],
        backing: .buffered,
        defer: false
    )
    let sessionMode: Bool
    var itemName: String
    var itemPath: String
    var mode = "app"
    var detailText = ""
    var policy = "review-only"
    var copyText = ""
    var headlineText = ""
    var pendingApprovals = 0
    var closeOnUnfocus = false
    var canCloseOnUnfocus = false
    var snoozeHours: Double = 720
    var keepDays: Double = 60
    var deleteEnabled = true
    var statusItem: NSStatusItem?
    var feedbackLabel: NSTextField?
    var didFinish = false
    var waitingForNext = false
    var keyMonitor: Any?

    init(sessionMode: Bool, name: String = "", path: String = "") {
        self.sessionMode = sessionMode
        itemName = name
        itemPath = path
        super.init()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        window.title = "Idle Maintenance"
        window.center()
        window.isReleasedWhenClosed = false
        window.level = .floating
        window.delegate = self
        setupMenuBar()
        installKeyMonitor()
        if sessionMode {
            showLoadingWindow()
            startSessionReader()
        } else {
            setupWindow()
        }
        NSApp.activate(ignoringOtherApps: true)
    }

    func duration(hours: Double) -> String {
        if hours >= 24, hours.truncatingRemainder(dividingBy: 24) == 0 {
            let days = Int(hours / 24)
            return days == 1 ? "1 day" : "\(days) days"
        }
        let value = Int(hours.rounded())
        return value == 1 ? "1 hour" : "\(value) hours"
    }

    func dayDuration(_ days: Double) -> String {
        let value = Int(days.rounded())
        return value == 1 ? "1 day" : "\(value) days"
    }

    func actions() -> [PromptAction] {
        if mode != "process" {
            return [
                PromptAction(title: "S. Snooze \(duration(hours: snoozeHours))", key: "s", result: "SNOOZE", confirmation: nil),
                PromptAction(title: "T. Move to Trash", key: "t", result: "DELETE", confirmation: "Move this application to Trash?"),
                PromptAction(title: "O. Open", key: "o", result: "TRY", confirmation: nil),
                PromptAction(title: "K. Keep \(dayDuration(keepDays))", key: "k", result: "KEEP", confirmation: nil),
            ]
        }
        if policy == "graceful-quit" {
            return [
                PromptAction(title: "S. Snooze \(duration(hours: snoozeHours))", key: "s", result: "SNOOZE", confirmation: nil),
                PromptAction(title: "Q. Quit App", key: "q", result: "GRACEFUL_QUIT", confirmation: "Quit this app gracefully?"),
                PromptAction(title: "C. Copy Prompt", key: "c", result: "COPY", confirmation: nil),
                PromptAction(title: "I. Investigate", key: "i", result: "INVESTIGATE", confirmation: nil),
                PromptAction(title: "L. Leave \(dayDuration(keepDays))", key: "l", result: "KEEP", confirmation: nil),
            ]
        }
        return [
            PromptAction(title: "S. Snooze \(duration(hours: snoozeHours))", key: "s", result: "SNOOZE", confirmation: nil),
            PromptAction(title: "C. Copy Prompt", key: "c", result: "COPY", confirmation: nil),
            PromptAction(title: "I. Investigate", key: "i", result: "INVESTIGATE", confirmation: nil),
            PromptAction(title: "L. Leave \(dayDuration(keepDays))", key: "l", result: "KEEP", confirmation: nil),
        ]
    }

    func setupMenuBar() {
        let mainMenu = NSMenu()
        let appMenuItem = NSMenuItem()
        mainMenu.addItem(appMenuItem)
        let appMenu = NSMenu(title: "Idle Maintenance")
        appMenu.addItem(withTitle: "About Idle Maintenance", action: nil, keyEquivalent: "")
        appMenu.addItem(NSMenuItem.separator())
        let closeItem = appMenu.addItem(withTitle: "Close Review", action: #selector(onClose), keyEquivalent: "w")
        closeItem.target = self
        appMenuItem.submenu = appMenu
        NSApp.mainMenu = mainMenu

        if statusItem == nil {
            let status = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
            status.button?.title = "🛠"
            statusItem = status
        }
        let menu = NSMenu(title: "Idle Maintenance")
        let showItem = menu.addItem(withTitle: "Show Review", action: #selector(showWindow), keyEquivalent: "")
        showItem.target = self
        menu.addItem(NSMenuItem.separator())
        for action in actions() {
            let item = menu.addItem(withTitle: action.title, action: #selector(onMenuAction(_:)), keyEquivalent: action.key)
            item.target = self
            item.representedObject = action.result
            if action.result == "DELETE" { item.isEnabled = deleteEnabled }
            if action.result == "COPY" { item.isEnabled = !copyText.isEmpty }
        }
        menu.addItem(NSMenuItem.separator())
        let closeReview = menu.addItem(withTitle: "Close Review", action: #selector(onClose), keyEquivalent: "")
        closeReview.target = self
        statusItem?.menu = menu
    }

    func installKeyMonitor() {
        guard keyMonitor == nil else { return }
        keyMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { event in
            guard !self.waitingForNext,
                  let key = event.charactersIgnoringModifiers?.lowercased() else { return event }
            if key == "\u{1b}" { self.onClose(); return nil }
            if let action = self.actions().first(where: { $0.key == key }) {
                self.perform(action)
                return nil
            }
            return event
        }
    }

    @objc func showWindow() {
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func showLoadingWindow() {
        let content = NSView(frame: window.contentRect(forFrameRect: window.frame))
        let label = NSTextField(labelWithString: "Loading maintenance review…")
        label.font = .systemFont(ofSize: 15, weight: .medium)
        label.alignment = .center
        label.frame = NSRect(x: 24, y: 185, width: 592, height: 24)
        content.addSubview(label)
        window.contentView = content
        window.makeKeyAndOrderFront(nil)
    }

    func setupWindow() {
        let content = NSView(frame: window.contentRect(forFrameRect: window.frame))
        window.contentView = content

        let icon = NSWorkspace.shared.icon(forFile: itemPath)
        icon.size = NSSize(width: 56, height: 56)
        let image = NSImageView(frame: NSRect(x: 24, y: 300, width: 56, height: 56))
        image.image = icon
        content.addSubview(image)

        let heading = mode == "process" ? "Review sustained resource use" : "Review stale application"
        let section = NSTextField(labelWithString: heading)
        section.font = .boldSystemFont(ofSize: 13)
        section.frame = NSRect(x: 96, y: 337, width: 330, height: 20)
        content.addSubview(section)

        if pendingApprovals > 0 {
            let pending = NSTextField(labelWithString: "Approvals pending: \(pendingApprovals)")
            pending.font = .systemFont(ofSize: 11, weight: .medium)
            pending.textColor = .secondaryLabelColor
            pending.alignment = .right
            pending.frame = NSRect(x: 420, y: 337, width: 196, height: 20)
            content.addSubview(pending)
        }

        let name = NSTextField(wrappingLabelWithString: itemName)
        name.font = mode == "process" ? .monospacedSystemFont(ofSize: 17, weight: .medium) : .systemFont(ofSize: 18, weight: .medium)
        name.frame = NSRect(x: 96, y: 307, width: 520, height: 28)
        content.addSubview(name)

        let path = NSTextField(wrappingLabelWithString: itemPath)
        path.font = mode == "process" ? .monospacedSystemFont(ofSize: 10, weight: .regular) : .systemFont(ofSize: 10)
        path.textColor = .secondaryLabelColor
        path.frame = NSRect(x: 96, y: 271, width: 520, height: 34)
        path.isSelectable = true
        content.addSubview(path)

        var detailTop: CGFloat = 255
        if mode == "process", !headlineText.isEmpty {
            let headline = NSTextField(wrappingLabelWithString: headlineText)
            headline.font = .boldSystemFont(ofSize: 16)
            headline.alignment = .center
            headline.frame = NSRect(x: 24, y: 229, width: 592, height: 26)
            content.addSubview(headline)
            detailTop = 222
        }

        let detail = NSTextField(wrappingLabelWithString: detailText)
        detail.font = .systemFont(ofSize: 11)
        detail.textColor = .secondaryLabelColor
        detail.alignment = .center
        detail.isSelectable = true
        detail.frame = NSRect(x: 24, y: 121, width: 592, height: max(54, detailTop - 121))
        content.addSubview(detail)

        let feedback = NSTextField(labelWithString: "")
        feedback.font = .systemFont(ofSize: 11, weight: .medium)
        feedback.alignment = .center
        feedback.frame = NSRect(x: 24, y: 92, width: 592, height: 18)
        content.addSubview(feedback)
        feedbackLabel = feedback

        let helpText = mode == "process"
            ? "Use a highlighted letter; C copies without closing; Escape ends this review run."
            : "Use a highlighted letter; Escape ends this review run."
        let help = NSTextField(labelWithString: helpText)
        help.font = .systemFont(ofSize: 11)
        help.alignment = .center
        help.frame = NSRect(x: 24, y: 70, width: 592, height: 18)
        content.addSubview(help)

        let values = actions()
        let spacing: CGFloat = 8
        let totalWidth: CGFloat = 592
        let width = (totalWidth - spacing * CGFloat(values.count - 1)) / CGFloat(values.count)
        for (index, action) in values.enumerated() {
            let button = ActionButton(title: action.title, target: self, action: #selector(onButton(_:)))
            button.frame = NSRect(x: 24 + CGFloat(index) * (width + spacing), y: 25, width: width, height: 34)
            button.result = action.result
            if action.result == "DELETE" { button.isEnabled = deleteEnabled }
            if action.result == "COPY" { button.isEnabled = !copyText.isEmpty }
            content.addSubview(button)
        }

        waitingForNext = false
        canCloseOnUnfocus = false
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        DispatchQueue.main.asyncAfter(deadline: .now() + 1) {
            self.canCloseOnUnfocus = true
        }
    }

    func apply(_ payload: ReviewPayload) {
        guard !didFinish else { return }
        itemName = payload.name
        itemPath = payload.path
        closeOnUnfocus = payload.closeOnUnfocus ?? false
        mode = payload.mode ?? "app"
        detailText = payload.detail ?? ""
        snoozeHours = payload.snoozeHours ?? (mode == "process" ? 24 : 720)
        keepDays = payload.keepDays ?? (mode == "process" ? 1 : 60)
        policy = payload.policy ?? "review-only"
        copyText = payload.copyText ?? ""
        pendingApprovals = max(0, payload.pending ?? 0)
        headlineText = payload.headline ?? ""
        deleteEnabled = !detailText.contains("delete disabled")
        setupMenuBar()
        setupWindow()
    }

    func startSessionReader() {
        DispatchQueue.global(qos: .userInitiated).async {
            while let line = readLine(strippingNewline: true) {
                if line.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { continue }
                guard let data = line.data(using: .utf8) else { continue }
                do {
                    let payload = try JSONDecoder().decode(ReviewPayload.self, from: data)
                    DispatchQueue.main.async {
                        self.apply(payload)
                    }
                } catch {
                    continue
                }
            }
            DispatchQueue.main.async {
                if !self.didFinish {
                    self.didFinish = true
                    NSApp.terminate(nil)
                }
            }
        }
    }

    func copyInvestigationPrompt() {
        guard !copyText.isEmpty else {
            feedbackLabel?.stringValue = "No investigation prompt is available."
            return
        }
        let pasteboard = NSPasteboard.general
        pasteboard.clearContents()
        if pasteboard.setString(copyText, forType: .string) {
            feedbackLabel?.stringValue = "Copied the Investigate prompt."
        } else {
            feedbackLabel?.stringValue = "Could not copy the Investigate prompt."
        }
    }

    func perform(_ action: PromptAction) {
        guard !waitingForNext else { return }
        if action.result == "DELETE" && !deleteEnabled { return }
        if action.result == "COPY" {
            copyInvestigationPrompt()
            return
        }
        if let confirmation = action.confirmation {
            let alert = NSAlert()
            alert.alertStyle = action.result == "DELETE" ? .critical : .warning
            alert.messageText = confirmation
            alert.informativeText = action.result == "DELETE"
                ? "The application will be moved to Trash and recorded in the deletion ledger."
                : "The process identity will be revalidated before a graceful quit signal is sent."
            alert.addButton(withTitle: "Cancel")
            alert.addButton(withTitle: action.result == "DELETE" ? "Move to Trash" : "Quit App")
            guard alert.runModal() == .alertSecondButtonReturn else { return }
        }
        finish(action.result)
    }

    @objc func onButton(_ sender: ActionButton) {
        guard let action = actions().first(where: { $0.result == sender.result }) else { return }
        perform(action)
    }

    @objc func onMenuAction(_ sender: NSMenuItem) {
        guard let result = sender.representedObject as? String,
              let action = actions().first(where: { $0.result == result }) else { return }
        perform(action)
    }

    @objc func onClose() { finish("QUIT") }

    func emit(_ result: String) {
        guard let data = "\(result)\n".data(using: .utf8) else { return }
        FileHandle.standardOutput.write(data)
    }

    func finish(_ result: String) {
        guard !didFinish else { return }
        if sessionMode && result != "QUIT" {
            waitingForNext = true
            feedbackLabel?.stringValue = "Applying…"
            emit(result)
            return
        }
        didFinish = true
        emit(result)
        NSApp.terminate(nil)
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        finish("QUIT")
        return false
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        if !didFinish {
            didFinish = true
            emit("QUIT")
        }
        return true
    }

    func windowDidResignKey(_ notification: Notification) {
        if canCloseOnUnfocus && closeOnUnfocus { finish("QUIT") }
    }
}

let args = CommandLine.arguments
ProcessInfo.processInfo.processName = "Idle Maintenance"
UserDefaults.standard.set("Idle Maintenance", forKey: "CFBundleName")
UserDefaults.standard.set("Idle Maintenance", forKey: "CFBundleExecutable")

if args.count >= 2, args[1] == "--session" {
    let delegate = MaintenanceApp(sessionMode: true)
    let app = NSApplication.shared
    app.delegate = delegate
    app.setActivationPolicy(.regular)
    app.run()
} else {
    guard args.count >= 4, !args[1].isEmpty, !args[2].isEmpty else { exit(0) }
    let delegate = MaintenanceApp(sessionMode: false, name: args[1], path: args[2])
    delegate.closeOnUnfocus = args[3] == "true"
    if args.count >= 8, args[4] == "app" || args[4] == "process" {
        delegate.mode = args[4]
        delegate.detailText = args[5]
        delegate.snoozeHours = Double(args[6]) ?? (delegate.mode == "process" ? 24 : 720)
        delegate.keepDays = Double(args[7]) ?? (delegate.mode == "process" ? 1 : 60)
        if args.count >= 9 { delegate.policy = args[8] }
        if args.count >= 10 { delegate.copyText = args[9] }
        if args.count >= 11 { delegate.pendingApprovals = max(0, Int(args[10]) ?? 0) }
        if args.count >= 12 { delegate.headlineText = args[11] }
    } else if args.count > 4 {
        delegate.detailText = args[4]
    }
    delegate.deleteEnabled = !delegate.detailText.contains("delete disabled")

    let app = NSApplication.shared
    app.delegate = delegate
    app.setActivationPolicy(.regular)
    app.run()
}
