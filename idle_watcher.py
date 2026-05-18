#!/usr/bin/python3
import subprocess
import time
import os
import sys
import signal
import shlex
from idle_config import APP_SUPPORT_DIR, get_handoff_app, get_handoff_url, get_shortcut_review_command, load_config

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = APP_SUPPORT_DIR
WATCHER_LOCK_FILE = "/tmp/idle_watcher.lock"
APP_USAGE_WATCHER_LOCK_FILE = "/tmp/idle_maintenance_app_usage_watcher.lock"

def get_idle_time_seconds():
    cmd = "ioreg -c IOHIDSystem | awk '/HIDIdleTime/ {print $NF/1000000000; exit}'"
    return float(subprocess.check_output(cmd, shell=True).strip())

def trigger_maintenance():
    # Run the interactive maintenance script directly without confirmation
    interactive_script = os.path.join(BASE_DIR, "maintenance_interactive.py")
    subprocess.run(["/usr/bin/python3", interactive_script])
    
    config = load_config(BASE_DIR)
    target_url = get_handoff_url(config)
    if target_url:
        subprocess.run(["open", target_url])
    else:
        target_app = get_handoff_app(config)
        if target_app:
            subprocess.run(["open", "-a", target_app])

    shortcut_review_command = get_shortcut_review_command(config)
    if shortcut_review_command:
        subprocess.Popen(
            shlex.split(shortcut_review_command),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True
        )

def is_pid_running(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False

def is_app_usage_watcher_running():
    if os.path.exists(APP_USAGE_WATCHER_LOCK_FILE):
        try:
            with open(APP_USAGE_WATCHER_LOCK_FILE, "r") as f:
                pid = int(f.read().strip())
            return is_pid_running(pid)
        except (OSError, ValueError):
            pass
    return False

def start_app_usage_watcher():
    if is_app_usage_watcher_running():
        return

    watcher_binary = os.path.join(BASE_DIR, "app_usage_watcher")
    watcher_script = os.path.join(BASE_DIR, "app_usage_watcher.swift")
    if os.path.exists(watcher_binary) and os.access(watcher_binary, os.X_OK):
        cmd = [watcher_binary]
    elif os.path.exists(watcher_script):
        cmd = ["/usr/bin/swift", watcher_script]
    else:
        return

    subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True
    )

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
    start_app_usage_watcher()

    # Clean up lock on exit
    signal.signal(signal.SIGTERM, lambda *_: (remove_watcher_lock(), sys.exit(0)))
    signal.signal(signal.SIGINT,  lambda *_: (remove_watcher_lock(), sys.exit(0)))

    try:
        config = load_config(BASE_DIR)
        idle_threshold_seconds = max(0.0, float(config.get("idle_threshold_minutes", 10))) * 60
        check_interval_seconds = max(1.0, float(config.get("check_interval_seconds", 30)))
        post_trigger_cooldown_seconds = max(0.0, float(config.get("post_trigger_cooldown_seconds", 3600)))

        # Immediate trigger on launch
        trigger_maintenance()
        last_triggered = time.time()  # Record when we last ran maintenance

        was_idle = False
        while True:
            idle_time = get_idle_time_seconds()

            if idle_time > idle_threshold_seconds:
                was_idle = True
            elif was_idle and idle_time < 30:  # User just returned from idle
                # Guard: only re-trigger if enough time has passed since last run.
                # This prevents the loop where the handoff app opens → user clicks it →
                # idle_time drops → maintenance fires again immediately.
                time_since_last = time.time() - last_triggered
                if time_since_last >= post_trigger_cooldown_seconds:
                    trigger_maintenance()
                    last_triggered = time.time()
                was_idle = False
                time.sleep(post_trigger_cooldown_seconds)

            time.sleep(check_interval_seconds)
    finally:
        remove_watcher_lock()

if __name__ == "__main__":
    main()
