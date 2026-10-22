# Time Machine: guarded idle backups

This worker starts Apple's native Time Machine engine using tmutil startbackup --auto --block. It does not replace Time Machine, create a resident daemon, disable the system backup schedule, cancel an active backup, or stop one when you return to your keyboard.

## Ownership

- wiki-automation's existing com.john.idle-maintenance-monitor checks idle time and starts this one-shot worker after at least 30 minutes without input. It retries eligibility checks at most once per additional 30 minutes of inactivity.
- idle-maintenance/time_machine_backup.py owns the policy, Time Machine status checks, one-instance lock, local state, and overdue notifications.
- No new LaunchAgent or always-running monitor is needed.
- The feature is OFF by default. Merely merging the code does not modify the running Mac or Apple's schedule.

## 1. Validate the installation

After updating BOTH repos, rebuilding/reinstalling the signed Wiki Automation helper, and confirming Time Machine has a reachable backup destination, run:

    cd ~/repos/idle-maintenance
    python3 -m unittest discover -s tests -p 'test_time_machine_backup.py' -v
    python3 time_machine_backup.py --status
    tmutil destinationinfo
    tmutil latestbackup
    tmutil status

You may need to grant Full Disk Access to your automation runtime or terminal for tmutil. Do not disable Apple's regular schedule yet.

## 2. Enable the policy locally

Merge this object into ~/.config/idle-watcher/config.json (or, when absent, ~/Library/Application Support/idle-maintenance/config.json):

    "time_machine_backup": {
      "enabled": true,
      "start_hour": 20,
      "end_hour": 7,
      "idle_minutes": 30,
      "min_interval_hours": 24,
      "failure_cooldown_hours": 3,
      "overdue_hours": 48,
      "alert_cooldown_hours": 24,
      "max_load_1min": 2.0
    }

The top-level file must remain a JSON object. Hours use the Mac's local timezone, with the ending hour excluded. The defaults allow 8 PM–7 AM. The script additionally requires AC power, low one-minute load, low disk I/O, known idle time, a configured Time Machine destination, no other active native backup, and no successful backup within 24 hours. Unknown required conditions defer safely.

Allow the Mac to stay awake on AC long enough to reach the idle threshold and finish the backup. If macOS sleeps before the idle threshold is reached, the backup is delayed. The script intentionally does not inhibit sleep or wake the Mac.

## 3. Verify before changing Apple's schedule

    python3 ~/repos/idle-maintenance/time_machine_backup.py --status
    tmutil latestbackup
    tail -n 60 ~/Library/Logs/wiki-automation/time-machine-backup.log

Check that saved last_result is success and Time Machine shows a fresh backup. Only THEN open System Settings > General > Time Machine > Options > Backup Frequency and choose Manually. Apple's independent automatic schedule would otherwise continue starting backups outside the chosen window. This code never toggles that setting.

## Monitoring and rollback

- Each check saves the last eligibility decision in a local, permission-0600 state file: ~/Library/Application Support/idle-maintenance/time-machine-backup-state.json.
- Native backup failures have a minimum three-hour retry cooldown. Once a backup begins, it can finish even when you return or the clock leaves the eligible window.
- A macOS alert is rate-limited to once per day when the latest verified backup is older than 48 hours; if no earlier backup is discoverable, the first alert starts 48 hours after first observation.
- The worker must be invoked for the alert check. If the Mac is asleep or the monitor is stopped, notifications can be delayed.
- The worker never calls tmutil stopbackup and never deletes backups or destinations.
- To roll back, set enabled to false and, if desired, restore Apple's automatic Backup Frequency. Keep the backup destination unchanged.

Tests are portable and mock macOS command outputs. Verify the full workflow on the actual Mac before relying on it.
