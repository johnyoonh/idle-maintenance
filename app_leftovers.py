#!/usr/bin/python3
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from restore_sources import normalize_app_name

EXCLUDED_PARTS = {
    "Cache",
    "Caches",
    "CrashReporter",
    "Logs",
    "Saved Application State",
}

def expand_path(path):
    return Path(os.path.expanduser(path))

def app_display_name(app_path):
    name = Path(app_path).name
    if name.endswith(".app"):
        name = name[:-4]
    return name

def path_size(path):
    path = Path(path)
    if path.is_file():
        return path.stat().st_size
    total = 0
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in EXCLUDED_PARTS]
        for filename in files:
            item = Path(root) / filename
            try:
                if item.is_symlink():
                    continue
                total += item.stat().st_size
            except OSError:
                continue
    return total

def contains_excluded_part(path):
    return any(part in EXCLUDED_PARTS for part in Path(path).parts)

def backup_path_for(path):
    home = Path.home().resolve()
    try:
        rel = Path(path).resolve().relative_to(home)
    except ValueError:
        return None
    return home / "Library/CloudStorage/Dropbox/Mackup" / rel

def is_mackup_backed(path):
    backup_path = backup_path_for(path)
    return bool(backup_path and backup_path.exists())

def is_yadm_tracked(path):
    home = Path.home().resolve()
    try:
        rel = str(Path(path).resolve().relative_to(home))
    except ValueError:
        return False
    try:
        result = subprocess.run(
            ["yadm", "ls-files", "--error-unmatch", rel],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return result.returncode == 0
    except Exception:
        return False

def preference_matches(path, app_key):
    stem = Path(path).stem
    return app_key and app_key in normalize_app_name(stem)

def candidate_paths(app_path, metadata):
    home = Path.home().resolve()
    name = app_display_name(app_path)
    app_key = normalize_app_name(name)
    candidates = []

    support_names = [name, app_key, name.replace(" ", "-").lower(), name.replace(" ", "").lower()]
    config_names = [app_key, name.replace(" ", "-").lower(), name.replace(" ", "").lower()]
    seen_names = set()
    for dirname in support_names:
        if dirname in seen_names:
            continue
        seen_names.add(dirname)
        if dirname:
            candidates.append(home / "Library/Application Support" / dirname)
    seen_names = set()
    for dirname in config_names:
        if dirname in seen_names:
            continue
        seen_names.add(dirname)
        if dirname:
            candidates.append(home / ".config" / dirname)
            candidates.append(home / ".local/share" / dirname)

    bundle_id = metadata.get("bundle_id") if isinstance(metadata, dict) else ""
    if bundle_id:
        candidates.append(home / "Library/Preferences" / f"{bundle_id}.plist")

    preferences = home / "Library/Preferences"
    if preferences.exists():
        try:
            for item in preferences.iterdir():
                if item.name.endswith(".plist") and preference_matches(item, app_key):
                    candidates.append(item)
        except OSError:
            pass

    deduped = []
    seen = set()
    for path in candidates:
        expanded = Path(path).expanduser()
        try:
            key = str(expanded.resolve()) if expanded.exists() else str(expanded)
        except OSError:
            key = str(expanded)
        key = key.lower()
        if key not in seen:
            seen.add(key)
            deduped.append(expanded)
    return deduped

def discover_leftovers(app_path, metadata, config):
    cleanup = config.get("app_cleanup", {})
    leftover_cfg = cleanup.get("leftover_review", {})
    if not leftover_cfg.get("enabled", True):
        return []
    if leftover_cfg.get("mode", "conservative") != "conservative":
        return []

    max_item_size = int(float(leftover_cfg.get("max_item_size_mb", 25)) * 1024 * 1024)
    max_total_size = int(float(leftover_cfg.get("max_total_size_mb", 250)) * 1024 * 1024)

    leftovers = []
    total_size = 0
    for path in candidate_paths(app_path, metadata):
        try:
            if not path.exists() or path.is_symlink() or contains_excluded_part(path):
                continue
            size = path_size(path)
        except OSError:
            continue

        excluded_reason = ""
        eligible = True
        if size > max_item_size:
            eligible = False
            excluded_reason = "item-size-limit"
        elif total_size + size > max_total_size:
            eligible = False
            excluded_reason = "total-size-limit"

        if eligible:
            total_size += size

        leftovers.append({
            "path": str(path),
            "size": size,
            "eligible": eligible,
            "excluded_reason": excluded_reason,
            "mackup_backed": is_mackup_backed(path),
            "yadm_tracked": is_yadm_tracked(path),
        })

    return leftovers

def summarize_leftovers(leftovers):
    eligible = [item for item in leftovers if item.get("eligible")]
    if not eligible:
        return ""
    total = sum(int(item.get("size", 0)) for item in eligible)
    backed = sum(1 for item in eligible if item.get("mackup_backed") or item.get("yadm_tracked"))
    size_text = format_size(total)
    if backed == len(eligible):
        return f"Leftovers: {len(eligible)} config items, {size_text}, backed"
    if backed:
        return f"Leftovers: {len(eligible)} config items, {size_text}, partly backed"
    return f"Leftovers: {len(eligible)} config items, {size_text}"

def format_size(size):
    size = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024

def unique_quarantine_path(base, rel):
    dest = Path(base) / rel
    if not dest.exists():
        return dest
    suffix = time.strftime("%H%M%S")
    return dest.with_name(f"{dest.name}.{suffix}")

def quarantine_leftovers(app_path, metadata, leftovers, config):
    cleanup = config.get("app_cleanup", {})
    leftover_cfg = cleanup.get("leftover_review", {})
    if leftover_cfg.get("action", "quarantine") != "quarantine":
        return []

    today = time.strftime("%Y-%m-%d")
    quarantine_base = expand_path(leftover_cfg.get(
        "quarantine_dir",
        "~/Library/Application Support/idle-maintenance/quarantine",
    )) / today
    ledger_path = expand_path(leftover_cfg.get(
        "ledger",
        "~/Library/Application Support/idle-maintenance/config-quarantine.jsonl",
    ))
    ledger_path.parent.mkdir(parents=True, exist_ok=True)

    home = Path.home().resolve()
    entries = []
    for item in leftovers:
        if not item.get("eligible"):
            continue
        src = Path(item["path"])
        if not src.exists() or src.is_symlink():
            continue
        try:
            rel = src.resolve().relative_to(home)
        except ValueError:
            rel = Path(src.name)
        dest = unique_quarantine_path(quarantine_base, rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))
        entry = {
            "action": "quarantined-config",
            "app_path": app_path,
            "bundle_id": metadata.get("bundle_id", ""),
            "original_path": str(src),
            "quarantine_path": str(dest),
            "quarantined_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "size": item.get("size", 0),
            "mackup_backed": bool(item.get("mackup_backed")),
            "yadm_tracked": bool(item.get("yadm_tracked")),
        }
        with open(ledger_path, "a") as f:
            f.write(json.dumps(entry, sort_keys=True) + "\n")
        entries.append(entry)
    return entries
