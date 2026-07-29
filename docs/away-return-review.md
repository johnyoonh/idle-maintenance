# Resource and shortcut review surfaces

Idle Maintenance keeps three review paths separate so a background resource monitor does not become a general content automation service.

## Resource Activity menu

The menu-bar app exposes:

- **Review Recent I/O Incidents…** — opens maintenance status with monitor health, queued prompts, and recent incidents.
- **Sample CPU + Disk I/O (1 min)** — runs the existing manual process audit, which samples both sustained CPU and process I/O.

The resident resource monitor sends a notification when a qualifying incident opens. A first incident is queued and its review window is shown only after the user has been idle for at least 15 minutes and then returns. A recurrence for the same process identity within 30 minutes prompts immediately. Notification delivery is deduplicated per process identity for six hours.

A queued process window is skipped if the process exits or its identity changes before review. The absence of a return-time window therefore does not prove that no incident was recorded; use `maint status` to inspect recent history.

## Refresh and review shortcuts

Use one canonical command:

```bash
maint shortcuts
```

It runs the configured focused-content export first and opens the GUI review popup only when that refresh succeeds. This prevents a global hotkey or menu action from showing stale review content.

Default command sequence:

```bash
$HOME/.local/bin/kb export-srs --mode focused --max-shortcut-cards 7 --underused-limit 0
$HOME/.local/bin/kb popup --surface gui --group auto --force
```

The menu item **Refresh & Review Shortcuts** and the global Hammerspoon binding both call `maint shortcuts` instead of duplicating this sequence.

## Optional away-return review

**Start / Restart Away-Return Review** starts the legacy opt-in watcher. It is separate from the signed resource monitor and is not required for normal monitoring.

Default policy:

- arm after more than 10 minutes idle;
- trigger when idle falls below 30 seconds after return;
- require one hour between triggers;
- run interactive maintenance and the handoff action;
- refresh shortcut content, then open the shortcut review popup.

Starting the watcher intentionally runs one review immediately. `maint status` reports whether this optional watcher is running and displays its current thresholds.

The signed resource-monitor LaunchAgent remains process-focused. It does not export shortcut content, write to a vault, or invoke the optional away-return watcher.
