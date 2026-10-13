#!/usr/bin/env python3
"""Install only the lid backlight watcher for the current macOS user."""
import os
from pathlib import Path
import plistlib
import shutil
import subprocess


def main():
    home = Path.home()
    label = "com.john.idle-maintenance.lid-backlight"
    runtime = home / "Library/Scripts/idle-maintenance/lid_backlight.py"
    logs = home / "Library/Logs/idle-maintenance"
    plist = home / "Library/LaunchAgents" / (label + ".plist")
    runtime.parent.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    plist.parent.mkdir(parents=True, exist_ok=True)
    domain = "gui/" + str(os.getuid())
    service = domain + "/" + label
    # Stop the old owner before replacing its executable or plist.
    existing = subprocess.run(["/bin/launchctl", "print", service], capture_output=True)
    if existing.returncode == 0:
        subprocess.run(["/bin/launchctl", "bootout", service], check=True)
    shutil.copy2(Path(__file__).with_name("lid_backlight.py"), runtime)
    with plist.open("wb") as stream:
        plistlib.dump({
            "Label": label,
            "ProgramArguments": ["/usr/bin/python3", str(runtime)],
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 30,
            "ProcessType": "Background",
            "StandardOutPath": str(logs / "lid-backlight.log"),
            "StandardErrorPath": str(logs / "lid-backlight.log"),
        }, stream)
    subprocess.run(["/bin/launchctl", "bootstrap", domain, str(plist)], check=True)
    subprocess.run(["/bin/launchctl", "print", service], check=True)


if __name__ == "__main__":
    main()
