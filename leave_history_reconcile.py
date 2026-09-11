#!/usr/bin/env python3
"""Recover process Leave backoff from resource-monitor prompt history."""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
from typing import Any

from idle_config import APP_SUPPORT_DIR, atomic_write_json, parse_keep_entry

DEFAULT_HISTORY_PATH = Path(APP_SUPPORT_DIR) / "resource-monitor-history.jsonl"
DEFAULT_WHITELIST_PATH = Path(APP_SUPPORT_DIR) / "process_whitelist.json"
PROCESS_FAMILY_PREFIX = "process-family:"


def _family_name(value: Any) -> str:
    raw = str(value or "").strip()
    token = raw.split(None, 1)[0] if raw else ""
    return os.path.basename(token).strip()


def _load_whitelist(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as error:
        raise OSError(f"unable to read process whitelist {path}: {error}") from error
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError(f"process whitelist is invalid JSON: {path}") from error
    if not isinstance(value, dict):
        raise ValueError(f"process whitelist must contain a JSON object: {path}")
    return value


def historical_keep_entries(history_path: Path) -> dict[str, dict[str, Any]]:
    """Return lower-bound Leave counts keyed by case-insensitive process family."""
    try:
        lines = history_path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return {}
    except OSError as error:
        raise OSError(f"unable to read resource-monitor history {history_path}: {error}") from error

    entries: dict[str, dict[str, Any]] = {}
    seen: set[tuple[str, str]] = set()
    for raw in lines:
        if not raw.strip():
            continue
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        if str(record.get("event") or "").lower() != "prompted":
            continue
        if str(record.get("action") or "").upper() != "KEEP":
            continue

        family = _family_name(record.get("process"))
        if not family:
            continue
        try:
            timestamp = float(record.get("timestamp"))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(timestamp) or timestamp < 0:
            continue

        normalized = family.casefold()
        incident_id = str(record.get("incident_id") or "").strip()
        if incident_id:
            identity = f"incident:{incident_id}"
        else:
            identity = "row:" + json.dumps(record, sort_keys=True, separators=(",", ":"))
        marker = (normalized, identity)
        if marker in seen:
            continue
        seen.add(marker)

        entry = entries.setdefault(
            normalized,
            {"family": family, "kept_at": timestamp, "keep_count": 0},
        )
        entry["keep_count"] += 1
        if timestamp >= float(entry["kept_at"]):
            entry["kept_at"] = timestamp
            entry["family"] = family
    return entries


def _preferred_existing_family_key(
    whitelist: dict[str, Any],
    normalized_family: str,
) -> str | None:
    for key in whitelist:
        if not isinstance(key, str) or not key.startswith(PROCESS_FAMILY_PREFIX):
            continue
        if key[len(PROCESS_FAMILY_PREFIX):].casefold() == normalized_family:
            return key
    return None


def reconcile_process_leave_history(
    *,
    history_path: Path = DEFAULT_HISTORY_PATH,
    whitelist_path: Path = DEFAULT_WHITELIST_PATH,
) -> dict[str, Any]:
    """Persist missing stable-family Leave counts without reducing newer state."""
    history_path = Path(history_path)
    whitelist_path = Path(whitelist_path)
    historical = historical_keep_entries(history_path)
    if not historical:
        return {
            "ok": True,
            "families": 0,
            "history_keep_events": 0,
            "updated": 0,
            "changed": False,
        }

    whitelist = _load_whitelist(whitelist_path)
    updated = 0
    for normalized, recovered in historical.items():
        key = _preferred_existing_family_key(whitelist, normalized)
        if key is None:
            key = f"{PROCESS_FAMILY_PREFIX}{recovered['family']}"

        existing = parse_keep_entry(whitelist.get(key))
        recovered_count = int(recovered["keep_count"])
        recovered_at = float(recovered["kept_at"])
        should_update = (
            existing is None
            or recovered_count > int(existing["keep_count"])
            or (
                recovered_count == int(existing["keep_count"])
                and recovered_at > float(existing["kept_at"])
            )
        )
        if not should_update:
            continue

        if existing and int(existing["keep_count"]) > recovered_count:
            next_entry = existing
        else:
            next_entry = {
                "kept_at": max(recovered_at, float(existing["kept_at"])) if existing else recovered_at,
                "keep_count": max(recovered_count, int(existing["keep_count"])) if existing else recovered_count,
            }
        whitelist[key] = next_entry
        updated += 1

    if updated and not atomic_write_json(whitelist_path, whitelist):
        raise OSError(f"failed to atomically write process whitelist: {whitelist_path}")

    return {
        "ok": True,
        "families": len(historical),
        "history_keep_events": sum(int(item["keep_count"]) for item in historical.values()),
        "updated": updated,
        "changed": bool(updated),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", type=Path, default=DEFAULT_HISTORY_PATH)
    parser.add_argument("--whitelist", type=Path, default=DEFAULT_WHITELIST_PATH)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        result = reconcile_process_leave_history(
            history_path=args.history,
            whitelist_path=args.whitelist,
        )
    except (OSError, ValueError) as error:
        if args.json:
            print(json.dumps({"ok": False, "error": str(error)}, sort_keys=True))
        else:
            print(f"Leave history reconciliation failed: {error}")
        return 1

    if args.json:
        print(json.dumps(result, sort_keys=True))
    elif result["changed"]:
        print(
            "Recovered "
            f"{result['history_keep_events']} Leave decisions across "
            f"{result['families']} process families; updated {result['updated']} entries."
        )
    else:
        print("Process Leave history is already reconciled.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
