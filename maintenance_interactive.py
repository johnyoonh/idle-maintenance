#!/usr/bin/python3
import subprocess
import os
import json
import time
import sys
import shlex
from idle_config import APP_SUPPORT_DIR, DEFAULT_CONFIG, load_config
from restore_sources import app_metadata, classify_app_restore_source

LOCK_FILE = "/tmp/idle_maintenance.lock"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = APP_SUPPORT_DIR
LOG_DIR = os.path.expanduser("~/Library/Logs")
LOG_PATH = os.path.join(LOG_DIR, "IdleMaintenance.log")
QUEUE_PATH = os.path.join(STATE_DIR, "stale_queue.json")
WHITELIST_PATH = os.path.join(STATE_DIR, "custom_whitelist.json")
PROCESS_QUEUE_PATH = os.path.join(STATE_DIR, "process_queue.json")
PROCESS_WHITELIST_PATH = os.path.join(STATE_DIR, "process_whitelist.json")
DELETION_LEDGER_PATH = os.path.join(STATE_DIR, "app-deletions.jsonl")
DEFAULT_MAX_PROMPTS = int(DEFAULT_CONFIG["max_prompts"])

def log(msg):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(LOG_PATH, "a") as f:
        f.write(f"[{timestamp}] {msg}\n")

def notify_user(title, message):
    script = '''
on run argv
    display notification (item 2 of argv) with title (item 1 of argv)
end run
'''
    try:
        subprocess.run(
            ["osascript", "-"] + [title, message],
            input=script,
            capture_output=True,
            text=True,
        )
    except Exception:
        pass

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

def ensure_state_dir():
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
    except Exception as e:
        log(f"Failed to create state directory {STATE_DIR}: {e}")

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

def parse_keep_entry(value):
    if isinstance(value, dict):
        kept_at = value.get("kept_at", value.get("timestamp"))
        keep_count = value.get("keep_count", 1)
        try:
            kept_at = float(kept_at)
            keep_count = max(1, int(keep_count))
            return {"kept_at": kept_at, "keep_count": keep_count}
        except (TypeError, ValueError):
            return None
    try:
        return {"kept_at": float(value), "keep_count": 1}
    except (TypeError, ValueError):
        return None

def record_keep(whitelist, key):
    previous = parse_keep_entry(whitelist.get(key))
    keep_count = 1
    if previous:
        keep_count = previous["keep_count"] + 1
    whitelist[key] = {
        "kept_at": time.time(),
        "keep_count": keep_count
    }

def get_keep_delay_days(config, keep_count, prefix=""):
    base_key = f"{prefix}keep_days_limit"
    multiplier_key = f"{prefix}keep_backoff_multiplier"
    max_key = f"{prefix}keep_backoff_max_days"
    base_days = max(0.0, float(config.get(base_key, 1)))
    multiplier = max(1.0, float(config.get(multiplier_key, 2.0)))
    max_days = max(base_days, float(config.get(max_key, 30)))
    delay = base_days * (multiplier ** max(0, keep_count - 1))
    return min(delay, max_days)

def keep_entry_is_active(config, entry, prefix=""):
    keep_entry = parse_keep_entry(entry)
    if not keep_entry:
        return False
    keep_delay_days = get_keep_delay_days(config, keep_entry["keep_count"], prefix)
    time_since_keep = (time.time() - keep_entry["kept_at"]) / 86400.0
    return time_since_keep <= keep_delay_days

def save_json(path, data):
    ensure_state_dir()
    try:
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
    except: pass

def append_jsonl(path, entry):
    path = os.path.expanduser(path)
    ensure_state_dir()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path, "a") as f:
            f.write(json.dumps(entry, sort_keys=True) + "\n")
        return True
    except Exception as e:
        log(f"Failed to append {path}: {e}")
        return False

def app_cleanup_config(config):
    cleanup = config.get("app_cleanup", {})
    if not isinstance(cleanup, dict):
        cleanup = {}
    hooks = config.get("hooks", {})
    if not isinstance(hooks, dict):
        hooks = {}
    return cleanup, hooks

def get_restore_source(config, app_path):
    cleanup, _ = app_cleanup_config(config)
    providers = cleanup.get("restore_sources", [])
    if not isinstance(providers, list):
        providers = []
    return classify_app_restore_source(app_path, providers)

