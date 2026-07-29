#!/usr/bin/env python3
"""Apply the small review-menu overlay to the legacy menu-bar app builder."""
from __future__ import annotations

import argparse
from pathlib import Path

OLD_MENU = '''        menu.addItem(withTitle: "Review Sustained High CPU (1 min)", action: #selector(reviewHighCpuApps), keyEquivalent: "h")
        menu.addItem(withTitle: "Review Keyboard Shortcuts", action: #selector(reviewKeyboardShortcuts), keyEquivalent: "k")
        menu.addItem(withTitle: "Run Next Maintenance Prompt", action: #selector(runMaintenanceReview), keyEquivalent: "m")
        menu.addItem(withTitle: "Open Activity Monitor", action: #selector(openActivityMonitor), keyEquivalent: "a")
        menu.addItem(NSMenuItem.separator())
        menu.addItem(withTitle: "Start / Restart Legacy Watcher", action: #selector(restartWatcher), keyEquivalent: "r")
        menu.addItem(withTitle: "Open Logs", action: #selector(openLogs), keyEquivalent: "l")'''

NEW_MENU = '''        let resourceHeader = NSMenuItem(title: "Resource Activity", action: nil, keyEquivalent: "")
        resourceHeader.isEnabled = false
        menu.addItem(resourceHeader)
        menu.addItem(withTitle: "Review Recent I/O Incidents…", action: #selector(showMaintenanceStatus), keyEquivalent: "i")
        menu.addItem(withTitle: "Sample CPU + Disk I/O (1 min)", action: #selector(reviewHighCpuApps), keyEquivalent: "h")
        menu.addItem(withTitle: "Open Activity Monitor", action: #selector(openActivityMonitor), keyEquivalent: "a")
        menu.addItem(NSMenuItem.separator())
        let shortcutHeader = NSMenuItem(title: "Shortcut Review", action: nil, keyEquivalent: "")
        shortcutHeader.isEnabled = false
        menu.addItem(shortcutHeader)
        menu.addItem(withTitle: "Refresh & Review Shortcuts", action: #selector(reviewKeyboardShortcuts), keyEquivalent: "k")
        menu.addItem(withTitle: "Run Next Maintenance Prompt", action: #selector(runMaintenanceReview), keyEquivalent: "m")
        menu.addItem(NSMenuItem.separator())
        menu.addItem(withTitle: "Start / Restart Away-Return Review", action: #selector(restartWatcher), keyEquivalent: "r")
        menu.addItem(withTitle: "Open Logs", action: #selector(openLogs), keyEquivalent: "l")'''

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

OLD_STATUS_SCRIPT = 'let statusScript = maintenanceDir.appendingPathComponent("maintenance_status.py").path'
NEW_STATUS_SCRIPT = 'let statusScript = maintenanceDir.appendingPathComponent("maintenance_status_extended.py").path'


def _replace_once(source: str, old: str, new: str, label: str) -> str:
    count = source.count(old)
    if count != 1:
        raise ValueError(f"expected exactly one {label} marker, found {count}")
    return source.replace(old, new, 1)


def apply_overlay(source: str) -> str:
    value = _replace_once(source, OLD_MENU, NEW_MENU, "menu")
    value = _replace_once(value, OLD_SHORTCUT_ACTION, NEW_SHORTCUT_ACTION, "shortcut action")
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
