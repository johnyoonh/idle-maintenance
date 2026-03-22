#!/usr/bin/env python3
"""
Idle Maintenance Prompt Suggester
Shows one suggestion per shell session - persists until acted upon
"""
import json
import os
import sys
import time
import random
import stat
import subprocess
from datetime import datetime

STATE_PATH = os.path.expanduser("~/Library/Application Support/idle-maintenance/state.json")
CACHE_PATH = os.path.expanduser("~/Library/Application Support/idle-maintenance/cache.json")
SESSION_PATH = os.path.expanduser("~/Library/Application Support/idle-maintenance/session.json")
CACHE_TTL = 300  # 5 minutes cache TTL

# Script directories to scan
SCRIPT_DIRS = [
    os.path.expanduser("~/.local/bin"),
    os.path.expanduser("~/Dropbox/Mackup/scripts"),
]

# Common maintenance commands/aliases
COMMON_COMMANDS = [
    {"cmd": "brew cleanup -s && brew autoremove", "desc": "Clean up Homebrew cache and old versions", "freq": 168},
    {"cmd": "docker system prune -af --volumes", "desc": "Remove unused Docker images and containers", "freq": 168},
    {"cmd": "bw sync", "desc": "Sync Bitwarden vault with server", "freq": 24},
    {"cmd": "chezmoi update && chezmoi apply", "desc": "Update dotfiles from Bitwarden", "freq": 24},
]

# ANSI colors
CYAN = "\033[36m"
BLUE = "\033[34m"
YELLOW = "\033[33m"
GOLD = "\033[38;5;220m"  # 256-color gold/yellow
GREEN = "\033[32m"
RED = "\033[31m"
MAGENTA = "\033[35m"
WHITE = "\033[97m"
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"

def load_json(path, default=None):
    """Load JSON file with error handling"""
    if default is None:
        default = {}
    if not os.path.exists(path):
        return default
    try:
        with open(path, 'r') as f:
            return json.load(f)
    except:
        return default

