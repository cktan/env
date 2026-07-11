#!/usr/bin/env python3
"""sprite_idle_killer.py — watchdog that shuts down an idle Sprite VM.

Runs forever in a loop, checking every SLEEP_INTERVAL seconds whether the
machine looks idle. When it decides the machine is idle (or a bash shell has
been open unreasonably long), it stops all services and kills every process
on the box, then exits. It is meant to be started once (e.g. from a systemd
unit or session bootstrap) and left running in the background.

Step by step, what happens when this script is executed:

1. Parse `-h`/`--help` or `-v`/`--verbose` from argv.
2. `kill_existing_instances()` — scan /proc for any other process whose
   cmdline mentions this script's filename and kill it (SIGTERM, then
   SIGKILL after a grace period). This guarantees only one watchdog runs
   at a time, even if the script gets started twice.
3. Sleep 30s to let the system settle right after startup before the first
   idle check.
4. Enter `main_loop()`, which repeats forever:
   a. `old_bash_process()` — if any bash process has been alive for
      BASH_OLD_SECS (10h) or more, treat that as an unconditional
      "shut down now" trigger, regardless of load or other activity.
   b. Otherwise check idleness. The machine is considered NOT idle (and the
      loop just sleeps and retries) if any of these hold:
        - a skip file exists at /tmp/sprite-idle-killer-skip
        - any of the 1m/5m/15m load averages exceeds LOAD_THRESHOLD
        - a bash process was started less than BASH_RECENT_SECS (30m) ago
   c. If none of those apply, the system is idle: log a `ps -ef` snapshot,
      stop all running systemd services, SIGTERM/SIGKILL every remaining
      process (`kill_processes()`), confirm nothing survived
      (`survivors()`), and exit(0).

All decisions and actions are appended to LOG_PATH so the shutdown reason
can be reconstructed after the fact.
"""
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

LOG_PATH = "/tmp/sprite-idle-killer.log"
LOG_MAX_LINES = 500
LOAD_THRESHOLD = 0.03   # all three load averages must be <= this to be idle
SLEEP_INTERVAL = 300   # seconds between idle checks
SIGTERM_WAIT = 5
BASH_RECENT_SECS = 1800
BASH_OLD_SECS = 36000  # 10 hours

VERBOSE = False


def log(msg):
    """Append a timestamped line to LOG_PATH, trimming the file to LOG_MAX_LINES."""
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    path = Path(LOG_PATH)
    with open(path, "a") as f:
        f.write(f"{ts} {msg}\n")
    # Re-read and rewrite the whole file every call to cap it at LOG_MAX_LINES.
    # Simple and fine at this log volume (one line per 5-minute check).
    lines = path.read_text().splitlines(keepends=True)
    if len(lines) > LOG_MAX_LINES:
        path.write_text("".join(lines[-LOG_MAX_LINES:]))


def vlog(msg):
    """Log only when -v is active; prefixes line with [v]."""
    if VERBOSE:
        log(f"[v] {msg}")


def kill_existing_instances():
    """Kill any other running process whose cmdline contains this script's filename."""
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
            # /proc/<pid>/cmdline is NUL-separated argv; a plain substring
            # check against the decoded bytes is enough to spot the script
            # name without bothering to split on NULs.
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace")
            if script_name in cmdline:
                os.kill(pid, signal.SIGTERM)
                killed.append(pid)
        except (FileNotFoundError, PermissionError):
            # Process exited between the scandir listing and the read, or
            # we don't have permission to inspect it — either way, skip it.
            pass
    if killed:
        # Give SIGTERM a couple seconds to take effect, then force-kill any
        # old instance that ignored it or was mid-cleanup.
        time.sleep(2)
        for pid in killed:
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    return killed


def load_avgs():
    """Return (1-min, 5-min, 15-min) load averages."""
    # /proc/loadavg format: "<1m> <5m> <15m> <running>/<total> <last-pid>"
    parts = open("/proc/loadavg").read().split()
    return float(parts[0]), float(parts[1]), float(parts[2])


