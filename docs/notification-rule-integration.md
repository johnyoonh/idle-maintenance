# Shared notification-rule boundary

The existing Idle Maintenance shortcut-review provider rotates **keyboard**
and **Apple Shortcuts** based on their prior usage. It retains its automatic
cooldowns and the browser shortcut audit.

On automatic review only, `shortcut_review.py` now reads
`~/Library/Application Support/wiki-automation/notification-rules.json`,
or `WIKI_NOTIFICATION_RULES_PATH` when explicitly configured.

- `idle-maintenance.shortcut-review = action` or `critical` keeps the
  existing automatic review behavior.
- `silent` or `digest` skips the automatic popup and its associated
  refresh/audit work.
- The explicit `maint shortcuts` and provider-specific manual review remain
  available regardless of this setting.
- A missing or invalid policy file preserves the old behavior.

Other Idle Maintenance notifications (resource monitor and backup overdue)
retain their independent safety thresholds and are **not** silently disabled
by the wiki dashboard. The dashboard describes those dependencies but marks
them as externally controlled.

Test with `python3 -m unittest tests/test_shortcut_review.py`.
