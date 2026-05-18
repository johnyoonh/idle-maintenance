#!/usr/bin/env swift
import AppKit
import Foundation

let stateDir = FileManager.default.homeDirectoryForCurrentUser
    .appendingPathComponent("Library/Application Support/idle-maintenance")
let usagePath = stateDir.appendingPathComponent("app_usage.json")
let lockPath = URL(fileURLWithPath: "/tmp/idle_maintenance_app_usage_watcher.lock")

func writeLock() {
    let pid = String(ProcessInfo.processInfo.processIdentifier)
    try? pid.write(to: lockPath, atomically: true, encoding: .utf8)
}

func removeLock() {
    try? FileManager.default.removeItem(at: lockPath)
}

func existingWatcherIsRunning() -> Bool {
    guard let content = try? String(contentsOf: lockPath, encoding: .utf8),
          let pid = Int32(content.trimmingCharacters(in: .whitespacesAndNewlines)),
          pid > 0 else {
        return false
    }
    return kill(pid, 0) == 0
}

func canonicalPath(_ url: URL) -> String {
    url.standardizedFileURL.resolvingSymlinksInPath().path
}

final class AppUsageRecorder {
    private var usage: [String: TimeInterval] = [:]
    private var lastPath = ""
    private var lastWrite: TimeInterval = 0

    init() {
        load()
    }

    func load() {
        guard let data = try? Data(contentsOf: usagePath),
              let decoded = try? JSONSerialization.jsonObject(with: data) as? [String: TimeInterval] else {
            return
        }
        usage = decoded
    }

    func record(_ app: NSRunningApplication?) {
        guard let bundleURL = app?.bundleURL else {
            return
        }

        let path = canonicalPath(bundleURL)
        guard path.hasSuffix(".app") else {
            return
        }

        let now = Date().timeIntervalSince1970
        if path == lastPath && now - lastWrite < 60 {
            return
        }

        usage[path] = now
        lastPath = path
        lastWrite = now
        save()
    }

    func save() {
        do {
            try FileManager.default.createDirectory(at: stateDir, withIntermediateDirectories: true)
            let data = try JSONSerialization.data(withJSONObject: usage, options: [.prettyPrinted, .sortedKeys])
            try data.write(to: usagePath, options: [.atomic])
        } catch {
            fputs("app_usage_watcher: failed to save usage data: \(error)\n", stderr)
        }
    }
}

if existingWatcherIsRunning() {
    exit(0)
}

writeLock()
atexit {
    removeLock()
}

let recorder = AppUsageRecorder()
let workspaceCenter = NSWorkspace.shared.notificationCenter

workspaceCenter.addObserver(
    forName: NSWorkspace.didActivateApplicationNotification,
    object: nil,
    queue: .main
) { notification in
    recorder.record(notification.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication)
}

workspaceCenter.addObserver(
    forName: NSWorkspace.didLaunchApplicationNotification,
    object: nil,
    queue: .main
) { notification in
    recorder.record(notification.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication)
}

recorder.record(NSWorkspace.shared.frontmostApplication)
RunLoop.main.run()
