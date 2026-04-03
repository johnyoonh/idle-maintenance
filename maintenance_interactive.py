#!/usr/bin/python3
import subprocess
import os
import json
import time
import sys

LOCK_FILE = "/tmp/idle_maintenance.lock"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
QUEUE_PATH = os.path.join(BASE_DIR, "stale_queue.json")
WHITELIST_PATH = os.path.join(BASE_DIR, "custom_whitelist.json")
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

def load_config():
    path = os.path.join(BASE_DIR, "config.json")
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                return json.load(f)
        except: pass
    return {"max_prompts": 10, "close_on_unfocus": True}

def load_custom_whitelist(path):
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return {app: time.time() for app in data}
                return data
        except: pass
    return {}

def save_json(path, data):
    try:
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
    except: pass

def prompt_user(app_path, close_on_unfocus=True, last_used=""):
    app_name = os.path.basename(app_path)
    swift_script = os.path.join(BASE_DIR, "prompt.swift")
    try:
        cmd = ["swift", swift_script, app_name, app_path, str(close_on_unfocus).lower()]
        if last_used:
            cmd.append(last_used)
        res = subprocess.check_output(cmd, text=True).strip()
        for keyword in ["KEEP", "DELETE", "TRY", "SKIP", "QUIT"]:
            if keyword in res: return keyword
        return "QUIT"
    except:
        return "QUIT"

def delete_app(app_path):
    app_name = os.path.basename(app_path)
    if app_name.endswith(".app"):
        app_name = app_name[:-4]
        
    subprocess.run(["pkill", "-9", "-x", app_name], stderr=subprocess.DEVNULL)
    subprocess.run(["pkill", "-9", "-f", app_path], stderr=subprocess.DEVNULL)
    time.sleep(0.5)

    trash_dir = os.path.expanduser("~/.Trash")
    base_name = os.path.basename(app_path)
    dest_path = os.path.join(trash_dir, base_name)
    
    if os.path.exists(dest_path):
        import uuid
        dest_path = os.path.join(trash_dir, f"{base_name}_{uuid.uuid4().hex[:8]}")
        
    import shutil
    try:
        shutil.move(app_path, dest_path)
        return True
    except Exception as e:
        log(f"Failed to trash {app_path} via shutil (falling back to AppleScript): {e}")
        applescript = f'''
        tell application "Finder"
            try
                delete POSIX file "{app_path}"
                return true
            on error
                return false
            end try
        end tell
        '''
        res = subprocess.run(["osascript", "-e", applescript], capture_output=True, text=True)
        return "true" in res.stdout.lower()

def main():
    if is_running():
        return
    create_lock()

    auditor_path = os.path.join(BASE_DIR, "app_auditor.py")
    try:
        stale_output = subprocess.check_output(["/usr/bin/python3", auditor_path], text=True).splitlines()
    except:
        stale_output = []
        
    stale_apps = []
    stale_dates = {}
    for line in stale_output:
        if "|" in line:
            path, date_str = line.split("|", 1)
            stale_apps.append(path)
            stale_dates[path] = date_str
        else:
            stale_apps.append(line)
            stale_dates[line] = "Unknown"
    
    config = load_config()
    max_prompts = config.get("max_prompts", 10)
    close_on_unfocus = config.get("close_on_unfocus", True)
    
    queue = load_json(QUEUE_PATH)
    if isinstance(queue, dict): queue = [] # Just in case it gets mangled
    whitelist = load_custom_whitelist(WHITELIST_PATH)
    
    queue = [item for item in queue if item["path"] in stale_apps]
    existing_paths = [item["path"] for item in queue]
    for app in stale_apps:
        if app not in existing_paths and app not in whitelist:
            queue.append({"path": app, "last_prompted": 0})
            
    queue.sort(key=lambda x: x["last_prompted"])
    
    processed = 0
    current_queue = [item for item in queue]
    
    for item in queue:
        if processed >= max_prompts:
            break
            
        app_done = False
        while not app_done:
            last_used_info = stale_dates.get(item["path"], "Unknown")
            if item.get("last_prompted", 0) > 0:
                last_used_info += f" (Last prompted/tried: {time.strftime('%Y-%m-%d', time.localtime(item['last_prompted']))})"
            action = prompt_user(item["path"], close_on_unfocus, last_used_info)
            
            if action == "QUIT":
                save_json(QUEUE_PATH, current_queue)
                save_json(WHITELIST_PATH, whitelist)
                if os.path.exists(LOCK_FILE): os.remove(LOCK_FILE)
                return

            if action == "KEEP":
                whitelist[item["path"]] = time.time()
                current_queue = [i for i in current_queue if i["path"] != item["path"]]
                processed += 1
                app_done = True
            elif action == "DELETE":
                success = delete_app(item["path"])
                if success:
                    current_queue = [i for i in current_queue if i["path"] != item["path"]]
                else:
                    for q_item in current_queue:
                        if q_item["path"] == item["path"]:
                            q_item["last_prompted"] = int(time.time())
                processed += 1
                app_done = True
            elif action == "TRY":
                subprocess.run(["open", item["path"]])
                for q_item in current_queue:
                    if q_item["path"] == item["path"]:
                        q_item["last_prompted"] = int(time.time())
                save_json(QUEUE_PATH, current_queue)
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
