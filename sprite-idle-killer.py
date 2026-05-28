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
LOG_PATH = str(Path("~/.local/share/sprite_idle_killer.log").expanduser())
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
            cpu = cpu_usage()
            if cpu > CPU_THRESHOLD:
                log(f"active (CPU {cpu:.1f}%), sleeping")
            else:
                log(f"idle (CPU {cpu:.1f}%) — acting")
                stopped = stop_services()
                if stopped:
                    log(f"stopped services: {', '.join(stopped)}")
                n = kill_processes()
                log(f"killed {n} processes")
        except Exception as e:
            log(f"error: {e}")
        time.sleep(SLEEP_INTERVAL)


if __name__ == "__main__":
    if not is_registered():
        log("first run — registering service")
        register()
        log("registered, exiting (service takes over)")
        sys.exit(0)
    main_loop()
