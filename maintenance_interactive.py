#!/usr/bin/python3
import subprocess
import os
import json
import time
import sys

LOCK_FILE = "/tmp/idle_maintenance.lock"
QUEUE_PATH = os.path.expanduser("~/Library/Scripts/idle-maintenance/stale_queue.json")
WHITELIST_PATH = os.path.expanduser("~/Library/Scripts/idle-maintenance/custom_whitelist.json")
MAX_PROMPTS = 10

def log(msg):
    LOG_PATH = "/tmp/interactive_maintenance.log"
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_PATH, "a") as f:
        f.write(f"[{timestamp}] {msg}\n")

def is_running():
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE, "r") as f:
                pid = int(f.read().strip())
            os.kill(pid, 0)
            return True
        except (OSError, ValueError):
            return False
    return False

def create_lock():
    with open(LOCK_FILE, "w") as f:
        f.write(str(os.getpid()))

def load_json(path):
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                return json.load(f)
        except: pass
    return []

def save_json(path, data):
    try:
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
    except: pass

def prompt_user(app_path):
    app_name = os.path.basename(app_path)
    swift_script = os.path.expanduser("~/Library/Scripts/idle-maintenance/prompt.swift")
    try:
        res = subprocess.check_output(["swift", swift_script, app_name, app_path], text=True).strip()
        for keyword in ["KEEP", "DELETE", "TRY", "SKIP", "QUIT"]:
            if keyword in res: return keyword
        return "QUIT"
    except:
        return "QUIT"

def delete_app(app_path):
    # This script quits the app (if running) before trashing it
    applescript = f"""
    set appBundlePath to "{app_path}"
    tell application "Finder"
        try
            set appName to name of (POSIX file appBundlePath as alias)
            -- Trim .app from name
            if appName ends with ".app" then
                set appName to text 1 thru -5 of appName
            end if
            
            tell application "System Events"
                if exists (process appName) then
                    tell application appName to quit
                    delay 1
                end if
            end tell
            
            move POSIX file appBundlePath to trash
        on error err
            log err
        end try
    end tell
    """
    subprocess.run(["osascript", "-e", applescript])

def main():
    if is_running():
        return
    create_lock()

    auditor_path = os.path.expanduser("~/Library/Scripts/idle-maintenance/app_auditor.py")
    try:
        stale_apps = subprocess.check_output(["/usr/bin/python3", auditor_path], text=True).splitlines()
    except:
        stale_apps = []
    
    queue = load_json(QUEUE_PATH)
    whitelist = load_json(WHITELIST_PATH)
    
    queue = [item for item in queue if item["path"] in stale_apps]
    existing_paths = [item["path"] for item in queue]
    for app in stale_apps:
        if app not in existing_paths and app not in whitelist:
            queue.append({"path": app, "last_prompted": 0})
            
    queue.sort(key=lambda x: x["last_prompted"])
    
    processed = 0
    current_queue = [item for item in queue]
    
    for item in queue:
        if processed >= MAX_PROMPTS:
            break
            
        app_done = False
        while not app_done:
            action = prompt_user(item["path"])
            
            if action == "QUIT":
                save_json(QUEUE_PATH, current_queue)
                save_json(WHITELIST_PATH, whitelist)
                if os.path.exists(LOCK_FILE): os.remove(LOCK_FILE)
                return

            if action == "KEEP":
                whitelist.append(item["path"])
                current_queue = [i for i in current_queue if i["path"] != item["path"]]
                processed += 1
                app_done = True
            elif action == "DELETE":
                delete_app(item["path"])
                current_queue = [i for i in current_queue if i["path"] != item["path"]]
                processed += 1
                app_done = True
            elif action == "TRY":
                subprocess.run(["open", item["path"]])
                time.sleep(1)
            else: # SKIP
                for q_item in current_queue:
                    if q_item["path"] == item["path"]:
                        q_item["last_prompted"] = int(time.time())
                processed += 1
                app_done = True
        
    save_json(QUEUE_PATH, current_queue)
    save_json(WHITELIST_PATH, whitelist)
    if os.path.exists(LOCK_FILE): os.remove(LOCK_FILE)

if __name__ == "__main__":
    main()
