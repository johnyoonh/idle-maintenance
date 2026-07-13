#!/usr/bin/env python3
"""Act on the current terminal maintenance suggestion."""

import json
import os
import subprocess
import sys
import time

APP_SUPPORT_DIR = os.path.expanduser("~/Library/Application Support/idle-maintenance")
STATE_PATH = os.path.join(APP_SUPPORT_DIR, "state.json")
CACHE_PATH = os.path.join(APP_SUPPORT_DIR, "cache.json")
SESSION_PATH = os.path.join(APP_SUPPORT_DIR, "session.json")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROMPT_SCRIPT = os.path.join(BASE_DIR, "prompt-suggest.py")

COMMANDS = {"list", "run", "dismiss", "preview", "later", "enable", "status", "help"}
LEGACY_ACTIONS = {
    "1": "run", "r": "run", "run": "run",
    "2": "dismiss", "d": "dismiss", "delete": "dismiss", "dismiss": "dismiss",
    "3": "preview", "p": "preview", "t": "preview", "try": "preview", "preview": "preview",
    "4": "later", "l": "later", "s": "later", "skip": "later", "later": "later",
}


def load_json(path, default):
    try:
        with open(path, "r") as handle:
            return json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)


def session_id():
    return os.environ.get("MAINT_SESSION_ID") or str(os.getpid())


def current_suggestion():
    sessions = load_json(SESSION_PATH, {})
    return sessions.get(session_id(), {}).get("suggestion")


def clear_current_session():
    sessions = load_json(SESSION_PATH, {})
    sessions.pop(session_id(), None)
    save_json(SESSION_PATH, sessions)


def suggestions():
    cached = load_json(CACHE_PATH, {}).get("scripts", [])
    current = current_suggestion()
    if current and not any(item.get("script") == current.get("script") for item in cached):
        cached.insert(0, current)
    return cached


def find_suggestion(script_id=None):
    if not script_id:
        current = current_suggestion()
        if current:
            return current
        script_id = os.environ.get("_MAINT_CURRENT_SCRIPT")
    for suggestion in suggestions():
        if suggestion.get("script") == script_id:
            return suggestion
    return None


def default_state():
    return {"last_run": {}, "dismissed": {}, "disabled": {}, "completed": []}


def load_state():
    state = load_json(STATE_PATH, default_state())
    for key, value in default_state().items():
        state.setdefault(key, value.copy() if isinstance(value, dict) else list(value))
    return state


def show_next_suggestion():
    if os.path.exists(PROMPT_SCRIPT):
        subprocess.run([sys.executable, PROMPT_SCRIPT], check=False)


def run_command(suggestion, runner=subprocess.run):
    print(f"\033[1;32m→ Running:\033[0m {suggestion['description']}")
    print(f"\033[90m$ {suggestion['command']}\033[0m\n")
    result = runner(suggestion["command"], shell=True, check=False)
    state = load_state()
    state["completed"].append({
        "script": suggestion["script"],
        "timestamp": time.time(),
        "success": result.returncode == 0,
    })
    if result.returncode == 0:
        state["last_run"][suggestion["script"]] = time.time()
        state["dismissed"].pop(suggestion["script"], None)
        save_json(STATE_PATH, state)
        clear_current_session()
        print("\n\033[1;32m✓ Completed\033[0m")
    else:
        save_json(STATE_PATH, state)
        print(f"\n\033[1;31m✗ Failed with exit code {result.returncode}\033[0m")
    return result.returncode


def dismiss_suggestion(suggestion):
    state = load_state()
    state["disabled"][suggestion["script"]] = time.time()
    state["dismissed"].pop(suggestion["script"], None)
    save_json(STATE_PATH, state)
    clear_current_session()
    print(f"Dismissed {suggestion['description']}. Re-enable with: maint enable {suggestion['script']}")
    show_next_suggestion()
    return 0


