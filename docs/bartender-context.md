# Bartender return-status bridge

`bartender_context.py` is a read-only consumer of the resident resource monitor
and an optional publisher to the separate Bartender context controller. It does
not start maintenance, launch applications, record input/audio, or change any
Bartender preferences. It does not replace the resident return detector.

## Signals and ownership

The Hammerspoon bridge invokes `publish` asynchronously every ten seconds using
the Python interpreter already installed for the Bartender LaunchAgent.
It supplies the existing Zoom meeting signal and explicitly registered,
short-lived recording/job events. This module emits producer-scoped leases
through the controller's `emit` function; it never writes the controller's
`state.json` or another producer's leases.

Return visibility comes from a fresh resident-monitor snapshot:

* A false-to-true `return_pending` transition observed while the bridge is
  running starts a bounded, 120-second visibility window. A private checkpoint
  preserves that edge across short reloads. A continuously pending review does
  not repeatedly restart the window.
* A recent `last_return_flow_at` is also usable, whether the return callback
  succeeded or failed. The expiry is anchored to the recorded timestamp, not
  renewed for another two minutes each time the file is polled.
* Starting the bridge while a review is already pending does not manufacture
  an event. Missing, malformed, stale, future-dated, disabled, or unsupported
  monitor state fails closed. A publisher gap over 45 seconds establishes a new
  baseline. The monitor freshness limit defaults to 90 seconds.

The resident monitor's existing away threshold, quiet-input gate, routing
setting, and cooldown remain authoritative and unchanged. Returns that the
monitor does not record are not invented by this bridge. A pending transition
missed while Hammerspoon is stopped can be missed; a long-running return callback
may also outlive the fallback timestamp window.

## Installation

Update this repository, Hammerspoon's `lib/bartender_bridge.lua`, and the
Bartender controller with `idle` lease support together. The default monitor
file is `$HOME/Library/Application Support/idle-maintenance/resource-monitor-state.json`.
Use Hammerspoon's `bartenderContext.monitorState` setting for a custom path.

Read-only inspection, from this repository:

```sh
uv run --python 3.12 bartender_context.py snapshot
uv run --python 3.12 -m unittest discover -s tests -p 'test_bartender_context.py' -v
```

Snapshot output is a small allowlisted projection: health, pending status,
visibility, timing, and a reason code. It never prints incident descriptions,
process arguments, private paths, or exception details. The two-minute pending
edge is evaluated by `publish`; `snapshot` alone intentionally writes nothing.

`publish` reads at most 64 KiB of JSON from standard input:

```json
{"schema":1,"at":1700000000,"meeting":false,"events":[{"signal":"ai_cursor","source":"job1","expires_at":1700000030}]}
```

The timestamp above is illustrative and deliberately stale. Live callers must
supply the current Unix time. Requests expire after 15 seconds, contain at most
64 unique channel/source pairs, and use opaque IDs of at most 64 characters.
Allowed event channels are `wispr`, `meeting`, `ai_openai`, `ai_cursor`,
`ai_antigravity`, and `ai_copilot`. An expired `expires_at`, including zero,
clears only that source. Do not use document titles, URLs, prompts, or user text
as source IDs. Polling never extends an explicit event's original deadline.

`--controller`, `--controller-state`, `--monitor-state`, `--window`, and `--stale`
are explicit local path/time overrides. No executable path or command is
accepted in an event. The controller module is trusted local code and must be
user-owned and not writable by another user. An older controller without idle
leases is rejected before publication.

The checkpoint and short leases stay under the private, unsynced controller
state directory. A stopped Hammerspoon producer's leases expire within 30
seconds, followed by the controller's existing voice/AI cooldowns. No recording
or AI application is assumed busy merely because it is open or using the mic.
