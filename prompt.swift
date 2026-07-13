#!/usr/bin/env swift
import AppKit

final class MaintenanceApp: NSObject, NSApplicationDelegate, NSWindowDelegate {
    let windowWidth: CGFloat = 640
    let windowHeight: CGFloat = 360
    let window = NSWindow(
        contentRect: NSRect(x: 0, y: 0, width: 640, height: 360),
        styleMask: [.titled, .closable],
        backing: .buffered,
        defer: false
    )

    let appName: String
    let appPath: String
    var closeOnUnfocus = false
    var canCloseOnUnfocus = false
    var mode = "app"
    var detailText = ""
    var snoozeHours: Double = 720
    var keepDays: Double = 60
    var deleteEnabled = true
    var statusItem: NSStatusItem?
    var didFinish = false

    init(name: String, path: String) {
        appName = name
        appPath = path
        super.init()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        setupMenu()
        setupStatusItem()
        setupWindow()
        NSApp.activate(ignoringOtherApps: true)
        DispatchQueue.main.asyncAfter(deadline: .now() + 1) {
            self.canCloseOnUnfocus = true
        }
    }

    func setupMenu() {
        let mainMenu = NSMenu()
        let appMenuItem = NSMenuItem()
        mainMenu.addItem(appMenuItem)
        let appMenu = NSMenu(title: "Idle Maintenance")
        appMenu.addItem(withTitle: "About Idle Maintenance", action: nil, keyEquivalent: "")
        appMenu.addItem(NSMenuItem.separator())
        appMenu.addItem(withTitle: "Quit Idle Maintenance", action: #selector(onQuit), keyEquivalent: "q")
        appMenuItem.submenu = appMenu
        NSApp.mainMenu = mainMenu
    }

    func duration(hours: Double) -> String {
        if hours >= 24, hours.truncatingRemainder(dividingBy: 24) == 0 {
            let days = Int(hours / 24)
            return days == 1 ? "1 day" : "\(days) days"
        }
        let rounded = Int(hours.rounded())
        return rounded == 1 ? "1 hour" : "\(rounded) hours"
    }

    func dayDuration(_ days: Double) -> String {
        if days.rounded() == days {
            let value = Int(days)
            return value == 1 ? "1 day" : "\(value) days"
        }
        return String(format: "%.1f days", days)
    }

    var snoozeTitle: String { "S. Snooze \(duration(hours: snoozeHours))" }
    var destructiveTitle: String { mode == "process" ? "K. Kill" : "T. Move to Trash" }
    var inspectTitle: String { mode == "process" ? "I. Investigate" : "O. Open" }
    var keepTitle: String {
        mode == "process"
            ? "L. Leave \(dayDuration(keepDays))"
            : "K. Keep \(dayDuration(keepDays))"
    }

    func setupStatusItem() {
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        item.button?.title = "🛠"
        item.button?.toolTip = "Idle Maintenance"

        let menu = NSMenu(title: "Idle Maintenance")
        menu.addItem(withTitle: "Show Review", action: #selector(showWindow), keyEquivalent: "")
        menu.addItem(NSMenuItem.separator())
        menu.addItem(withTitle: snoozeTitle, action: #selector(onSnooze), keyEquivalent: "s")
        let destructive = menu.addItem(
            withTitle: destructiveTitle,
            action: #selector(onDestructive),
            keyEquivalent: mode == "process" ? "k" : "t"
        )
        destructive.isEnabled = mode == "process" || deleteEnabled
        menu.addItem(
            withTitle: inspectTitle,
            action: #selector(onInspect),
            keyEquivalent: mode == "process" ? "i" : "o"
        )
        menu.addItem(
            withTitle: keepTitle,
            action: #selector(onKeep),
            keyEquivalent: mode == "process" ? "l" : "k"
        )
        menu.addItem(NSMenuItem.separator())
        menu.addItem(withTitle: "Quit", action: #selector(onQuit), keyEquivalent: "q")
        item.menu = menu
        statusItem = item
    }

    @objc func showWindow() {
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func cpuValue() -> Double? {
        guard mode == "process" else { return nil }
        let pattern = #"CPU samples: ([0-9]+(?:\.[0-9]+)?)%"#
        guard let range = detailText.range(of: pattern, options: .regularExpression) else {
            return nil
        }
        let match = String(detailText[range])
        let number = match
            .replacingOccurrences(of: "CPU samples: ", with: "")
            .replacingOccurrences(of: "%", with: "")
        return Double(number)
    }

    func cpuColor(_ cpu: Double) -> NSColor {
        if cpu >= 200 { return .systemPurple }
        if cpu >= 100 { return .systemRed }
        if cpu >= 50 { return .systemOrange }
        return .systemYellow
    }

    func setupWindow() {
        window.title = "Idle Maintenance"
        window.center()
        window.isReleasedWhenClosed = false
        window.level = .floating
        window.delegate = self

        let content = NSView(frame: window.contentRect(forFrameRect: window.frame))
        window.contentView = content

        let icon = NSWorkspace.shared.icon(forFile: appPath)
        icon.size = NSSize(width: 56, height: 56)
        let imageView = NSImageView(frame: NSRect(x: 24, y: 274, width: 56, height: 56))
        imageView.image = icon
        content.addSubview(imageView)

        let section = NSTextField(labelWithString: mode == "process" ? "Review sustained high-impact process" : "Review stale application")
        section.font = .boldSystemFont(ofSize: 13)
        section.frame = NSRect(x: 96, y: 308, width: 520, height: 20)
        content.addSubview(section)

        let name = NSTextField(wrappingLabelWithString: appName)
        name.font = mode == "process"
            ? .monospacedSystemFont(ofSize: 17, weight: .medium)
            : .systemFont(ofSize: 18, weight: .medium)
        name.frame = NSRect(x: 96, y: 278, width: 520, height: 28)
        content.addSubview(name)

        let path = NSTextField(wrappingLabelWithString: appPath)
        path.font = mode == "process"
            ? .monospacedSystemFont(ofSize: 10, weight: .regular)
            : .systemFont(ofSize: 10)
        path.textColor = .secondaryLabelColor
        path.frame = NSRect(x: 96, y: 245, width: 520, height: 32)
        path.isSelectable = true
        content.addSubview(path)

        var detailTop: CGFloat = 224
        if let cpu = cpuValue() {
            let cpuLabel = NSTextField(labelWithString: String(format: "CPU %.1f%%", cpu))
            cpuLabel.font = .boldSystemFont(ofSize: 22)
            cpuLabel.textColor = cpuColor(cpu)
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

        let help = NSTextField(labelWithString: "Use the highlighted letter; Escape quits without acting.")
        help.font = .systemFont(ofSize: 11)
        help.alignment = .center
        help.frame = NSRect(x: 24, y: 79, width: 592, height: 18)
        content.addSubview(help)

        let buttonWidth: CGFloat = 142
        let buttonHeight: CGFloat = 34
        let spacing: CGFloat = 8
        let startX: CGFloat = 24
        let titles = [snoozeTitle, destructiveTitle, inspectTitle, keepTitle]
        let actions = [#selector(onSnooze), #selector(onDestructive), #selector(onInspect), #selector(onKeep)]
        for index in 0..<4 {
            let button = NSButton(title: titles[index], target: self, action: actions[index])
            button.frame = NSRect(
                x: startX + CGFloat(index) * (buttonWidth + spacing),
                y: 30,
                width: buttonWidth,
                height: buttonHeight
            )
            if index == 1 { button.isEnabled = mode == "process" || deleteEnabled }
            content.addSubview(button)
        }

        NSEvent.addLocalMonitorForEvents(matching: .keyDown) { event in
            switch event.charactersIgnoringModifiers?.lowercased() {
            case "s": self.onSnooze(); return nil
            case self.mode == "process" ? "k" : "t": self.onDestructive(); return nil
            case self.mode == "process" ? "i" : "o": self.onInspect(); return nil
            case self.mode == "process" ? "l" : "k": self.onKeep(); return nil
            case "\u{1b}": self.onQuit(); return nil
            default: return event
            }
        }

        window.makeKeyAndOrderFront(nil)
    }

    func confirmDestructiveAction() -> Bool {
        let alert = NSAlert()
        alert.alertStyle = .critical
        if mode == "process" {
            alert.messageText = "Kill this process?"
            alert.informativeText = "This will terminate \(appName) and may discard unsaved work."
        } else {
            alert.messageText = "Move this application to Trash?"
            alert.informativeText = "\(appName) will be moved to Trash and recorded in the deletion ledger."
        }
        alert.addButton(withTitle: "Cancel")
        alert.addButton(withTitle: mode == "process" ? "Kill Process" : "Move to Trash")
        if alert.buttons.count > 1 { alert.buttons[1].hasDestructiveAction = true }
        return alert.runModal() == .alertSecondButtonReturn
    }

    @objc func onSnooze() { finish("SNOOZE") }
    @objc func onDestructive() {
        guard mode == "process" || deleteEnabled else { return }
        if confirmDestructiveAction() { finish(mode == "process" ? "KILL" : "DELETE") }
    }
    @objc func onInspect() { finish(mode == "process" ? "INVESTIGATE" : "TRY") }
    @objc func onKeep() { finish("KEEP") }
    @objc func onQuit() { finish("QUIT") }

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
} else if args.count > 5, args[4].hasPrefix("__MODE__=") {
    delegate.mode = String(args[4].dropFirst("__MODE__=".count))
    delegate.detailText = args[5]
} else if args.count > 4 {
    delegate.detailText = args[4]
}
delegate.deleteEnabled = !delegate.detailText.contains("delete disabled")

let app = NSApplication.shared
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
