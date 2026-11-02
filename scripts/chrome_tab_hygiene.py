#!/usr/bin/env python3
"""Prune orphaned and duplicate root browser tabs left by background automation.

Preserves:
- All conversation threads (https://chatgpt.com/c/<id> or /g/<id>).
- All non-ChatGPT URLs (user browsing, docs, GitHub, etc.).
- Exactly one root ChatGPT composer tab if present.

Closes:
- Stale about:blank tabs detached by automation leases.
- Redundant duplicate root https://chatgpt.com/ composer tabs.

Executes headlessly via macOS JXA without activating windows or stealing focus.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

ROOT_CHATGPT_RE = re.compile(r"^https?://(www\.)?(chatgpt\.com|chat\.openai\.com)/?(\?.*)?$")
CONVERSATION_RE = re.compile(r"^https?://(www\.)?(chatgpt\.com|chat\.openai\.com)/(c|g)/")

JXA_SCRIPT = """
(() => {
  const chrome = Application("Google Chrome");
  if (!chrome.running()) return JSON.stringify({ running: false, closedCount: 0, closed: [] });

  const dryRun = __DRY_RUN__;
  let closed = [];
  let kept = [];
  let keepWindowIdx = -1;
  let keepTabIdx = -1;

  const windows = chrome.windows();

  // Pass 1: find the first root ChatGPT tab to keep
  for (let wIdx = 0; wIdx < windows.length; wIdx++) {
    const tabs = windows[wIdx].tabs();
    for (let tIdx = 0; tIdx < tabs.length; tIdx++) {
      const url = tabs[tIdx].url() || "";
      if (/^https?:\\/\\/(www\\.)?(chatgpt\\.com|chat\\.openai\\.com)\\/?(\\?.*)?$/.test(url) &&
          !url.includes("/c/") && !url.includes("/g/")) {
        keepWindowIdx = wIdx;
        keepTabIdx = tIdx;
        break;
      }
    }
    if (keepWindowIdx !== -1) break;
  }

  // Pass 2: iterate backwards per window to safely close
  for (let wIdx = 0; wIdx < windows.length; wIdx++) {
    const w = windows[wIdx];
    const tabs = w.tabs();
    for (let tIdx = tabs.length - 1; tIdx >= 0; tIdx--) {
      const tab = tabs[tIdx];
      const url = tab.url() || "";
      const title = tab.title() || "";

      if (url === "about:blank" || url === "") {
        closed.push({ window: wIdx, title, url, reason: "blank" });
        if (!dryRun) {
          try { chrome.close(tab); } catch (e) {}
        }
      } else if (/^https?:\\/\\/(www\\.)?(chatgpt\\.com|chat\\.openai\\.com)\\/?(\\?.*)?$/.test(url) &&
                 !url.includes("/c/") && !url.includes("/g/")) {
        if (wIdx === keepWindowIdx && tIdx === keepTabIdx) {
          kept.push({ window: wIdx, title, url, reason: "first-root" });
        } else {
          closed.push({ window: wIdx, title, url, reason: "duplicate-root" });
          if (!dryRun) {
            try { chrome.close(tab); } catch (e) {}
          }
        }
      } else {
        kept.push({ window: wIdx, title, url, reason: "conversation-or-user" });
      }
    }
  }

  return JSON.stringify({
    running: true,
    dryRun: dryRun,
    closedCount: closed.length,
    keptCount: kept.length,
    closed: closed,
    kept: kept
  });
})()
"""


def classify_tab(url: str) -> str:
    """Classify a tab URL for hygiene review."""
    if not url or url.strip() == "" or url.strip() == "about:blank":
        return "blank"
    stripped = url.strip()
    if CONVERSATION_RE.match(stripped):
        return "conversation"
    if ROOT_CHATGPT_RE.match(stripped):
        return "root_chatgpt"
    return "user_page"


def plan_cleanup(windows_data: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Determine tabs to keep versus tabs to close from window inventory."""
    to_keep: list[dict[str, Any]] = []
    to_close: list[dict[str, Any]] = []
    root_kept = False

    for w_idx, win in enumerate(windows_data):
        tabs = win.get("tabs", [])
        for t_idx, tab in enumerate(tabs):
            url = tab.get("url", "")
            title = tab.get("title", "")
            cat = classify_tab(url)
            entry = {"window": w_idx, "tab": t_idx, "url": url, "title": title, "category": cat}
            if cat == "blank":
                to_close.append({**entry, "reason": "blank"})
            elif cat == "root_chatgpt":
                if not root_kept:
                    root_kept = True
                    to_keep.append({**entry, "reason": "first_root_chatgpt"})
                else:
                    to_close.append({**entry, "reason": "duplicate_root_chatgpt"})
            else:
                to_keep.append({**entry, "reason": "preserved_tab"})

    return to_keep, to_close


def run_hygiene(dry_run: bool = False) -> dict[str, Any]:
    """Execute tab hygiene via macOS JXA."""
    script = JXA_SCRIPT.replace("__DRY_RUN__", "true" if dry_run else "false")
    result = subprocess.run(
        ["/usr/bin/osascript", "-l", "JavaScript", "-e", script],
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    raw = result.stdout.strip()
    if not raw:
        return {"running": False, "closedCount": 0, "closed": []}
    return json.loads(raw)


def main() -> None:
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    status_only = "--status" in args

    log_dir = Path.home() / "Library/Logs/idle-maintenance"
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=str(log_dir / "chrome-tab-hygiene.log"),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    try:
        report = run_hygiene(dry_run=dry_run or status_only)
        if not report.get("running"):
            logging.debug("Chrome is not running; hygiene skipped.")
            if status_only or dry_run:
                print(json.dumps(report, indent=2))
            return

        closed_count = report.get("closedCount", 0)
        kept_count = report.get("keptCount", 0)
        mode_label = "DRY-RUN " if (dry_run or status_only) else ""

        if closed_count > 0:
            logging.info(
                "%sPruned %d orphaned/duplicate tab(s); %d tab(s) preserved. Closed: %s",
                mode_label,
                closed_count,
                kept_count,
                [c.get("url") for c in report.get("closed", [])],
            )
        else:
            logging.debug("%sTab hygiene checked; all %d tab(s) healthy.", mode_label, kept_count)

        if dry_run or status_only:
            print(json.dumps(report, indent=2))
        elif closed_count > 0:
            print(f"Closed {closed_count} orphaned tab(s); {kept_count} preserved.")
    except Exception as exc:
        logging.error("Chrome tab hygiene failed: %s", exc)
        print(f"Error running tab hygiene: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
