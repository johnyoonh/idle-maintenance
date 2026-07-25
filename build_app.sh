#!/bin/bash
set -euo pipefail
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
DEST_DIR="${1:-$HOME/Applications}"
APP_PATH="$DEST_DIR/IdleMaintenance.app"
CODESIGN_IDENTITY="${CODESIGN_IDENTITY:--}"
"$SRC_DIR/build_app_core.sh" "$@"
RES_DIR="$APP_PATH/Contents/Resources/maintenance"
cp "$SRC_DIR"/{maintenance_core.py,process_identity.py,process_sampling.py,process_review.py,storage_cleanup_core.py,disk_activity.py} "$RES_DIR/"
codesign --force --deep --sign "$CODESIGN_IDENTITY" "$APP_PATH" >/dev/null
codesign --verify --deep --strict "$APP_PATH"
