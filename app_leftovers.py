#!/usr/bin/python3
import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path

from restore_sources import normalize_app_name

EXCLUDED_PARTS = {
    "Cache",
    "Caches",
    "CrashReporter",
    "Logs",
    "Saved Application State",
}


def quarantine_enabled(config):
    cleanup = config.get("app_cleanup", {})
    policy = cleanup.get("leftover_review", {}) if isinstance(cleanup, dict) else {}
    return (
        isinstance(policy, dict)
        and policy.get("enabled") is True
        and policy.get("mode", "conservative") == "conservative"
        and policy.get("action") == "quarantine"
    )

def expand_path(path):
    return Path(os.path.expanduser(path))

def app_display_name(app_path):
    name = Path(app_path).name
    if name.endswith(".app"):
        name = name[:-4]
    return name

def inspect_path(path):
    """Return a complete size and reject trees that cannot be moved safely as a unit."""
    path = Path(path)
    try:
        if path.is_symlink():
            return 0, "contains-excluded-content"
        if path.is_file():
            return path.stat().st_size, ""
        if not path.is_dir():
            return 0, "unsupported-path"
    except OSError:
        return 0, "unreadable-content"

    total = 0
    failure = ""

    def on_walk_error(_error):
        nonlocal failure
        failure = "unreadable-content"

    for root, dirs, files in os.walk(path, topdown=True, onerror=on_walk_error, followlinks=False):
        kept_dirs = []
        for name in dirs:
            child = Path(root) / name
            if name in EXCLUDED_PARTS or child.is_symlink():
                if not failure:
                    failure = "contains-excluded-content"
                continue
            kept_dirs.append(name)
        dirs[:] = kept_dirs
        for name in files:
            child = Path(root) / name
            try:
                if child.is_symlink():
                    if not failure:
                        failure = "contains-excluded-content"
                    continue
                total += child.stat().st_size
            except OSError:
                failure = "unreadable-content"
    return total, failure

def contains_excluded_part(path):
    return any(part in EXCLUDED_PARTS for part in Path(path).parts)


def _is_same_or_nested(path, parent):
    try:
        Path(path).resolve().relative_to(Path(parent).resolve())
        return True
    except ValueError:
        return False

def _size_limits(policy):
    try:
        item = max(0, int(float(policy.get("max_item_size_mb", 25)) * 1024 * 1024))
        total = max(0, int(float(policy.get("max_total_size_mb", 250)) * 1024 * 1024))
        return item, total
    except (TypeError, ValueError, OverflowError):
        return 25 * 1024 * 1024, 250 * 1024 * 1024

def preference_matches(path, app_key):
    stem = Path(path).stem
    return bool(app_key) and normalize_app_name(stem) == app_key

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
    if isinstance(bundle_id, str) and re.fullmatch(r"[A-Za-z0-9.-]+", bundle_id):
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
    if not quarantine_enabled(config):
        return []

    cleanup = config.get("app_cleanup", {})
    leftover_cfg = cleanup.get("leftover_review", {})

    max_item_size, max_total_size = _size_limits(leftover_cfg)

    leftovers = []
    total_size = 0
    home = Path.home().resolve()
    quarantine_root = expand_path(leftover_cfg.get(
        "quarantine_dir",
        "~/Library/Application Support/idle-maintenance/quarantine",
    ))
    ledger_path = expand_path(leftover_cfg.get(
        "ledger",
        "~/Library/Application Support/idle-maintenance/config-quarantine.jsonl",
    ))
    for path in candidate_paths(app_path, metadata):
        try:
            if not path.exists() or path.is_symlink() or contains_excluded_part(path):
                continue
            resolved_path = path.resolve(strict=True)
            if not _is_same_or_nested(resolved_path, home):
                continue
            if (
                _is_same_or_nested(path, quarantine_root)
                or _is_same_or_nested(quarantine_root, path)
                or _is_same_or_nested(path, ledger_path)
                or _is_same_or_nested(ledger_path, path)
            ):
                continue
            size, inspection_error = inspect_path(resolved_path)
        except OSError:
            continue

        excluded_reason = inspection_error
        eligible = not inspection_error
        if eligible and size > max_item_size:
            eligible = False
            excluded_reason = "item-size-limit"
        elif eligible and total_size + size > max_total_size:
            eligible = False
            excluded_reason = "total-size-limit"

        if eligible:
            total_size += size

        leftovers.append({
            "path": str(path),
            "size": size,
            "eligible": eligible,
            "excluded_reason": excluded_reason,
        })

    return leftovers

def summarize_leftovers(leftovers):
    eligible = [item for item in leftovers if item.get("eligible")]
    if not eligible:
        return ""
    total = sum(int(item.get("size", 0)) for item in eligible)
    size_text = format_size(total)
    return f"Leftovers: {len(eligible)} config items, {size_text}"


def describe_leftovers(leftovers, limit=5):
    eligible = [item for item in leftovers if item.get("eligible")]
    if not eligible:
        return ""
    shown = [str(item["path"]) for item in eligible[:limit]]
    lines = ["Config paths to quarantine if you choose Delete:"]
    lines.extend(f"  {path}" for path in shown)
    if len(eligible) > limit:
        lines.append(f"  … and {len(eligible) - limit} more")
    return "\n".join(lines)

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
    if not os.path.lexists(dest):
        return dest
    while True:
        candidate = dest.with_name(f"{dest.name}.{uuid.uuid4().hex[:12]}")
        if not os.path.lexists(candidate):
            return candidate

def quarantine_leftovers(app_path, metadata, leftovers, config):
    if not quarantine_enabled(config):
        return []
    cleanup = config.get("app_cleanup", {})
    leftover_cfg = cleanup.get("leftover_review", {})

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
    max_item_size, max_total_size = _size_limits(leftover_cfg)
    total_size = 0
    for item in leftovers:
        if not item.get("eligible"):
            continue
        src = Path(item["path"])
        if not src.exists() or src.is_symlink():
            continue
        try:
            resolved_src = src.resolve(strict=True)
            rel = resolved_src.relative_to(home)
        except ValueError:
            continue
        except OSError:
            continue
        size, inspection_error = inspect_path(resolved_src)
        if (
            inspection_error
            or size > max_item_size
            or total_size + size > max_total_size
        ):
            continue
        total_size += size
        dest = unique_quarantine_path(quarantine_base, rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(resolved_src), str(dest))
        entry = {
            "action": "quarantined-config",
            "app_path": app_path,
            "bundle_id": metadata.get("bundle_id", ""),
            "original_path": str(src),
            "quarantine_path": str(dest),
            "quarantined_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "size": size,
        }
        try:
            with open(ledger_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, sort_keys=True) + "\n")
        except OSError as error:
            try:
                shutil.move(str(dest), str(resolved_src))
            except OSError as rollback_error:
                raise RuntimeError(
                    f"quarantine ledger write failed; config remains at {dest}: {rollback_error}"
                ) from error
            raise
        entries.append(entry)
    return entries
