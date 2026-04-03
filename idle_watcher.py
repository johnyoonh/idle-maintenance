#!/usr/bin/python3
import subprocess
import time
import os
import sys
import json

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

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

def main():
    # Immediate trigger for testing/launch
    trigger_maintenance() 
    
    was_idle = False
    while True:
        idle_time = get_idle_time_seconds()
        
        if idle_time > IDLE_THRESHOLD_SECONDS:
            was_idle = True
        elif was_idle and idle_time < 30: # User just returned
            trigger_maintenance()
            was_idle = False
            # Prevent re-triggering for 1 hour
            time.sleep(3600) 
            
        time.sleep(CHECK_INTERVAL_SECONDS)

if __name__ == "__main__":
    main()
