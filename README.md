# Idle Maintenance

Low-friction maintenance for this Mac, split into two lightweight paths:

- **Terminal suggestions**: one command suggestion when a new shell tab opens.
- **Scheduled runner**: low-load maintenance tasks launched by `com.john.idle-maintenance` through Wiki Automation every 15 minutes.

The scheduled runner is the authoritative background maintenance path. The old resident `IdleMaintenance.app` / `idle_watcher.py` GUI watcher is legacy/manual and is not expected to stay running.

## Terminal Suggestions

When a new terminal tab opens during work hours, `~/.zshrc` calls:

```bash
~/repos/idle-maintenance/prompt-suggest.py
```

Display format:

```bash
brew cleanup -s && brew autoremove • Clean up Homebrew cache and old versions | R=Run D=Dismiss P=Preview L=Later
```

The terminal suggestion list can include low-friction review prompts, not only cleanup commands. GUI shortcut review is intentionally wired here because opening a MacBook or a new terminal tab is already a context-switching moment:

```bash
$HOME/.local/bin/kb popup --surface gui --group auto --force
$HOME/.local/bin/kb export-srs --mode focused --max-shortcut-cards 7 --underused-limit 0
```

These stay out of the scheduled runner. The prompt asks first; the background job never opens GUI review windows or rewrites flashcard files.

Quick actions are handled by functions in `~/.zshrc`:

```bash
R     # Run it
D     # Dismiss it until explicitly re-enabled
P     # Preview it before running
L     # Defer it for its normal frequency
```

The canonical CLI uses action-first commands:

```bash
maint run [script-id]
maint dismiss [script-id]
maint preview [script-id]
maint later [script-id]
maint enable <script-id>
maint list --all
maint status
```

Dismiss only changes suggestion state; it never deletes the underlying executable.
The older `maint <script-id> <1-4>` forms remain temporarily available with a
deprecation warning.

State lives in:

```bash
~/Library/Application Support/idle-maintenance/
```

Important files:

- `session.json`: current suggestion per shell session.
- `state.json`: run/dismiss history.
- `cache.json`: discovered command cache.

Suggestions default to 9am–9pm with an exclusive end boundary. Override
`terminal_suggestion_start_hour` and `terminal_suggestion_end_hour` in the normal
config; an overnight range is supported and equal values mean always available.

## Scheduled Runner

Launchd runs this job every 15 minutes:

```bash
launchctl print gui/$(id -u)/com.john.idle-maintenance
```

That LaunchAgent calls:

```bash
"$HOME/Applications/Wiki Automation.app/Contents/MacOS/wiki-automation" idle-maintenance
```

Which dispatches to:

```bash
"$WIKI_PATH/99_meta/scripts/idle_maintenance_runner.sh"
```

The runner executes at most one due task per launchd tick. Before starting work, and while a task is running, it requires:

- AC power.
- Enough idle time for that task.
- 1-minute load average under that task's limit.

Current task policy:

- Homebrew autoupdate: daily, at least 600s idle, load <= 1.2.
- Cockpit tools update: daily, at least 600s idle, load <= 1.2.
- yadm auto-push: weekly, at least 300s idle, load <= 2.0.
- Log pruning: daily, no idle minimum, load <= 4.0.
- App cleanup review: weekly, at least 600s idle, load <= 1.5.
- Storage cleanup: daily, at least 600s idle, load <= 1.5. It prunes aged
  caches/logs/Trash and Xcode artifacts, removes the disposable Screenpipe
  store, and invokes supported package-manager cleanup commands.

Preview storage cleanup without changing files:

```bash
python3 ~/repos/idle-maintenance/storage_cleanup.py --dry-run
```

Each run writes a JSON report to
`~/Library/Logs/idle-maintenance/storage-cleanup.jsonl`. Policy is configured
under `storage_cleanup` in the normal idle-maintenance config, including
retention periods, minimum free space, Screenpipe deletion, and package
cleaner toggles. `protected_paths` are never traversed or deleted; defaults
explicitly protect aisess, its prompt index and mirrors, agent-blame state,
provider session stores, and the Codex/OpenCode databases.

Check status:

```bash
maint status
maint status --json
```

The menu-bar app exposes the same data under **Maintenance Status…**. It reports
launchd health, authoritative runner output, the latest event, interactive
queues, snoozes/backoff, and terminal availability. It discovers the runner
through `scheduled_runner_status_command`, `$WIKI_PATH`, or `$HOME/wiki`.

Logs:

