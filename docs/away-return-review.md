# Resource and shortcut review surfaces

Idle Maintenance keeps review paths separate so a background resource monitor does not become a general content automation service.

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

## Automatic away-return review

The resident resource monitor is the authoritative return detector. It polls HID idle time even when no process incident is queued, so automatic resume routing does not depend on the legacy `idle_watcher.py` process being enabled. A return is recorded immediately, but the review and resume handoff wait until HID input has been quiet for 30 seconds; they do not open while the user is actively typing or moving the pointer.

Default policy:

- arm the resume flow after more than 10 minutes idle;
- consider the user returned when idle falls below 30 seconds;
- require one hour between resume-flow triggers;
- treat failed HID-idle reads as unknown instead of a synthetic return;
- keep the stricter 15-minute threshold for queued resource-incident prompts;
- deliver any armed resource prompt before the general interactive maintenance review;
- run interactive app/process maintenance;
- invoke `open hammerspoon://resumerouter` as the final UI;
- fall back to the configured handoff URL or app if the coordinator cannot launch.

The return detector persists its armed/cooldown state and records the most recent return-flow success or failure in resource-monitor health. This prevents a monitor restart from turning a single return into repeated resume launches.

Set `return_routing_enabled` to `false` to disable the contextual handoff while keeping HID sampling available for queued process reviews. `return_active_cutoff_seconds` controls how recently active the Mac must be before an armed return fires (30 seconds by default). `maint status` reports both the routing state and HID sampling failures.

The Hammerspoon coordinator asks wiki-automation for the highest-ranked TaskForge task. A TaskNote may save an exact digital work target using `resume_required`, `resume_kind`, `resume_target`, `resume_app`, `resume_profile`, `resume_label`, and `resume_confidence`. Exact mappings open immediately; inferred or missing mappings are confirmed in Hammerspoon and the selection is recorded back to the TaskNote.

Examples include a tuition URL, a direct subscription-email or draft link, a Canvas course page, or a saved interview-preparation conversation. Public docs and tests use synthetic domains and profile names; real targets remain in the private vault.

`maint shortcuts` remains the manual refresh-and-review workflow. The automatic return path does not open that separate popup, so it cannot steal focus from the selected work target.

## Legacy watcher

**Start / Restart Away-Return Review** starts `idle_watcher.py` only for legacy/manual compatibility. It is no longer required for automatic return routing and should not be enabled alongside the resident return detector merely to obtain the resume handoff. Starting the legacy watcher still intentionally runs one review immediately, and `maint status` can report whether that optional process is running.
