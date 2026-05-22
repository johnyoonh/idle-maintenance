#!/bin/bash
set -euo pipefail

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_NAME="IdleMaintenance.app"
DEST_DIR="${1:-$HOME/Applications}"
APP_PATH="$DEST_DIR/$APP_NAME"
RES_DIR="$APP_PATH/Contents/Resources/maintenance"
MACOS_DIR="$APP_PATH/Contents/MacOS"
INFO_PLIST="$APP_PATH/Contents/Info.plist"
CODESIGN_IDENTITY="${CODESIGN_IDENTITY:--}"

mkdir -p "$DEST_DIR"
rm -rf "$APP_PATH"

mkdir -p "$RES_DIR" "$MACOS_DIR"

TMP_SWIFT="$(mktemp /tmp/idlemaintenance-launcher.XXXXXX.swift)"
trap 'rm -f "$TMP_SWIFT"' EXIT

cat > "$TMP_SWIFT" <<'EOF'
import AppKit
import Foundation

struct WatchJobsFile: Decodable {
    let jobs: [String: WatchJob]
}

struct HealthChecksFile: Decodable {
    let healthChecks: [String: HealthCheck]?
}

struct WatchJob: Decodable {
    let watchDirectory: String
    let filePrefix: String
    let fileSuffix: String
    let stateFile: String?
    let alertLockFile: String?
    let alertTitle: String
    let alertMessage: String
    let timestampRegex: String?
    let openAppPath: String?
    let processName: String?
}

struct HealthCheck: Decodable {
    let processName: String
    let appPath: String
    let stateFile: String?
    let lockFile: String?
    let minimumRelaunchIntervalSeconds: Double?
}

enum WatchJobError: Error, CustomStringConvertible {
    case missingConfig(String)
    case missingJob(String)
    case missingHealthCheck(String)
    case invalidLock

    var description: String {
        switch self {
        case .missingConfig(let path):
            return "watch job config is missing: \(path)"
        case .missingJob(let name):
            return "watch job is not configured: \(name)"
        case .missingHealthCheck(let name):
            return "health check is not configured: \(name)"
        case .invalidLock:
            return "unable to create alert lock"
        }
    }
}

func expandedPath(_ path: String) -> String {
    if path == "~" {
        return FileManager.default.homeDirectoryForCurrentUser.path
    }
    if path.hasPrefix("~/") {
        return FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(String(path.dropFirst(2)))
            .path
    }
    return path
}

func appSupportDirectory() -> URL {
    FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Application Support/idle-maintenance")
}

func defaultWatchJobsConfigPath() -> String {
    appSupportDirectory().appendingPathComponent("watch-jobs.json").path
}

func defaultStateFile(jobName: String) -> String {
    appSupportDirectory()
        .appendingPathComponent("watch-jobs")
        .appendingPathComponent("\(jobName).last")
        .path
}

func defaultAlertLockFile(jobName: String) -> String {
    appSupportDirectory()
        .appendingPathComponent("watch-jobs")
        .appendingPathComponent("\(jobName).alert.lock")
        .path
}

func defaultHealthStateFile(name: String) -> String {
    appSupportDirectory()
        .appendingPathComponent("health-checks")
        .appendingPathComponent("\(name).last-reopen")
        .path
}

func defaultHealthLockFile(name: String) -> String {
    appSupportDirectory()
        .appendingPathComponent("health-checks")
        .appendingPathComponent("\(name).lock")
        .path
}

func readWatchJob(name: String, configPath: String = defaultWatchJobsConfigPath()) throws -> WatchJob {
    let url = URL(fileURLWithPath: expandedPath(configPath))
    guard FileManager.default.fileExists(atPath: url.path) else {
        throw WatchJobError.missingConfig(url.path)
    }

    let data = try Data(contentsOf: url)
    let file = try JSONDecoder().decode(WatchJobsFile.self, from: data)
    guard let job = file.jobs[name] else {
        throw WatchJobError.missingJob(name)
    }
    return job
}

func readHealthCheck(name: String, configPath: String = defaultWatchJobsConfigPath()) throws -> HealthCheck {
    let url = URL(fileURLWithPath: expandedPath(configPath))
    guard FileManager.default.fileExists(atPath: url.path) else {
        throw WatchJobError.missingConfig(url.path)
    }

    let data = try Data(contentsOf: url)
    let file = try JSONDecoder().decode(HealthChecksFile.self, from: data)
    guard let check = file.healthChecks?[name] else {
        throw WatchJobError.missingHealthCheck(name)
    }
    return check
}

