"""macOS process identity and cumulative disk-I/O counters."""
from __future__ import annotations
import ctypes, hashlib, os, shlex, subprocess, sys

class RUsageInfoV2(ctypes.Structure):
    _fields_ = [
        ("uuid", ctypes.c_uint8 * 16),
        ("user", ctypes.c_uint64), ("system", ctypes.c_uint64),
        ("pkg_idle", ctypes.c_uint64), ("interrupt", ctypes.c_uint64),
        ("pageins", ctypes.c_uint64), ("wired", ctypes.c_uint64),
        ("resident", ctypes.c_uint64), ("phys", ctypes.c_uint64),
        ("start", ctypes.c_uint64), ("exit", ctypes.c_uint64),
        ("child_user", ctypes.c_uint64), ("child_system", ctypes.c_uint64),
        ("child_pkg", ctypes.c_uint64), ("child_interrupt", ctypes.c_uint64),
        ("child_pageins", ctypes.c_uint64), ("child_elapsed", ctypes.c_uint64),
        ("read", ctypes.c_uint64), ("written", ctypes.c_uint64),
    ]

_LIBPROC = None

def io_counters(pid):
    global _LIBPROC
    if sys.platform != "darwin": return None
    try:
        if _LIBPROC is None:
            _LIBPROC = ctypes.CDLL("libproc.dylib", use_errno=True)
            _LIBPROC.proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.POINTER(RUsageInfoV2)]
            _LIBPROC.proc_pid_rusage.restype = ctypes.c_int
        info = RUsageInfoV2()
        if _LIBPROC.proc_pid_rusage(int(pid), 2, ctypes.byref(info)): return None
        return {"io_read_bytes": int(info.read), "io_write_bytes": int(info.written), "start_abstime": int(info.start)}
    except (OSError, AttributeError, TypeError, ValueError):
        return None

def etime_seconds(value):
    value = str(value).strip(); days = 0
    if "-" in value:
        day, value = value.split("-", 1); days = int(day)
    parts = [int(x) for x in value.split(":")]
    if len(parts) == 3: hours, minutes, seconds = parts
    elif len(parts) == 2: hours, (minutes, seconds) = 0, parts
    else: return 0
    return days * 86400 + hours * 3600 + minutes * 60 + seconds

def normalize(command):
    try: return " ".join(shlex.split(command or ""))
    except ValueError: return " ".join(str(command or "").split())

def fingerprint(command):
    return hashlib.sha256(normalize(command).encode("utf-8", "replace")).hexdigest()

def key(proc):
    name = os.path.basename(proc.get("comm") or "process")
    return f"{name}:{proc.get('fingerprint', fingerprint(proc.get('command', '')))[:20]}"

def parse_line(line):
    parts = line.strip().split(None, 10)
    if len(parts) < 11: return None
    pid, ppid, uid, cpu, etime = parts[:5]
    started = " ".join(parts[5:10]); command = parts[10]
    try:
        command_parts = shlex.split(command)
    except ValueError:
        command_parts = command.split()
    comm = command_parts[0] if command_parts else command
    try:
        proc = {"pid": int(pid), "ppid": int(ppid), "uid": int(uid), "user": str(uid),
                "cpu": float(cpu), "etime": etime, "elapsed_seconds": etime_seconds(etime),
                "start_time": started, "comm": comm, "command": command,
                "fingerprint": fingerprint(command)}
    except ValueError: return None
    proc["process_key"] = key(proc)
    return proc

def snapshot(config, counter_provider=io_counters, output=None):
    if output is None:
        try:
            output = subprocess.check_output(["ps", "-Ar", "-o", "pid=,ppid=,uid=,%cpu=,etime=,lstart=,command="], text=True).splitlines()
        except (OSError, subprocess.CalledProcessError): return {}
    ignored = {str(x) for x in config.get("process_ignore_commands", [])}
    internal = {"maintenance_interactive.py", "maintenance_core.py", "storage_cleanup.py", "disk_activity.py"}
    result = {}
    for line in output:
        proc = parse_line(line)
        if not proc or proc["uid"] != os.getuid() or proc["pid"] == os.getpid(): continue
        base = os.path.basename(proc["comm"])
        if proc["comm"] in ignored or base in ignored or base in internal: continue
        if config.get("process_io_enabled", True):
            counters = counter_provider(proc["pid"])
            if counters: proc.update(counters)
        result[proc["pid"]] = proc
    return result

def same(left, right):
    if not left or not right or left.get("uid") != right.get("uid"): return False
    if left.get("fingerprint") != right.get("fingerprint"): return False
    a, b = left.get("start_abstime"), right.get("start_abstime")
    return a == b if a is not None and b is not None else left.get("start_time") == right.get("start_time")

def read(pid):
    try:
        lines = subprocess.check_output(["ps", "-p", str(pid), "-o", "pid=,ppid=,uid=,%cpu=,etime=,lstart=,command="], text=True).splitlines()
    except (OSError, subprocess.CalledProcessError): return None
    proc = parse_line(lines[0]) if lines else None
    counters = io_counters(pid) if proc else None
    if counters: proc.update(counters)
    return proc
