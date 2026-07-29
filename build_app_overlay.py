#!/usr/bin/env python3
"""Apply the focused menu-bar UI overlay to the legacy app builder."""
from __future__ import annotations

import argparse
from pathlib import Path

OLD_STATUS_BUTTON = '''        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.button?.title = "🛠"
        item.button?.toolTip = "Idle Maintenance"'''

NEW_STATUS_BUTTON = '''        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        if let symbol = NSImage(
            systemSymbolName: "gauge.with.dots.needle.67percent",
            accessibilityDescription: "Idle Maintenance"
        ) {
            symbol.isTemplate = true
            item.button?.image = symbol
            item.button?.imagePosition = .imageOnly
        } else {
            item.button?.title = "IM"
        }
        item.button?.toolTip = "Idle Maintenance"'''

OLD_MENU = '''        menu.addItem(withTitle: "Review Sustained High CPU (1 min)", action: #selector(reviewHighCpuApps), keyEquivalent: "h")
        menu.addItem(withTitle: "Review Keyboard Shortcuts", action: #selector(reviewKeyboardShortcuts), keyEquivalent: "k")
        menu.addItem(withTitle: "Run Next Maintenance Prompt", action: #selector(runMaintenanceReview), keyEquivalent: "m")
        menu.addItem(withTitle: "Open Activity Monitor", action: #selector(openActivityMonitor), keyEquivalent: "a")
        menu.addItem(NSMenuItem.separator())
        menu.addItem(withTitle: "Start / Restart Legacy Watcher", action: #selector(restartWatcher), keyEquivalent: "r")
        menu.addItem(withTitle: "Open Logs", action: #selector(openLogs), keyEquivalent: "l")'''

NEW_MENU = '''        func addAction(_ title: String, symbolName: String, action: Selector, key: String) {
            let entry = menu.addItem(withTitle: title, action: action, keyEquivalent: key)
            if let symbol = NSImage(systemSymbolName: symbolName, accessibilityDescription: title) {
                symbol.isTemplate = true
                entry.image = symbol
            }
        }

        let resourceHeader = NSMenuItem(title: "RESOURCE ACTIVITY", action: nil, keyEquivalent: "")
        resourceHeader.isEnabled = false
        resourceHeader.attributedTitle = NSAttributedString(
            string: "RESOURCE ACTIVITY",
            attributes: [
                .font: NSFont.systemFont(ofSize: 10, weight: .semibold),
                .foregroundColor: NSColor.secondaryLabelColor,
            ]
        )
        menu.addItem(resourceHeader)
        addAction("Review Recent I/O Incidents…", symbolName: "externaldrive.badge.exclamationmark", action: #selector(showMaintenanceStatus), key: "i")
        addAction("Sample CPU + Disk I/O (1 min)", symbolName: "waveform.path.ecg", action: #selector(reviewHighCpuApps), key: "h")
        addAction("Open Activity Monitor", symbolName: "gauge.with.dots.needle.50percent", action: #selector(openActivityMonitor), key: "a")
        menu.addItem(NSMenuItem.separator())

        let shortcutHeader = NSMenuItem(title: "SHORTCUT REVIEW", action: nil, keyEquivalent: "")
        shortcutHeader.isEnabled = false
        shortcutHeader.attributedTitle = NSAttributedString(
            string: "SHORTCUT REVIEW",
            attributes: [
                .font: NSFont.systemFont(ofSize: 10, weight: .semibold),
                .foregroundColor: NSColor.secondaryLabelColor,
            ]
        )
        menu.addItem(shortcutHeader)
        addAction("Refresh & Review Shortcuts", symbolName: "keyboard.badge.ellipsis", action: #selector(reviewKeyboardShortcuts), key: "k")
        addAction("Run Next Maintenance Prompt", symbolName: "sparkles", action: #selector(runMaintenanceReview), key: "m")
        menu.addItem(NSMenuItem.separator())
        addAction("Start / Restart Away-Return Review", symbolName: "figure.walk.arrival", action: #selector(restartWatcher), key: "r")
        addAction("Open Logs", symbolName: "doc.text.magnifyingglass", action: #selector(openLogs), key: "l")'''

OLD_SHORTCUT_ACTION = '''    @objc func reviewKeyboardShortcuts() {
        runDetached(
            executable: "/bin/zsh",
            arguments: [
                "-lc",
                "$HOME/.local/bin/kb popup --surface gui --group auto --force"
            ]
        )
    }'''

NEW_SHORTCUT_ACTION = '''    @objc func reviewKeyboardShortcuts() {
        runDetached(
            executable: "/usr/bin/python3",
            arguments: [
                maintenanceDir.appendingPathComponent("maint.py").path,
                "shortcuts"
            ]
        )
    }'''

