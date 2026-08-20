#!/bin/bash
set -euo pipefail

DEST="$HOME/Library/Scripts/idle-maintenance"
mkdir -p "$DEST"

echo "Deploying Idle Maintenance to $DEST..."

# Copy runtime scripts together so imports cannot resolve to mixed generations.
cp app_auditor.py "$DEST/"
cp activity_intelligence.py "$DEST/"
cp idle_config.py "$DEST/"
cp idle_watcher.py "$DEST/"
cp maint.py "$DEST/"
cp maintenance_core.py "$DEST/"
cp maintenance_interactive.py "$DEST/"
cp maintenance_status.py "$DEST/"
cp maintenance_status_extended.py "$DEST/"
cp process_identity.py "$DEST/"
cp process_sampling.py "$DEST/"
cp process_triage.py "$DEST/"
cp process_review.py "$DEST/"
cp prompt_session.py "$DEST/"
cp review_ui.py "$DEST/"
cp prompt.swift "$DEST/"
cp restore_sources.py "$DEST/"
cp shortcut_review.py "$DEST/"
cp storage_cleanup.py "$DEST/"
cp storage_cleanup_core.py "$DEST/"
cp disk_activity.py "$DEST/"

# Preserve local settings and state.
cp -n config.json "$DEST/" 2>/dev/null || true
cp -n custom_whitelist.json "$DEST/" 2>/dev/null || true
cp -n stale_queue.json "$DEST/" 2>/dev/null || true

chmod +x "$DEST"/*.py
chmod +x "$DEST"/*.swift

echo "✓ Deployment complete."
