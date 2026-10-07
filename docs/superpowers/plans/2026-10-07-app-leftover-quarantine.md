# App leftover quarantine port

## Outcome and invariants

Port the conservative config-leftover review from the stale branch to current `main`. App config is only moved when local config explicitly enables quarantine. The prompt shows eligible paths before the user chooses Delete. App Trash success remains success even if a later config quarantine operation fails.

## Files and responsibilities

- `app_leftovers.py`: discover bounded, non-symlink config candidates; summarize/display eligible candidates; quarantine with collision-safe destinations and a per-item ledger.
- `idle_config.py`: add conservative size limits and make quarantine disabled by default.
- `maintenance_interactive.py`: show paths only when quarantine is explicitly enabled.
- `maintenance_core.py`: revalidate and perform quarantine after the app Trash move; keep app deletion ledger and result authoritative if quarantine fails.
- `tests/test_app_leftovers.py`, `tests/test_app_actions.py`: cover candidate filtering, opt-in, collision handling, and partial success.
- `docs/app-cleanup-policy.md`: document the opt-in and recovery boundary.

## Checkpoints

1. Add synthetic regression tests and observe expected failures.
2. Implement the helper and integrate the opt-in policy in the current review and deletion flow.
3. Run focused tests, then `uv run python -m unittest discover -s tests`; inspect the final diff and public-safety boundaries.
4. Commit, push, open a PR, obtain independent review, and merge after required verification.
