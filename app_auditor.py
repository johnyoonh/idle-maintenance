#!/usr/bin/python3
import plistlib
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
DEFAULT_EXCLUDED_APP_DIRS = ("~/Applications/Local Automation",)
DEFAULT_APP_MIN_AGE_DAYS = 14


def _extract_app_bundle_path(raw_value):
    if not isinstance(raw_value, str) or ".app" not in raw_value:
        return None
    marker = ".app"
    idx = raw_value.find(marker)
    candidate = raw_value[: idx + len(marker)]
    if candidate.endswith(".app"):
        return os.path.normcase(normalize_app_path(os.path.expanduser(candidate)))
    return None


def launchagent_referenced_app_paths(launch_agents_dirs=None):
    """Return normalized .app bundle paths referenced by active LaunchAgent plists."""
    dirs = launch_agents_dirs or (os.path.expanduser("~/Library/LaunchAgents"),)
    referenced = set()
    for directory in dirs:
        try:
            entries = os.scandir(directory)
        except OSError:
            continue
        with entries:
            for entry in entries:
                if not entry.name.endswith(".plist"):
                    continue
                try:
                    with open(entry.path, "rb") as handle:
                        plist = plistlib.load(handle)
                except Exception:
                    continue
                if not isinstance(plist, dict):
                    continue
                values = []
                if isinstance(plist.get("Program"), str):
                    values.append(plist["Program"])
                if isinstance(plist.get("ProgramArguments"), list):
                    values.extend(arg for arg in plist["ProgramArguments"] if isinstance(arg, str))
                for val in values:
                    bundle = _extract_app_bundle_path(val)
                    if bundle:
                        referenced.add(bundle)
    return referenced


def is_background_only_bundle(app_path):
    info_plist = os.path.join(app_path, "Contents", "Info.plist")
    if not os.path.exists(info_plist):
        return False
    try:
        with open(info_plist, "rb") as handle:
            info = plistlib.load(handle)
    except Exception:
        return False
    if not isinstance(info, dict):
        return False
    return bool(info.get("LSBackgroundOnly"))


def bundle_age_days(app_path, now_ts=None):
    try:
        st = os.stat(app_path)
    except OSError:
        return None
    created_or_mod = getattr(st, "st_birthtime", None)
    if created_or_mod is None or created_or_mod <= 0:
        created_or_mod = st.st_mtime
    now_value = time.time() if now_ts is None else now_ts
    return max(0.0, (now_value - float(created_or_mod)) / 86400.0)


def is_excluded_app(app_path, config, launchagent_apps, now_ts=None):
    norm_app = os.path.normcase(normalize_app_path(app_path))
    app_name = os.path.basename(app_path)

    excluded_dirs = config.get("excluded_app_dirs", DEFAULT_EXCLUDED_APP_DIRS)
    if isinstance(excluded_dirs, (list, tuple)):
        for raw_dir in excluded_dirs:
            norm_dir = os.path.normcase(normalize_app_path(os.path.expanduser(str(raw_dir))))
            if norm_app == norm_dir or norm_app.startswith(norm_dir + os.sep):
                return True

    ignored_apps = config.get("ignored_apps", [])
    if isinstance(ignored_apps, (list, tuple)):
        for ignored in ignored_apps:
            ignored_str = str(ignored)
            if app_name == ignored_str:
                return True
            if os.path.normcase(normalize_app_path(os.path.expanduser(ignored_str))) == norm_app:
                return True

    if norm_app in launchagent_apps:
        return True

    if is_background_only_bundle(app_path):
        return True

    min_age_days = float(config.get("app_min_age_days", DEFAULT_APP_MIN_AGE_DAYS))
    if min_age_days > 0:
        age = bundle_age_days(app_path, now_ts=now_ts)
        if age is not None and age < min_age_days:
            return True

    return False

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
    spotlight_datetimes = {}
    spotlight_apps = list(apps)

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
                            try:
                                last_used = datetime.strptime(value, "%Y-%m-%d %H:%M:%S %z")
                                last_used = last_used.astimezone().replace(tzinfo=None)
                            except ValueError:
                                last_used = datetime.strptime(value[:10], "%Y-%m-%d")
                            spotlight_datetimes[app] = last_used
                            date_text = value[:10]
                            resolved[app] = ((datetime.now() - last_used).days, date_text)
                            cache[normalize_app_path(app)] = date_text
                        except ValueError:
                            pass
        except (OSError, subprocess.SubprocessError):
            pass

    for app in apps:
        usage_timestamp = app_usage.get(normalize_app_path(app))
        usage_dt = None
        if usage_timestamp is not None:
            try:
                usage_dt = datetime.fromtimestamp(usage_timestamp)
            except (TypeError, ValueError, OverflowError):
                usage_dt = None

        spotlight_dt = spotlight_datetimes.get(app)

        if usage_dt is not None and (spotlight_dt is None or usage_dt >= spotlight_dt):
            resolved[app] = (
                (datetime.now() - usage_dt).days,
                usage_dt.strftime("%Y-%m-%d") + " (observed)",
            )
        elif spotlight_dt is not None:
            pass
        else:
            cached = cache.get(normalize_app_path(app))
            if isinstance(cached, str) and cached and cached != "Unknown":
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
    launchagent_apps = launchagent_referenced_app_paths()
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
        if is_excluded_app(app, config, launchagent_apps): continue
        
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
