#!/usr/bin/env swift
import AppKit
import Foundation

let stateDir = FileManager.default.homeDirectoryForCurrentUser
    .appendingPathComponent("Library/Application Support/idle-maintenance")
let xdgConfigDir = FileManager.default.homeDirectoryForCurrentUser
    .appendingPathComponent(".config/idle-watcher")
let usagePath = stateDir.appendingPathComponent("app_usage.json")
let lockPath = URL(fileURLWithPath: "/tmp/idle_maintenance_app_usage_watcher.lock")
let defaultMinimumDwellSeconds: TimeInterval = 120

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

func executableConfigPath() -> URL? {
    guard let executable = CommandLine.arguments.first, !executable.isEmpty else {
        return nil
    }

    return URL(fileURLWithPath: executable)
        .deletingLastPathComponent()
        .appendingPathComponent("config.json")
}

func configPaths() -> [URL] {
    var paths = [
        xdgConfigDir.appendingPathComponent("config.json"),
        stateDir.appendingPathComponent("config.json"),
    ]
    if let executableConfig = executableConfigPath() {
        paths.append(executableConfig)
    }
    return paths
}

func configuredMinimumDwellSeconds() -> TimeInterval {
    for path in configPaths() {
        guard
            let data = try? Data(contentsOf: path),
            let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
            let value = object["app_usage_minimum_dwell_seconds"]
        else {
            continue
        }

        if let seconds = value as? NSNumber {
            return max(0, seconds.doubleValue)
        }
        if let seconds = value as? String, let parsed = Double(seconds) {
            return max(0, parsed)
        }
    }

    return defaultMinimumDwellSeconds
}

final class AppUsageRecorder {
    private var usage: [String: TimeInterval] = [:]
    private var lastPath = ""
    private var lastWrite: TimeInterval = 0
    private var pendingPath = ""
    private var pendingSince: TimeInterval = 0
    private let minimumDwellSeconds: TimeInterval

    init(minimumDwellSeconds: TimeInterval = configuredMinimumDwellSeconds()) {
        self.minimumDwellSeconds = minimumDwellSeconds
        load()
    }

    func load() {
        guard let data = try? Data(contentsOf: usagePath),
              let decoded = try? JSONSerialization.jsonObject(with: data) as? [String: TimeInterval] else {
            return
        }
        usage = decoded
    }

    func observeActivation(_ app: NSRunningApplication?) {
        guard let bundleURL = app?.bundleURL else {
            return
        }

        let path = canonicalPath(bundleURL)
        guard path.hasSuffix(".app") else {
            return
        }

        let now = Date().timeIntervalSince1970
        pendingPath = path
        pendingSince = now

        if minimumDwellSeconds <= 0 {
            record(path, at: now)
            return
        }

        Timer.scheduledTimer(withTimeInterval: minimumDwellSeconds, repeats: false) { [weak self] _ in
            self?.recordIfStillFrontmost(path: path, since: now)
        }
    }

    func recordIfStillFrontmost(path: String, since: TimeInterval) {
        guard pendingPath == path, pendingSince == since else {
            return
        }

        guard let frontmostURL = NSWorkspace.shared.frontmostApplication?.bundleURL else {
            return
        }

        guard canonicalPath(frontmostURL) == path else {
            return
        }

        record(path, at: Date().timeIntervalSince1970)
    }

    func record(_ path: String, at now: TimeInterval) {
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
    recorder.observeActivation(notification.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication)
}

recorder.observeActivation(NSWorkspace.shared.frontmostApplication)
RunLoop.main.run()
