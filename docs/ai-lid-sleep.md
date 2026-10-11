# Sleep after closed-lid AI work

The optional AI sleep controller replaces an unconditional global sleep override
with a bounded decision: verified local AI work plus reachable internet. It checks
every 15 seconds and releases `pmset disablesleep` after 60 seconds without that
combination. This arms the override before closing the lid. Releasing it restores
normal macOS power management; it never issues forced sleep or terminates a job.

The independent [backlight guard](lid-backlight.md) still zeros only the built-in
brightness while closed and restores it on reopening. Other applications may
hold their own idle-sleep assertions. External-display clamshell mode also follows
macOS rules after the override is released.

## What counts as work

| Tool | Evidence | Limits |
| --- | --- | --- |
| Codex | A live Codex process holds an open rollout whose latest lifecycle event starts an unfinished turn, with no pending approval or user-input request | A turn with no rollout updates for 15 minutes stops qualifying; unreadable or unsupported lifecycle state is inactive |
| Claude Code | Lifecycle hooks with matching live process birth time and boot session | Hooks cover new turns after settings load; approval/elicitation waits and Stop/StopFailure/SessionEnd release activity; progress must be younger than 15 minutes |
| Cursor | Native thought/response/tool-return hooks with matching process birth time and generation identity | Prompt submission alone is tentative and does not qualify; pre-tool approval ambiguity suspends activity; progress expires after 60 seconds |
| Antigravity | Authenticated read-only loopback trajectory RPC reporting fresh local RUNNING state without waiting steps | Unknown/BUSY/cancelled/interrupted/cloud states do not qualify; last modification must be within 15 minutes |

Cursor has no authoritative persistent live-generation status in the inspected
app version. Its hooks describe milestones, not a continuous heartbeat. Long
silent generations or tool calls can therefore be allowed to sleep. The controller
prefers releasing the override when work cannot be verified. Claude and Cursor
may need a new session/app settings reload before their new hooks take effect;
the installer does not restart apps. No CPU or app-name-only heuristic grants
wakefulness. Unsupported Claude desktop chat is not Claude Code lifecycle work.

Internet checks use unauthenticated HTTPS HEAD requests to provider endpoints,
with short timeouts and no prompts, credentials, inference, or response-body
collection. A request is made only when local work qualifies. Internet reachability
does not prove a particular provider or job is healthy.

## Failure handling

The controller has a single-owner lock. Power mutation and lease publication share
a second lock with the watchdog. Successful iterations write an atomic 90-second
lease tied to process and boot, measured with a shared monotonic kernel clock. The separate watchdog runs every 60 seconds and
releases a stale/dead/previous-boot override. SIGTERM also releases it. Thus crash
recovery can take up to about 150 seconds while macOS can schedule the watchdog.
The same-user watchdog is unavailable after logout or if launchd itself cannot
run. No software watchdog can provide a time bound while the operating system is
stalled. There is no TTL in the underlying global `pmset` setting itself.

The controller reconciles the global setting on each check. Manual writes from
another owner may be overwritten; do not run a second controller for that setting.
Invalid or stale provider state cannot renew awake eligibility. Errors never log
hook inputs, process arguments, provider tokens, trajectory identifiers, or text.
Hook state retains only hashed identifiers and lifecycle/process metadata.
Hook events are durably queued before taking the shared state lock. Stop and
approval events survive lock contention; delayed progress cannot reopen a stopped
generation or bypass an approval wait. Parallel tool returns clear only their
matching hashed wait IDs. Claude waits without tool IDs require a complete
PostToolBatch event before resuming. Session-end cutoffs also reject earlier
events that arrive late. Claude requires a newer prompt submission to reopen a
stopped turn.

## Install and inspect

Requires macOS system Python 3 and existing noninteractive sudo permission for
exactly `pmset -a disablesleep 0` and `pmset -a disablesleep 1`. The installer does
not modify sudo permissions or credentials.

```sh
/usr/bin/python3 scripts/install_ai_sleep.py
/usr/bin/python3 scripts/ai_sleep.py --status
```

The installer adds only its own entries to Claude and Cursor hook settings,
preserving unrelated settings and hooks, with private original-file backups under
`~/Library/Application Support/idle-maintenance/ai-sleep/settings-backups/`.
It installs `com.john.idle-maintenance.ai-sleep` and
`com.john.idle-maintenance.ai-sleep-watchdog` LaunchAgents. Runtime, state, and logs
use the existing `Library/Scripts/idle-maintenance`,
`Library/Application Support/idle-maintenance`, and `Library/Logs/idle-maintenance`
directories. The status command reads live state without changing power settings.

To disable the controller, open the lid, boot out both LaunchAgents, remove their
plist files, and restore normal sleep with `sudo pmset -a disablesleep 0`. Remove
only the commands ending in `ai_sleep.py --hook claude` / `--hook cursor` from hook
settings. Keep unrelated hook entries intact. Leave the backlight agent installed
if desired.

Acceptance must distinguish synthetic lifecycle tests, live API/state readback,
and physical closed-lid sleep. A live active-job transition is not proven by an
idle API response or by installing hooks. Tests never disconnect the user's
network, submit AI work, activate windows, or force the machine to sleep.
