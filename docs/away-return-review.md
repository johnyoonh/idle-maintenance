# Resource and shortcut review surfaces

Idle Maintenance keeps review paths separate so a background resource monitor does not become a general content automation service.

## Resource Activity menu

The menu-bar app exposes:

- **Review Recent I/O Incidents…** — opens maintenance status with monitor health, queued prompts, and recent incidents.
- **Sample CPU + Disk I/O (1 min)** — runs the existing manual process audit, which samples both sustained CPU and process I/O.

The resident resource monitor sends a notification when a qualifying incident opens. Every non-suppressed incident is queued, including a recurrence, and its review window can open only after a fresh HID sample reports 30 seconds to 5 minutes of quiet input. Notification delivery is deduplicated per process identity for six hours.

A queued process window is skipped if the process exits or its identity changes before review. The absence of a return-time window therefore does not prove that no incident was recorded; use `maint status` to inspect recent history.

## Refresh and review shortcuts

Use the canonical next-due command:

```bash
maint shortcuts
```

It selects the provider whose last successful display is oldest, refreshes its
content, and opens the review only when that refresh succeeds. The keyboard
provider remains the first choice on a fresh installation. An Apple provider
with no candidates is skipped. If a next-due provider fails, the other provider
is tried, and only a successful display updates rotation state.

Default command sequence:

```bash
$HOME/.local/bin/kb export-srs --mode focused --max-shortcut-cards 7 --underused-limit 0
$HOME/.local/bin/kb popup --surface gui --group auto --force
```

Use `maint shortcuts --provider keyboard` or `maint shortcuts --provider apple`
to bypass rotation and open a particular provider. The menu-bar app exposes the
same choices under **Review Shortcuts**. The existing global Hammerspoon binding
continues to call next-due `maint shortcuts`.

The Apple provider runs the local `Shortcut Review` workflow. It presents
advisory **Use Soon** and **Consider Removing** lists and records a UUID-keyed
decision; it never deletes an Apple Shortcut.

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
- invoke `open hammerspoon://resumerouter` (or its configured fallback);
- activate Obsidian and open Spaced Repetition for the active Markdown page.

The return detector persists its armed/cooldown state and records the most recent return-flow success or failure in resource-monitor health. This prevents a monitor restart from turning a single return into repeated resume launches.

Set `return_routing_enabled` to `false` to disable the contextual handoff while keeping HID sampling available for queued process reviews. `return_active_cutoff_seconds` controls how recently active the Mac must be before an armed return fires (30 seconds by default). `maint status` reports both the routing state and HID sampling failures.

The Hammerspoon coordinator asks wiki-automation for the highest-ranked TaskForge task. A TaskNote may save an exact digital work target using `resume_required`, `resume_kind`, `resume_target`, `resume_app`, `resume_profile`, `resume_label`, and `resume_confidence`. Exact mappings open immediately; inferred or missing mappings are confirmed in Hammerspoon and the selection is recorded back to the TaskNote.

Examples include a tuition URL, a direct subscription-email or draft link, a Canvas course page, or a saved interview-preparation conversation. Public docs and tests use synthetic domains and profile names; real targets remain in the private vault.

After an interactive app/process maintenance review, Idle Maintenance opens at
most one shortcut provider per cooldown window. The return flow separately
activates Obsidian and invokes the plugin command
`obsidian-spaced-repetition:srs-review-flashcards-in-note`, so the current page's
cards are available immediately. Set `return_obsidian_command` or
`return_obsidian_srs_command` to an empty value to omit that command; the
shortcuts provider remains independently controlled by
`show_shortcuts_on_finish`.

## Legacy watcher

**Start / Restart Away-Return Review** starts `idle_watcher.py` only for legacy/manual compatibility. It is no longer required for automatic return routing and should not be enabled alongside the resident return detector merely to obtain the resume handoff. Starting the legacy watcher still intentionally runs one review immediately, and `maint status` can report whether that optional process is running.
