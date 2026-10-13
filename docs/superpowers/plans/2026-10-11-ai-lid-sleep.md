# Conditional closed-lid AI work plan

**Goal:** Keep a closed Mac awake only for verified local Codex, Claude, Cursor,
or Antigravity work while internet is reachable; otherwise restore normal sleep.

**Design:** A separate per-user controller owns `pmset disablesleep`, using
bounded read-only lifecycle probes and a 60-second release grace. The existing
backlight guard continues independently. A second scheduled watchdog clears the
override if the controller's short lease expires, including after crashes or a
reboot. Open applications and CPU usage are not job evidence. Hook integrations
preserve unrelated configuration, collect no prompts, and execute no AI work.

**Constraints:** No window focus, job submission, app termination, network
disconnection, immediate forced sleep, new credentials, or external data sharing.
Network checks send an unauthenticated request only to provider endpoints.
No repository-wide deployment or changes to unrelated automation.

- [x] Capture global unconditional sleep override and existing heuristic behavior.
- [x] Add failing synthetic regressions for job completion/cancellation, offline
  grace, stale/missing state, restart, watchdog expiry, duplicate ownership,
  command failure/readback, and provider lifecycle classification.
- [x] Implement bounded provider probes, policy, atomic lease, and watchdog.
- [x] Add an installer for only this controller/watchdog and necessary hooks;
  preserve and back up existing settings before scoped changes.
- [x] Run focused/full tests, syntax/diff checks, and independent review.
- [x] Install and verify live probe decisions, service ownership, lease freshness,
  unchanged backlight guard, and actual resulting power setting. Do not simulate
  offline state by disconnecting the user's network or force sleep during work.
- [x] Commit and push the focused branch; report physical sleep evidence separately.
