#!/bin/bash
set -euo pipefail

DEST="$HOME/Library/Scripts/idle-maintenance"
mkdir -p "$DEST"

echo "Deploying Idle Maintenance to $DEST..."

# Copy runtime scripts together so imports cannot resolve to mixed generations.
cp app_auditor.py "$DEST/"
cp idle_config.py "$DEST/"
cp idle_watcher.py "$DEST/"
cp maint.py "$DEST/"
cp maintenance_interactive.py "$DEST/"
cp maintenance_status.py "$DEST/"
cp maintenance_status_extended.py "$DEST/"
cp prompt.swift "$DEST/"
cp shortcut_review.py "$DEST/"
cp storage_cleanup.py "$DEST/"

# Preserve local settings and state.
cp -n config.json "$DEST/" 2>/dev/null || true
cp -n custom_whitelist.json "$DEST/" 2>/dev/null || true
cp -n stale_queue.json "$DEST/" 2>/dev/null || true

chmod +x "$DEST"/*.py
chmod +x "$DEST"/*.swift

echo "✓ Deployment complete."
