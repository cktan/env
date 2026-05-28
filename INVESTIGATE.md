# Idle Detection — sprite_idle_killer.py

The script checks two signals every 5 minutes. If either is true, the machine
is considered active and nothing is killed.

## Signal 1: CPU usage > 5%

Reads `/proc/stat` twice, one second apart. Computes:

```
usage = (1 - delta_idle / delta_total) * 100
```

Catches anything that is actively burning CPU — compilations, test runs,
data processing.

**Why not TTY presence?** Sprite leaves bash sessions attached to dead TTYs
(master side closed, SSH gone). Their `tty_nr` in `/proc/[pid]/stat` stays
non-zero even though no human is on the other end, causing false positives.

## Signal 2: Established network connections

Reads `/proc/net/tcp` and `/proc/net/tcp6` for rows with state `01`
(ESTABLISHED), collects their socket inodes, then walks `/proc/[pid]/fd/`
for all PIDs ≥ 10 to see if any socket fd matches.

Catches active Claude sessions, running dev servers, ongoing API calls, etc.
A dead bash session has no sockets, so it does not trigger this signal.

## Skip file

If `/tmp/sprite-idle-killer-skip` exists, the kill cycle is skipped entirely.
Use this when running a long background job that has no network connections
and low CPU (e.g. a script sleeping between iterations):

```
touch /tmp/sprite-idle-killer-skip   # before disconnecting
rm /tmp/sprite-idle-killer-skip      # when done
```

The file lives in `/tmp` and clears on reboot.
