#!/usr/bin/python3
import subprocess
import os
from datetime import datetime

DAYS_LIMIT = 90

# Apps we never want to flag
WHITELIST = [
    "Safari.app", "Mail.app", "Messages.app", "Photos.app", 
    "Calendar.app", "Notes.app", "Reminders.app", "Numbers.app", 
    "Pages.app", "Keynote.app", "iMovie.app", "GarageBand.app",
    "App Store.app", "System Settings.app", "TickTick.app", "iTerm.app"
]

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
    
    stale = []
    for app in apps_raw:
        app_name = os.path.basename(app)
        if app_name in WHITELIST: continue
        if any(ext in app for ext in extensions): continue
        if ".Trash" in app: continue
        if "localized" in app: continue 
        if "Xcode.app" in app: continue
        
        try:
            res = subprocess.check_output(["mdls", "-name", "kMDItemLastUsedDate", app], text=True).strip()
            date_str = res.split("= ")[1].strip().replace('"', '')
            if date_str == "(null)":
                stale.append(app)
                continue
            
            last_used = datetime.strptime(date_str[:10], "%Y-%m-%d")
            if (datetime.now() - last_used).days > DAYS_LIMIT:
                stale.append(app)
        except:
            continue
            
    return stale

if __name__ == "__main__":
    # Just print the paths for the interactive script to consume
    print("\n".join(get_stale_apps()))
