# Troubleshooting

## Which System Should Be Running?

Normal background maintenance is handled by:

```bash
com.john.idle-maintenance
```

Check it with:

```bash
launchctl print gui/$(id -u)/com.john.idle-maintenance
```

It runs every 15 minutes through Wiki Automation, exits quickly, and logs to:

```bash
~/Library/Logs/wiki-automation/idle-maintenance-runtime.log
```

It is normal for these processes to be absent:

```bash
idle_watcher.py
maintenance_interactive.py
app_usage_watcher
```

Those belong to the legacy GUI watcher path and are not required for the low-load scheduled runner.

## Scheduled Runner Keeps Deferring

This is usually correct. The runner waits for:

- AC power.
- Task-specific idle time.
- Low enough 1-minute load average.

Check the current state:

```bash
"$HOME/Library/Mobile Documents/iCloud~md~obsidian/Documents/wiki/99_meta/scripts/idle_maintenance_runner.sh" --status
```

Inspect recent decisions:

```bash
tail -80 ~/Library/Logs/wiki-automation/idle-maintenance-runtime.log
```

Deferral lines include power source, idle seconds, and load. During active or heavy use, repeated deferrals are expected and protect system responsiveness.

## LaunchAgent Not Loaded

If this fails:

```bash
launchctl print gui/$(id -u)/com.john.idle-maintenance
```

Check the source plist:

```bash
plutil -lint "$HOME/Library/Mobile Documents/iCloud~md~obsidian/Documents/wiki/99_meta/scripts/launchagents/com.john.idle-maintenance.plist"
```

Then inspect the Wiki Automation logs:

```bash
ls -l ~/Library/Logs/wiki-automation/idle-maintenance*.log
tail -80 ~/Library/Logs/wiki-automation/idle-maintenance.err.log
```

Do not debug `com.user.idle_maintenance.plist` for normal operation. That file is legacy and points at an older resident watcher model.

## Terminal Suggestion Not Showing

Terminal suggestions are separate from the scheduled runner. They appear only once per shell tab and only during 9am-8pm.

Check the shell session:

```bash
echo $MAINT_SESSION_ID
echo $_MAINT_SHOWN
echo $_MAINT_CURRENT_SCRIPT
```

Reload the shell:

```bash
exec zsh
```

Run the suggester manually:

```bash
MAINT_SESSION_ID="manual_test_$(date +%s)" ~/repos/idle-maintenance/prompt-suggest.py
```

If it is outside work hours, no output is expected.

## Terminal Action Alias Not Working

Check aliases:

```bash
alias 1
alias 2
alias 3
alias 4
```

Check the current session record:

```bash
jq ".[\"$MAINT_SESSION_ID\"]" ~/Library/Application\ Support/idle-maintenance/session.json
```

If `_MAINT_CURRENT_SCRIPT` is empty, no suggestion was shown for that shell session.

## Reset Terminal Suggestion State

This resets prompt suggestions only. It does not affect the scheduled runner.

```bash
rm ~/Library/Application\ Support/idle-maintenance/session.json
unset _MAINT_SHOWN _MAINT_CURRENT_SCRIPT
exec zsh
```

## Health Check Commands

```bash
python3 -m py_compile idle_config.py idle_watcher.py app_auditor.py maintenance_interactive.py prompt-suggest.py
swiftc -typecheck prompt.swift
swiftc -typecheck app_usage_watcher.swift
launchctl print gui/$(id -u)/com.john.idle-maintenance
"$HOME/Library/Mobile Documents/iCloud~md~obsidian/Documents/wiki/99_meta/scripts/idle_maintenance_runner.sh" --status
```
