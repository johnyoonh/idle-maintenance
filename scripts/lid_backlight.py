#!/usr/bin/env python3
"""Keep only the built-in backlight dark while the lid is closed.

Does not change sleep policy, activate applications, or control external screens.
DisplayServices is a private macOS API; failures are logged and retried, never
replaced with keyboard simulation or system sleep.
"""
import ctypes
import fcntl
import json
import logging
import math
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import time


class Guard:
    def __init__(self, path, display):
        self.path = path
        self.display = display
        self.saved = json.loads(path.read_text()) if path.exists() else None
        if self.saved is not None:
            if (not isinstance(self.saved, dict)
                    or type(self.saved.get("display")) is not int
                    or type(self.saved.get("brightness")) not in (int, float)
                    or not math.isfinite(self.saved["brightness"])
                    or not 0 <= self.saved["brightness"] <= 1):
                raise ValueError("Invalid saved brightness; preserving state")

    def save(self, value):
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w") as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, self.path)
        self.saved = value

    def tick(self, closed, display_id):
        if closed is None or display_id is None:
            return
        level = self.display.get(display_id)
        if closed:
            if self.saved is None:
                self.save({"display": display_id, "brightness": level})
            if self.saved["display"] != display_id:
                raise ValueError("Built-in display identity changed; preserving state")
            if level != 0:
                self.display.set(display_id, 0)
                logging.info("Lid closed: built-in backlight set to zero")
        elif self.saved is not None:
            if self.saved["display"] != display_id:
                return
            # Respect brightness already restored by macOS or the user.
            if level == 0:
                self.display.set(display_id, self.saved["brightness"])
                logging.info("Lid open: restored built-in brightness")
            self.save(None)


class MacDisplay:
    def __init__(self):
        self.cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        self.ds = ctypes.CDLL("/System/Library/PrivateFrameworks/DisplayServices.framework/DisplayServices")
        self.cg.CGGetOnlineDisplayList.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32)]
        self.cg.CGDisplayIsBuiltin.argtypes = [ctypes.c_uint32]
        self.ds.DisplayServicesGetBrightness.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_float)]
        self.ds.DisplayServicesSetBrightness.argtypes = [ctypes.c_uint32, ctypes.c_float]

    def builtin(self):
        displays = (ctypes.c_uint32 * 32)()
        count = ctypes.c_uint32()
        if self.cg.CGGetOnlineDisplayList(32, displays, ctypes.byref(count)) != 0:
            raise OSError("Cannot enumerate displays")
        return next((d for d in displays[:count.value] if self.cg.CGDisplayIsBuiltin(d)), None)

    def get(self, display):
        level = ctypes.c_float()
        result = self.ds.DisplayServicesGetBrightness(display, ctypes.byref(level))
        if result != 0 or not math.isfinite(level.value) or not 0 <= level.value <= 1:
            raise OSError("Cannot read built-in brightness")
        return level.value

    def set(self, display, level):
        if self.ds.DisplayServicesSetBrightness(display, level) != 0:
            raise OSError("Cannot set built-in brightness")
        if abs(self.get(display) - level) > 0.01:
            raise OSError("Built-in brightness readback did not match")


def lid_closed():
    result = subprocess.run(
        ["/usr/sbin/ioreg", "-a", "-r", "-c", "IOPMrootDomain", "-d", "1"],
        check=True, capture_output=True, timeout=5,
    )
    roots = plistlib.loads(result.stdout)
    state = roots[0].get("AppleClamshellState") if roots else None
    return state if type(state) is bool else None


def main():
    os.umask(0o077)
    display = MacDisplay()
    if sys.argv[1:] == ["--status"]:
        identity = display.builtin()
        print(json.dumps({"lid_closed": lid_closed(), "builtin_display": identity,
                          "brightness": display.get(identity) if identity is not None else None}))
        return
    if sys.argv[1:]:
        raise SystemExit("Usage: lid_backlight.py [--status]")
    root = Path.home() / "Library/Application Support/idle-maintenance/lid-backlight"
    root.mkdir(parents=True, exist_ok=True)
    with (root / "watcher.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Lid backlight watcher already running")
        guard = Guard(root / "brightness.json", display)
        last_error = None
        logging.info("Lid backlight watcher started")
        while True:
            try:
                guard.tick(lid_closed(), display.builtin())
                last_error = None
            except Exception as error:
                # Avoid an unbounded log stream during a persistent API failure.
                message = str(error)
                if message != last_error:
                    logging.error("Lid backlight check failed: %s", message)
                last_error = message
            time.sleep(2)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    main()
