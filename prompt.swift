#!/usr/bin/env swift
import AppKit

class MaintenanceApp: NSObject, NSApplicationDelegate, NSWindowDelegate {
    let window = NSWindow(
        contentRect: NSRect(x: 0, y: 0, width: 450, height: 280),
        styleMask: [.titled, .closable],
        backing: .buffered, defer: false
    )
    
    var appName: String = ""
    var appPath: String = ""
    var canCloseOnUnfocus: Bool = false
    
    init(name: String, path: String) {
        self.appName = name
        self.appPath = path
        super.init()
    }
    
    func applicationDidFinishLaunching(_ notification: Notification) {
        setupMenu()
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
        let imageView = NSImageView(frame: NSRect(x: (450-64)/2, y: 190, width: 64, height: 64))
        imageView.image = icon
        contentView.addSubview(imageView)
        
        // --- TEXT LABELS ---
        let titleLabel = NSTextField(labelWithString: "Cleanup Unused App:")
        titleLabel.font = NSFont.boldSystemFont(ofSize: 13)
        titleLabel.frame = NSRect(x: 20, y: 165, width: 410, height: 20)
        titleLabel.alignment = .center
        contentView.addSubview(titleLabel)
        
        let nameLabel = NSTextField(labelWithString: appName)
        nameLabel.font = NSFont.systemFont(ofSize: 18)
        nameLabel.frame = NSRect(x: 20, y: 140, width: 410, height: 25)
        nameLabel.alignment = .center
        contentView.addSubview(nameLabel)
        
        let pathLabel = NSTextField(labelWithString: appPath)
        pathLabel.font = NSFont.systemFont(ofSize: 10)
        pathLabel.textColor = .secondaryLabelColor
        pathLabel.frame = NSRect(x: 20, y: 120, width: 410, height: 15)
        pathLabel.alignment = .center
        contentView.addSubview(pathLabel)
        
        var lastUsedText = ""
        if CommandLine.arguments.count > 4 {
             lastUsedText = "Date info: " + CommandLine.arguments[4]
        }
        
        let dateLabel = NSTextField(labelWithString: lastUsedText)
        dateLabel.font = NSFont.systemFont(ofSize: 10)
        dateLabel.textColor = .selectedControlColor
        dateLabel.frame = NSRect(x: 20, y: 105, width: 410, height: 15)
        dateLabel.alignment = .center
        contentView.addSubview(dateLabel)
        
        let helpLabel = NSTextField(labelWithString: "Press a number to act immediately:")
        helpLabel.font = NSFont.systemFont(ofSize: 12)
        helpLabel.frame = NSRect(x: 20, y: 90, width: 410, height: 20)
        helpLabel.alignment = .center
        contentView.addSubview(helpLabel)
        
        // --- BUTTONS ---
        let buttonWidth: CGFloat = 100
        let buttonHeight: CGFloat = 32
        let spacing: CGFloat = 5
        let totalWidth = (buttonWidth * 4) + (spacing * 3)
        let startX = (450 - totalWidth) / 2
        
        let btn1 = NSButton(title: "1. Keep", target: self, action: #selector(onKeep))
        btn1.frame = NSRect(x: startX, y: 50, width: buttonWidth, height: buttonHeight)
        contentView.addSubview(btn1)
        
        let btn2 = NSButton(title: "2. Delete", target: self, action: #selector(onDelete))
        btn2.frame = NSRect(x: startX + buttonWidth + spacing, y: 50, width: buttonWidth, height: buttonHeight)
        contentView.addSubview(btn2)
        
        let btn3 = NSButton(title: "3. Try", target: self, action: #selector(onTry))
        btn3.frame = NSRect(x: startX + (buttonWidth + spacing) * 2, y: 50, width: buttonWidth, height: buttonHeight)
        contentView.addSubview(btn3)
        
        let btn4 = NSButton(title: "4. Skip", target: self, action: #selector(onSkip))
        btn4.frame = NSRect(x: startX + (buttonWidth + spacing) * 3, y: 50, width: buttonWidth, height: buttonHeight)
        contentView.addSubview(btn4)
        
        // --- KEY LISTENERS ---
        NSEvent.addLocalMonitorForEvents(matching: .keyDown) { event in
            switch event.characters {
            case "1": self.finish("KEEP")
            case "2": self.finish("DELETE")
            case "3": self.finish("TRY")
            case "4": self.finish("SKIP")
            case "\u{1B}": self.finish("QUIT")
            default: break
            }
            return event
        }
        
        window.makeKeyAndOrderFront(nil)
    }
    
    @objc func onKeep() { finish("KEEP") }
    @objc func onDelete() { finish("DELETE") }
    @objc func onTry() { finish("TRY") }
    @objc func onSkip() { finish("SKIP") }
    
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

ProcessInfo.processInfo.processName = "Idle Maintenance"
UserDefaults.standard.set("Idle Maintenance", forKey: "CFBundleName")
UserDefaults.standard.set("Idle Maintenance", forKey: "CFBundleExecutable")

let app = NSApplication.shared
let delegate = MaintenanceApp(name: args[1], path: args[2])
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
