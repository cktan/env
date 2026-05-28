#!/bin/python3
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

SERVICE_NAME = "sprite-idle-killer"
SCRIPT_PATH = str(Path("~/.local/bin/sprite_idle_killer.py").expanduser())
LOG_PATH = str(Path("~/.local/share/idle_killer.log").expanduser())
LOG_MAX_LINES = 500
CPU_THRESHOLD = 5.0
SLEEP_INTERVAL = 300
SIGTERM_WAIT = 5


def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    entry = f"{ts} {msg}\n"
    path = Path(LOG_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(entry)
    lines = path.read_text().splitlines(keepends=True)
    if len(lines) > LOG_MAX_LINES:
        path.write_text("".join(lines[-LOG_MAX_LINES:]))


def cpu_usage():
    def read():
        with open("/proc/stat") as f:
            parts = f.readline().split()
        vals = [int(x) for x in parts[1:]]
        return sum(vals), vals[3]  # total, idle

    t1, i1 = read()
    time.sleep(1)
    t2, i2 = read()
    delta = t2 - t1
    return 0.0 if delta == 0 else (1 - (i2 - i1) / delta) * 100


def active_connections():
    """Return True if any PID >= 10 has an established network connection."""
    # Collect socket inodes for ESTABLISHED connections (state 01) from tcp/tcp6
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


def list_services():
    r = subprocess.run(["sprite-env", "services", "list"], capture_output=True, text=True)
    try:
        return json.loads(r.stdout)
    except Exception:
        return []


def is_registered():
    for svc in list_services():
        name = svc.get("name") if isinstance(svc, dict) else svc
        if name == SERVICE_NAME:
            return True
    return False


def register():
    subprocess.run([
        "sprite-env", "services", "create", SERVICE_NAME,
        "--cmd", "/bin/python3",
        "--args", SCRIPT_PATH,
        "--no-stream",
    ], check=True)


def stop_services():
    stopped = []
    for svc in list_services():
        name = svc.get("name") if isinstance(svc, dict) else svc
        if name and name != SERVICE_NAME:
            subprocess.run(["sprite-env", "services", "stop", name], capture_output=True)
            stopped.append(name)
    return stopped


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
                stopped = stop_services()
                if stopped:
                    log(f"stopped services: {', '.join(stopped)}")
                n = kill_processes()
                log(f"killed {n} processes")
        except Exception as e:
            log(f"error: {e}")
        time.sleep(SLEEP_INTERVAL)


if __name__ == "__main__":
    if "-h" in sys.argv or "--help" in sys.argv:
        print("""sprite_idle_killer.py — kills idle processes on this Sprite machine

Runs every 5 minutes as a sprite-env service. On each cycle, if the machine
is idle it stops all sprite-env services, then kills all PIDs >= 10.

ACTIVITY DETECTION (any of these = active, skip kill):
  - CPU usage > 5%
  - Any process has an established network connection

SKIP FILE:
  touch /tmp/sprite-idle-killer-skip   suspend killing indefinitely
  rm /tmp/sprite-idle-killer-skip      resume normal operation
  (file is in /tmp so it clears on reboot)

LOG: ~/.local/share/idle_killer.log

FIRST RUN:
  python3 ~/.local/bin/sprite_idle_killer.py
  Registers itself as the 'sprite-idle-killer' service and exits.
  Subsequent runs (via the service) enter the monitoring loop.""")
        sys.exit(0)

    if not is_registered():
        log("first run — registering service")
        register()
        log("registered, exiting (service takes over)")
        sys.exit(0)
    main_loop()
