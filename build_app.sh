#!/bin/bash
set -euo pipefail
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
DEST_DIR="${1:-$HOME/Applications}"
APP_PATH="$DEST_DIR/IdleMaintenance.app"
CODESIGN_IDENTITY="${CODESIGN_IDENTITY:--}"
TMP_CORE="$(mktemp "$SRC_DIR/.build_app_core.overlay.XXXXXX")"
cleanup() {
  rm -f "$TMP_CORE"
}
trap cleanup EXIT

python3 "$SRC_DIR/build_app_overlay.py" "$SRC_DIR/build_app_core.sh" "$TMP_CORE"
chmod +x "$TMP_CORE"
"$TMP_CORE" "$@"

RES_DIR="$APP_PATH/Contents/Resources/maintenance"
cp "$SRC_DIR"/{maintenance_core.py,process_identity.py,process_sampling.py,process_review.py,storage_cleanup_core.py,disk_activity.py,maint.py,shortcut_review.py,maintenance_status_extended.py} "$RES_DIR/"
codesign --force --deep --sign "$CODESIGN_IDENTITY" "$APP_PATH" >/dev/null
codesign --verify --deep --strict "$APP_PATH"