def run_delete_hooks(hook_paths, payload):
    for hook_path in hook_paths or []:
        hook_path = os.path.expanduser(str(hook_path))
        if not hook_path:
            continue
        try:
            result = subprocess.run(
                [hook_path, payload["app_path"], json.dumps(payload, sort_keys=True)],
                capture_output=True,
                text=True,
            )
        except Exception as e:
            log(f"Delete hook {hook_path} failed to run: {e}")
            return False

        if result.returncode != 0:
            details = (result.stderr or result.stdout).strip()
            log(f"Delete hook {hook_path} vetoed {payload['app_path']} with exit {result.returncode}: {details}")
            return False
    return True

def run_osascript(script, args):
    return subprocess.run(
        ["osascript", "-"] + args,
        input=script,
        capture_output=True,
        text=True,
    )

def trash_with_finder(app_path):
    script = '''
on run argv
    set appPath to item 1 of argv
    tell application "Finder"
        delete POSIX file appPath
    end tell
    return "true"
end run
'''
    result = run_osascript(script, [app_path])
    if result.returncode == 0 and "true" in result.stdout.lower():
        return True
    details = (result.stderr or result.stdout).strip()
    log(f"Finder failed to trash {app_path}: {details}")
    return False

def trash_with_admin_mv(app_path, dest_path):
    script = '''
on run argv
    set appPath to item 1 of argv
    set destPath to item 2 of argv
    set userName to item 3 of argv
    set groupName to item 4 of argv
    set shellCommand to "mkdir -p " & quoted form of POSIX path of (path to trash folder) & " && mv " & quoted form of appPath & " " & quoted form of destPath & " && chown -R " & quoted form of (userName & ":" & groupName) & " " & quoted form of destPath
    do shell script shellCommand with administrator privileges
    return "true"
end run
'''
    user_name = os.getenv("USER", "")
    group_name = subprocess.check_output(["id", "-gn"], text=True).strip()
    result = run_osascript(script, [app_path, dest_path, user_name, group_name])
    if result.returncode == 0 and "true" in result.stdout.lower():
        return True
    details = (result.stderr or result.stdout).strip()
    log(f"Admin move failed to trash {app_path}: {details}")
    return False

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

def parse_etime_seconds(etime):
    # ps etime format: [[dd-]hh:]mm:ss
    etime = etime.strip()
    days = 0
    if "-" in etime:
        day_part, time_part = etime.split("-", 1)
        days = int(day_part)
    else:
        time_part = etime

    parts = [int(p) for p in time_part.split(":")]
    if len(parts) == 3:
        hours, minutes, seconds = parts
    elif len(parts) == 2:
        hours = 0
        minutes, seconds = parts
    else:
        return 0
    return days * 86400 + hours * 3600 + minutes * 60 + seconds

def get_candidate_processes(config):
    high_cpu_threshold = float(config.get("process_high_cpu_threshold", 50.0))
    long_running_hours = int(config.get("process_long_running_hours", 24))
    long_running_min_cpu = float(config.get("process_long_running_min_cpu", 10.0))
    ignored = set(config.get("process_ignore_commands", []))
    current_user = os.getenv("USER", "")

    try:
        output = subprocess.check_output(
            ["ps", "-Ao", "pid,user,%cpu,etime,comm,command", "-r"],
            text=True
        ).splitlines()
    except Exception as e:
        log(f"Failed to collect process list: {e}")
        return []

    candidates_by_comm = {}
    min_long_seconds = long_running_hours * 3600

    for line in output[1:]:
        parts = line.strip().split(None, 5)
        if len(parts) < 6:
            continue
        pid_str, user, cpu_str, etime, comm, command = parts
        try:
            pid = int(pid_str)
            cpu = float(cpu_str)
            elapsed = parse_etime_seconds(etime)
        except ValueError:
            continue

        if user != current_user:
            continue
        if pid == os.getpid():
            continue
        if comm in ignored:
            continue

        high_cpu = cpu >= high_cpu_threshold
        long_running = elapsed >= min_long_seconds and cpu >= long_running_min_cpu
        if not (high_cpu or long_running):
            continue

        entry = {
            "pid": pid,
            "user": user,
            "cpu": cpu,
            "etime": etime,
            "elapsed_seconds": elapsed,
            "comm": comm,
            "command": command
        }
        existing = candidates_by_comm.get(comm)
        if not existing or cpu > existing["cpu"]:
            candidates_by_comm[comm] = entry

    candidates = list(candidates_by_comm.values())
    candidates.sort(key=lambda p: (-p["cpu"], -p["elapsed_seconds"]))
    return candidates

