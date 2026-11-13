"""Build a high-signal, cooldown-gated recommendations digest for idle-maintenance."""

from __future__ import annotations

import datetime as dt
import json
import re
import time
from pathlib import Path
from typing import Any

DEFAULT_COOLDOWN_SECONDS = 7 * 86400


def normalize_path(text: str, home: Path | None = None) -> str:
    h = str(home or Path.home())
    wiki_icloud = f"{h}/Library/Mobile Documents/iCloud~md~obsidian/Documents/wiki"
    out = text.replace(wiki_icloud, "🧠").replace(f"{h}/wiki", "🧠")
    out = out.replace(f"{h}/Library/CloudStorage/Dropbox", "📤").replace(f"{h}/Dropbox", "📤")
    out = out.replace(f"{h}/Library/CloudStorage/OneDrive-Personal", "🌍")
    out = re.sub(rf"{re.escape(h)}/Library/CloudStorage/GoogleDrive-[^/\s\"']+", "🗂️", out)
    return out.replace(h, "~")


def build_recommendations(
    audit_data: dict[str, Any] | None,
    stale_apps: list[str] | None = None,
    *,
    home: Path | None = None,
) -> list[dict[str, str]]:
    recs: list[dict[str, str]] = []
    data = audit_data or {}

    la = data.get("launchagents") or {}
    for item in la.get("failing_loops") or []:
        lbl = str(item.get("label") or "")
        if not lbl:
            continue
        recs.append(
            {
                "key": f"launchagent-loop:{lbl}",
                "severity": "high",
                "title": f"Fix or disable crash-looping LaunchAgent `{lbl}`",
                "detail": f"State `{item.get('state', 'failed')}` on schedule `{item.get('schedule', '')}`.",
                "action": "Open launchagents-state-diagram.html -> Diagnose or Toggle OFF.",
            }
        )

    for lf in data.get("oversized_logs") or []:
        mb = float(lf.get("mb") or 0)
        path = normalize_path(str(lf.get("path") or ""), home=home)
        if mb >= 25.0 and path:
            recs.append(
                {
                    "key": f"log-bloat:{path}",
                    "severity": "medium",
                    "title": f"Trim oversized log `{path}` ({mb:.1f} MiB)",
                    "detail": "Large log increases disk writes and Spotlight/backup churn.",
                    "action": "Run `99_meta/scripts/prune_wiki_automation_logs.sh`.",
                }
            )

    swap_used = float((data.get("swap") or {}).get("swap_used_mb") or 0)
    if swap_used >= 1024.0:
        top_rss = (data.get("processes") or {}).get("top_rss") or []
        top_names = ", ".join(f"`{p.get('name')}` ({p.get('rss_mb')} MB)" for p in top_rss[:3] if p.get("name"))
        recs.append(
            {
                "key": "swap-pressure:high",
                "severity": "medium",
                "title": f"Reduce swap I/O ({swap_used:.0f} MB swap used)",
                "detail": f"Top memory consumers: {top_names or 'see system-io-report.md'}.",
                "action": "Sleep/close idle browser tabs or restart heavy Electron helpers.",
            }
        )

    if stale_apps:
        shown = [normalize_path(p, home=home) for p in stale_apps[:5]]
        recs.append(
            {
                "key": f"stale-apps:{len(stale_apps)}",
                "severity": "low",
                "title": f"Review {len(stale_apps)} unused app(s) (>90d idle)",
                "detail": "Candidates: " + ", ".join(f"`{s}`" for s in shown),
                "action": "Review in next capped interactive session or uninstall unused bundles.",
            }
        )
    return recs


def filter_active_recommendations(
    recs: list[dict[str, str]],
    state: dict[str, Any],
    *,
    now: float | None = None,
    cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    ts = time.time() if now is None else float(now)
    last_seen = dict(state.get("last_seen") or {})
    fresh: list[dict[str, str]] = []
    for rec in recs:
        key = rec["key"]
        prev = float(last_seen.get(key, 0) or 0)
        if key not in last_seen or ts - prev >= cooldown_seconds:
            fresh.append(rec)
            last_seen[key] = ts
    return fresh, {"last_seen": last_seen, "updated_at": ts}


def render_recommendations_markdown(recs: list[dict[str, str]]) -> str:
    now_str = dt.datetime.now(dt.timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M %Z")
    lines = [
        "# Idle Maintenance — Action-Worthy Recommendations",
        "",
        f"- **Updated**: `{now_str}`",
        "",
    ]
    if not recs:
        lines.append("- No high-signal recommendations right now.")
        lines.append("")
        return "\n".join(lines)
    for rec in recs:
        lines.append(f"- **[{rec['severity'].upper()}] {rec['title']}**")
        lines.append(f"  - Why: {rec['detail']}")
        lines.append(f"  - Suggested action: {rec['action']}")
    lines.append("")
    return "\n".join(lines)


def sync_recommendations(
    *,
    audit_state_path: Path | None = None,
    rec_state_path: Path | None = None,
    out_path: Path | None = None,
    stale_apps: list[str] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    h = Path.home()
    audit_path = audit_state_path or (h / ".local/state/idle-maintenance/system_io_audit.json")
    state_path = rec_state_path or (h / "Library/Application Support/idle-maintenance/recommendation_state.json")
    target = out_path or (h / "Library/Mobile Documents/iCloud~md~obsidian/Documents/wiki/99_meta/system/idle-recommendations.md")

    audit_data: dict[str, Any] = {}
    if audit_path.is_file():
        try:
            audit_data = json.loads(audit_path.read_text(encoding="utf-8"))
        except Exception:
            audit_data = {}

    prev_state: dict[str, Any] = {}
    if state_path.is_file():
        try:
            prev_state = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            prev_state = {}

    recs = build_recommendations(audit_data, stale_apps=stale_apps, home=h)
    fresh, next_state = filter_active_recommendations(recs, prev_state, now=now)

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_recommendations_markdown(recs), encoding="utf-8")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(next_state, indent=2), encoding="utf-8")
    return {"total": len(recs), "newly_surfaced": len(fresh), "out_path": str(target)}
