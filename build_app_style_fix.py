#!/usr/bin/env python3
"""Apply guarded post-overlay style corrections to generated Idle Maintenance Swift."""
from __future__ import annotations

import argparse
from pathlib import Path

OLD_STATUS_COLOR_ORDER = '''                if normalized.contains("healthy") || normalized.contains("available now") || normalized.contains("running") {
                    attributes[.foregroundColor] = NSColor.systemGreen
                } else if normalized.contains("stale") || normalized.contains("degraded") || normalized.contains("needs attention") || normalized.contains("unavailable") {
                    attributes[.foregroundColor] = NSColor.systemOrange
                }'''

NEW_STATUS_COLOR_ORDER = '''                if normalized.contains("stale") || normalized.contains("degraded") || normalized.contains("needs attention") || normalized.contains("unavailable") {
                    attributes[.foregroundColor] = NSColor.systemOrange
                } else if normalized.contains("healthy") || normalized.contains("available now") || normalized.contains("running") {
                    attributes[.foregroundColor] = NSColor.systemGreen
                }'''


def apply_style_fix(source: str) -> str:
    count = source.count(OLD_STATUS_COLOR_ORDER)
    if count != 1:
        raise ValueError(f"expected exactly one status color marker, found {count}")
    return source.replace(OLD_STATUS_COLOR_ORDER, NEW_STATUS_COLOR_ORDER, 1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args(argv)
    source = args.source.read_text(encoding="utf-8")
    args.destination.write_text(apply_style_fix(source), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