def get_fileprovider_offender_summary(pid):
    try:
        output = subprocess.check_output(
            ["lsof", "-p", str(pid), "-Fn"],
            text=True,
            stderr=subprocess.DEVNULL
        )
    except Exception as e:
        log(f"Failed to inspect fileproviderd offenders: {e}")
        return ""

    counts = {
        "OneDrive": 0,
        "Dropbox": 0,
        "iCloud": 0,
        "GoogleDrive": 0,
        "Other": 0
    }

    for line in output.splitlines():
        if not line.startswith("n"):
            continue
        path = line[1:]
        if "/Library/CloudStorage/OneDrive" in path:
            counts["OneDrive"] += 1
        elif "/Library/CloudStorage/Dropbox" in path:
            counts["Dropbox"] += 1
        elif "/Library/Mobile Documents" in path:
            counts["iCloud"] += 1
        elif "/Library/CloudStorage/GoogleDrive" in path:
            counts["GoogleDrive"] += 1
        elif "/Library/CloudStorage/" in path:
            counts["Other"] += 1

    total = sum(counts.values())
    if total == 0:
        return ""

    ranked = sorted(
        [(name, count) for name, count in counts.items() if count > 0],
        key=lambda x: x[1],
        reverse=True
    )
    top = ranked[:3]
    parts = [f"{name} {((count / total) * 100):.0f}%" for name, count in top]
    return "Providers: " + " • ".join(parts)

def prompt_process(proc):
    command = (proc.get("command") or "").strip()
    cmd_token = (command.split() or [proc["comm"]])[0]
    process_name = os.path.basename(cmd_token) or os.path.basename(proc["comm"]) or proc["comm"]
    display_name = process_name
    if command and command != process_name:
        display_name = f"{process_name} ({command})"
    detail = f"PID {proc['pid']} • CPU {proc['cpu']:.1f}% • Elapsed {proc['etime']}"
    if process_name == "fileproviderd":
        offender_summary = get_fileprovider_offender_summary(proc["pid"])
        if offender_summary:
            detail += f" • {offender_summary}"
    display_path = command or proc["comm"]
    swift_script = os.path.join(BASE_DIR, "prompt.swift")
    try:
        res = subprocess.check_output(
            [
                "swift",
                swift_script,
                display_name,
                display_path,
                "false",
                "__MODE__=process",
                detail
            ],
            text=True
        ).strip()
        upper = res.upper()
        if upper == "DELETE":
            return "KILL"
        if upper in {"INVESTIGATE", "KILL", "KEEP", "SNOOZE", "TRY", "SKIP", "WHITELIST", "QUIT"}:
            return upper
        return "QUIT"
    except Exception as e:
        log(f"Process prompt failed for {proc.get('comm', '?')}: {e}")
        return "QUIT"

def build_process_investigation_prompt(proc):
    lines = [
        "Investigate this high-impact macOS process and help me decide what to do.",
        "",
        "Please cover:",
        "1. What this process likely is.",
        "2. The most likely reason it is using resources right now.",
        "3. Whether it is usually safe to kill.",
        "4. Concrete commands to verify the cause on macOS.",
        "5. Recommended next action.",
        "",
        "Process details:",
        f"- PID: {proc['pid']}",
        f"- Command name: {proc['comm']}",
        f"- Full command: {proc['command']}",
        f"- CPU: {proc['cpu']:.1f}%",
        f"- Elapsed: {proc['etime']}",
    ]
    if proc["comm"] == "fileproviderd":
        offender_summary = get_fileprovider_offender_summary(proc["pid"])
        if offender_summary:
            lines.append(f"- Notes: {offender_summary}")
    return "\n".join(lines)

def copy_text_to_clipboard(text):
    try:
        subprocess.run(["pbcopy"], input=text, text=True, check=True)
        return True
    except Exception as e:
        log(f"Failed to copy investigation prompt to clipboard: {e}")
        return False

