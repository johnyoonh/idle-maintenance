# Sustained resource monitoring

Idle Maintenance includes a single-instance process I/O monitor intended to surface sustained, reviewable incidents without taking autonomous corrective action.

## Detection policy

The monitor runs continuously under launchd and uses one 10-second aggregate `iostat` interval followed by one `proc_pid_rusage(RUSAGE_INFO_V2)` process snapshot. It keeps a rolling three-sample window, which produces two process-counter intervals.

An incident opens only when all of these conditions hold:

- aggregate system disk throughput is at least 50 MiB/s;
- process throughput is at least 20 MiB/s total or 10 MiB/s writes;
- two intervals qualify across three process samples;
- the two intervals contain at least 256 MiB total;
- the PID, process start identity, executable, and normalized command remain the same;
- cumulative counters remain monotonic.

PID reuse and counter resets discard the rolling window. An active incident recovers after six available, non-qualifying samples.

The evidence statement is deliberately bounded: **I/O charged to the process during the sampled window; this is not definitive physical-disk attribution.** Per-process accounting can identify a useful correlation, but it cannot by itself prove which component caused physical storage traffic.

## Notifications and review timing

- Each process identity receives at most one notification every six hours.
- The first incident is queued for review until the user returns after at least 15 minutes idle.
- A second incident for the same process identity within 30 minutes of recovery opens the review prompt immediately.
- Historical incidents remain in the incident ledger and JSONL history even after the live process queue changes.

State is written atomically under `$HOME/Library/Application Support/idle-maintenance/`:

- `resource-monitor-state.json`: bounded health, active incidents, recent incident summaries, notification cooldowns, and pending prompts;
- `resource-monitor-history.jsonl`: bounded append history for incident open, recovery, and prompt outcomes;
- `resource-monitor.lock`: the single-instance lock.

Use `maint status` or `maint status --json` to inspect launchd health, monitor heartbeat, active incidents, queued prompts, recent incidents, and the attribution boundary.

## Process action policy

Protected Apple daemons and similar helpers expose only **Investigate**, **Snooze**, and **Leave**. This includes `mediaanalysisd`, `photoanalysisd`, `contactsd`, `corespotlightd`, `mds`, `mdworker` variants, `fileproviderd`, and related indexing or cloud helpers.

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

Investigation prompts provide only bounded, rootless observations. Any deeper diagnosis remains an explicit, interactive decision outside the monitor.

## Balanced macOS settings

Use these as review placeholders rather than machine-specific automation:

- **Photos:** keep iCloud Photos enabled and enable **Optimize Mac Storage**.
- **Mail:** set **Check for New Messages** to **Manually** and **Download Attachments** to **None**.
- **Spotlight:** add only narrowly identified high-churn folders such as `<high-churn-build-folder>` to **Spotlight Search Privacy**. Do not exclude broad home, cloud, or document roots.
- **Contacts:** leave synchronization unchanged until repeated incidents demonstrate sustained Contacts activity.
- **Battery:** use **Automatic** Energy Mode.

These settings reduce unnecessary background work without disabling core synchronization or hiding broad storage areas from system services.
