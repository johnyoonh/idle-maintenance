#!/usr/bin/python3
import os
import signal
import subprocess
import sys
import time

from idle_config import APP_SUPPORT_DIR, get_handoff_app, get_handoff_url, load_config
from shortcut_review import normalize_command

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = APP_SUPPORT_DIR
WATCHER_LOCK_FILE = "/tmp/idle_watcher.lock"
APP_USAGE_WATCHER_LOCK_FILE = "/tmp/idle_maintenance_app_usage_watcher.lock"


def get_idle_time_seconds():
    cmd = "ioreg -c IOHIDSystem | awk '/HIDIdleTime/ {print $NF/1000000000; exit}'"
    return float(subprocess.check_output(cmd, shell=True).strip())


def review_gate_transition(
    idle_time,
    *,
    was_idle,
    review_pending,
    away_seconds,
    active_cutoff_seconds,
    review_idle_seconds,
    review_idle_max_seconds,
):
    """Advance the legacy review gate without observing individual input events."""
    if idle_time > away_seconds:
        return True, review_pending, False
    if was_idle and idle_time < active_cutoff_seconds:
        return False, True, False
    if review_pending and review_idle_seconds <= idle_time < review_idle_max_seconds:
        return was_idle, False, True
    return was_idle, review_pending, False


def trigger_maintenance(
    *,
    command_runner=subprocess.run,
):
    """Finish interactive reviews, then delegate focus to the resume router."""
    interactive_script = os.path.join(BASE_DIR, "maintenance_interactive.py")
    child_env = os.environ.copy()
    child_env["IDLE_MAINTENANCE_SKIP_SHORTCUT_REVIEW"] = "1"
    command_runner(["/usr/bin/python3", interactive_script], check=False, env=child_env)

    config = load_config(BASE_DIR)
    focus_command = normalize_command(
        config.get("return_focus_command") or config.get("return_handoff_command")
    )
    focus_error = ""
    focus_returncode = 127
    if focus_command:
        try:
            focus_result = command_runner(focus_command, check=False)
            focus_returncode = int(focus_result.returncode)
        except (OSError, subprocess.SubprocessError) as error:
            focus_error = str(error)
    if focus_returncode == 0:
        return {
            "ok": True,
            "command": focus_command,
            "returncode": 0,
            "fallback": False,
            "error": "",
        }

    # Preserve the existing handoff as a fail-safe when Hammerspoon or the
    # configured coordinator cannot be launched.
    target_url = get_handoff_url(config)
    if target_url:
        command_runner(["open", target_url], check=False)
    else:
        target_app = get_handoff_app(config)
        if target_app:
            command_runner(["open", "-a", target_app], check=False)
    return {
        "ok": False,
        "command": focus_command,
        "returncode": focus_returncode,
        "fallback": True,
        "error": focus_error or f"resume focus exited {focus_returncode}",
    }


def is_pid_running(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def is_app_usage_watcher_running():
    if os.path.exists(APP_USAGE_WATCHER_LOCK_FILE):
        try:
            with open(APP_USAGE_WATCHER_LOCK_FILE, "r") as handle:
                pid = int(handle.read().strip())
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
        start_new_session=True,
    )


def is_watcher_running():
    if os.path.exists(WATCHER_LOCK_FILE):
        try:
            with open(WATCHER_LOCK_FILE, "r") as handle:
                pid = int(handle.read().strip())
            os.kill(pid, 0)
            return True
        except (OSError, ValueError):
            pass
    return False


def create_watcher_lock():
    with open(WATCHER_LOCK_FILE, "w") as handle:
        handle.write(str(os.getpid()))


def remove_watcher_lock():
    if os.path.exists(WATCHER_LOCK_FILE):
        os.remove(WATCHER_LOCK_FILE)


def main():
    if is_watcher_running():
        print("idle_watcher already running. Exiting.")
        sys.exit(0)
    create_watcher_lock()
    start_app_usage_watcher()

    signal.signal(signal.SIGTERM, lambda *_: (remove_watcher_lock(), sys.exit(0)))
    signal.signal(signal.SIGINT, lambda *_: (remove_watcher_lock(), sys.exit(0)))

    try:
        config = load_config(BASE_DIR)
        idle_threshold_seconds = max(0.0, float(config.get("idle_threshold_minutes", 10))) * 60
        check_interval_seconds = max(1.0, float(config.get("check_interval_seconds", 30)))
        post_trigger_cooldown_seconds = max(
            0.0,
            float(config.get("post_trigger_cooldown_seconds", 3600)),
        )
        active_cutoff_seconds = max(
            0.0,
            float(config.get("return_active_cutoff_seconds", 30)),
        )
        review_idle_seconds = max(
            active_cutoff_seconds,
            float(config.get("review_prompt_idle_seconds", 30)),
        )
        review_idle_max_seconds = max(
            review_idle_seconds,
            float(config.get("review_prompt_idle_max_seconds", 5 * 60)),
        )

        # This watcher is opt-in. Starting it intentionally runs one review immediately.
        trigger_maintenance()
        last_triggered = time.time()

        was_idle = False
        review_pending = False
        while True:
            idle_time = get_idle_time_seconds()
            was_idle, review_pending, should_review = review_gate_transition(
                idle_time,
                was_idle=was_idle,
                review_pending=review_pending,
                away_seconds=idle_threshold_seconds,
                active_cutoff_seconds=active_cutoff_seconds,
                review_idle_seconds=review_idle_seconds,
                review_idle_max_seconds=review_idle_max_seconds,
            )

            if should_review:
                if time.time() - last_triggered >= post_trigger_cooldown_seconds:
                    trigger_maintenance()
                    last_triggered = time.time()
                time.sleep(post_trigger_cooldown_seconds)

            time.sleep(check_interval_seconds)
    finally:
        remove_watcher_lock()


if __name__ == "__main__":
    main()