def recent_bash_process():
    """Return (pid, age_secs) of the most recently started bash within BASH_RECENT_SECS, or None."""
    # SC_CLK_TCK converts the kernel's jiffy-based starttime (below) to seconds.
    clk_tck = os.sysconf("SC_CLK_TCK")
    boot_time = None
    with open("/proc/stat") as f:
        for line in f:
            if line.startswith("btime"):
                # btime is the system boot time as a Unix timestamp; process
                # starttimes are relative to it.
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
            # /proc/<pid>/stat is "pid (comm) state field3 field4 ...". The
            # comm can itself contain spaces/parens, so we can't just
            # .split() the whole line — instead find the LAST ')' (comm is
            # always the second, parenthesized token) and parse everything
            # after it, where field positions are fixed.
            after_comm = stat[stat.rfind(")") + 2:]
            fields = after_comm.split()
            # starttime is field 22 of the whole stat line; since we've
            # already consumed pid, comm and state, it's index 19 here
            # (22 - 3), measured in clock ticks since boot.
            start_ticks = int(fields[19])  # field 22 overall = index 19 after state
            age = now - (boot_time + start_ticks / clk_tck)
            if age < BASH_RECENT_SECS:
                vlog(f"bash pid {pid} age {age:.0f}s — recent")
                if youngest is None or age < youngest[1]:
                    youngest = (pid, age)
            else:
                vlog(f"bash pid {pid} age {age:.0f}s — old, ignoring")
        except (FileNotFoundError, PermissionError, ValueError, IndexError):
            # Process exited mid-scan, or /proc fields were in an
            # unexpected shape — just skip that pid.
            pass

    return youngest


def old_bash_process():
    """Return (pid, age_secs) of any bash process older than BASH_OLD_SECS, or None.

    Mirrors recent_bash_process()'s /proc/stat parsing, but checks the
    opposite direction (age >= threshold) and returns on the first match
    since any single long-lived bash is enough to trigger a forced shutdown.
    """
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
            # /proc/<pid>/stat is "pid (comm) state field3 field4 ...". The
            # comm can itself contain spaces/parens, so we can't just
            # .split() the whole line — instead find the LAST ')' (comm is
            # always the second, parenthesized token) and parse everything
            # after it, where field positions are fixed.
            after_comm = stat[stat.rfind(")") + 2:]
            fields = after_comm.split()
            # starttime is field 22 of the whole stat line; since we've
            # already consumed pid, comm and state, it's index 19 here
            # (22 - 3), measured in clock ticks since boot.
            start_ticks = int(fields[19])
            age = now - (boot_time + start_ticks / clk_tck)
            if age >= BASH_OLD_SECS:
                return (pid, age)
        except (FileNotFoundError, PermissionError, ValueError, IndexError):
            # Process exited mid-scan, or /proc fields were in an
            # unexpected shape — just skip that pid.
            pass

    return None


def stop_services():
    """Stop all running systemd services; return list of service names stopped."""
    try:
        # --no-legend/--no-pager give a plain "unit load active sub ..."
        # table with no header/footer, so the first whitespace-separated
        # column of each line is just the unit name.
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
        # Broad catch is intentional: this runs during shutdown and must
        # never abort the rest of the kill sequence just because systemctl
        # is missing/misbehaving.
        log(f"stop_services error: {e}")
        return []


def is_tmux(pid):
    """Return True if pid's comm starts with 'tmux'."""
    try:
        return Path(f"/proc/{pid}/comm").read_text().strip().startswith("tmux")
    except (FileNotFoundError, PermissionError):
        return False


def kill_processes():
    """SIGTERM then SIGKILL all PIDs >= 10 except self and tmux; return count of initial targets."""
    my_pid = os.getpid()

    # PIDs < 10 are early-boot/kernel-critical processes (init, kthreads,
    # etc.) that we never want to touch. tmux is excluded so the terminal
    # multiplexer session itself survives the sweep — everything running
    # inside it still gets killed, but the server process it's attached to
    # does not.
    def killable():
        return [
            int(e.name)
            for e in os.scandir("/proc")
            if e.name.isdigit() and int(e.name) >= 10 and int(e.name) != my_pid
            and not is_tmux(int(e.name))
        ]

    targets = killable()
    for pid in targets:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    # Grace period for processes to exit cleanly on SIGTERM before being
    # force-killed.
    time.sleep(SIGTERM_WAIT)
    # Re-scan rather than reusing `targets`: some processes may have exited
    # on their own during the wait, and killable() also naturally excludes
    # any new children spawned in the meantime that are themselves tmux.
    for pid in killable():
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    return len(targets)