func newestMatchingFile(job: WatchJob) throws -> URL? {
    let directory = URL(fileURLWithPath: expandedPath(job.watchDirectory))
    let contents = try FileManager.default.contentsOfDirectory(
        at: directory,
        includingPropertiesForKeys: [.contentModificationDateKey, .isRegularFileKey],
        options: [.skipsHiddenFiles]
    )

    let matches = contents.filter { url in
        let name = url.lastPathComponent
        return name.hasPrefix(job.filePrefix) && name.hasSuffix(job.fileSuffix)
    }

    return try matches.max { left, right in
        let leftDate = try left.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate ?? .distantPast
        let rightDate = try right.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate ?? .distantPast
        return leftDate < rightDate
    }
}

func readStringIfPresent(_ path: String) -> String {
    (try? String(contentsOfFile: path, encoding: .utf8))?
        .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
}

func writeString(_ value: String, to path: String) throws {
    let url = URL(fileURLWithPath: path)
    try FileManager.default.createDirectory(
        at: url.deletingLastPathComponent(),
        withIntermediateDirectories: true
    )
    try value.write(to: url, atomically: true, encoding: .utf8)
}

func pidIsRunning(_ pid: Int32) -> Bool {
    kill(pid, 0) == 0 || errno == EPERM
}

func createAlertLock(path: String) throws -> FileHandle? {
    let url = URL(fileURLWithPath: path)
    try FileManager.default.createDirectory(
        at: url.deletingLastPathComponent(),
        withIntermediateDirectories: true
    )

    if FileManager.default.fileExists(atPath: path) {
        let existingPid = Int32(readStringIfPresent(path)) ?? -1
        if existingPid > 0 && pidIsRunning(existingPid) {
            return nil
        }
        try? FileManager.default.removeItem(atPath: path)
    }

    let fd = open(path, O_WRONLY | O_CREAT | O_EXCL, S_IRUSR | S_IWUSR)
    if fd < 0 {
        if errno == EEXIST {
            return nil
        }
        throw WatchJobError.invalidLock
    }

    let handle = FileHandle(fileDescriptor: fd, closeOnDealloc: true)
    if let data = "\(getpid())\n".data(using: .utf8) {
        try handle.write(contentsOf: data)
    }
    return handle
}

func createProcessLock(path: String) throws -> FileHandle? {
    try createAlertLock(path: path)
}

func extractTimestamp(from report: URL, regex pattern: String?) -> String {
    guard let pattern,
          let firstLine = try? String(contentsOf: report, encoding: .utf8)
            .split(separator: "\n", maxSplits: 1, omittingEmptySubsequences: false)
            .first
    else {
        return ""
    }

    let text = String(firstLine)
    guard let regex = try? NSRegularExpression(pattern: pattern),
          let match = regex.firstMatch(in: text, range: NSRange(text.startIndex..., in: text)),
          match.numberOfRanges > 1,
          let range = Range(match.range(at: 1), in: text)
    else {
        return ""
    }
    return String(text[range])
}

func isProcessRunning(name: String) -> Bool {
    let process = Process()
    process.executableURL = URL(fileURLWithPath: "/usr/bin/pgrep")
    process.arguments = ["-x", name]
    process.standardOutput = Pipe()
    process.standardError = Pipe()
    do {
        try process.run()
        process.waitUntilExit()
        return process.terminationStatus == 0
    } catch {
        return false
    }
}

func showAlert(title: String, message: String) {
    NSApp.setActivationPolicy(.accessory)
    let alert = NSAlert()
    alert.alertStyle = .critical
    alert.messageText = title
    alert.informativeText = message
    alert.addButton(withTitle: "OK")
    NSApp.activate(ignoringOtherApps: true)
    alert.runModal()
}

func runWatchJob(name: String) throws {
    let job = try readWatchJob(name: name)
    guard let latest = try newestMatchingFile(job: job) else {
        return
    }

    let statePath = expandedPath(job.stateFile ?? defaultStateFile(jobName: name))
    if readStringIfPresent(statePath) == latest.path {
        return
    }

    let lockPath = expandedPath(job.alertLockFile ?? defaultAlertLockFile(jobName: name))
    guard let lockHandle = try createAlertLock(path: lockPath) else {
        return
    }
    defer {
        try? lockHandle.close()
        try? FileManager.default.removeItem(atPath: lockPath)
    }

    let timestamp = extractTimestamp(from: latest, regex: job.timestampRegex)
    let message = job.alertMessage
        .replacingOccurrences(of: "{timestamp}", with: timestamp)
        .replacingOccurrences(of: "{timestampSuffix}", with: timestamp.isEmpty ? "" : " at \(timestamp)")
        .replacingOccurrences(of: "{path}", with: latest.path)

    try writeString(latest.path, to: statePath)
    showAlert(title: job.alertTitle, message: message)

    if let appPath = job.openAppPath {
        if let processName = job.processName, isProcessRunning(name: processName) {
            return
        }
        NSWorkspace.shared.openApplication(
            at: URL(fileURLWithPath: expandedPath(appPath)),
            configuration: NSWorkspace.OpenConfiguration()
        )
    }
}

