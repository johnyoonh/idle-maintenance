#!/usr/bin/python3
import subprocess
import os
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def load_config():
    path = os.path.join(BASE_DIR, "config.json")
    if os.path.exists(path):
        import json
        try:
            with open(path, "r") as f:
                return json.load(f)
        except: pass
    return {"stale_days_limit": 90, "keep_days_limit": 60, "max_prompts": 10, "close_on_unfocus": True}

# Apps we never want to flag
WHITELIST = [
    "Safari.app", "Mail.app", "Messages.app", "Photos.app", 
    "Calendar.app", "Notes.app", "Reminders.app", "Numbers.app", 
    "Pages.app", "Keynote.app", "iMovie.app", "GarageBand.app",
    "App Store.app", "System Settings.app", "TickTick.app", "iTerm.app"
]

def load_custom_whitelist():
    path = os.path.join(BASE_DIR, "custom_whitelist.json")
    if os.path.exists(path):
        import json
        import time
        try:
            with open(path, "r") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return {app: time.time() for app in data}
                return data
        except: pass
    return {}

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
    
    config = load_config()
    STALE_LIMIT = config.get("stale_days_limit", 90)
    KEEP_LIMIT = config.get("keep_days_limit", 60)
    
    custom_whitelist = load_custom_whitelist()
    import time
    stale = []
    for app in apps_raw:
        app_name = os.path.basename(app)
        if app_name in WHITELIST: continue
        if any(ext in app for ext in extensions): continue
        if ".Trash" in app: continue
        if "localized" in app: continue 
        if "Xcode.app" in app: continue
        
        last_used_days = STALE_LIMIT + 1
        date_str = "(null)"
        try:
            res = subprocess.check_output(["mdls", "-name", "kMDItemLastUsedDate", app], text=True).strip()
            date_str = res.split("= ")[1].strip().replace('"', '')
            if date_str != "(null)":
                last_used = datetime.strptime(date_str[:10], "%Y-%m-%d")
                last_used_days = (datetime.now() - last_used).days
        except:
            pass

        if app in custom_whitelist:
            keep_time = custom_whitelist[app]
            time_since_keep = (time.time() - keep_time) / 86400.0
            
            if time_since_keep > KEEP_LIMIT and last_used_days > KEEP_LIMIT:
                # App kept over KEEP_LIMIT days ago and still unused
                pass
            else:
                continue
                
        if last_used_days > STALE_LIMIT:
            if date_str == "(null)":
                stale.append(f"{app}|Unknown")
            else:
                stale.append(f"{app}|{date_str[:10]}")
                
    return stale

if __name__ == "__main__":
    # Just print the paths for the interactive script to consume
    print("\n".join(get_stale_apps()))
