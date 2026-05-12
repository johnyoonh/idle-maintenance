#!/usr/bin/python3
import subprocess
import os
from datetime import datetime
from idle_config import APP_SUPPORT_DIR, get_handoff_app, load_config, read_json_file

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = APP_SUPPORT_DIR
WHITELIST_PATH = os.path.join(STATE_DIR, "custom_whitelist.json")
APP_USAGE_PATH = os.path.join(STATE_DIR, "app_usage.json")

# Apps we never want to flag
WHITELIST = [
    "Safari.app", "Mail.app", "Messages.app", "Photos.app", 
    "Calendar.app", "Notes.app", "Reminders.app", "Numbers.app", 
    "Pages.app", "Keynote.app", "iMovie.app", "GarageBand.app",
    "App Store.app", "System Settings.app", "iTerm.app"
]

def load_custom_whitelist():
    data = read_json_file(WHITELIST_PATH)
    if data is None:
        data = read_json_file(os.path.join(BASE_DIR, "custom_whitelist.json"))
    if data is not None:
        import time
        if isinstance(data, list):
            return {app: time.time() for app in data}
        if isinstance(data, dict):
            return data
    return {}

def parse_keep_entry(value):
    if isinstance(value, dict):
        kept_at = value.get("kept_at", value.get("timestamp"))
        keep_count = value.get("keep_count", 1)
        try:
            kept_at = float(kept_at)
            keep_count = max(1, int(keep_count))
            return {"kept_at": kept_at, "keep_count": keep_count}
        except (TypeError, ValueError):
            return None
    try:
        return {"kept_at": float(value), "keep_count": 1}
    except (TypeError, ValueError):
        return None

def get_keep_delay_days(config, keep_count):
    base_days = max(1.0, float(config.get("keep_days_limit", 60)))
    multiplier = max(1.0, float(config.get("keep_backoff_multiplier", 2.0)))
    max_days = max(base_days, float(config.get("keep_backoff_max_days", 365)))
    delay = base_days * (multiplier ** max(0, keep_count - 1))
    return min(delay, max_days)

def normalize_app_path(path):
    return os.path.realpath(os.path.abspath(path))

def load_app_usage():
    data = read_json_file(APP_USAGE_PATH)
    if not isinstance(data, dict):
        return {}

    normalized = {}
    for path, timestamp in data.items():
        try:
            normalized[normalize_app_path(path)] = float(timestamp)
        except (TypeError, ValueError):
            continue
    return normalized

def get_spotlight_last_used(app):
    try:
        res = subprocess.check_output(["mdls", "-name", "kMDItemLastUsedDate", app], text=True).strip()
        date_str = res.split("= ", 1)[1].strip().replace('"', '')
        if date_str != "(null)":
            last_used = datetime.strptime(date_str[:10], "%Y-%m-%d")
            return (datetime.now() - last_used).days, date_str[:10]
    except Exception:
        pass
    return None, "Unknown"

def get_last_used(app, app_usage):
    usage_timestamp = app_usage.get(normalize_app_path(app))
    if usage_timestamp is not None:
        last_used = datetime.fromtimestamp(usage_timestamp)
        return (datetime.now() - last_used).days, last_used.strftime("%Y-%m-%d") + " (observed)"

    return get_spotlight_last_used(app)

def get_active_extensions():
    try:
        output = subprocess.check_output(["pluginkit", "-m", "-v", "-p", "com.apple.Safari.extension"], text=True)
        return list(set(line.split("/Applications/")[1].split(".app")[0] + ".app" 
                        for line in output.splitlines() if "/Applications/" in line))
    except Exception:
        return []

def get_stale_apps():
    extensions = get_active_extensions()
    find_cmd = ["find", "/Applications", os.path.expanduser("~/Applications"), 
                "-maxdepth", "2", "-name", "*.app", "-type", "d"]
    
    try:
        apps_raw = subprocess.check_output(find_cmd, text=True).splitlines()
    except:
        apps_raw = []
    
    config = load_config(BASE_DIR)
    STALE_LIMIT = config.get("stale_days_limit", 90)
    KEEP_LIMIT = config.get("keep_days_limit", 60)
    handoff_app = get_handoff_app(config)
    handoff_app_name = handoff_app if handoff_app.endswith(".app") else f"{handoff_app}.app"
    
    custom_whitelist = load_custom_whitelist()
    app_usage = load_app_usage()
    import time
    stale = []
    for app in apps_raw:
        app_name = os.path.basename(app)
        if app_name in WHITELIST: continue
        if app_name == handoff_app_name: continue
        if any(ext in app for ext in extensions): continue
        if ".Trash" in app: continue
        if "localized" in app: continue 
        if "Xcode.app" in app: continue
        
        last_used_days, date_str = get_last_used(app, app_usage)
        if last_used_days is None:
            last_used_days = STALE_LIMIT + 1

        if app in custom_whitelist:
            keep_entry = parse_keep_entry(custom_whitelist[app])
            if not keep_entry:
                continue
            keep_time = keep_entry["kept_at"]
            keep_delay_days = get_keep_delay_days(config, keep_entry["keep_count"])
            time_since_keep = (time.time() - keep_time) / 86400.0

            if time_since_keep > keep_delay_days and last_used_days > KEEP_LIMIT:
                # App kept over KEEP_LIMIT days ago and still unused
                pass
            else:
                continue
                
        if last_used_days > STALE_LIMIT:
            if date_str == "Unknown":
                stale.append(f"{app}|Unknown")
            else:
                stale.append(f"{app}|{date_str}")
                
    return stale

if __name__ == "__main__":
    # Just print the paths for the interactive script to consume
    print("\n".join(get_stale_apps()))