def defer_suggestion(suggestion):
    hours = float(suggestion.get("frequency_hours", 168))
    state = load_state()
    state["dismissed"][suggestion["script"]] = time.time()
    save_json(STATE_PATH, state)
    clear_current_session()
    print(f"Later: {suggestion['description']} will return in {hours:g} hours.")
    show_next_suggestion()
    return 0


def preview_suggestion(suggestion, input_fn=input):
    print(f"\033[1m{suggestion['description']}\033[0m")
    print(f"\033[90m$ {suggestion['command']}\033[0m")
    try:
        input_fn("Press Enter to run, or Ctrl+C to cancel: ")
    except KeyboardInterrupt:
        print("\nCancelled.")
        return 130
    return run_command(suggestion)


def enable_suggestion(script_id):
    state = load_state()
    if script_id not in state["disabled"]:
        print(f"Suggestion '{script_id}' is not dismissed.")
        return 1
    state["disabled"].pop(script_id, None)
    save_json(STATE_PATH, state)
    print(f"Enabled suggestion '{script_id}'.")
    return 0


def list_suggestions(include_disabled=False):
    state = load_state()
    disabled = state["disabled"]
    rows = []
    for suggestion in suggestions():
        script_id = suggestion.get("script", "")
        if script_id in disabled and not include_disabled:
            continue
        status = "dismissed" if script_id in disabled else "available"
        rows.append((script_id, status, suggestion.get("description", "")))
    if not rows:
        print("No cached maintenance suggestions.")
        return 0
    for script_id, status, description in rows:
        print(f"{script_id:24} {status:10} {description}")
    return 0


def print_help():
    print("""Usage:
  maint <command> [script-id]

Commands:
  list [--all]          List cached suggestions
  run [script-id]       Run a suggestion
  dismiss [script-id]   Hide a suggestion until explicitly enabled
  preview [script-id]   Show the command, then optionally run it
  later [script-id]     Defer it for its normal frequency
  enable <script-id>    Re-enable a dismissed suggestion
  status [--json]       Show scheduled and interactive maintenance status

If script-id is omitted, the current terminal suggestion is used.
Legacy 'maint <script-id> <1-4>' forms remain temporarily supported.""")


def parse_args(argv):
    if not argv or argv[0] in {"-h", "--help", "help"}:
        return "help", None, False, False
    if argv[0] in COMMANDS:
        command = argv[0]
        include_all = "--all" in argv[1:]
        as_json = "--json" in argv[1:]
        script_id = next((arg for arg in argv[1:] if not arg.startswith("--")), None)
        return command, script_id, include_all, as_json
    if len(argv) >= 2 and argv[1].lower() in LEGACY_ACTIONS:
        print(
            "Deprecated: use 'maint <action> [script-id]'; legacy numeric actions will be removed.",
            file=sys.stderr,
        )
        return LEGACY_ACTIONS[argv[1].lower()], argv[0], False, False
    return "show", argv[0], False, False


def main(argv=None):
    command, script_id, include_all, as_json = parse_args(list(sys.argv[1:] if argv is None else argv))
    if command == "help":
        print_help()
        return 0
    if command == "list":
        return list_suggestions(include_all)
    if command == "status":
        from maintenance_status import main as status_main
        return status_main(["--json"] if as_json else [])
    if command == "enable":
        if not script_id:
            print("maint enable requires a script-id.", file=sys.stderr)
            return 2
        return enable_suggestion(script_id)

    suggestion = find_suggestion(script_id)
    if not suggestion:
        print("No matching maintenance suggestion. Run 'maint list' to inspect the cache.", file=sys.stderr)
        return 1
    if command == "show":
        print(suggestion["description"])
        print(f"$ {suggestion['command']}")
        return 0
    if command == "run":
        return run_command(suggestion)
    if command == "dismiss":
        return dismiss_suggestion(suggestion)
    if command == "preview":
        return preview_suggestion(suggestion)
    if command == "later":
        return defer_suggestion(suggestion)
    print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