```bash
~/Library/Logs/wiki-automation/idle-maintenance-runtime.log
~/Library/Logs/wiki-automation/idle-maintenance.out.log
~/Library/Logs/wiki-automation/idle-maintenance.err.log
```

Deferral log lines include current power source, idle seconds, and 1-minute load so it is clear why work did not run.

## App Cleanup Policy

App cleanup is intentionally configurable. The project owns the cleanup lifecycle:

- detect stale apps;
- ask before acting;
- move apps to Trash;
- write a deletion ledger;
- call optional hooks before and after deletion.

Local config owns policy, including which apps are considered recoverable.

Config is loaded from the first existing file in this order:

```bash
~/.config/idle-watcher/config.json
~/Library/Application Support/idle-maintenance/config.json
<repo>/config.json
```

Example:

```json
{
  "app_cleanup": {
    "delete_mode": "trash",
    "allow_unknown_restore_source": false,
    "deletion_ledger": "~/Library/Application Support/idle-maintenance/app-deletions.jsonl",
    "restore_sources": [
      {
        "type": "homebrew_bundle",
        "path": "~/repos/Brewfile"
      },
      {
        "type": "mas_tsv",
        "path": "~/repos/app-store-apps.tsv"
      }
    ]
  },
  "hooks": {
    "before_delete_app": [
      "~/bin/idle-maintenance-before-delete"
    ],
    "after_delete_app": [
      "~/bin/idle-maintenance-after-delete"
    ]
  }
}
```

Supported restore source providers:

- `homebrew_bundle`: reads `cask "..."` and `mas "...", id: ...` lines from a Brewfile.
- `mas_tsv`: reads a tab-separated file with `app_id`, `name`, and optional version columns.

When `allow_unknown_restore_source` is `false`, Delete is refused for apps that are not found in any configured restore source. The prompt still shows the candidate, but marks it as `Restore: unknown; delete disabled`.

Before-delete hooks receive two arguments:

```bash
hook "$APP_PATH" "$DELETE_CONTEXT_JSON"
```

A non-zero exit code vetoes deletion. After-delete hooks receive the same shape of JSON plus the ledger fields for the completed Trash action. Hook scripts are a good place to enforce local recovery policy without adding personal backup assumptions to the project.

The deletion ledger is JSONL. Each line records the app path, bundle id, version, Trash path, restore source, restore command, and timestamp.

See `docs/app-cleanup-policy.md` for the full public config contract.

## Legacy GUI Watcher

These files are retained for manual/legacy use:

- `idle_watcher.py`
- `idle_config.py`
- `maintenance_interactive.py`
- `app_auditor.py`
- `prompt.swift`
- `app_usage_watcher.swift`
- `build_app.sh`
- `deploy.sh`
- `com.user.idle_maintenance.plist`

They are not the normal background maintenance path. `com.user.idle_maintenance.plist` is kept as a disabled legacy sample only. Opening the menu-bar app does not start the legacy watcher; use **Start / Restart Legacy Watcher** explicitly when needed.

The legacy watcher reads configuration from the first available file in this order:

```bash
~/.config/idle-watcher/config.json
~/Library/Application Support/idle-maintenance/config.json
./config.json
```

If unset, the handoff app defaults to Apple Reminders. Example runtime config:

```json
{
  "handoff_app": "Reminders",
  "idle_threshold_minutes": 10,
  "check_interval_seconds": 30,
  "post_trigger_cooldown_seconds": 3600,
  "max_entries_per_idle_return": 5,
  "app_snooze_hours": 720,
  "process_high_cpu_threshold": 50.0,
  "process_cpu_sample_count": 3,
  "process_cpu_sample_interval_seconds": 30,
  "process_snooze_hours": 24,
  "terminal_suggestion_start_hour": 9,
  "terminal_suggestion_end_hour": 21,
  "stale_days_limit": 90
}
```

## Health Checks

Recommended checks:

```bash
python3 -m py_compile idle_config.py idle_watcher.py app_auditor.py maintenance_interactive.py maintenance_status.py maint.py prompt-suggest.py
python3 -m unittest discover -s tests -v
swiftc -typecheck prompt.swift
swiftc -typecheck app_usage_watcher.swift
launchctl print gui/$(id -u)/com.john.idle-maintenance
maint status
tail -80 ~/Library/Logs/wiki-automation/idle-maintenance-runtime.log
```

Expected normal state:

- `com.john.idle-maintenance` is loaded.
- The runner usually exits quickly.
- Logs may show repeated deferrals during active/heavy use.
- No resident `idle_watcher.py` or `app_usage_watcher` process is required.