func runHealthCheck(name: String) throws {
    let check = try readHealthCheck(name: name)
    if isProcessRunning(name: check.processName) {
        return
    }

    let lockPath = expandedPath(check.lockFile ?? defaultHealthLockFile(name: name))
    guard let lockHandle = try createProcessLock(path: lockPath) else {
        return
    }
    defer {
        try? lockHandle.close()
        try? FileManager.default.removeItem(atPath: lockPath)
    }

    if isProcessRunning(name: check.processName) {
        return
    }

    let statePath = expandedPath(check.stateFile ?? defaultHealthStateFile(name: name))
    let now = Date().timeIntervalSince1970
    let minimumInterval = check.minimumRelaunchIntervalSeconds ?? 300
    if let lastReopen = Double(readStringIfPresent(statePath)),
       now - lastReopen < minimumInterval {
        return
    }

    NSWorkspace.shared.openApplication(
        at: URL(fileURLWithPath: expandedPath(check.appPath)),
        configuration: NSWorkspace.OpenConfiguration()
    )
    try writeString(String(Int(now)), to: statePath)
}

final class IdleMaintenanceApp: NSObject, NSApplicationDelegate {
    var statusItem: NSStatusItem?
    var watcher: Process?

    let logsDir = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Logs")

    var maintenanceDir: URL {
        Bundle.main.resourceURL!.appendingPathComponent("maintenance")
    }

    var idleWatcherPath: URL {
        maintenanceDir.appendingPathComponent("idle_watcher.py")
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        setupStatusItem()
        startWatcherIfNeeded()
    }

