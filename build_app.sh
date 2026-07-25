#!/bin/bash
set -euo pipefail
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
DEST_DIR="${1:-$HOME/Applications}"
"$SRC_DIR/build_app_core.sh" "$@"
RES_DIR="$DEST_DIR/IdleMaintenance.app/Contents/Resources/maintenance"
cp "$SRC_DIR"/{maintenance_core.py,process_identity.py,process_sampling.py,process_review.py,storage_cleanup_core.py,disk_activity.py} "$RES_DIR/"
