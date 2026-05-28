#!/usr/bin/env python3
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

LOG_PATH = "/tmp/sprite-idle-killer.log"
LOG_MAX_LINES = 500
LOAD_THRESHOLD = 0.1   # all three load averages must be <= this to be idle
SLEEP_INTERVAL = 300   # seconds between idle checks
SIGTERM_WAIT = 5
BASH_RECENT_SECS = 3600

VERBOSE = False


def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    path = Path(LOG_PATH)
    with open(path, "a") as f:
        f.write(f"{ts} {msg}\n")
    lines = path.read_text().splitlines(keepends=True)
    if len(lines) > LOG_MAX_LINES:
        path.write_text("".join(lines[-LOG_MAX_LINES:]))


def vlog(msg):
    if VERBOSE:
        log(f"[v] {msg}")


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


def load_avgs():
    """Return (1-min, 5-min, 15-min) load averages."""
    parts = open("/proc/loadavg").read().split()
    return float(parts[0]), float(parts[1]), float(parts[2])


def recent_bash_process():
    """Return (pid, age_secs) of the youngest bash started < 1hr ago, or None."""
    clk_tck = os.sysconf("SC_CLK_TCK")
    boot_time = None
    with open("/proc/stat") as f:
        for line in f:
            if line.startswith("btime"):
                boot_time = int(line.split()[1])
                break
    if boot_time is None:
        return None

    now = time.time()
    my_pid = os.getpid()
    youngest = None

    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == my_pid:
            continue
        try:
            comm = Path(f"/proc/{pid}/comm").read_text().strip()
            if comm != "bash":
                continue
            stat = Path(f"/proc/{pid}/stat").read_text()
            # starttime is field 22; strip "(comm) " to get remaining fields
            after_comm = stat[stat.rfind(")") + 2:]
            fields = after_comm.split()
            start_ticks = int(fields[19])  # field 22 overall = index 19 after state
            age = now - (boot_time + start_ticks / clk_tck)
            if age < BASH_RECENT_SECS:
                vlog(f"bash pid {pid} age {age:.0f}s — recent")
                if youngest is None or age < youngest[1]:
                    youngest = (pid, age)
            else:
                vlog(f"bash pid {pid} age {age:.0f}s — old, ignoring")
        except (FileNotFoundError, PermissionError, ValueError, IndexError):
            pass

    return youngest


def stop_services():
    try:
        result = subprocess.run(
            ["systemctl", "list-units", "--type=service", "--state=running",
             "--no-legend", "--no-pager"],
            capture_output=True, text=True,
        )
        services = [line.split()[0] for line in result.stdout.splitlines() if line.strip()]
        for svc in services:
            subprocess.run(["systemctl", "stop", svc], capture_output=True, timeout=10)
        return services
    except Exception as e:
        log(f"stop_services error: {e}")
        return []


def kill_processes():
    my_pid = os.getpid()

    def killable():
        return [
            int(e.name)
            for e in os.scandir("/proc")
            if e.name.isdigit() and int(e.name) >= 10 and int(e.name) != my_pid
        ]

    targets = killable()
    for pid in targets:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    time.sleep(SIGTERM_WAIT)
    for pid in killable():
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    return len(targets)


def survivors():
    my_pid = os.getpid()
    return [
        int(e.name)
        for e in os.scandir("/proc")
        if e.name.isdigit() and int(e.name) >= 10 and int(e.name) != my_pid
    ]


def main_loop():
    log("started in -v mode" if VERBOSE else "started")
    while True:
        active_reasons = []

        if Path("/tmp/sprite-idle-killer-skip").exists():
            log("skip file present — skipping idle check")
            active_reasons.append("skip file present")

        l1, l5, l15 = load_avgs()
        vlog(f"load averages: {l1:.2f} {l5:.2f} {l15:.2f}")
        for label, val in (("1m", l1), ("5m", l5), ("15m", l15)):
            if val > LOAD_THRESHOLD:
                active_reasons.append(f"load({label}) {val:.2f} > {LOAD_THRESHOLD}")
        if not any(v > LOAD_THRESHOLD for v in (l1, l5, l15)):
            log(f"idle check: load {l1:.2f} {l5:.2f} {l15:.2f} all <= {LOAD_THRESHOLD}")

        bash = recent_bash_process()
        if bash:
            pid, age = bash
            active_reasons.append(f"bash pid {pid} started {age:.0f}s ago (< {BASH_RECENT_SECS}s)")
        else:
            log("idle check: no recent bash process")

        if active_reasons:
            log(f"not idle: {'; '.join(active_reasons)}")
            time.sleep(SLEEP_INTERVAL)
            continue

        log("system is idle — going down")
        services = stop_services()
        if services:
            log(f"stopped services: {', '.join(services)}")
        n = kill_processes()
        log(f"killed {n} processes")
        still_running = survivors()
        if still_running:
            log(f"WARNING: survivors after kill: {still_running}")
        else:
            log("verified: no survivors")
        sys.exit(0)


if __name__ == "__main__":
    if "-h" in sys.argv or "--help" in sys.argv:
        print("""sprite_idle_killer.py — kills idle processes on this Sprite machine

Checks every 5 minutes. On startup, kills any previous instance.

IDLE when ALL of the following are true:
  - No skip file at /tmp/sprite-idle-killer-skip
  - All three load averages (1m, 5m, 15m) <= 0.1
  - No bash process started less than 1 hour ago

If not idle: wait for next check cycle.
If idle: stop services, kill all PIDs >= 10 (except self), verify, exit 0.

LOG: /tmp/sprite-idle-killer.log""")
        sys.exit(0)

    if "-v" in sys.argv or "--verbose" in sys.argv:
        VERBOSE = True

    killed = kill_existing_instances()
    if killed:
        log(f"killed previous instance(s): {killed}")
    main_loop()
