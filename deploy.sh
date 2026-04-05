#!/bin/bash

# Target directory
DEST="$HOME/Library/Scripts/idle-maintenance"

# Create destination if it doesn't exist
mkdir -p "$DEST"

echo "Deploying Idle Maintenance to $DEST..."

# 1. Copy core scripts (always overwrite with latest logic)
cp app_auditor.py "$DEST/"
cp idle_watcher.py "$DEST/"
cp maintenance_interactive.py "$DEST/"
cp prompt.swift "$DEST/"

# 2. Copy data/config files ONLY if they don't exist (preserve user settings/state)
cp -n config.json "$DEST/" 2>/dev/null
cp -n custom_whitelist.json "$DEST/" 2>/dev/null
cp -n stale_queue.json "$DEST/" 2>/dev/null

chmod +x "$DEST"/*.py
chmod +x "$DEST"/*.swift

echo "✓ Deployment complete."
