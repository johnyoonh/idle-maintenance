# Leftover Config Review Continuation Notes

Branch: `feature/app-leftover-config-review`

Worktree:

```bash
/Users/john/github/idle-maintenance-leftover-review
```

Pull request:

```text
https://github.com/johnyoonh/idle-maintenance/pull/new/feature/app-leftover-config-review
```

## Why This Exists

This branch explores whether app cleanup should also review likely app-related config leftovers. The concern was that deleting an app may leave stale config/data behind, but full config cleanup could become too noisy or too risky for `main`.

The compromise implemented here:

- review only one app per idle cleanup run;
- detect leftovers conservatively;
- quarantine eligible config leftovers after app deletion;
- never permanently delete leftover config;
- keep Mackup/yadm as annotations, not cleanup executors.

## Implemented Behavior

New config defaults live in `idle_config.py`:

- `app_cleanup.review_budget_per_run: 1`
- `app_cleanup.leftover_review.enabled: true`
- `app_cleanup.leftover_review.mode: "conservative"`
- `app_cleanup.leftover_review.action: "quarantine"`
- `app_cleanup.leftover_review.max_item_size_mb: 25`
- `app_cleanup.leftover_review.max_total_size_mb: 250`
- `app_cleanup.leftover_review.quarantine_dir`
- `app_cleanup.leftover_review.ledger`

New module:

```text
app_leftovers.py
```

It detects likely config leftovers under:

- `~/Library/Application Support/<AppName>`
- `~/Library/Preferences/<bundle-id>.plist`
- matching files in `~/Library/Preferences`
- `~/.config/<normalized-app-name>`
- `~/.local/share/<normalized-app-name>`

It excludes caches, logs, saved app state, symlinks, and files over configured size limits.

## Verification Already Run

```bash
/usr/bin/python3 -m unittest tests.test_app_leftovers
/usr/bin/python3 -m py_compile idle_config.py restore_sources.py app_leftovers.py maintenance_interactive.py app_auditor.py
```

Also ran a fake app smoke test:

- fake `.app` moved to Trash;
- fake `Library/Application Support/<AppName>` moved to quarantine;
- app deletion ledger written;
- config quarantine ledger written.

## Things To Reconsider Before Merging

- Whether leftover review belongs in `idle-maintenance` at all.
- Whether the prompt UI has enough space for restore + leftover summaries.
- Whether `review_budget_per_run: 1` should apply only to app cleanup or also reduce total process/app prompts.
- Whether Mackup/yadm annotations are useful enough to keep.
- Whether quarantine should be opt-in rather than enabled by default.

## Current Recommendation

Keep this as a branch until it has been used manually with a few fake or low-risk apps. If it feels too noisy, keep only `review_budget_per_run: 1` and drop leftover quarantine from `idle-maintenance`.