def save_json(path, data):
    """Save JSON file with error handling"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        pass  # Silently fail to avoid breaking prompt

def is_cache_valid():
    """Check if cache exists and is still valid"""
    if not os.path.exists(CACHE_PATH):
        return False
    try:
        cache = load_json(CACHE_PATH)
        cache_time = cache.get("timestamp", 0)
        return (time.time() - cache_time) < CACHE_TTL
    except:
        return False

def load_cached_scripts():
    """Load scripts from cache"""
    cache = load_json(CACHE_PATH)
    return cache.get("scripts", [])

def is_executable(filepath):
    """Check if file is executable"""
    try:
        st = os.stat(filepath)
        return bool(st.st_mode & stat.S_IXUSR)
    except:
        return False

def extract_description(filepath, filename):
    """Extract meaningful description from script, checking multiple sources"""
    desc = f"Run {filename}"

    # First tier: Try script comments
    try:
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()
            candidates = []

            # Look through first 30 lines for good descriptions
            for i, line in enumerate(lines[:30]):
                line = line.strip()
                if line.startswith('#'):
                    comment = line.lstrip('#').strip()

                    # Skip common boilerplate
                    if not comment:
                        continue
                    if 'coding:' in comment.lower() or 'encoding:' in comment.lower():
                        continue
                    if comment.startswith('-*-') or comment.startswith('vim:'):
                        continue
                    if comment.lower().startswith('usage:'):
                        continue
                    if comment.lower().startswith('author:'):
                        continue
                    if comment.startswith('!/'):  # Shebang without #
                        continue
                    if comment.startswith('<!'):  # Binary/encoded content
                        continue
                    if len(comment) < 10:  # Too short
                        continue
                    # Skip if mostly non-printable characters
                    if sum(1 for c in comment if not c.isprintable()) > len(comment) * 0.3:
                        continue

                    # Prefer longer, more descriptive comments
                    # Also prefer comments that contain certain keywords
                    score = len(comment)
                    if any(keyword in comment.lower() for keyword in ['usage:', 'description:', 'purpose:', 'script to', 'tool to', 'command to']):
                        score += 100
                    if i < 5:  # Early in file
                        score += 20
                    if comment[0].isupper():  # Starts with capital
                        score += 10

                    candidates.append((score, comment))

            # Pick the best candidate from comments
            if candidates:
                candidates.sort(reverse=True)
                best_desc = candidates[0][1]
                # Only use if it's not just a generic description
                if not best_desc.lower().startswith('run '):
                    desc = best_desc
                    # Clean up common prefixes
                    for prefix in ['Description: ', 'Purpose: ', 'Script to ', 'Tool to ', 'Command to ']:
                        if desc.startswith(prefix):
                            desc = desc[len(prefix):]
                            break
                    return desc
    except:
        pass

    # Second tier: Try --help flag (quick timeout)
    if desc.startswith('Run '):
        try:
            result = subprocess.run(
                [filepath, '--help'],
                capture_output=True,
                text=True,
                timeout=0.5,  # Quick timeout
                errors='ignore'
            )
            if result.returncode == 0 and result.stdout:
                # Look for description lines in help output
                lines = result.stdout.split('\n')

                # First, try to find a description line (often after blank line)
                for i, line in enumerate(lines[:20]):
                    line = line.strip()
                    # Skip empty, usage lines at the start, and option lists
                    if not line or line.startswith('-') or line.startswith('Usage:'):
                        continue
                    # Look for description patterns
                    if len(line) > 20 and not line.startswith(filename):
                        # Check if this looks like a description (sentence case, ends with period, etc)
                        if line[0].isupper() or ':' in line:
                            desc = line[:80]  # Limit to 80 chars
                            return desc

                # Fallback: use first non-empty substantial line
                for line in lines[:10]:
                    line = line.strip()
                    if line and len(line) > 15 and not line.startswith('-'):
                        if filename.lower() not in line.lower() or len(line) > len(filename) + 10:
                            desc = line[:80]
                            return desc
        except (subprocess.TimeoutExpired, FileNotFoundError, PermissionError):
            pass

    return desc

def discover_scripts():
    """Auto-discover executable scripts from configured directories"""
    scripts = []

    for script_dir in SCRIPT_DIRS:
        if not os.path.isdir(script_dir):
            continue

        for filename in os.listdir(script_dir):
            filepath = os.path.join(script_dir, filename)

            # Skip directories, hidden files, and non-executables
            if os.path.isdir(filepath) or filename.startswith('.') or not is_executable(filepath):
                continue

            desc = extract_description(filepath, filename)

            # Get modification time for LIFO ordering (newest first)
            try:
                mtime = os.path.getmtime(filepath)
            except:
                mtime = 0

            scripts.append({
                "script": filename,
                "command": filename,
                "description": desc,
                "frequency_hours": 168,  # Default to weekly
                "mtime": mtime,
            })

    # Add common commands (with current time so they appear at top of stack)
    for cmd_info in COMMON_COMMANDS:
        scripts.append({
            "script": cmd_info["cmd"].split()[0],  # First word as ID
            "command": cmd_info["cmd"],
            "description": cmd_info["desc"],
            "frequency_hours": cmd_info["freq"],
            "mtime": time.time(),  # Current time for common commands
        })

    # Sort by modification time DESC (newest first - LIFO stack)
    scripts.sort(key=lambda x: x.get("mtime", 0), reverse=True)

    # Cache the results
    save_json(CACHE_PATH, {
        "timestamp": time.time(),
        "scripts": scripts
    })

    return scripts

def get_scripts():
    """Get scripts, using cache if available, otherwise discover"""
    if is_cache_valid():
        return load_cached_scripts()

    # Trigger background refresh but return cached data if available
    old_cache = load_cached_scripts()
    if old_cache:
        # Start background refresh (non-blocking)
        import subprocess
        subprocess.Popen(
            [sys.executable, __file__, "--refresh-cache"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True
        )
        return old_cache
    else:
        # No cache at all, do sync discovery once
        return discover_scripts()

def refresh_cache():
    """Background task to refresh the cache"""
    discover_scripts()

def get_current_session_id():
    """Get a unique session ID for this shell instance"""
    # Use environment variable set by zshrc (stable per shell instance)
    session_id = os.environ.get("MAINT_SESSION_ID")
    if not session_id:
        # Fallback to PID if env var not set
        session_id = f"{os.getpid()}"
    return session_id

def has_session_shown():
    """Check if this shell session has already shown a suggestion"""
    session_id = get_current_session_id()
    session_data = load_json(SESSION_PATH, {})

    # Clean up old sessions (older than 24 hours)
    current_time = time.time()
    session_data = {
        sid: data for sid, data in session_data.items()
        if current_time - data.get("timestamp", 0) < 86400
    }
    save_json(SESSION_PATH, session_data)

    return session_id in session_data

def mark_session_shown(suggestion):
    """Mark this shell session as having shown a suggestion"""
    session_id = get_current_session_id()
    session_data = load_json(SESSION_PATH, {})
    session_data[session_id] = {
        "timestamp": time.time(),
        "suggestion": suggestion
    }
    save_json(SESSION_PATH, session_data)

def get_session_suggestion():
    """Get the suggestion for this session (same one until acted upon)"""
    session_id = get_current_session_id()
    session_data = load_json(SESSION_PATH, {})
    return session_data.get(session_id, {}).get("suggestion")

def get_suggestion():
    """Get a maintenance suggestion (once per shell session)"""
    # Check if this session already showed a suggestion
    if has_session_shown():
        return get_session_suggestion()

    state = load_json(STATE_PATH, {"last_run": {}, "dismissed": {}, "completed": []})

    # Get scripts (from cache or discovery)
    suggestions = get_scripts()
    if not suggestions:
        return None

    now = datetime.now()
    current_hour = now.hour

    # Only show during work hours (9am-8pm)
    if current_hour < 9 or current_hour > 20:
        return None

    # Filter suggestions that are due (already sorted by mtime DESC - LIFO stack)
    # We'll pick the FIRST matching suggestion (newest)
    for sug in suggestions:
        script_id = sug["script"]
        frequency_hours = sug.get("frequency_hours", 168)

        # Skip if dismissed recently
        dismissed_ts = state.get("dismissed", {}).get(script_id, 0)
        if time.time() - dismissed_ts < frequency_hours * 3600:
            continue

        # Check when last run
        last_run = state.get("last_run", {}).get(script_id, 0)
        hours_since = (time.time() - last_run) / 3600

        if hours_since >= frequency_hours:
            # Found the first (newest) due suggestion - use it (LIFO stack)
            mark_session_shown(sug)
            return sug

    # No suggestions found
    return None

def format_suggestion(sug):
    """Format suggestion in single line with visible colors"""
    script = sug["script"]
    desc = sug["description"]
    cmd = sug["command"]

    # Single line - command first in bold gold, then description
    output = f"\n{BOLD}{GOLD}{cmd}{RESET} {DIM}•{RESET} {WHITE}{desc}{RESET} {DIM}|{RESET} {GREEN}1=Run{RESET} {RED}2=Del{RESET} {YELLOW}3=Try{RESET} {MAGENTA}4=Skip{RESET}\n"

    return output

def main():
    """Main entry point"""
    # Check if this is a background cache refresh
    if len(sys.argv) > 1 and sys.argv[1] == "--refresh-cache":
        refresh_cache()
        sys.exit(0)

    # Normal operation - show suggestion (once per session)
    suggestion = get_suggestion()
    if suggestion:
        print(format_suggestion(suggestion))

if __name__ == "__main__":
    main()
