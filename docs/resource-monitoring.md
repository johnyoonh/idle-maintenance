# Resource-aware maintenance

Idle Maintenance now treats disk activity as a first-class resource signal instead of inferring it from CPU load.

## Process detection

The interactive process audit samples two independent signals:

- sustained CPU utilization;
- cumulative per-process disk bytes from `proc_pid_rusage(RUSAGE_INFO_V2)`.

A process is considered a high-I/O candidate only when its counters increase monotonically for the same process instance, the configured throughput threshold is met for the required number of intervals, and the total sampled byte window exceeds the configured minimum. PID, process start identity, executable, and normalized command are retained so PID reuse cannot redirect a later termination action.

Queue and keep state use an executable-and-command fingerprint rather than `comm`. Multiple Python, Node, browser-helper, or Git processes therefore remain distinct.

Default policy:

```json
{
  "process_io_enabled": true,
  "process_io_sample_count": 3,
  "process_io_sample_interval_seconds": 10,
  "process_high_io_total_mib_per_second": 20,
  "process_high_io_write_mib_per_second": 10,
  "process_io_minimum_window_mib": 256,
  "process_io_required_intervals": 2,
  "process_fs_usage_trace_seconds": 10,
  "process_terminate_grace_seconds": 5
}
```

The process prompt still uses the existing **Investigate** action. For I/O candidates, the generated investigation prompt includes a bounded command such as:

```bash
sudo /usr/bin/fs_usage -w -f filesys -t 10 12345
```

`fs_usage` is never run continuously or unattended. It is provided only for a user-selected short path-level diagnosis.

## Termination safety

A terminate action now follows this sequence:

1. Re-read the PID identity and verify user, start identity, executable, and command fingerprint.
2. Send `SIGTERM`.
3. Poll for up to the configured grace period.
4. If the process remains alive, display a separate **Force Kill** confirmation.
5. Revalidate identity again before any `SIGKILL`.

The previous automatic `SIGKILL` escalation after 400 milliseconds is removed.

## Disk-busy gate

`disk_activity.py` samples aggregate throughput with rootless `iostat`:

```bash
python3 disk_activity.py
python3 disk_activity.py --json
```

Exit codes:

- `0`: disk is below the configured threshold;
- `75`: disk is busy and maintenance should be deferred;
- `2`: activity could not be measured; callers may fail open while recording the error.

Default policy:

```json
{
  "system_disk_busy_mib_per_second": 50,
  "system_disk_sample_seconds": 1
}
```

The canonical external scheduled runner can use `disk_activity.py` before any high-I/O task. `storage_cleanup.py` applies the same gate itself and returns `75` when it defers.

## Storage-cleanup load reduction

Daily cleanup still prunes aged cache, log, Trash, and Xcode artifacts. Expensive broad scans and package-manager cleanup now run only when either:

- free space is below `minimum_free_gb + cleanup_pressure_headroom_gb`; or
- their periodic interval is due.

The default interval for both large-path inventory and package cleaners is seven days. State is persisted atomically in `storage-cleanup-state.json`.

## State integrity

Interactive state now uses:

- `flock` under the application-support directory instead of a reusable PID file in `/tmp`;
- temporary-file write, flush, `fsync`, and atomic replacement for JSON queues and keep state;
- explicit logging when state persistence fails.
