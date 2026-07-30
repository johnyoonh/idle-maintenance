#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST_DIR="${IDLE_MAINTENANCE_APP_DIR:-$HOME/Applications}"
APP_PATH="$DEST_DIR/IdleMaintenance.app"
LOG_DIR="${IDLE_MAINTENANCE_LOG_DIR:-$HOME/Library/Logs/idle-maintenance}"
LOG_FILE="$LOG_DIR/post-sync-build.log"
LOCK_DIR="${IDLE_MAINTENANCE_POST_SYNC_LOCK:-${TMPDIR:-/tmp}/idle-maintenance-post-sync.lock}"
DRY_RUN=0

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    *) echo "Unknown argument: $arg" >&2; exit 2 ;;
  esac
done

mkdir -p "$LOG_DIR"
log() {
  printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$LOG_FILE"
}

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  log "post-sync rebuild already running; skipping duplicate trigger"
  exit 0
fi
trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT HUP INT TERM

if [[ "$(uname -s)" != "Darwin" && "${IDLE_MAINTENANCE_TEST_MODE:-0}" != "1" ]]; then
  log "post-sync rebuild skipped: macOS is required"
  exit 0
fi

resolve_identity() {
  if [[ -n "${CODESIGN_IDENTITY:-}" ]]; then
    printf '%s\n' "$CODESIGN_IDENTITY"
    return 0
  fi
  local security_bin="${IDLE_MAINTENANCE_SECURITY_BIN:-security}"
  if command -v "$security_bin" >/dev/null 2>&1; then
    local identity
    identity="$($security_bin find-identity -v -p codesigning 2>/dev/null | awk '/Apple Development/ {print $2; exit}')"
    if [[ -n "$identity" ]]; then
      printf '%s\n' "$identity"
      return 0
    fi
  fi
  if [[ "${IDLE_MAINTENANCE_ALLOW_ADHOC:-0}" == "1" ]]; then
    printf '%s\n' '-'
    return 0
  fi
  return 1
}

identity="$(resolve_identity || true)"
if [[ -z "$identity" ]]; then
  log "post-sync rebuild blocked: no Apple Development signing identity; set IDLE_MAINTENANCE_ALLOW_ADHOC=1 only for an explicit ad-hoc fallback"
  exit 78
fi

pgrep_bin="${IDLE_MAINTENANCE_PGREP_BIN:-pgrep}"
pkill_bin="${IDLE_MAINTENANCE_PKILL_BIN:-pkill}"
open_bin="${IDLE_MAINTENANCE_OPEN_BIN:-open}"
sleep_bin="${IDLE_MAINTENANCE_SLEEP_BIN:-sleep}"
restart_attempts="${IDLE_MAINTENANCE_RESTART_ATTEMPTS:-50}"
restart_sleep_seconds="${IDLE_MAINTENANCE_RESTART_SLEEP_SECONDS:-0.2}"
was_running=0
if command -v "$pgrep_bin" >/dev/null 2>&1 && "$pgrep_bin" -x IdleMaintenance >/dev/null 2>&1; then
  was_running=1
fi

if [[ "$DRY_RUN" == "1" ]]; then
  log "dry-run: CODESIGN_IDENTITY=$identity $ROOT/build_app.sh $DEST_DIR; restart=$was_running"
  exit 0
fi

log "building IdleMaintenance.app from synced revision ${REPO_SYNC_NEW_HEAD:-unknown}"
CODESIGN_IDENTITY="$identity" "$ROOT/build_app.sh" "$DEST_DIR" >>"$LOG_FILE" 2>&1

if [[ "$was_running" == "1" ]]; then
  log "restarting the existing menu-bar process with the rebuilt app"
  "$pkill_bin" -TERM -x IdleMaintenance 2>/dev/null || true
  for ((attempt = 0; attempt < restart_attempts; attempt++)); do
    if ! "$pgrep_bin" -x IdleMaintenance >/dev/null 2>&1; then
      break
    fi
    "$sleep_bin" "$restart_sleep_seconds"
  done
  if "$pgrep_bin" -x IdleMaintenance >/dev/null 2>&1; then
    log "post-sync rebuild installed the new app, but the prior process did not exit after SIGTERM; leaving it closed to a later manual restart"
    exit 75
  fi
  "$open_bin" -g "$APP_PATH"
fi

log "post-sync rebuild completed"
