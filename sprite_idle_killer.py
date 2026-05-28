#!/bin/python3
import os
import signal
import sys
import time
from pathlib import Path

LOG_PATH = str(Path("~/.local/share/idle_killer.log").expanduser())
LOG_MAX_LINES = 500
CPU_THRESHOLD = 5.0
SLEEP_INTERVAL = 300
SIGTERM_WAIT = 5


def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    path = Path(LOG_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(f"{ts} {msg}\n")
    lines = path.read_text().splitlines(keepends=True)
    if len(lines) > LOG_MAX_LINES:
        path.write_text("".join(lines[-LOG_MAX_LINES:]))


def kill_existing_instances():
    my_pid = os.getpid()
    script_name = Path(__file__).name
    killed = []
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == my_pid:
            continue
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace")
            if script_name in cmdline:
                os.kill(pid, signal.SIGTERM)
                killed.append(pid)
        except (FileNotFoundError, PermissionError):
            pass
    if killed:
        time.sleep(2)
        for pid in killed:
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    return killed


def cpu_usage():
    def read():
        with open("/proc/stat") as f:
            parts = f.readline().split()
        vals = [int(x) for x in parts[1:]]
        return sum(vals), vals[3]

    t1, i1 = read()
    time.sleep(1)
    t2, i2 = read()
    delta = t2 - t1
    return 0.0 if delta == 0 else (1 - (i2 - i1) / delta) * 100


def active_connections():
    """Return True if any PID >= 10 (other than self) has an established TCP connection."""
    established_inodes = set()
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(path) as f:
                for line in f.readlines()[1:]:
                    parts = line.split()
                    if len(parts) > 9 and parts[3] == "01":
                        established_inodes.add(parts[9])
        except FileNotFoundError:
            pass

    if not established_inodes:
        return False

    my_pid = os.getpid()
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid < 10 or pid == my_pid:
            continue
        try:
            for fd in os.scandir(f"/proc/{pid}/fd"):
                try:
                    target = os.readlink(fd.path)
                    if target.startswith("socket:[") and target[8:-1] in established_inodes:
                        return True
                except (FileNotFoundError, PermissionError):
                    pass
        except (FileNotFoundError, PermissionError):
            pass
    return False


def killable_pids():
    my_pid = os.getpid()
    pids = []
    for entry in os.scandir("/proc"):
        if entry.name.isdigit():
            pid = int(entry.name)
            if pid >= 10 and pid != my_pid:
                pids.append(pid)
    return pids


def kill_processes():
    targets = killable_pids()
    for pid in targets:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    time.sleep(SIGTERM_WAIT)
    for pid in killable_pids():
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    return len(targets)


def main_loop():
    log("started")
    while True:
        try:
            if Path("/tmp/sprite-idle-killer-skip").exists():
                log("skip file present, sleeping")
                time.sleep(SLEEP_INTERVAL)
                continue
            cpu = cpu_usage()
            net = active_connections()
            if cpu > CPU_THRESHOLD or net:
                log(f"active (CPU {cpu:.1f}%, net={net}), sleeping")
            else:
                log(f"idle (CPU {cpu:.1f}%, net={net}) — acting")
                n = kill_processes()
                log(f"killed {n} processes")
        except Exception as e:
            log(f"error: {e}")
        time.sleep(SLEEP_INTERVAL)


if __name__ == "__main__":
    if "-h" in sys.argv or "--help" in sys.argv:
        print("""sprite_idle_killer.py — kills idle processes on this Sprite machine

Runs every 5 minutes in the background (started from ~/.profile).
On startup, any previous instance is killed before the new one takes over.

ACTIVITY DETECTION (any of these = active, skip kill):
  - CPU usage > 5%
  - Any process has an established network connection

If idle, kills all PIDs >= 10 except itself.

SKIP FILE:
  touch /tmp/sprite-idle-killer-skip   suspend killing indefinitely
  rm /tmp/sprite-idle-killer-skip      resume normal operation
  (file is in /tmp so it clears on reboot)

LOG: ~/.local/share/idle_killer.log

See also: INVESTIGATE.md in ~/p/env/ for idle detection rationale.""")
        sys.exit(0)

    killed = kill_existing_instances()
    if killed:
        log(f"killed previous instance(s): {killed}")
    main_loop()
