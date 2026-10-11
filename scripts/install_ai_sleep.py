#!/usr/bin/env python3
"""Install the scoped AI sleep controller, watchdog, and lifecycle observers."""
import copy
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shlex
import shutil
import subprocess
import time

CLAUDE_EVENTS = ("UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure",
                 "PermissionRequest", "Notification", "Elicitation", "PostToolBatch", "StopFailure", "Stop", "SessionEnd")
CURSOR_EVENTS = ("beforeSubmitPrompt", "afterAgentThought", "afterAgentResponse",
                 "preToolUse", "postToolUse", "postToolUseFailure", "stop", "sessionEnd")


def merge_hooks(data, kind, command):
    if not isinstance(data, dict):
        raise ValueError("Settings must be a JSON object")
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("Existing hooks must be an object")
    if kind == "cursor":
        data.setdefault("version", 1)
    for event in CLAUDE_EVENTS if kind == "claude" else CURSOR_EVENTS:
        entries = hooks.setdefault(event, [])
        if not isinstance(entries, list):
            raise ValueError("Existing event hooks must be a list")
        if kind == "claude":
            if any(isinstance(entry, dict) and any(isinstance(h, dict) and h.get("command") == command
                   for h in entry.get("hooks", [])) for entry in entries):
                continue
            entry = {"hooks": [{"type": "command", "command": command, "timeout": 3}]}
            if event == "Notification":
                entry["matcher"] = "permission_prompt|elicitation_dialog"
            entries.append(entry)
        elif not any(isinstance(entry, dict) and entry.get("command") == command for entry in entries):
            entries.append({"command": command})
    return data


def prepare_settings(home, runtime):
    prepared = []
    for kind, path in (("claude", home / ".claude/settings.json"), ("cursor", home / ".cursor/hooks.json")):
        raw = path.read_bytes() if path.exists() else None
        data = json.loads(raw) if raw is not None else {}
        merged = merge_hooks(copy.deepcopy(data), kind,
                             "/usr/bin/python3 " + shlex.quote(str(runtime / "ai_sleep.py")) + " --hook " + kind)
        if data != merged:
            prepared.append((path, raw, json.dumps(merged, indent=2).encode() + b"\n"))
    return prepared


def stop_service(service):
    if subprocess.run(["/bin/launchctl", "print", service], capture_output=True, timeout=3).returncode:
        return
    subprocess.run(["/bin/launchctl", "bootout", service], check=True, timeout=15)
    # bootout can return while launchd is still removing the old registration.
    # Rebootstrap only after authoritative absence, avoiding a transient error 5.
    for _ in range(75):
        if subprocess.run(["/bin/launchctl", "print", service], capture_output=True, timeout=3).returncode:
            return
        time.sleep(0.2)
    raise RuntimeError("LaunchAgent removal did not finish; refusing duplicate bootstrap")


def main():
    from ai_work import boot_id
    from ai_sleep import valid_lease
    os.umask(0o077)
    home = Path.home()
    runtime = home / "Library/Scripts/idle-maintenance"
    root = home / "Library/Application Support/idle-maintenance/ai-sleep"
    logs = home / "Library/Logs/idle-maintenance"
    launch = home / "Library/LaunchAgents"
    prepared = prepare_settings(home, runtime)  # Validate before touching services.
    for directory in (runtime, root, logs, launch):
        directory.mkdir(parents=True, exist_ok=True)
    domain = "gui/" + str(os.getuid())
    labels = ("com.john.idle-maintenance.ai-sleep", "com.john.idle-maintenance.ai-sleep-watchdog")
    boot = boot_id()
    for label in labels:
        service = domain + "/" + label
        stop_service(service)
    for name in ("ai_sleep.py", "ai_work.py", "antigravity_work.py"):
        tmp = runtime / (name + ".tmp")
        shutil.copy2(Path(__file__).with_name(name), tmp)
        os.replace(tmp, runtime / name)
    backups = root / "settings-backups"
    for path, old, new in prepared:
        if (path.read_bytes() if path.exists() else None) != old:
            raise RuntimeError("Settings changed during installation; refusing overwrite")
        if old is not None:
            backups.mkdir(exist_ok=True)
            digest = hashlib.sha256(old).hexdigest()[:12]
            backup = backups / (path.parent.name + "-" + path.name + "-" + digest)
            if not backup.exists():
                backup.write_bytes(old)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".ai-sleep.tmp")
        tmp.write_bytes(new)
        os.replace(tmp, path)
    for index, label in enumerate(labels):
        args = ["/usr/bin/python3", str(runtime / "ai_sleep.py")]
        config = {"Label": label, "ProgramArguments": args, "RunAtLoad": True,
                  "ProcessType": "Background", "StandardOutPath": str(logs / "ai-sleep.log"),
                  "StandardErrorPath": str(logs / "ai-sleep.log")}
        if index == 0:
            config.update(KeepAlive=True, ThrottleInterval=15)
        else:
            args.append("--watchdog")
            config["StartInterval"] = 60
        path = launch / (label + ".plist")
        tmp = path.with_suffix(".tmp")
        with tmp.open("wb") as stream:
            plistlib.dump(config, stream)
        os.replace(tmp, path)
        subprocess.run(["/bin/launchctl", "bootstrap", domain, str(path)], check=True, timeout=15)
        if index == 0:
            # Do not let RunAtLoad watchdog race the initial controller inventory.
            for _ in range(50):
                try:
                    state = json.loads((root / "lease.json").read_text())
                    if valid_lease(state, boot):
                        break
                except (OSError, ValueError, AttributeError):
                    pass
                time.sleep(0.2)
            else:
                print("Controller lease not ready; installing watchdog to release any stale override.")
    if not valid_lease(json.loads((root / "lease.json").read_text()), boot):
        raise RuntimeError("Controller did not publish a healthy lease; inspect ai-sleep.log")
    print("Installed AI sleep controller, watchdog, and preserved Claude/Cursor hook configuration.")


if __name__ == "__main__":
    main()
