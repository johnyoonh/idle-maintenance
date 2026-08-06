#!/usr/bin/env swift
import AppKit

struct PromptAction {
    let title: String
    let key: String
    let result: String
    let confirmation: String?
}

final class ActionButton: NSButton {
    var result = ""
}

final class MaintenanceApp: NSObject, NSApplicationDelegate, NSWindowDelegate {
    let window = NSWindow(
        contentRect: NSRect(x: 0, y: 0, width: 640, height: 360),
        styleMask: [.titled, .closable],
        backing: .buffered,
        defer: false
    )
    let itemName: String
    let itemPath: String
    var mode = "app"
    var detailText = ""
    var policy = "review-only"
    var closeOnUnfocus = false
    var canCloseOnUnfocus = false
    var snoozeHours: Double = 720
    var keepDays: Double = 60
    var deleteEnabled = true
    var statusItem: NSStatusItem?
    var didFinish = false

    init(name: String, path: String) {
        itemName = name
        itemPath = path
        super.init()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        setupMenuBar()
        setupWindow()
        NSApp.activate(ignoringOtherApps: true)
        DispatchQueue.main.asyncAfter(deadline: .now() + 1) {
            self.canCloseOnUnfocus = true
        }
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
                PromptAction(title: "I. Investigate", key: "i", result: "INVESTIGATE", confirmation: nil),
                PromptAction(title: "L. Leave \(dayDuration(keepDays))", key: "l", result: "KEEP", confirmation: nil),
            ]
        }
        return [
            PromptAction(title: "S. Snooze \(duration(hours: snoozeHours))", key: "s", result: "SNOOZE", confirmation: nil),
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
        appMenu.addItem(withTitle: "Close Review", action: #selector(onClose), keyEquivalent: "w")
        appMenuItem.submenu = appMenu
        NSApp.mainMenu = mainMenu

        let status = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        status.button?.title = "🛠"
        let menu = NSMenu(title: "Idle Maintenance")
        menu.addItem(withTitle: "Show Review", action: #selector(showWindow), keyEquivalent: "")
        menu.addItem(NSMenuItem.separator())
        for action in actions() {
            let item = menu.addItem(withTitle: action.title, action: #selector(onMenuAction(_:)), keyEquivalent: action.key)
            item.representedObject = action.result
            if action.result == "DELETE" { item.isEnabled = deleteEnabled }
        }
        menu.addItem(NSMenuItem.separator())
        menu.addItem(withTitle: "Close Review", action: #selector(onClose), keyEquivalent: "")
        status.menu = menu
        statusItem = status
    }

    @objc func showWindow() {
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func cpuValue() -> Double? {
        guard mode == "process" else { return nil }
        let pattern = #"CPU samples: ([0-9]+(?:\.[0-9]+)?)%"#
        guard let range = detailText.range(of: pattern, options: .regularExpression) else { return nil }
        return Double(String(detailText[range]).replacingOccurrences(of: "CPU samples: ", with: "").replacingOccurrences(of: "%", with: ""))
    }

    func setupWindow() {
        window.title = "Idle Maintenance"
        window.center()
        window.isReleasedWhenClosed = false
        window.level = .floating
        window.delegate = self

        let content = NSView(frame: window.contentRect(forFrameRect: window.frame))
        window.contentView = content

        let icon = NSWorkspace.shared.icon(forFile: itemPath)
        icon.size = NSSize(width: 56, height: 56)
        let image = NSImageView(frame: NSRect(x: 24, y: 274, width: 56, height: 56))
        image.image = icon
        content.addSubview(image)

        let heading = mode == "process" ? "Review sustained resource use" : "Review stale application"
        let section = NSTextField(labelWithString: heading)
        section.font = .boldSystemFont(ofSize: 13)
        section.frame = NSRect(x: 96, y: 308, width: 520, height: 20)
        content.addSubview(section)

        let name = NSTextField(wrappingLabelWithString: itemName)
        name.font = mode == "process" ? .monospacedSystemFont(ofSize: 17, weight: .medium) : .systemFont(ofSize: 18, weight: .medium)
        name.frame = NSRect(x: 96, y: 278, width: 520, height: 28)
        content.addSubview(name)

        let path = NSTextField(wrappingLabelWithString: itemPath)
        path.font = mode == "process" ? .monospacedSystemFont(ofSize: 10, weight: .regular) : .systemFont(ofSize: 10)
        path.textColor = .secondaryLabelColor
        path.frame = NSRect(x: 96, y: 245, width: 520, height: 32)
        path.isSelectable = true
        content.addSubview(path)

        var detailTop: CGFloat = 224
        if let cpu = cpuValue() {
            let cpuLabel = NSTextField(labelWithString: String(format: "CPU %.1f%%", cpu))
            cpuLabel.font = .boldSystemFont(ofSize: 22)
            cpuLabel.frame = NSRect(x: 24, y: 210, width: 592, height: 30)
            cpuLabel.alignment = .center
            content.addSubview(cpuLabel)
            detailTop = 198
        }

        let detail = NSTextField(wrappingLabelWithString: detailText)
        detail.font = .systemFont(ofSize: 11)
        detail.textColor = .secondaryLabelColor
        detail.alignment = .center
        detail.isSelectable = true
        detail.frame = NSRect(x: 24, y: 104, width: 592, height: detailTop - 104)
        content.addSubview(detail)

        let help = NSTextField(labelWithString: "Use a highlighted letter; Escape closes without acting.")
        help.font = .systemFont(ofSize: 11)
        help.alignment = .center
        help.frame = NSRect(x: 24, y: 79, width: 592, height: 18)
        content.addSubview(help)

        let values = actions()
        let spacing: CGFloat = 8
        let totalWidth: CGFloat = 592
        let width = (totalWidth - spacing * CGFloat(values.count - 1)) / CGFloat(values.count)
        for (index, action) in values.enumerated() {
            let button = ActionButton(title: action.title, target: self, action: #selector(onButton(_:)))
            button.frame = NSRect(x: 24 + CGFloat(index) * (width + spacing), y: 30, width: width, height: 34)
            button.result = action.result
            if action.result == "DELETE" { button.isEnabled = deleteEnabled }
            content.addSubview(button)
        }

        NSEvent.addLocalMonitorForEvents(matching: .keyDown) { event in
            guard let key = event.charactersIgnoringModifiers?.lowercased() else { return event }
            if key == "\u{1b}" { self.onClose(); return nil }
            if let action = self.actions().first(where: { $0.key == key }) {
                self.perform(action)
                return nil
            }
            return event
        }
        window.makeKeyAndOrderFront(nil)
    }

    func perform(_ action: PromptAction) {
        if action.result == "DELETE" && !deleteEnabled { return }
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

    func finish(_ result: String) {
        guard !didFinish else { return }
        didFinish = true
        print(result)
        NSApp.terminate(nil)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        if !didFinish { print("QUIT") }
        return true
    }

    func windowDidResignKey(_ notification: Notification) {
        if canCloseOnUnfocus && closeOnUnfocus { finish("QUIT") }
    }
}

let args = CommandLine.arguments
guard args.count >= 4, !args[1].isEmpty, !args[2].isEmpty else { exit(0) }
ProcessInfo.processInfo.processName = "Idle Maintenance"
UserDefaults.standard.set("Idle Maintenance", forKey: "CFBundleName")
UserDefaults.standard.set("Idle Maintenance", forKey: "CFBundleExecutable")

let delegate = MaintenanceApp(name: args[1], path: args[2])
delegate.closeOnUnfocus = args[3] == "true"
if args.count >= 8, args[4] == "app" || args[4] == "process" {
    delegate.mode = args[4]
    delegate.detailText = args[5]
    delegate.snoozeHours = Double(args[6]) ?? (delegate.mode == "process" ? 24 : 720)
    delegate.keepDays = Double(args[7]) ?? (delegate.mode == "process" ? 1 : 60)
    if args.count >= 9 { delegate.policy = args[8] }
} else if args.count > 4 {
    delegate.detailText = args[4]
}
delegate.deleteEnabled = !delegate.detailText.contains("delete disabled")

let app = NSApplication.shared
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
