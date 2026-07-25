#!/bin/bash
set -euo pipefail
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
(cd "$SRC_DIR" && ./deploy_core.sh)
DEST="$HOME/Library/Scripts/idle-maintenance"
cp "$SRC_DIR"/{maintenance_core.py,process_identity.py,process_sampling.py,process_review.py,storage_cleanup_core.py,disk_activity.py} "$DEST/"
chmod +x "$DEST"/*.py