def survivors():
    """Return list of (pid, cmd) for all PIDs >= 10 still alive except self and tmux."""
    my_pid = os.getpid()
    result = []
    for e in os.scandir("/proc"):
        if not e.name.isdigit():
            continue
        pid = int(e.name)
        if pid < 10 or pid == my_pid:
            continue
        try:
            cmd = Path(f"/proc/{pid}/comm").read_text().strip()
        except (FileNotFoundError, PermissionError):
            cmd = "?"
        if cmd.startswith("tmux"):
            continue
        result.append((pid, cmd))
    return result


def main_loop():
    """Check idleness every SLEEP_INTERVAL seconds; kill everything and exit when idle."""
    log("--- start" + (" (-v)" if VERBOSE else "") + " ---")
    log("started in -v mode" if VERBOSE else "started")
    while True:
        # A bash open for BASH_OLD_SECS is treated as a stuck/forgotten
        # session and forces a shutdown immediately, bypassing the
        # load-average / recent-bash idle checks below entirely.
        old_bash = old_bash_process()
        if old_bash:
            pid, age = old_bash
            log(f"bash pid {pid} has been running {age/3600:.1f}h (>= {BASH_OLD_SECS/3600:.0f}h) — going down -----------------------")
            ps = subprocess.run(["ps", "-ef"], capture_output=True, text=True)
            for line in ps.stdout.splitlines():
                log(f" -  {line}")
            services = stop_services()
            if services:
                log(f" - stopped services: {', '.join(services)}")
            n = kill_processes()
            log(f" - killed {n} processes")
            still_running = survivors()
            if still_running:
                log(f"WARNING: survivors after kill: {', '.join(f'{pid}({cmd})' for pid, cmd in still_running)}")
            else:
                log("verified: no survivors")
            log("--- exit ---")
            log("===================================================")
            sys.exit(0)

        # Collect every reason the machine looks "active" this cycle. Any
        # single reason is enough to skip the shutdown and sleep until the
        # next check — active_reasons stays empty only when all checks
        # agree the box is idle.
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
            log(f" - not idle: {'; '.join(active_reasons)}; sleep")
            time.sleep(SLEEP_INTERVAL)
            continue

        # All checks passed: idle. Snapshot the process table for the log
        # before tearing everything down, since this info is gone once we exit.
        log("system is idle — going down -----------------------")
        ps = subprocess.run(["ps", "-ef"], capture_output=True, text=True)
        for line in ps.stdout.splitlines():
            log(f" -  {line}")
        services = stop_services()
        if services:
            log(f" - stopped services: {', '.join(services)}")
        n = kill_processes()
        log(f" - killed {n} processes")
        still_running = survivors()
        if still_running:
            log(f"WARNING: survivors after kill: {', '.join(f'{pid}({cmd})' for pid, cmd in still_running)}")
        else:
            log("verified: no survivors")
        log("--- exit ---")
        log("===================================================")
        sys.exit(0)


if __name__ == "__main__":
    if "-h" in sys.argv or "--help" in sys.argv:
        print("""sprite_idle_killer.py — kills idle processes on this Sprite machine

Checks every 5 minutes. On startup, kills any previous instance.

IDLE when ALL of the following are true:
  - No skip file at /tmp/sprite-idle-killer-skip
  - All three load averages (1m, 5m, 15m) <= 0.05
  - No bash process started less than 30 minutes ago

If not idle: wait for next check cycle.
If idle: stop services, kill all PIDs >= 10 (except self), verify, exit 0.

LOG: /tmp/sprite-idle-killer.log""")
        sys.exit(0)

    if "-v" in sys.argv or "--verbose" in sys.argv:
        VERBOSE = True

    # Ensure only one watchdog is active before doing anything else.
    killed = kill_existing_instances()
    if killed:
        log(f"killed previous instance(s): {killed}")

    # Brief settle period after (re)start before the first idle check.
    time.sleep(30)
    main_loop()