OLD_STATUS_WINDOW = '''    func makeStatusWindow() -> NSWindow {
        if let existing = statusWindow {
            return existing
        }

        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 760, height: 560),
            styleMask: [.titled, .closable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = "Idle Maintenance Status"
        window.center()
        window.isReleasedWhenClosed = false

        let content = NSView(frame: window.contentRect(forFrameRect: window.frame))
        window.contentView = content

        let scroll = NSScrollView(frame: NSRect(x: 20, y: 60, width: 720, height: 480))
        scroll.hasVerticalScroller = true
        scroll.autoresizingMask = [.width, .height]
        let textView = NSTextView(frame: scroll.bounds)
        textView.isEditable = false
        textView.isSelectable = true
        textView.font = NSFont.monospacedSystemFont(ofSize: 12, weight: .regular)
        textView.textContainerInset = NSSize(width: 10, height: 10)
        scroll.documentView = textView
        content.addSubview(scroll)

        let refresh = NSButton(title: "Refresh", target: self, action: #selector(refreshMaintenanceStatus))
        refresh.frame = NSRect(x: 20, y: 16, width: 100, height: 32)
        content.addSubview(refresh)

        let logs = NSButton(title: "Open Logs", target: self, action: #selector(openLogs))
        logs.frame = NSRect(x: 130, y: 16, width: 100, height: 32)
        content.addSubview(logs)

        statusWindow = window
        statusTextView = textView
        return window
    }'''

NEW_STATUS_WINDOW = '''    func styledStatusText(_ text: String) -> NSAttributedString {
        let result = NSMutableAttributedString(string: "")
        let paragraph = NSMutableParagraphStyle()
        paragraph.lineSpacing = 3
        paragraph.paragraphSpacing = 2

        let lines = text.split(separator: "\\n", omittingEmptySubsequences: false)
        for (index, rawLine) in lines.enumerated() {
            let line = String(rawLine)
            var attributes: [NSAttributedString.Key: Any] = [
                .font: NSFont.monospacedSystemFont(ofSize: 12, weight: .regular),
                .foregroundColor: NSColor.labelColor,
                .paragraphStyle: paragraph,
            ]

            if line == "Idle maintenance status" {
                attributes[.font] = NSFont.systemFont(ofSize: 20, weight: .semibold)
                attributes[.foregroundColor] = NSColor.controlAccentColor
            } else if line.hasSuffix(":") && !line.hasPrefix("-") {
                attributes[.font] = NSFont.systemFont(ofSize: 13, weight: .semibold)
                attributes[.foregroundColor] = NSColor.secondaryLabelColor
            } else if line.hasPrefix("- Incident") {
                attributes[.font] = NSFont.monospacedSystemFont(ofSize: 11.5, weight: .medium)
                attributes[.foregroundColor] = NSColor.systemBlue
            } else {
                let normalized = line.lowercased()
                if normalized.contains("healthy") || normalized.contains("available now") || normalized.contains("running") {
                    attributes[.foregroundColor] = NSColor.systemGreen
                } else if normalized.contains("stale") || normalized.contains("degraded") || normalized.contains("needs attention") || normalized.contains("unavailable") {
                    attributes[.foregroundColor] = NSColor.systemOrange
                }
            }

            result.append(NSAttributedString(string: line, attributes: attributes))
            if index < lines.count - 1 {
                result.append(NSAttributedString(string: "\\n", attributes: attributes))
            }
        }
        return result
    }

    func makeStatusWindow() -> NSWindow {
        if let existing = statusWindow {
            return existing
        }

        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 820, height: 640),
            styleMask: [.titled, .closable, .resizable, .fullSizeContentView],
            backing: .buffered,
            defer: false
        )
        window.title = "Idle Maintenance"
        window.titleVisibility = .hidden
        window.titlebarAppearsTransparent = true
        window.isMovableByWindowBackground = true
        window.minSize = NSSize(width: 680, height: 500)
        window.center()
        window.isReleasedWhenClosed = false

        let content = NSVisualEffectView(frame: window.contentRect(forFrameRect: window.frame))
        content.material = .underWindowBackground
        content.blendingMode = .behindWindow
        content.state = .active
        window.contentView = content

        let header = NSVisualEffectView(frame: NSRect(x: 20, y: 530, width: 780, height: 84))
        header.material = .hudWindow
        header.blendingMode = .withinWindow
        header.state = .active
        header.wantsLayer = true
        header.layer?.cornerRadius = 14
        header.layer?.masksToBounds = true
        header.autoresizingMask = [.width, .minYMargin]
        content.addSubview(header)

        let icon = NSImageView(frame: NSRect(x: 22, y: 18, width: 48, height: 48))
        if let image = NSImage(systemSymbolName: "gauge.with.dots.needle.67percent", accessibilityDescription: "Idle Maintenance") {
            icon.image = image.withSymbolConfiguration(.init(pointSize: 36, weight: .medium))
            icon.contentTintColor = .controlAccentColor
        }
        header.addSubview(icon)

        let title = NSTextField(labelWithString: "Idle Maintenance")
        title.font = .systemFont(ofSize: 22, weight: .semibold)
        title.frame = NSRect(x: 84, y: 43, width: 650, height: 28)
        title.autoresizingMask = [.width]
        header.addSubview(title)

        let subtitle = NSTextField(labelWithString: "Resource health, I/O incidents, and review queues")
        subtitle.font = .systemFont(ofSize: 12, weight: .regular)
        subtitle.textColor = .secondaryLabelColor
        subtitle.frame = NSRect(x: 84, y: 20, width: 650, height: 20)
        subtitle.autoresizingMask = [.width]
        header.addSubview(subtitle)

        let scroll = NSScrollView(frame: NSRect(x: 20, y: 72, width: 780, height: 442))
        scroll.hasVerticalScroller = true
        scroll.autohidesScrollers = true
        scroll.drawsBackground = false
        scroll.borderType = .noBorder
        scroll.wantsLayer = true
        scroll.layer?.cornerRadius = 12
        scroll.layer?.masksToBounds = true
        scroll.autoresizingMask = [.width, .height]

        let textView = NSTextView(frame: scroll.bounds)
        textView.isEditable = false
        textView.isSelectable = true
        textView.drawsBackground = true
        textView.backgroundColor = NSColor.textBackgroundColor.withAlphaComponent(0.72)
        textView.textContainerInset = NSSize(width: 18, height: 16)
        textView.autoresizingMask = [.width]
        scroll.documentView = textView
        content.addSubview(scroll)

        let refresh = NSButton(title: "Refresh Status", target: self, action: #selector(refreshMaintenanceStatus))
        refresh.bezelStyle = .rounded
        refresh.image = NSImage(systemSymbolName: "arrow.clockwise", accessibilityDescription: "Refresh")
        refresh.imagePosition = .imageLeading
        refresh.frame = NSRect(x: 20, y: 22, width: 138, height: 32)
        refresh.autoresizingMask = [.maxYMargin]
        content.addSubview(refresh)

        let logs = NSButton(title: "Open Logs", target: self, action: #selector(openLogs))
        logs.bezelStyle = .rounded
        logs.image = NSImage(systemSymbolName: "doc.text.magnifyingglass", accessibilityDescription: "Open Logs")
        logs.imagePosition = .imageLeading
        logs.frame = NSRect(x: 168, y: 22, width: 124, height: 32)
        logs.autoresizingMask = [.maxYMargin]
        content.addSubview(logs)

        statusWindow = window
        statusTextView = textView
        return window
    }'''

