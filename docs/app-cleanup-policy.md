# App Cleanup Policy

`idle-maintenance` can review stale macOS apps and move selected apps to Trash. Destructive behavior is intentionally controlled by configuration, not by project-specific backup assumptions.

The core rule is:

```text
idle-maintenance owns the cleanup lifecycle.
Local config owns the deletion policy.
```

## Config Loading

Configuration is loaded from the first existing file:

```text
~/.config/idle-watcher/config.json
~/Library/Application Support/idle-maintenance/config.json
<repo>/config.json
```

Nested config objects are merged with defaults.

## Example

```json
{
  "app_cleanup": {
    "delete_mode": "trash",
    "allow_unknown_restore_source": false,
    "review_budget_per_run": 1,
    "deletion_ledger": "~/Library/Application Support/idle-maintenance/app-deletions.jsonl",
    "restore_sources": [
      {
        "type": "homebrew_bundle",
        "path": "~/repos/Brewfile"
      },
      {
        "type": "mas_tsv",
        "path": "~/repos/app-store-apps.tsv"
      }
    ],
    "leftover_review": {
      "enabled": true,
      "mode": "conservative",
      "action": "quarantine",
      "max_item_size_mb": 25,
      "max_total_size_mb": 250,
      "quarantine_dir": "~/Library/Application Support/idle-maintenance/quarantine",
      "ledger": "~/Library/Application Support/idle-maintenance/config-quarantine.jsonl"
    }
  },
  "hooks": {
    "before_delete_app": [
      "~/bin/idle-maintenance-before-delete"
    ],
    "after_delete_app": [
      "~/bin/idle-maintenance-after-delete"
    ]
  }
}
```

## Options

`app_cleanup.delete_mode`

Only `trash` is currently supported. Unsupported modes are refused.

`app_cleanup.allow_unknown_restore_source`

When `false`, Delete is refused for an app that cannot be matched to a configured restore source. The app can still be kept, skipped, or opened for review.

`app_cleanup.deletion_ledger`

Path to a JSONL ledger. Each deletion writes one line with the app path, bundle id, version, Trash path, restore source, restore command, and timestamp.

`app_cleanup.restore_sources`

Ordered list of providers used to decide whether an app is recoverable.

`app_cleanup.review_budget_per_run`

Maximum number of stale app prompts shown by one app-cleanup run. The default is `1`, so each prompt can include both the app decision and related config cleanup without turning an idle return into a long review session.

`app_cleanup.leftover_review`

Optional review of likely app-related config files. In `conservative` mode, idle-maintenance only considers common config locations such as app support directories, bundle-id preference plists, and matching `.config` / `.local/share` directories. Eligible leftovers are quarantined after the app is moved to Trash.

The leftover quarantine ledger is JSONL. Each entry records the original path, quarantine path, app path, bundle id, size, and whether the item appeared to be Mackup-backed or yadm-tracked. Large app data, caches, logs, symlinks, and package installs are ignored by default.

## Restore Source Providers

`homebrew_bundle`

Reads a Brewfile and recognizes:

```ruby
cask "google-chrome"
mas "Command X", id: 6448461551
```

`mas_tsv`

Reads a tab-separated inventory:

```text
app_id	name	version
6448461551	Command X	1.7.0
```

## Hooks

Hooks let local policy veto or observe destructive actions without modifying the project.

Before-delete hooks run before the app is moved to Trash:

```bash
hook "$APP_PATH" "$DELETE_CONTEXT_JSON"
```

A non-zero exit code vetoes deletion.

After-delete hooks run after a successful Trash move and receive the ledger entry as JSON:

```bash
hook "$APP_PATH" "$DELETE_RESULT_JSON"
```

Example veto hook:

```bash
#!/bin/sh
context_json="$2"

if printf '%s' "$context_json" | jq -e '.restore_source.source == "unknown"' >/dev/null; then
  echo "Refusing delete: unknown restore source" >&2
  exit 20
fi

exit 0
```

## Public Project Boundary

Good public defaults:

- move to Trash only;
- log every deletion;
- support restore-source providers;
- support before/after hooks;
- refuse unknown restore sources when configured.

Local-only policy belongs in config or hooks:

- personal Brewfile paths;
- cloud drive paths;
- dotfile managers;
- backup health checks;
- machine-specific app inventories.
