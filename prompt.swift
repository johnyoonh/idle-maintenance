#!/usr/bin/env swift
import AppKit

class MaintenanceApp: NSObject, NSApplicationDelegate, NSWindowDelegate {
    let windowWidth: CGFloat = 500
    let windowHeight: CGFloat = 280
    let window = NSWindow(
        contentRect: NSRect(x: 0, y: 0, width: 500, height: 280),
        styleMask: [.titled, .closable],
        backing: .buffered, defer: false
    )
    
    var appName: String = ""
    var appPath: String = ""
    var canCloseOnUnfocus: Bool = false
    var mode: String = "app"
    var detailText: String = ""
    var statusItem: NSStatusItem?
    var deleteEnabled: Bool = true
    
    init(name: String, path: String) {
        self.appName = name
        self.appPath = path
        super.init()
    }
    
    func applicationDidFinishLaunching(_ notification: Notification) {
        setupMenu()
        setupStatusItem()
        setupWindow()
        NSApp.activate(ignoringOtherApps: true)
        
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) {
            self.canCloseOnUnfocus = true
        }
    }
    
    func setupMenu() {
        let mainMenu = NSMenu()
        let appMenuItem = NSMenuItem()
        mainMenu.addItem(appMenuItem)
        
        let appMenu = NSMenu(title: "Idle Maintenance")
        appMenu.addItem(withTitle: "About Idle Maintenance v1.1", action: nil, keyEquivalent: "")
        appMenu.addItem(NSMenuItem.separator())
        appMenu.addItem(withTitle: "Quit Idle Maintenance", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appMenuItem.submenu = appMenu
        
        NSApp.mainMenu = mainMenu
    }

    func setupStatusItem() {
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        if let button = item.button {
            button.title = "🛠"
            button.toolTip = "Idle Maintenance"
        }

        let menu = NSMenu(title: "Idle Maintenance")
        menu.addItem(withTitle: "Show Idle Maintenance", action: #selector(showWindow), keyEquivalent: "")
        menu.addItem(NSMenuItem.separator())
        let keepItem = menu.addItem(withTitle: "1. Snooze", action: #selector(onKeep), keyEquivalent: "1")
        keepItem.toolTip = tooltipFor(action: "SNOOZE")

        let deleteItem = menu.addItem(withTitle: mode == "process" ? "2. Kill" : "2. Delete", action: #selector(onDelete), keyEquivalent: "2")
        deleteItem.toolTip = tooltipFor(action: mode == "process" ? "KILL" : "DELETE")
        deleteItem.isEnabled = mode == "process" || deleteEnabled

        let tryItem = menu.addItem(withTitle: mode == "process" ? "3. Investigate" : "3. Try", action: #selector(onTry), keyEquivalent: "3")
        tryItem.toolTip = tooltipFor(action: mode == "process" ? "INVESTIGATE" : "TRY")

        let skipItem = menu.addItem(withTitle: "4. Whitelist", action: #selector(onSkip), keyEquivalent: "4")
        skipItem.toolTip = tooltipFor(action: "WHITELIST")
        menu.addItem(NSMenuItem.separator())
        menu.addItem(withTitle: "Quit", action: #selector(onQuit), keyEquivalent: "q")
        item.menu = menu
        statusItem = item
    }

    func tooltipFor(action: String) -> String {
        switch action {
        case "SNOOZE":
            return mode == "process"
                ? "Leave this process running and ask again later."
                : "Leave this app installed and ask again later."
        case "KEEP":
            return mode == "process"
                ? "Ignore this process for now and wait longer before asking again."
                : "Keep this app and wait longer before asking about it again."
        case "DELETE":
            if !deleteEnabled {
                return "Delete is disabled because this app has no configured restore source."
            }
            return "Move this app to Trash and record it in the deletion ledger."
        case "KILL":
            return "Terminate the selected high-impact process."
        case "TRY":
            return "Open the app so you can review it before deciding."
        case "INVESTIGATE":
            return "Open a Codex investigation prompt for this process in a terminal tab."
        case "WHITELIST":
            return mode == "process"
                ? "Leave this process alone and suppress future prompts for the backoff window."
                : "Keep this app and suppress future prompts for the backoff window."
        case "SKIP":
            return "Leave this app installed and ask again later."
        default:
            return ""
        }
    }

    @objc func showWindow() {
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func processCPUValue() -> Double? {
        guard mode == "process" else { return nil }
        let pattern = #"CPU ([0-9]+(?:\.[0-9]+)?)%"#
        guard let range = detailText.range(of: pattern, options: .regularExpression) else {
            return nil
        }
        let match = String(detailText[range])
        let numberText = match
            .replacingOccurrences(of: "CPU ", with: "")
            .replacingOccurrences(of: "%", with: "")
        return Double(numberText)
    }

    func processDetailWithoutCPU() -> String {
        guard mode == "process" else { return detailText }
        var text = detailText
        if let range = text.range(of: #" ?• ?CPU [0-9]+(?:\.[0-9]+)?% ?"#, options: .regularExpression) {
            text.removeSubrange(range)
        } else if let range = text.range(of: #"CPU [0-9]+(?:\.[0-9]+)?% ?"#, options: .regularExpression) {
            text.removeSubrange(range)
        }
        return text.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    func cpuSeverityColor(_ cpu: Double) -> NSColor {
        if cpu >= 200 {
            return .systemPurple
        }
        if cpu >= 100 {
            return .systemRed
        }
        if cpu >= 50 {
            return .systemOrange
        }
        return .systemYellow
    }
    
    func setupWindow() {
        window.title = "Idle Maintenance"
        window.center()
        window.isReleasedWhenClosed = false
        window.level = .floating
        window.delegate = self
        
        let contentView = NSView(frame: window.contentRect(forFrameRect: window.frame))
        window.contentView = contentView
        
        // --- APP ICON ---
        let icon = NSWorkspace.shared.icon(forFile: appPath)
        icon.size = NSSize(width: 64, height: 64)
        let imageView = NSImageView(frame: NSRect(x: (windowWidth - 64) / 2, y: 190, width: 64, height: 64))
        imageView.image = icon
        contentView.addSubview(imageView)
        
        // --- TEXT LABELS ---
        let sectionTitle = (mode == "process") ? "Review High-Impact Process:" : "Cleanup Unused App:"
        let titleLabel = NSTextField(labelWithString: sectionTitle)
        titleLabel.font = NSFont.boldSystemFont(ofSize: 13)
        titleLabel.frame = NSRect(x: 20, y: 165, width: windowWidth - 40, height: 20)
        titleLabel.alignment = .center
        contentView.addSubview(titleLabel)
        
        let nameLabel = NSTextField(wrappingLabelWithString: appName)
        if mode == "process" {
            nameLabel.font = NSFont.monospacedSystemFont(ofSize: 17, weight: .medium)
        } else {
            nameLabel.font = NSFont.systemFont(ofSize: 18)
        }
        nameLabel.frame = NSRect(x: 20, y: 140, width: windowWidth - 40, height: 25)
        nameLabel.alignment = .center
        contentView.addSubview(nameLabel)
        
        let pathLabel = NSTextField(wrappingLabelWithString: appPath)
        if mode == "process" {
            pathLabel.font = NSFont.monospacedSystemFont(ofSize: 10, weight: .regular)
        } else {
            pathLabel.font = NSFont.systemFont(ofSize: 10)
        }
        pathLabel.textColor = .secondaryLabelColor
        pathLabel.frame = NSRect(x: 20, y: 112, width: windowWidth - 40, height: 24)
        pathLabel.alignment = .center
        contentView.addSubview(pathLabel)

        let cpuValue = processCPUValue()
        if let cpu = cpuValue {
            let cpuLabel = NSTextField(labelWithString: String(format: "CPU %.1f%%", cpu))
            cpuLabel.font = NSFont.boldSystemFont(ofSize: 24)
            cpuLabel.textColor = cpuSeverityColor(cpu)
            cpuLabel.frame = NSRect(x: 20, y: 82, width: windowWidth - 40, height: 30)
            cpuLabel.alignment = .center
            contentView.addSubview(cpuLabel)
        }
        
        var infoText = ""
        if !detailText.isEmpty {
            if mode == "process" {
                infoText = processDetailWithoutCPU()
            } else {
                infoText = "Date info: " + detailText
            }
        }
        
        let dateLabel = NSTextField(labelWithString: infoText)
        dateLabel.font = NSFont.systemFont(ofSize: 10)
        dateLabel.textColor = .selectedControlColor
        dateLabel.frame = NSRect(x: 20, y: cpuValue == nil ? 95 : 68, width: windowWidth - 40, height: 15)
        dateLabel.alignment = .center
        contentView.addSubview(dateLabel)
        
        let helpLabel = NSTextField(labelWithString: "Press a number to act immediately:")
        helpLabel.font = NSFont.systemFont(ofSize: 12)
        helpLabel.frame = NSRect(x: 20, y: cpuValue == nil ? 75 : 50, width: windowWidth - 40, height: 20)
        helpLabel.alignment = .center
        contentView.addSubview(helpLabel)
        
        // --- BUTTONS ---
        let buttonWidth: CGFloat = 110
        let buttonHeight: CGFloat = 32
        let spacing: CGFloat = 5
        let totalWidth = (buttonWidth * 4) + (spacing * 3)
        let startX = (windowWidth - totalWidth) / 2
        let buttonY: CGFloat = cpuValue == nil ? 50 : 16
        
        let action1 = "Snooze"
        let btn1 = NSButton(title: "1. " + action1, target: self, action: #selector(onKeep))
        btn1.frame = NSRect(x: startX, y: buttonY, width: buttonWidth, height: buttonHeight)
        btn1.toolTip = tooltipFor(action: "SNOOZE")
        contentView.addSubview(btn1)
        
        let action2 = (mode == "process") ? "Kill" : "Delete"
        let btn2 = NSButton(title: "2. " + action2, target: self, action: #selector(onDelete))
        btn2.frame = NSRect(x: startX + buttonWidth + spacing, y: buttonY, width: buttonWidth, height: buttonHeight)
        btn2.toolTip = tooltipFor(action: mode == "process" ? "KILL" : "DELETE")
        btn2.isEnabled = mode == "process" || deleteEnabled
        contentView.addSubview(btn2)
        
        let action3 = (mode == "process") ? "Investigate" : "Try"
        let btn3 = NSButton(title: "3. " + action3, target: self, action: #selector(onTry))
        btn3.frame = NSRect(x: startX + (buttonWidth + spacing) * 2, y: buttonY, width: buttonWidth, height: buttonHeight)
        btn3.toolTip = tooltipFor(action: mode == "process" ? "INVESTIGATE" : "TRY")
        contentView.addSubview(btn3)
        
        let action4 = "Whitelist"
        let btn4 = NSButton(title: "4. " + action4, target: self, action: #selector(onSkip))
        btn4.frame = NSRect(x: startX + (buttonWidth + spacing) * 3, y: buttonY, width: buttonWidth, height: buttonHeight)
        btn4.toolTip = tooltipFor(action: "WHITELIST")
        contentView.addSubview(btn4)
        
        // --- KEY LISTENERS ---
        NSEvent.addLocalMonitorForEvents(matching: .keyDown) { event in
            switch event.characters {
            case "1": self.finish("SNOOZE")
            case "2":
                if self.mode == "process" || self.deleteEnabled {
                    self.finish(self.mode == "process" ? "KILL" : "DELETE")
                }
            case "3": self.finish(self.mode == "process" ? "INVESTIGATE" : "TRY")
            case "4": self.finish("WHITELIST")
            case "\u{1B}": self.finish("QUIT")
            default: break
            }
            return event
        }
        
        window.makeKeyAndOrderFront(nil)
    }
    
    @objc func onKeep() { finish("SNOOZE") }
    @objc func onDelete() {
        if mode == "process" || deleteEnabled {
            finish(mode == "process" ? "KILL" : "DELETE")
        }
    }
    @objc func onTry() { finish(mode == "process" ? "INVESTIGATE" : "TRY") }
    @objc func onSkip() { finish("WHITELIST") }
    @objc func onQuit() { finish("QUIT") }
    
    func finish(_ result: String) {
        print(result)
        NSApp.terminate(nil)
    }
    
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        print("QUIT")
        return true
    }
    
    func windowDidResignKey(_ notification: Notification) {
        if canCloseOnUnfocus && CommandLine.arguments.count > 3 && CommandLine.arguments[3] == "true" {
            finish("QUIT")
        }
    }
}

let args = CommandLine.arguments
if args.count < 3 { exit(0) }
if args[1].isEmpty || args[2].isEmpty { exit(0) }

ProcessInfo.processInfo.processName = "Idle Maintenance"
UserDefaults.standard.set("Idle Maintenance", forKey: "CFBundleName")
UserDefaults.standard.set("Idle Maintenance", forKey: "CFBundleExecutable")

let app = NSApplication.shared
let delegate = MaintenanceApp(name: args[1], path: args[2])
if args.count > 5 && args[4].hasPrefix("__MODE__=") {
    delegate.mode = String(args[4].dropFirst("__MODE__=".count))
    delegate.detailText = args[5]
} else if args.count > 4 {
    delegate.detailText = args[4]
}
delegate.deleteEnabled = !delegate.detailText.contains("delete disabled")
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