OLD_STATUS_LOADING = '        statusTextView?.string = "Loading maintenance status…"'
NEW_STATUS_LOADING = '        statusTextView?.textStorage?.setAttributedString(styledStatusText("Loading maintenance status…"))'

OLD_STATUS_ASSIGNMENT = '                self.statusTextView?.string = text.trimmingCharacters(in: .whitespacesAndNewlines)'
NEW_STATUS_ASSIGNMENT = '                self.statusTextView?.textStorage?.setAttributedString(self.styledStatusText(text.trimmingCharacters(in: .whitespacesAndNewlines)))'

OLD_STATUS_SCRIPT = 'let statusScript = maintenanceDir.appendingPathComponent("maintenance_status.py").path'
NEW_STATUS_SCRIPT = 'let statusScript = maintenanceDir.appendingPathComponent("maintenance_status_extended.py").path'


def _replace_once(source: str, old: str, new: str, label: str) -> str:
    count = source.count(old)
    if count != 1:
        raise ValueError(f"expected exactly one {label} marker, found {count}")
    return source.replace(old, new, 1)


def apply_overlay(source: str) -> str:
    value = _replace_once(source, OLD_STATUS_BUTTON, NEW_STATUS_BUTTON, "status button")
    value = _replace_once(value, OLD_MENU, NEW_MENU, "menu")
    value = _replace_once(value, OLD_SHORTCUT_ACTION, NEW_SHORTCUT_ACTION, "shortcut action")
    value = _replace_once(value, OLD_STATUS_WINDOW, NEW_STATUS_WINDOW, "status window")
    value = _replace_once(value, OLD_STATUS_LOADING, NEW_STATUS_LOADING, "status loading")
    value = _replace_once(value, OLD_STATUS_ASSIGNMENT, NEW_STATUS_ASSIGNMENT, "status assignment")
    value = _replace_once(value, OLD_STATUS_SCRIPT, NEW_STATUS_SCRIPT, "status script")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args(argv)
    source = args.source.read_text(encoding="utf-8")
    args.destination.write_text(apply_overlay(source), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
