#!/usr/bin/python3
import subprocess
import time
import os
import sys
import json
import signal

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WATCHER_LOCK_FILE = "/tmp/idle_watcher.lock"

def load_config():
    path = os.path.join(BASE_DIR, "config.json")
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                return json.load(f)
        except: pass
    return {}

# CONFIG
IDLE_THRESHOLD_MINUTES = 10
IDLE_THRESHOLD_SECONDS = IDLE_THRESHOLD_MINUTES * 60
CHECK_INTERVAL_SECONDS = 30
POST_TRIGGER_COOLDOWN_SECONDS = 3600  # 1 hour cooldown between triggers

def get_idle_time_seconds():
    cmd = "ioreg -c IOHIDSystem | awk '/HIDIdleTime/ {print $NF/1000000000; exit}'"
    return float(subprocess.check_output(cmd, shell=True).strip())

def trigger_maintenance():
    # Run the interactive maintenance script directly without confirmation
    interactive_script = os.path.join(BASE_DIR, "maintenance_interactive.py")
    subprocess.run(["/usr/bin/python3", interactive_script])
    
    config = load_config()
    target_app = config.get("on_finish_app", "TickTick")
    if target_app:
        subprocess.run(["open", "-a", target_app])

def is_watcher_running():
    if os.path.exists(WATCHER_LOCK_FILE):
        try:
            with open(WATCHER_LOCK_FILE, "r") as f:
                pid = int(f.read().strip())
            os.kill(pid, 0)
            return True
        except (OSError, ValueError):
            pass
    return False

def create_watcher_lock():
    with open(WATCHER_LOCK_FILE, "w") as f:
        f.write(str(os.getpid()))

def remove_watcher_lock():
    if os.path.exists(WATCHER_LOCK_FILE):
        os.remove(WATCHER_LOCK_FILE)

def main():
    if is_watcher_running():
        print("idle_watcher already running. Exiting.")
        sys.exit(0)
    create_watcher_lock()

    # Clean up lock on exit
    signal.signal(signal.SIGTERM, lambda *_: (remove_watcher_lock(), sys.exit(0)))
    signal.signal(signal.SIGINT,  lambda *_: (remove_watcher_lock(), sys.exit(0)))

    try:
        # Immediate trigger on launch
        trigger_maintenance()
        last_triggered = time.time()  # Record when we last ran maintenance

        was_idle = False
        while True:
            idle_time = get_idle_time_seconds()

            if idle_time > IDLE_THRESHOLD_SECONDS:
                was_idle = True
            elif was_idle and idle_time < 30:  # User just returned from idle
                # Guard: only re-trigger if enough time has passed since last run.
                # This prevents the loop where TickTick opens → user clicks it →
                # idle_time drops → maintenance fires again immediately.
                time_since_last = time.time() - last_triggered
                if time_since_last >= POST_TRIGGER_COOLDOWN_SECONDS:
                    trigger_maintenance()
                    last_triggered = time.time()
                was_idle = False
                # Prevent re-triggering for 1 hour
                time.sleep(POST_TRIGGER_COOLDOWN_SECONDS)

            time.sleep(CHECK_INTERVAL_SECONDS)
    finally:
        remove_watcher_lock()

if __name__ == "__main__":
    main()
