# Sustained resource monitoring

Idle Maintenance includes a single-instance process I/O monitor intended to surface sustained, reviewable incidents without taking autonomous corrective action.

## Detection policy

The monitor runs continuously under launchd and uses aggregate `iostat` as a gate before collecting `proc_pid_rusage(RUSAGE_INFO_V2)` process snapshots. It keeps a rolling three-sample window, which produces two process-counter intervals while aggregate disk activity is high.

An incident opens only when all of these conditions hold:

- aggregate system disk throughput is at least 50 MiB/s;
- process throughput is at least 20 MiB/s total or 10 MiB/s writes;
- two intervals qualify across three process samples;
- the two intervals contain at least 256 MiB total;
- the PID, process start identity, executable, and normalized command remain the same;
- cumulative counters remain monotonic.

PID reuse and counter resets discard the rolling window. An active incident recovers after six available, non-qualifying samples.

The evidence statement is deliberately bounded: **I/O charged to the process during the sampled window; this is not definitive physical-disk attribution.** Per-process accounting can identify a useful correlation, but it cannot by itself prove which component caused physical storage traffic.

## Deterministic process triage

Known macOS background work is classified before a user alarm is raised. Process roles and default handling live in `process_review.py`; the shared suppress-versus-review threshold policy lives in `process_triage.py`. The live monitor, interactive process audit, and investigation prompts therefore reuse the same known-process guidance rather than rediscovering generic process identity each time.

Current known groups include Spotlight/Core Spotlight, Photos/media analysis, iCloud/File Provider sync, Contacts, suggestions/knowledge indexing, Time Machine, Software Update, and selected core macOS services.

A first isolated incident from a known group is still recorded in the incident ledger and JSONL history, but its notification and generic review prompt are suppressed when it remains below the routine review ceiling. The default recommendation from the known-process profile remains available in status/history and in any later escalated review.

Known work escalates back to normal review when either condition is true:

- the same process key or recurrence group returns within the configured recurrence window, including across a PID restart or command-fingerprint change; or
- CPU or process I/O reaches the routine review ceiling, which defaults to 4× the ordinary configured CPU/I/O threshold.

Unknown processes are never suppressed by this policy. They retain the existing notification and review behavior.

The behavior can be tuned without adding process-specific automation:

- `process_routine_suppression_enabled` defaults to `true`;
- `process_routine_review_multiplier` defaults to `4.0` and is clamped to at least `1.0`.
- `resource_monitor_notification_cooldown_seconds` defaults to 6 hours for unknown processes;
- `resource_monitor_known_notification_cooldown_seconds` defaults to 24 hours and is keyed by the known recurrence group, so a routine service restart does not reset the cooldown.

Google Drive synchronization is treated as known routine work. Expected downloads, uploads, offline pinning, hashing, and metadata reconciliation remain silent below the routine ceiling. CPU sustained at roughly one full core, or a recurrence after recovery, still opens review and offers a user-initiated graceful quit rather than process termination.

To reduce normal-operation notifications further while retaining incident history, increase only the known-process cooldown:

```json
{
  "process_routine_suppression_enabled": true,
  "resource_monitor_known_notification_cooldown_seconds": 172800
}
```

This example limits known-process notifications to once every 48 hours. Suppressed incidents continue to appear in status and history.

Suppression means “record the understood isolated case without interrupting the user.” It does not authorize termination, throttling, or any other corrective action.

## Notifications and review timing

- On systems with `terminal-notifier`, clicking **Show** opens `resource-monitor-history.jsonl` in VS Code when available, then falls back to the default macOS text editor. The AppleScript fallback remains available when that helper is not installed, but cannot attach a click destination.
- Each process identity receives at most one notification every six hours when review is warranted.
- Every non-suppressed incident, including a recurrence, is queued instead of opening a review window immediately.
- A queued review opens only from a fresh HID-idle sample between 30 seconds and 5 minutes. Active input and extended away time keep it queued, and only one review can open per fresh sample.
- Recovered incidents are removed from the queue and revalidated again before delivery, so a settled or replaced process cannot produce a stale popup.
- Historical and suppressed incidents remain in the incident ledger and JSONL history even after the live process queue changes.

The popup window can be tuned with `review_prompt_idle_seconds` and `review_prompt_idle_max_seconds`. The same gate defers the automatic away-return maintenance review until interaction becomes quiet; resume detection itself remains armed while the user is active. The monitor reuses its existing 30-second HID poll, so the gate adds no keyboard hook, event tap, or per-keypress processing.

State is written atomically under `$HOME/Library/Application Support/idle-maintenance/` with heartbeat writes throttled to a bounded cadence while lifecycle changes persist immediately:

- `resource-monitor-state.json`: bounded health, active incidents, recent incident summaries, notification cooldowns, pending prompts, and deterministic triage metadata. Per-process sampling windows remain runtime-only and are discarded when a process leaves the current hot snapshot;
- `resource-monitor-history.jsonl`: bounded append history for incident open, suppression, recovery, and prompt outcomes;
- `resource-monitor.lock`: the single-instance lock.

Use `maint status` or `maint status --json` to inspect launchd health, monitor heartbeat, active incidents, queued prompts, recent incidents, and the attribution boundary.

## Process action policy

Known Apple daemons and similar helpers expose only **Investigate**, **Snooze**, and **Leave** when they do reach review. Their deterministic guidance also supplies the likely role and default handling so the investigation begins from known context.

The main Mail and Shortcuts application processes may expose **Quit App**. A quit action:

1. re-reads the PID identity;
2. refuses the action if the process instance changed;
3. sends `SIGTERM` once;
4. waits for the configured grace period;
5. reports whether the app exited or remained running.

There is no force-kill escalation. The monitor never terminates a process without a user selecting **Quit App**.

## Safety boundary

The unattended monitor never runs:

- privileged commands or unattended `sudo`;
- filesystem tracing;
- recursive cloud-storage scans;
- process throttling;
- automatic termination.

Investigation prompts provide only bounded, rootless observations. For a recognized macOS group they also include the known role, default handling, and deterministic triage reason. Any deeper diagnosis remains an explicit, interactive decision outside the monitor.

## Balanced macOS settings

Use these as review placeholders rather than machine-specific automation:

- **Photos:** keep iCloud Photos enabled and enable **Optimize Mac Storage**.
- **Mail:** set **Check for New Messages** to **Manually** and **Download Attachments** to **None**.
- **Spotlight:** add only narrowly identified high-churn folders such as `<high-churn-build-folder>` to **Spotlight Search Privacy**. Do not exclude broad home, cloud, or document roots.
- **Contacts:** leave synchronization unchanged until repeated incidents demonstrate sustained Contacts activity.
- **Battery:** use **Automatic** Energy Mode.

These settings reduce unnecessary background work without disabling core synchronization or hiding broad storage areas from system services.
