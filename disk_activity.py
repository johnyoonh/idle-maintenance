#!/usr/bin/env python3
"""Exit 75 while aggregate disk activity is above the configured threshold."""
import argparse, json
from idle_config import disk_busy_status, load_config

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv); status = disk_busy_status(load_config())
    if args.json: print(json.dumps(status, indent=2, sort_keys=True))
    elif status.get("available"): print(f"disk activity: {status['mib_per_second']:.1f} MiB/s; threshold: {status['threshold_mib_per_second']:.1f} MiB/s")
    else: print(f"disk activity unavailable: {status.get('error', 'unknown error')}")
    if not status.get("available"): return 2
    return 75 if status.get("busy") else 0
if __name__ == "__main__": raise SystemExit(main())
