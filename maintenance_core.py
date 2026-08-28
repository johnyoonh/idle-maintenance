#!/usr/bin/python3
import subprocess
import os
import json
import time
import sys
import shlex
import shutil
import tempfile
import uuid
from pathlib import Path
from idle_config import (
    APP_SUPPORT_DIR,
    DEFAULT_CONFIG,
    keep_entry_is_active,
    load_config,
    next_keep_delay_days,
    parse_keep_entry,
)
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

def _terminal_notifier_path():
    discovered = shutil.which("terminal-notifier")
    if discovered:
        return discovered
    for candidate in (
        "/opt/homebrew/bin/terminal-notifier",
        "/usr/local/bin/terminal-notifier",
    ):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def notify_user(title, message, *, click_path=None):
    if click_path is not None:
        notifier = _terminal_notifier_path()
        if notifier:
            resolved_path = str(Path(click_path).expanduser().resolve())
            editor_command = (
                "/usr/bin/open -a "
                + shlex.quote("Visual Studio Code")
                + " -- "
                + shlex.quote(resolved_path)
                + " || /usr/bin/open -t -- "
                + shlex.quote(resolved_path)
            )
            try:
                result = subprocess.run(
                    [
                        notifier,
                        "-title",
                        title,
                        "-message",
                        message,
                        "-execute",
                        editor_command,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                if result.returncode == 0:
                    return
                detail = (result.stderr or result.stdout or "").strip()
                log(f"Actionable notification failed: {detail or f'exit {result.returncode}'}")
            except (OSError, subprocess.TimeoutExpired) as error:
                log(f"Actionable notification failed: {error}")

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

def record_keep(whitelist, key):
    previous = parse_keep_entry(whitelist.get(key))
    keep_count = 1
    if previous:
        keep_count = previous["keep_count"] + 1
    whitelist[key] = {
        "kept_at": time.time(),
        "keep_count": keep_count
    }

def queue_item_is_snoozed(item, snooze_hours, now=None):
    last_prompted = float(item.get("last_prompted", 0) or 0)
    if last_prompted <= 0:
        return False
    current_time = time.time() if now is None else float(now)
    return current_time - last_prompted < max(0.0, float(snooze_hours)) * 3600

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

def app_usage_detail(last_used, stale_days_limit):
    value = (last_used or "Unknown").strip()
    if value == "Unknown":
        return (
            f"Last used: unknown • surfaced because the stale threshold is "
            f"{stale_days_limit} days"
        )

    date_text = value[:10]
    source = "observed usage" if "(observed)" in value else "Spotlight metadata"
    try:
        used_at = time.mktime(time.strptime(date_text, "%Y-%m-%d"))
        days_ago = max(0, int((time.time() - used_at) / 86400))
        return (
            f"Last used: {date_text} ({days_ago} days ago, {source}) • "
            f"stale threshold: {stale_days_limit} days"
        )
    except ValueError:
        return f"Last used: {value} • stale threshold: {stale_days_limit} days"

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

def trash_with_admin_mv(app_path, dest_path):
    script = '''
on run argv
    set appPath to item 1 of argv
    set destPath to item 2 of argv
    set shellCommand to "mkdir -p " & quoted form of POSIX path of (path to trash folder) & " && mv " & quoted form of appPath & " " & quoted form of destPath
    do shell script shellCommand with administrator privileges
    return "true"
end run
'''
    result = run_osascript(script, [app_path, dest_path])
    if result.returncode == 0 and "true" in result.stdout.lower():
        return True
    details = (result.stderr or result.stdout).strip()
    log(f"Admin move failed to trash {app_path}: {details}")
    return False

def prompt_user(app_path, close_on_unfocus=True, detail="", snooze_hours=720, keep_days=60):
    app_name = os.path.basename(app_path)
    swift_script = os.path.join(BASE_DIR, "prompt.swift")
    try:
        cmd = [
            "swift", swift_script, app_name, app_path,
            str(close_on_unfocus).lower(), "app", detail,
            str(snooze_hours), str(keep_days),
        ]
        res = subprocess.check_output(cmd, text=True).strip()
        for keyword in ["WHITELIST", "SNOOZE", "KEEP", "DELETE", "TRY", "SKIP", "QUIT"]:
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

def get_process_snapshot(config):
    ignored = set(config.get("process_ignore_commands", []))
    current_user = os.getenv("USER", "")

    try:
        output = subprocess.check_output(
            ["ps", "-Ao", "pid,user,%cpu,etime,comm,command", "-r"],
            text=True
        ).splitlines()
    except Exception as e:
        log(f"Failed to collect process list: {e}")
        return {}

    snapshot = {}

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

        snapshot[pid] = {
            "pid": pid,
            "user": user,
            "cpu": cpu,
            "etime": etime,
            "elapsed_seconds": elapsed,
            "comm": comm,
            "command": command
        }

    return snapshot

def get_candidate_processes(config, snapshot_provider=None, sleep_fn=time.sleep):
    high_cpu_threshold = float(config.get("process_high_cpu_threshold", 50.0))
    long_running_hours = int(config.get("process_long_running_hours", 24))
    long_running_min_cpu = float(config.get("process_long_running_min_cpu", 10.0))
    sample_count = max(1, int(config.get("process_cpu_sample_count", 3)))
    sample_interval = max(0.0, float(config.get("process_cpu_sample_interval_seconds", 30)))
    min_long_seconds = long_running_hours * 3600
    provider = snapshot_provider or (lambda: get_process_snapshot(config))

    first = provider()
    high_pids = {
        pid for pid, proc in first.items()
        if proc["cpu"] >= high_cpu_threshold
    }
    samples = [first]
    for _ in range(1, sample_count):
        if not high_pids:
            break
        sleep_fn(sample_interval)
        current = provider()
        samples.append(current)
        high_pids = {
            pid for pid in high_pids
            if pid in current and current[pid]["cpu"] >= high_cpu_threshold
        }

    candidates_by_comm = {}
    long_running_pids = {
        pid for pid, proc in first.items()
        if proc["elapsed_seconds"] >= min_long_seconds
        and proc["cpu"] >= long_running_min_cpu
    }
    eligible_pids = long_running_pids | high_pids
    latest = samples[-1]
    for pid in eligible_pids:
        proc = dict(latest.get(pid) or first[pid])
        cpu_samples = [sample[pid]["cpu"] for sample in samples if pid in sample]
        reasons = []
        if pid in high_pids and len(cpu_samples) == sample_count:
            reasons.append(
                f"CPU stayed at or above {high_cpu_threshold:.1f}% for "
                f"{sample_count} samples over {sample_interval * (sample_count - 1):.0f}s"
            )
        if pid in long_running_pids:
            reasons.append(
                f"Running {proc['etime']} (limit {long_running_hours}h) with "
                f"CPU at or above {long_running_min_cpu:.1f}%"
            )
        proc["cpu_samples"] = cpu_samples
        proc["reason"] = " • ".join(reasons)
        existing = candidates_by_comm.get(proc["comm"])
        if not existing or proc["cpu"] > existing["cpu"]:
            candidates_by_comm[proc["comm"]] = proc

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

def prompt_process(proc, snooze_hours=24, keep_days=1):
    command = (proc.get("command") or "").strip()
    cmd_token = (command.split() or [proc["comm"]])[0]
    process_name = os.path.basename(cmd_token) or os.path.basename(proc["comm"]) or proc["comm"]
    display_name = process_name
    if command and command != process_name:
        display_name = f"{process_name} ({command})"
    samples = proc.get("cpu_samples") or [proc["cpu"]]
    sample_text = ", ".join(f"{value:.1f}%" for value in samples)
    detail = (
        f"PID {proc['pid']} • CPU samples: {sample_text} • Elapsed {proc['etime']}"
        f"\nReason: {proc.get('reason', 'Matched the configured process policy')}"
    )
    if process_name == "fileproviderd":
        offender_summary = get_fileprovider_offender_summary(proc["pid"])
        if offender_summary:
            detail += f" • {offender_summary}"
    display_path = command or proc["comm"]
    swift_script = os.path.join(BASE_DIR, "prompt.swift")
    command = [
        "swift",
        swift_script,
        display_name,
        display_path,
        "false",
        "process",
        detail,
        str(snooze_hours),
        str(keep_days),
    ]
    try:
        result = subprocess.run(command, text=True, capture_output=True, check=False)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or f"swift exited {result.returncode}").strip()
            raise RuntimeError(detail.splitlines()[-1][:500])
        res = result.stdout.strip()
        upper = res.upper()
        if upper == "DELETE":
            return "KILL"
        if upper in {"INVESTIGATE", "KILL", "KEEP", "SNOOZE", "TRY", "SKIP", "WHITELIST", "QUIT"}:
            return upper
        return "QUIT"
    except Exception as e:
        log(f"Process prompt failed for {proc.get('comm', '?')}: {e}")
        raise RuntimeError(f"process prompt failed: {e}") from e

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

def build_codex_investigation_command(
    prompt_text,
    cwd="/",
    *,
    investigation_context=None,
    capture_script=None,
    investigation_token=None,
    started_at=None,
):
    launch_cwd = cwd if os.path.isabs(cwd) and os.path.isdir(cwd) else "/"
    context = investigation_context if isinstance(investigation_context, dict) else {}
    if not context or not capture_script or not os.path.isfile(capture_script):
        return "cd " + shlex.quote(launch_cwd) + " && exec codex " + shlex.quote(prompt_text)

    from activity_intelligence import investigation_summary_instruction

    token = str(investigation_token or uuid.uuid4().hex)
    launched_at = time.time() if started_at is None else float(started_at)
    augmented_prompt = prompt_text + investigation_summary_instruction(token)
    capture = [
        "/usr/bin/python3",
        capture_script,
        "capture",
        "--token",
        token,
        "--started-at",
        str(launched_at),
    ]
    for option, key in (
        ("--incident-id", "incident_id"),
        ("--process-key", "process_key"),
        ("--recurrence-group", "recurrence_group"),
    ):
        value = str(context.get(key) or "")
        if value:
            capture.extend([option, value])
    return (
        "cd " + shlex.quote(launch_cwd)
        + " && codex " + shlex.quote(augmented_prompt)
        + "; codex_status=$?; " + shlex.join(capture)
        + " >/dev/null 2>&1 || true; exit $codex_status"
    )

def create_codex_launch_file(
    prompt_text,
    cwd="/",
    directory=None,
    *,
    investigation_context=None,
    capture_script=None,
    investigation_token=None,
    started_at=None,
):
    """Create a private, self-deleting command file for a terminal tab."""
    codex_command = build_codex_investigation_command(
        prompt_text,
        cwd,
        investigation_context=investigation_context,
        capture_script=capture_script,
        investigation_token=investigation_token,
        started_at=started_at,
    )
    descriptor, path = tempfile.mkstemp(
        prefix="idle-maintenance-investigate-",
        suffix=".command",
        dir=directory,
        text=True,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/zsh\n")
            handle.write('rm -f -- "$0"\n')
            handle.write("exec /bin/zsh -lic " + shlex.quote(codex_command) + "\n")
        os.chmod(path, 0o700)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return path


def open_codex_in_terminal(
    prompt_text,
    cwd="/",
    launch_runner=subprocess.run,
    clipboard_fn=copy_text_to_clipboard,
    launch_directory=None,
    investigation_context=None,
    capture_script=None,
):
    """Open an investigation in a new terminal tab without Apple Events/TCC."""
    prompt_copied = clipboard_fn(prompt_text)
    try:
        launch_file = create_codex_launch_file(
            prompt_text,
            cwd,
            launch_directory,
            investigation_context=investigation_context,
            capture_script=capture_script or os.path.join(BASE_DIR, "activity_intelligence.py"),
        )
    except OSError as error:
        log(f"Failed to create Codex investigation launch file: {error}")
        return False, None, prompt_copied

    launchers = (
        ("iTerm", ["/usr/bin/open", "-b", "com.googlecode.iterm2", launch_file]),
        ("Terminal", ["/usr/bin/open", "-b", "com.apple.Terminal", launch_file]),
    )
    for app_name, command in launchers:
        try:
            result = launch_runner(
                command,
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError) as error:
            log(f"Failed to open {app_name} for Codex investigation: {error}")
            continue
        if result.returncode == 0:
            return True, app_name, prompt_copied
        detail = (result.stderr or result.stdout or f"open exited {result.returncode}").strip()
        log(f"Failed to open {app_name} for Codex investigation: {detail}")

    try:
        os.unlink(launch_file)
    except OSError:
        pass
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
    snooze_hours = max(0.0, float(config.get("process_snooze_hours", 24)))

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
        if queue_item_is_snoozed(item, snooze_hours):
            continue

        keep_days = next_keep_delay_days(
            config, process_whitelist.get(item["comm"]), "process_"
        )
        action = prompt_process(proc, snooze_hours=snooze_hours, keep_days=keep_days)
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
                notify_user(
                    "Idle Maintenance",
                    f"Stopped {proc['comm']} (PID {proc['pid']}).",
                )
            else:
                for q_item in current_queue:
                    if q_item.get("comm") == item["comm"]:
                        q_item["last_prompted"] = int(time.time())
                notify_user(
                    "Idle Maintenance",
                    f"Could not stop {proc['comm']} (PID {proc['pid']}). See IdleMaintenance.log.",
                )
            processed += 1
            continue
        if action == "INVESTIGATE":
            prompt_text = build_process_investigation_prompt(proc)
            cwd = process_cwd(proc)
            opened, terminal_app, prompt_copied = open_codex_in_terminal(
                prompt_text,
                cwd,
                investigation_context={
                    "incident_id": str(proc.get("incident_id") or ""),
                    "process_key": str(proc.get("process_key") or ""),
                    "recurrence_group": str(proc.get("recurrence_group") or ""),
                },
            )
            if not opened:
                if prompt_copied:
                    log(f"Copied Codex investigation prompt for {proc['comm']} to clipboard.")
                    notify_user(
                        "Idle Maintenance",
                        "Could not open an investigation tab; the prompt was copied to the clipboard.",
                    )
                else:
                    log(f"Failed to open Codex investigation prompt for {proc['comm']}.")
                    notify_user(
                        "Idle Maintenance",
                        "Could not open or copy the investigation prompt. See IdleMaintenance.log.",
                    )
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
        log(
            f"Failed to trash {app_path} via shutil; "
            f"skipping Finder automation and trying a privileged move: {e}"
        )
        success = trash_with_admin_mv(app_path, dest_path)
        if success:
            ledger_entry["action"] = "admin-trash"
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
        close_on_unfocus = False
        app_snooze_hours = max(0.0, float(config.get("app_snooze_hours", 720)))
        stale_days_limit = int(config.get("stale_days_limit", 90))

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
            if queue_item_is_snoozed(item, app_snooze_hours):
                continue

            app_done = False
            while not app_done and processed < max_prompts:
                last_used_info = app_usage_detail(
                    stale_dates.get(item["path"], "Unknown"), stale_days_limit
                )
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
                keep_days = next_keep_delay_days(config, whitelist.get(item["path"]))
                action = prompt_user(
                    item["path"], close_on_unfocus, last_used_info,
                    snooze_hours=app_snooze_hours, keep_days=keep_days,
                )

                if action == "QUIT":
                    save_json(QUEUE_PATH, current_queue)
                    save_json(WHITELIST_PATH, whitelist)
                    return

                if action in {"KEEP", "WHITELIST"}:
                    record_keep(whitelist, item["path"])
                    current_queue = [i for i in current_queue if i["path"] != item["path"]]
                    processed += 1
                    app_done = True
                elif action == "DELETE":
                    success = delete_app(item["path"], config)
                    if success:
                        current_queue = [i for i in current_queue if i["path"] != item["path"]]
                        restore_command = restore_source.get("restore_command", "")
                        restore_note = f" Restore with: {restore_command}" if restore_command else ""
                        notify_user(
                            "Idle Maintenance",
                            f"Moved {os.path.basename(item['path'])} to Trash.{restore_note}",
                        )
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
                    app_done = True
                else:  # SNOOZE/SKIP
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
