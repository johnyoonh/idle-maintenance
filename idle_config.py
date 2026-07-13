import json
import os
import time
from datetime import datetime

APP_SUPPORT_DIR = os.path.expanduser("~/Library/Application Support/idle-maintenance")
XDG_CONFIG_DIR = os.path.expanduser("~/.config/idle-watcher")
DEFAULT_HANDOFF_APP = "TaskForge"
DEFAULT_HANDOFF_URL = "taskforge://upcoming"
LOCAL_BIN_DIR = os.path.expanduser("~/.local/bin")

DEFAULT_CONFIG = {
    "handoff_app": DEFAULT_HANDOFF_APP,
    "handoff_url": DEFAULT_HANDOFF_URL,
    "show_shortcuts_on_finish": True,
    "shortcut_review_command": f"{LOCAL_BIN_DIR}/kb popup --surface gui --group auto --force",
    "idle_threshold_minutes": 10,
    "check_interval_seconds": 30,
    "post_trigger_cooldown_seconds": 3600,
    "stale_days_limit": 90,
    "keep_days_limit": 60,
    "keep_backoff_multiplier": 2.0,
    "keep_backoff_max_days": 365,
    "max_prompts": 5,
    "max_entries_per_idle_return": 5,
    "close_on_unfocus": True,
    "app_usage_minimum_dwell_seconds": 120,
    "process_max_prompts": 5,
    "process_high_cpu_threshold": 50.0,
    "process_long_running_hours": 24,
    "process_long_running_min_cpu": 10.0,
    "process_ignore_commands": [],
    "process_keep_days_limit": 1,
    "process_keep_backoff_multiplier": 2.0,
    "process_keep_backoff_max_days": 30,
    "process_snooze_hours": 24,
    "process_cpu_sample_count": 3,
    "process_cpu_sample_interval_seconds": 30,
    "app_snooze_hours": 720,
    "terminal_suggestion_start_hour": 9,
    "terminal_suggestion_end_hour": 21,
    "scheduled_runner_status_command": [],
    "return_from_away_minutes": 15,
    "return_shortcut_popup_command": f"{LOCAL_BIN_DIR}/kb popup --surface gui --group auto --force",
    "return_flashcard_refresh_command": f"{LOCAL_BIN_DIR}/kb export-srs --mode focused --max-shortcut-cards 7 --underused-limit 0",
    "return_handoff_command": "open -a Flow",
    "return_handoff_delay_seconds": 90,
    "storage_cleanup": {
        "enabled": True,
        "dry_run": False,
        "cache_retention_days": 30,
        "log_retention_days": 30,
        "trash_retention_days": 30,
        "xcode_derived_data_retention_days": 14,
        "xcode_archive_retention_days": 90,
        "delete_screenpipe": True,
        "minimum_free_gb": 100,
        "large_path_warning_gb": 5,
        "run_package_cleaners": True,
        "delete_workers": 8,
        "protected_paths": [
            "~/.cache/aisess",
            "~/.cache/opencode",
            "~/.cache/codex",
            "~/.cache/claude",
            "~/.cache/gemini",
            "~/.cache/cursor",
            "~/.cache/agy",
            "~/.cache/qwen",
            "~/.cache/deepseek",
            "~/.local/share/aisess",
            "~/.local/share/opencode",
            "~/.local/state/agent-blame",
            "~/.codex",
            "~/.claude",
            "~/.gemini",
            "~/.cursor",
            "~/Library/Application Support/aisess-prompt-index",
        ],
    },
    "app_cleanup": {
        "delete_mode": "trash",
        "allow_unknown_restore_source": False,
        "deletion_ledger": "~/Library/Application Support/idle-maintenance/app-deletions.jsonl",
        "restore_sources": [],
    },
    "hooks": {
        "before_delete_app": [],
        "after_delete_app": [],
    },
}

def read_json_file(path):
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return None

def config_paths(base_dir=None):
    paths = [
        os.path.join(XDG_CONFIG_DIR, "config.json"),
        os.path.join(APP_SUPPORT_DIR, "config.json"),
    ]
    if base_dir:
        paths.append(os.path.join(base_dir, "config.json"))
    return paths

def deep_merge(base, override):
    merged = base.copy()
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged

def load_config(base_dir=None, defaults=None):
    merged = DEFAULT_CONFIG.copy()
    if defaults:
        merged = deep_merge(merged, defaults)

    for path in config_paths(base_dir):
        cfg = read_json_file(path)
        if isinstance(cfg, dict):
            merged = deep_merge(merged, cfg)
            break

    return merged

def get_handoff_app(config):
    target_app = config.get("handoff_app")
    if target_app is not None:
        return target_app

    target_app = config.get("on_finish_app", DEFAULT_HANDOFF_APP)
    if target_app in {"TickTick", "Ticktick"}:
        target_app = DEFAULT_HANDOFF_APP
    return target_app

def get_handoff_url(config):
    return config.get("handoff_url", DEFAULT_HANDOFF_URL)

def get_shortcut_review_command(config):
    if config.get("show_shortcuts_on_finish", True):
        return config.get("shortcut_review_command") or DEFAULT_CONFIG["shortcut_review_command"]
    return ""

def parse_keep_entry(value):
    if isinstance(value, dict):
        kept_at = value.get("kept_at", value.get("timestamp"))
        keep_count = value.get("keep_count", 1)
        try:
            return {
                "kept_at": float(kept_at),
                "keep_count": max(1, int(keep_count)),
            }
        except (TypeError, ValueError):
            return None
    try:
        return {"kept_at": float(value), "keep_count": 1}
    except (TypeError, ValueError):
        return None

def get_keep_delay_days(config, keep_count, prefix=""):
    base_days = max(0.0, float(config.get(f"{prefix}keep_days_limit", 1)))
    multiplier = max(1.0, float(config.get(f"{prefix}keep_backoff_multiplier", 2.0)))
    max_days = max(base_days, float(config.get(f"{prefix}keep_backoff_max_days", 30)))
    delay = base_days * (multiplier ** max(0, keep_count - 1))
    return min(delay, max_days)

def keep_entry_is_active(config, entry, prefix="", now=None):
    keep_entry = parse_keep_entry(entry)
    if not keep_entry:
        return False
    current_time = time.time() if now is None else float(now)
    keep_delay_days = get_keep_delay_days(config, keep_entry["keep_count"], prefix)
    return current_time - keep_entry["kept_at"] <= keep_delay_days * 86400

def next_keep_delay_days(config, entry, prefix=""):
    keep_entry = parse_keep_entry(entry)
    next_count = 1 if not keep_entry else keep_entry["keep_count"] + 1
    return get_keep_delay_days(config, next_count, prefix)

def is_terminal_suggestion_time(config, now=None):
    current_hour = (now or datetime.now()).hour
    try:
        start = int(config.get("terminal_suggestion_start_hour", 9))
        end = int(config.get("terminal_suggestion_end_hour", 21))
        if not 0 <= start <= 23 or not 0 <= end <= 23:
            raise ValueError
    except (TypeError, ValueError):
        start, end = 9, 21

    if start == end:
        return True
    if start < end:
        return start <= current_hour < end
    return current_hour >= start or current_hour < end