    func setupStatusItem() {
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.button?.title = "🛠"
        item.button?.toolTip = "Idle Maintenance"

        let menu = NSMenu(title: "Idle Maintenance")
        menu.addItem(withTitle: "Idle Maintenance Running", action: nil, keyEquivalent: "")
        menu.addItem(NSMenuItem.separator())
        menu.addItem(withTitle: "Review High CPU Apps", action: #selector(reviewHighCpuApps), keyEquivalent: "h")
        menu.addItem(withTitle: "Review Keyboard Shortcuts", action: #selector(reviewKeyboardShortcuts), keyEquivalent: "k")
        menu.addItem(withTitle: "Run Next Maintenance Prompt", action: #selector(runMaintenanceReview), keyEquivalent: "m")
        menu.addItem(withTitle: "Open Activity Monitor", action: #selector(openActivityMonitor), keyEquivalent: "a")
        menu.addItem(NSMenuItem.separator())
        menu.addItem(withTitle: "Restart Watcher", action: #selector(restartWatcher), keyEquivalent: "r")
        menu.addItem(withTitle: "Open Logs", action: #selector(openLogs), keyEquivalent: "l")
        menu.addItem(NSMenuItem.separator())
        menu.addItem(withTitle: "Quit", action: #selector(quit), keyEquivalent: "q")
        item.menu = menu
        statusItem = item
    }

    func startWatcherIfNeeded() {
        if isWatcherRunning() {
            return
        }

        try? FileManager.default.createDirectory(at: logsDir, withIntermediateDirectories: true)
        let outURL = logsDir.appendingPathComponent("IdleMaintenance.out")
        let errURL = logsDir.appendingPathComponent("IdleMaintenance.err")
        FileManager.default.createFile(atPath: outURL.path, contents: nil)
        FileManager.default.createFile(atPath: errURL.path, contents: nil)

        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        process.arguments = [idleWatcherPath.path]
        process.standardOutput = try? FileHandle(forWritingTo: outURL)
        process.standardError = try? FileHandle(forWritingTo: errURL)
        do {
            try process.run()
            watcher = process
        } catch {
            NSWorkspace.shared.open(logsDir)
        }
    }

    func isWatcherRunning() -> Bool {
        let process = Process()
        let pipe = Pipe()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/pgrep")
        process.arguments = ["-f", "[i]dle_watcher.py"]
        process.standardOutput = pipe
        process.standardError = Pipe()
        do {
            try process.run()
            process.waitUntilExit()
            return process.terminationStatus == 0
        } catch {
            return false
        }
    }

    @objc func restartWatcher() {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/pkill")
        process.arguments = ["-f", "idle_watcher.py"]
        try? process.run()
        process.waitUntilExit()
        watcher = nil
        startWatcherIfNeeded()
    }

    func runDetached(executable: String, arguments: [String]) {
        try? FileManager.default.createDirectory(at: logsDir, withIntermediateDirectories: true)
        let outURL = logsDir.appendingPathComponent("IdleMaintenance.menu.out")
        let errURL = logsDir.appendingPathComponent("IdleMaintenance.menu.err")
        FileManager.default.createFile(atPath: outURL.path, contents: nil)
        FileManager.default.createFile(atPath: errURL.path, contents: nil)

        let process = Process()
        process.executableURL = URL(fileURLWithPath: executable)
        process.arguments = arguments
        if let out = try? FileHandle(forWritingTo: outURL) {
            _ = try? out.seekToEnd()
            process.standardOutput = out
        }
        if let err = try? FileHandle(forWritingTo: errURL) {
            _ = try? err.seekToEnd()
            process.standardError = err
        }
        try? process.run()
    }

    @objc func reviewHighCpuApps() {
        runDetached(
            executable: "/usr/bin/python3",
            arguments: [
                maintenanceDir.appendingPathComponent("maintenance_interactive.py").path,
                "--process-audit"
            ]
        )
    }

    @objc func reviewKeyboardShortcuts() {
        runDetached(
            executable: "/bin/zsh",
            arguments: [
                "-lc",
                "/Users/john/.local/bin/kb popup --surface gui --group obsidian-navigation --force"
            ]
        )
    }

    @objc func runMaintenanceReview() {
        runDetached(
            executable: "/usr/bin/python3",
            arguments: [maintenanceDir.appendingPathComponent("maintenance_interactive.py").path]
        )
    }

    @objc func openActivityMonitor() {
        NSWorkspace.shared.openApplication(
            at: URL(fileURLWithPath: "/System/Applications/Utilities/Activity Monitor.app"),
            configuration: NSWorkspace.OpenConfiguration()
        )
    }

    @objc func openLogs() {
        NSWorkspace.shared.open(logsDir)
    }

    @objc func quit() {
        NSApp.terminate(nil)
    }
}

let app = NSApplication.shared
if CommandLine.arguments.count >= 3 && CommandLine.arguments[1] == "run-watch-job" {
    do {
        try runWatchJob(name: CommandLine.arguments[2])
        exit(0)
    } catch {
        FileHandle.standardError.write(Data("IdleMaintenance watch job failed: \(error)\n".utf8))
        exit(1)
    }
}

if CommandLine.arguments.count >= 3 && CommandLine.arguments[1] == "run-health-check" {
    do {
        try runHealthCheck(name: CommandLine.arguments[2])
        exit(0)
    } catch {
        FileHandle.standardError.write(Data("IdleMaintenance health check failed: \(error)\n".utf8))
        exit(1)
    }
}

let delegate = IdleMaintenanceApp()
app.delegate = delegate
app.run()
EOF

swiftc -O -o "$MACOS_DIR/IdleMaintenance" "$TMP_SWIFT"

cat > "$INFO_PLIST" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleDevelopmentRegion</key>
  <string>en</string>
  <key>CFBundleExecutable</key>
  <string>IdleMaintenance</string>
  <key>CFBundleIdentifier</key>
  <string>com.john.idlemaintenance</string>
  <key>CFBundleInfoDictionaryVersion</key>
  <string>6.0</string>
  <key>CFBundleName</key>
  <string>IdleMaintenance</string>
  <key>CFBundleDisplayName</key>
  <string>IdleMaintenance</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleSignature</key>
  <string>????</string>
  <key>CFBundleShortVersionString</key>
  <string>1.0</string>
  <key>CFBundleVersion</key>
  <string>1</string>
  <key>LSMinimumSystemVersion</key>
  <string>12.0</string>
  <key>LSUIElement</key>
  <true/>
</dict>
</plist>
EOF

echo -n "APPL????" > "$APP_PATH/Contents/PkgInfo"

cp "$SRC_DIR/app_auditor.py" "$RES_DIR/"
cp "$SRC_DIR/idle_config.py" "$RES_DIR/"
cp "$SRC_DIR/idle_watcher.py" "$RES_DIR/"
cp "$SRC_DIR/maintenance_interactive.py" "$RES_DIR/"
cp "$SRC_DIR/prompt.swift" "$RES_DIR/"
cp "$SRC_DIR/restore_sources.py" "$RES_DIR/"
cp "$SRC_DIR/app_usage_watcher.swift" "$RES_DIR/"
cp "$SRC_DIR/config.json" "$RES_DIR/"

swiftc -O -o "$RES_DIR/app_usage_watcher" "$SRC_DIR/app_usage_watcher.swift"

chmod +x "$MACOS_DIR/IdleMaintenance" "$RES_DIR/"*.py "$RES_DIR/"*.swift

codesign --force --deep --sign "$CODESIGN_IDENTITY" "$APP_PATH" >/dev/null

echo "Built: $APP_PATH"
echo "Launch with: open \"$APP_PATH\""
