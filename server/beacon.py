#!/usr/bin/env python3
"""Tell the Claude Code usage display that Claude is working - from any machine.

Watches this computer's Claude Code session logs (~/.claude/projects/**/*.jsonl)
and, whenever one was written in the last few seconds, pings the display so its
green LED blinks. Run a copy on every computer you use Claude Code on (macOS,
Windows, Linux) - the display blinks whenever *any* of them is active.

    python3 beacon.py                       # find the display via mDNS (claude-display.local)
    python3 beacon.py --host 192.168.1.42   # or point straight at its IP
    CLAUDE_DISPLAY_HOST=192.168.1.42 python3 beacon.py

The device shows its IP and "<name>.local" on its status line. On Windows,
mDNS (.local) needs Apple Bonjour installed; if that's missing, pass --host.

No third-party dependencies. This replaces the old usage server - the display
now reads usage itself; this only drives the "thinking" LED.
"""

import argparse
import os
import sys
import time
import urllib.request

CLAUDE_PROJECTS = os.path.expanduser(os.path.join("~", ".claude", "projects"))
ACTIVE_WINDOW_SECS = 8   # a transcript written this recently => Claude is working
PING_EVERY_SECS = 4      # cadence while active; must stay under the device's BEACON_TTL_MS
IDLE_POLL_SECS = 2       # how often to re-check for activity while idle


def recently_active():
    """True if any session transcript was written within ACTIVE_WINDOW_SECS."""
    cutoff = time.time() - ACTIVE_WINDOW_SECS
    try:
        for root, _dirs, files in os.walk(CLAUDE_PROJECTS):
            for name in files:
                if not name.endswith(".jsonl"):
                    continue
                try:
                    if os.path.getmtime(os.path.join(root, name)) > cutoff:
                        return True
                except OSError:
                    pass
    except Exception:
        pass
    return False


def ping(host, port, path="/thinking/on"):
    url = f"http://{host}:{port}{path}"
    try:
        req = urllib.request.Request(url, method="POST")
        urllib.request.urlopen(req, timeout=2).read()
        return True
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser(description="Beacon Claude activity to the usage display.")
    ap.add_argument("--host", default=os.environ.get("CLAUDE_DISPLAY_HOST", "claude-display.local"),
                    help="display hostname or IP (default: claude-display.local)")
    ap.add_argument("--port", type=int, default=int(os.environ.get("CLAUDE_DISPLAY_PORT", "8080")))
    args = ap.parse_args()

    if not os.path.isdir(CLAUDE_PROJECTS):
        print(f"warning: {CLAUDE_PROJECTS} not found - is Claude Code installed for this user?",
              file=sys.stderr)

    print(f"Beaconing {args.host}:{args.port} while Claude Code is active here. Ctrl-C to stop.")
    print("(For a more precise signal, use Claude Code hooks instead - see README.)")
    state = None  # None / "active" / "idle", only act/print on change
    try:
        while True:
            if recently_active():
                ok = ping(args.host, args.port, "/thinking/on")
                if state != "active":
                    print("blinking" if ok else f"could not reach {args.host}:{args.port}")
                    state = "active"
                time.sleep(PING_EVERY_SECS)
            else:
                if state == "active":              # just went idle: turn it off now
                    ping(args.host, args.port, "/thinking/off")
                    print("idle")
                state = "idle"
                time.sleep(IDLE_POLL_SECS)
    finally:
        ping(args.host, args.port, "/thinking/off")  # don't leave it stuck on when we exit


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
