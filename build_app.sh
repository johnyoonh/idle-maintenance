#!/bin/bash
set -euo pipefail

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
DEST_DIR="${1:-$HOME/Applications}"
APP_PATH="$DEST_DIR/IdleMaintenance.app"
CODESIGN_IDENTITY="${CODESIGN_IDENTITY:--}"
XCRUN="${XCRUN:-/usr/bin/xcrun}"

if [ "$#" -gt 1 ]; then
  echo "Usage: $0 [destination-directory]" >&2
  exit 2
fi

mkdir -p "$DEST_DIR"
DEST_DIR="$(cd "$DEST_DIR" && pwd -P)"
APP_PATH="$DEST_DIR/IdleMaintenance.app"
STAGE_ROOT="$(mktemp -d "$DEST_DIR/.idle-maintenance-build.XXXXXX")"
STAGED_APP="$STAGE_ROOT/IdleMaintenance.app"
TMP_CORE="$(mktemp "$SRC_DIR/.build_app_core.overlay.XXXXXX")"
BACKUP_PATH=""

safe_remove_tree() {
  local path="$1"
  case "$path" in
    "$DEST_DIR"/.idle-maintenance-build.*|"$DEST_DIR"/.IdleMaintenance.app.backup.*)
      rm -rf -- "$path"
      ;;
    "")
      ;;
    *)
      echo "Refusing unsafe cleanup target: $path" >&2
      return 1
      ;;
  esac
}

cleanup() {
  rm -f -- "$TMP_CORE"
  if [ -n "$BACKUP_PATH" ] && [ -e "$BACKUP_PATH" ] && [ ! -e "$APP_PATH" ]; then
    mv -- "$BACKUP_PATH" "$APP_PATH" 2>/dev/null || true
  fi
  safe_remove_tree "$STAGE_ROOT" || true
}
trap cleanup EXIT HUP INT TERM

python3 "$SRC_DIR/build_app_overlay.py" "$SRC_DIR/build_app_core.sh" "$TMP_CORE"
python3 "$SRC_DIR/build_app_style_fix.py" "$TMP_CORE" "$TMP_CORE"
chmod +x "$TMP_CORE"

# The legacy builder is intentionally confined to a same-filesystem staging
# directory. The installed app remains untouched through compile and signing.
CODESIGN_IDENTITY="$CODESIGN_IDENTITY" "$TMP_CORE" "$STAGE_ROOT"

RES_DIR="$STAGED_APP/Contents/Resources/maintenance"
cp "$SRC_DIR"/{activity_intelligence.py,app_actions.py,maintenance_core.py,process_identity.py,process_sampling.py,process_triage.py,process_review.py,prompt_session.py,review_ui.py,resource_monitor.py,storage_cleanup_core.py,disk_activity.py,maint.py,shortcut_review.py,maintenance_status_extended.py} "$RES_DIR/"

# Compile the AppKit review helper before signing so normal launches never pay
# Swift interpreter startup cost or compile while the user is choosing actions.
"$XCRUN" swiftc -O -framework AppKit "$SRC_DIR/prompt.swift" -o "$RES_DIR/IdleMaintenancePrompt"
chmod +x "$RES_DIR/IdleMaintenancePrompt"

codesign --force --deep --sign "$CODESIGN_IDENTITY" "$STAGED_APP" >/dev/null
codesign --verify --deep --strict "$STAGED_APP"

BACKUP_PATH="$DEST_DIR/.IdleMaintenance.app.backup.$$"
if [ -e "$APP_PATH" ]; then
  mv -- "$APP_PATH" "$BACKUP_PATH"
fi

if ! mv -- "$STAGED_APP" "$APP_PATH"; then
  if [ -e "$BACKUP_PATH" ]; then
    mv -- "$BACKUP_PATH" "$APP_PATH"
  fi
  echo "Failed to install staged IdleMaintenance.app; restored the previous app." >&2
  exit 1
fi

if [ -e "$BACKUP_PATH" ]; then
  safe_remove_tree "$BACKUP_PATH"
fi
BACKUP_PATH=""
safe_remove_tree "$STAGE_ROOT"
STAGE_ROOT=""
rm -f -- "$TMP_CORE"
TMP_CORE=""
trap - EXIT HUP INT TERM

echo "Built and installed atomically: $APP_PATH"
echo "Launch with: open \"$APP_PATH\""
