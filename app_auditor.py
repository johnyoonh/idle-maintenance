#!/usr/bin/python3
import subprocess
import os
import time
from datetime import datetime
from idle_config import (
    APP_SUPPORT_DIR,
    atomic_write_json,
    get_handoff_app,
    get_keep_delay_days,
    load_config,
    parse_keep_entry,
    read_json_file,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = APP_SUPPORT_DIR
WHITELIST_PATH = os.path.join(STATE_DIR, "custom_whitelist.json")
APP_USAGE_PATH = os.path.join(STATE_DIR, "app_usage.json")
APP_AUDIT_CACHE_PATH = os.path.join(STATE_DIR, "app-audit-cache.json")
AUDIT_BUDGET_SECONDS = 5.0
COMMAND_TIMEOUT_SECONDS = 2.0

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

def load_audit_cache():
    data = read_json_file(APP_AUDIT_CACHE_PATH)
    return data if isinstance(data, dict) else {}


def get_last_used_many(apps, app_usage, cache, timeout, command_runner=subprocess.run):
    """Resolve Spotlight metadata in one bounded subprocess, preserving input order."""
    resolved = {}
    spotlight_apps = []
    for app in apps:
        usage_timestamp = app_usage.get(normalize_app_path(app))
        if usage_timestamp is not None:
            last_used = datetime.fromtimestamp(usage_timestamp)
            resolved[app] = (
                (datetime.now() - last_used).days,
                last_used.strftime("%Y-%m-%d") + " (observed)",
            )
        else:
            spotlight_apps.append(app)

    if spotlight_apps and timeout > 0:
        try:
            result = command_runner(
                ["mdls", "-raw", "-name", "kMDItemLastUsedDate", *spotlight_apps],
                text=True,
                capture_output=True,
                check=False,
                timeout=timeout,
            )
            # mdls separates raw values for multiple paths with NUL bytes.
            output = (result.stdout or "").replace("\0", "\n")
            lines = [line.strip().replace('"', "") for line in output.splitlines()]
            if result.returncode == 0 and len(lines) == len(spotlight_apps):
                for app, value in zip(spotlight_apps, lines):
                    if value and value != "(null)":
                        try:
                            last_used = datetime.strptime(value[:10], "%Y-%m-%d")
                            resolved[app] = ((datetime.now() - last_used).days, value[:10])
                            cache[normalize_app_path(app)] = value[:10]
                        except ValueError:
                            pass
        except (OSError, subprocess.SubprocessError):
            pass

    for app in spotlight_apps:
        if app in resolved:
            continue
        cached = cache.get(normalize_app_path(app))
        if isinstance(cached, str) and cached:
            try:
                last_used = datetime.strptime(cached[:10], "%Y-%m-%d")
                resolved[app] = ((datetime.now() - last_used).days, cached[:10])
                continue
            except ValueError:
                pass
        resolved[app] = (None, "Unknown")
    return resolved

def get_active_extensions(timeout=COMMAND_TIMEOUT_SECONDS, command_runner=subprocess.run):
    try:
        result = command_runner(
            ["pluginkit", "-m", "-v", "-p", "com.apple.Safari.extension"],
            text=True,
            capture_output=True,
            check=False,
            timeout=max(0.1, timeout),
        )
        output = result.stdout if result.returncode == 0 else ""
        return list(set(line.split("/Applications/")[1].split(".app")[0] + ".app" 
                        for line in output.splitlines() if "/Applications/" in line))
    except (OSError, subprocess.SubprocessError):
        return []


def discover_apps(roots=None, deadline=None, now_fn=time.monotonic):
    """Find application bundles with a bounded two-level directory walk."""
    roots = roots or ["/Applications", os.path.expanduser("~/Applications")]
    apps = []
    for root in roots:
        if deadline is not None and now_fn() >= deadline:
            break
        try:
            with os.scandir(root) as entries:
                first_level = list(entries)
        except OSError:
            continue
        for entry in first_level:
            if deadline is not None and now_fn() >= deadline:
                return apps
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            if entry.name.endswith(".app"):
                apps.append(entry.path)
                continue
            try:
                with os.scandir(entry.path) as children:
                    for child in children:
                        if deadline is not None and now_fn() >= deadline:
                            return apps
                        if child.name.endswith(".app") and child.is_dir(follow_symlinks=False):
                            apps.append(child.path)
            except OSError:
                continue
    return apps

def get_stale_apps(now_fn=time.monotonic):
    started = now_fn()
    deadline = started + AUDIT_BUDGET_SECONDS
    extensions = get_active_extensions(
        timeout=min(COMMAND_TIMEOUT_SECONDS, max(0.1, deadline - now_fn()))
    )
    apps_raw = discover_apps(deadline=deadline, now_fn=now_fn)
    
    config = load_config(BASE_DIR)
    STALE_LIMIT = config.get("stale_days_limit", 90)
    KEEP_LIMIT = config.get("keep_days_limit", 60)
    handoff_app = get_handoff_app(config)
    handoff_app_name = handoff_app if handoff_app.endswith(".app") else f"{handoff_app}.app"
    
    custom_whitelist = load_custom_whitelist()
    app_usage = load_app_usage()
    cache = load_audit_cache()
    remaining = max(0.0, deadline - now_fn())
    last_used_by_app = get_last_used_many(apps_raw, app_usage, cache, remaining)
    atomic_write_json(APP_AUDIT_CACHE_PATH, cache)
    stale = []
    for app in apps_raw:
        app_name = os.path.basename(app)
        if app_name in WHITELIST: continue
        if app_name == handoff_app_name: continue
        if any(ext in app for ext in extensions): continue
        if ".Trash" in app: continue
        if "localized" in app: continue 
        if "Xcode.app" in app: continue
        
        last_used_days, date_str = last_used_by_app.get(app, (None, "Unknown"))
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