def run_applescript(script, args):
    cmd = ["osascript", "-e", script, "--"]
    cmd.extend(args)
    return subprocess.run(cmd, capture_output=True, text=True)

def process_cwd(proc, default="/"):
    try:
        result = subprocess.run(
            ["lsof", "-a", "-p", str(proc["pid"]), "-d", "cwd", "-Fn"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if line.startswith("n"):
                    cwd = line[1:]
                    if os.path.isabs(cwd) and os.path.isdir(cwd):
                        return cwd
    except Exception as e:
        log(f"Failed to resolve cwd for process {proc.get('pid', '?')}: {e}")
    return default

def build_codex_investigation_command(prompt_text, cwd="/"):
    launch_cwd = cwd if os.path.isabs(cwd) and os.path.isdir(cwd) else "/"
    return "cd " + shlex.quote(launch_cwd) + " && codex " + shlex.quote(prompt_text)

def open_codex_in_terminal(prompt_text, cwd="/"):
    prompt_copied = copy_text_to_clipboard(prompt_text)
    codex_command = build_codex_investigation_command(prompt_text, cwd)

    iterm_script = """
on run argv
    set commandText to item 1 of argv
    tell application "iTerm"
        activate
        if (count of windows) = 0 then
            create window with default profile
        else
            tell current window
                create tab with default profile
            end tell
        end if
        tell current session of current window
            write text commandText
        end tell
    end tell
end run
"""
    terminal_script = """
on run argv
    set commandText to item 1 of argv
    tell application "Terminal"
        activate
        do script commandText
    end tell
end run
"""

    for app_name, script in (("iTerm", iterm_script), ("Terminal", terminal_script)):
        result = run_applescript(script, [codex_command])
        if result.returncode == 0:
            return True, app_name, prompt_copied
        log(f"Failed to open {app_name} for Codex investigation: {result.stderr.strip()}")

    return False, None, prompt_copied

def kill_process(pid):
    try:
        os.kill(pid, 15)
        time.sleep(0.4)
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except Exception:
        pass

    try:
        os.kill(pid, 9)
        return True
    except ProcessLookupError:
        return True
    except Exception as e:
        log(f"Failed to kill process {pid}: {e}")
        return False

def run_process_audit(config, prompt_budget=None):
    process_max_prompts = int(config.get("process_max_prompts", DEFAULT_MAX_PROMPTS))
    if prompt_budget is not None:
        process_max_prompts = min(process_max_prompts, max(0, int(prompt_budget)))
    if process_max_prompts <= 0:
        return True, 0

    process_queue = load_json(PROCESS_QUEUE_PATH)
    if isinstance(process_queue, dict):
        process_queue = []
    process_whitelist = load_custom_whitelist(PROCESS_WHITELIST_PATH)

    candidates = get_candidate_processes(config)
    candidate_by_comm = {p["comm"]: p for p in candidates}

    process_queue = [item for item in process_queue if item.get("comm") in candidate_by_comm]
    existing = {item.get("comm") for item in process_queue}
    for proc in candidates:
        if keep_entry_is_active(config, process_whitelist.get(proc["comm"]), "process_"):
            continue
        if proc["comm"] not in existing:
            process_queue.append({"comm": proc["comm"], "last_prompted": 0})

    process_queue.sort(key=lambda x: x.get("last_prompted", 0))
    current_queue = [item for item in process_queue]
    processed = 0

    for item in process_queue:
        if processed >= process_max_prompts:
            break
        proc = candidate_by_comm.get(item["comm"])
        if not proc:
            continue

        action = prompt_process(proc)
        if action == "QUIT":
            save_json(PROCESS_QUEUE_PATH, current_queue)
            save_json(PROCESS_WHITELIST_PATH, process_whitelist)
            return False, processed
        if action in {"KEEP", "WHITELIST"}:
            record_keep(process_whitelist, item["comm"])
            current_queue = [i for i in current_queue if i.get("comm") != item["comm"]]
            processed += 1
            continue
        if action == "KILL":
            success = kill_process(proc["pid"])
            if success:
                current_queue = [i for i in current_queue if i.get("comm") != item["comm"]]
            else:
                for q_item in current_queue:
                    if q_item.get("comm") == item["comm"]:
                        q_item["last_prompted"] = int(time.time())
            processed += 1
            continue
        if action == "INVESTIGATE":
            prompt_text = build_process_investigation_prompt(proc)
            cwd = process_cwd(proc)
            opened, terminal_app, prompt_copied = open_codex_in_terminal(prompt_text, cwd)
            if not opened:
                if prompt_copied:
                    log(f"Copied Codex investigation prompt for {proc['comm']} to clipboard.")
                else:
                    log(f"Failed to open Codex investigation prompt for {proc['comm']}.")
            for q_item in current_queue:
                if q_item.get("comm") == item["comm"]:
                    q_item["last_prompted"] = int(time.time())
            save_json(PROCESS_QUEUE_PATH, current_queue)
            processed += 1
            if opened:
                log(f"Opened Codex investigation for {proc['comm']} in {terminal_app} at {cwd}.")
            continue
        if action == "TRY":
            subprocess.run(["open", "-a", "Activity Monitor"], stderr=subprocess.DEVNULL)
            for q_item in current_queue:
                if q_item.get("comm") == item["comm"]:
                    q_item["last_prompted"] = int(time.time())
            save_json(PROCESS_QUEUE_PATH, current_queue)
            processed += 1
            time.sleep(1)
            continue

        # SNOOZE keeps the process in rotation but moves it behind older prompts.
        if action == "SNOOZE":
            for q_item in current_queue:
                if q_item.get("comm") == item["comm"]:
                    q_item["last_prompted"] = int(time.time())
            processed += 1
            continue

        # Backward compatibility for older prompt.swift processes.
        record_keep(process_whitelist, item["comm"])
        current_queue = [i for i in current_queue if i.get("comm") != item["comm"]]
        processed += 1

    save_json(PROCESS_QUEUE_PATH, current_queue)
    save_json(PROCESS_WHITELIST_PATH, process_whitelist)
    return True, processed

def delete_app(app_path, config):
    cleanup, hooks = app_cleanup_config(config)
    restore_source = get_restore_source(config, app_path)
    allow_unknown = bool(cleanup.get("allow_unknown_restore_source", False))
    delete_mode = cleanup.get("delete_mode", "trash")
    ledger_path = cleanup.get("deletion_ledger", DELETION_LEDGER_PATH)

    if delete_mode != "trash":
        log(f"Refusing to delete {app_path}; unsupported delete_mode={delete_mode}.")
        notify_user("Idle Maintenance", f"Delete refused for {os.path.basename(app_path)}: unsupported delete mode.")
        return False

    if restore_source.get("source") == "unknown" and not allow_unknown:
        log(f"Refusing to delete unrecoverable app {app_path}; no Brewfile or MAS restore source found.")
        notify_user("Idle Maintenance", f"Delete refused for {os.path.basename(app_path)}: no restore source is configured.")
        return False

    metadata = app_metadata(app_path)
    hook_payload = {
        "action": "before_delete_app",
        "app_path": app_path,
        "bundle_id": metadata.get("bundle_id", ""),
        "delete_mode": delete_mode,
        "restore_command": restore_source.get("restore_command", ""),
        "restore_source": restore_source,
        "version": metadata.get("short_version") or metadata.get("version", ""),
    }
    if not run_delete_hooks(hooks.get("before_delete_app", []), hook_payload):
        notify_user("Idle Maintenance", f"Delete refused for {os.path.basename(app_path)}: a before-delete hook vetoed it.")
        return False

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
    ledger_entry = {
        "action": "trashed",
        "app_path": app_path,
        "bundle_id": metadata.get("bundle_id", ""),
        "deleted_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "restore_command": restore_source.get("restore_command", ""),
        "restore_source": restore_source,
        "trash_path": dest_path,
        "version": metadata.get("short_version") or metadata.get("version", ""),
    }
    try:
        shutil.move(app_path, dest_path)
        append_jsonl(ledger_path, ledger_entry)
        run_delete_hooks(hooks.get("after_delete_app", []), ledger_entry)
        return True
    except Exception as e:
        log(f"Failed to trash {app_path} via shutil (falling back to AppleScript): {e}")
        success = trash_with_finder(app_path)
        if not success:
            success = trash_with_admin_mv(app_path, dest_path)
        if success:
            ledger_entry["action"] = "applescript-trash"
            append_jsonl(ledger_path, ledger_entry)
            run_delete_hooks(hooks.get("after_delete_app", []), ledger_entry)
        else:
            notify_user("Idle Maintenance", f"Could not move {os.path.basename(app_path)} to Trash. See IdleMaintenance.log.")
        return success

def main():
    process_only = len(sys.argv) > 1 and sys.argv[1] == "--process-audit"

    if is_running():
        log("Already running (lock file active). Exiting.")
        return
    ensure_state_dir()
    create_lock()

    try:
        config = load_config(BASE_DIR)
        if process_only:
            run_process_audit(config)
            return

        max_entries = int(config.get("max_entries_per_idle_return", config.get("max_prompts", DEFAULT_MAX_PROMPTS)))
        max_entries = max(0, max_entries)

        process_ok, process_prompts = run_process_audit(config, prompt_budget=max_entries)
        if not process_ok:
            return
        remaining_prompts = max(0, max_entries - process_prompts)

        auditor_path = os.path.join(BASE_DIR, "app_auditor.py")
        try:
            stale_output = subprocess.check_output(["/usr/bin/python3", auditor_path], text=True).splitlines()
        except:
            stale_output = []

        stale_apps = []
        stale_dates = {}
        for line in stale_output:
            line = line.strip()
            if not line:
                continue
            if "|" in line:
                path, date_str = line.split("|", 1)
                stale_apps.append(path)
                stale_dates[path] = date_str
            else:
                stale_apps.append(line)
                stale_dates[line] = "Unknown"

        max_prompts = min(int(config.get("max_prompts", DEFAULT_MAX_PROMPTS)), remaining_prompts)
        close_on_unfocus = config.get("close_on_unfocus", True)

        queue = load_json(QUEUE_PATH)
        if isinstance(queue, dict): queue = []  # Just in case it gets mangled
        whitelist = load_custom_whitelist(WHITELIST_PATH)

        queue = [item for item in queue if item["path"] in stale_apps]
        existing_paths = [item["path"] for item in queue]
        for app in stale_apps:
            if app not in existing_paths and not keep_entry_is_active(config, whitelist.get(app)):
                queue.append({"path": app, "last_prompted": 0})

        queue.sort(key=lambda x: x["last_prompted"])

        processed = 0
        current_queue = [item for item in queue]

        for item in queue:
            if processed >= max_prompts:
                break

            app_done = False
            while not app_done and processed < max_prompts:
                last_used_info = stale_dates.get(item["path"], "Unknown")
                restore_source = get_restore_source(config, item["path"])
                cleanup, _ = app_cleanup_config(config)
                allow_unknown_restore = bool(cleanup.get("allow_unknown_restore_source", False))
                if restore_source.get("source") == "unknown" and not allow_unknown_restore:
                    last_used_info += " • Restore: unknown; delete disabled"
                elif restore_source.get("source") == "unknown":
                    last_used_info += " • Restore: unknown"
                else:
                    last_used_info += f" • Restore: {restore_source.get('restore_command', restore_source.get('source'))}"
                if item.get("last_prompted", 0) > 0:
                    last_used_info += f" (Last prompted/tried: {time.strftime('%Y-%m-%d', time.localtime(item['last_prompted']))})"
                action = prompt_user(item["path"], close_on_unfocus, last_used_info)

                if action == "QUIT":
                    save_json(QUEUE_PATH, current_queue)
                    save_json(WHITELIST_PATH, whitelist)
                    return

                if action == "KEEP":
                    record_keep(whitelist, item["path"])
                    current_queue = [i for i in current_queue if i["path"] != item["path"]]
                    processed += 1
                    app_done = True
                elif action == "DELETE":
                    success = delete_app(item["path"], config)
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
                    processed += 1
                    time.sleep(1)
                else:  # SKIP
                    for q_item in current_queue:
                        if q_item["path"] == item["path"]:
                            q_item["last_prompted"] = int(time.time())
                    processed += 1
                    app_done = True

        save_json(QUEUE_PATH, current_queue)
        save_json(WHITELIST_PATH, whitelist)

    finally:
        # Always clean up the lock file, even on crash or early exit
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)

if __name__ == "__main__":
    main()
