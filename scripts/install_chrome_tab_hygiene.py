#!/usr/bin/env python3
"""Install the Chrome tab hygiene periodic LaunchAgent for the current macOS user."""
import os
from pathlib import Path
import plistlib
import shutil
import subprocess


def main():
    home = Path.home()
    label = "com.john.idle-maintenance.chrome-tab-hygiene"
    runtime = home / "Library/Scripts/idle-maintenance/chrome_tab_hygiene.py"
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

    shutil.copy2(Path(__file__).with_name("chrome_tab_hygiene.py"), runtime)
    runtime.chmod(0o755)

    with plist.open("wb") as stream:
        plistlib.dump({
            "Label": label,
            "ProgramArguments": ["/usr/bin/python3", str(runtime)],
            "RunAtLoad": True,
            "StartInterval": 1800,  # every 30 minutes
            "ProcessType": "Background",
            "LowPriorityIO": True,
            "StandardOutPath": str(logs / "chrome-tab-hygiene.out.log"),
            "StandardErrorPath": str(logs / "chrome-tab-hygiene.err.log"),
        }, stream)

    subprocess.run(["/bin/launchctl", "bootstrap", domain, str(plist)], check=True)
    subprocess.run(["/bin/launchctl", "print", service], check=True)
    print(f"Successfully installed and bootstrapped {label}")


if __name__ == "__main__":
    main()
