#!/usr/bin/env python3
"""Find the usage display on your LAN and print its IP + ready-to-use commands.

Resolves the device's mDNS name (default claude-display.local) to an IP, checks
that it responds, and prints the curl test lines and the Claude Code hook
commands with the IP filled in.

Use the IP - not the .local name - in your hooks: curl's short timeout often
can't resolve mDNS in time, so a hostname-based hook silently does nothing.

    python3 find_display.py
    python3 find_display.py --name claude-display.local --port 8080

No third-party dependencies.
"""

import argparse
import socket
import sys
import urllib.error
import urllib.request


def resolve(name):
    """Return the list of IPv4 addresses the name resolves to (mDNS included)."""
    try:
        infos = socket.getaddrinfo(name, None, socket.AF_INET)
    except socket.gaierror:
        return []
    ips = []
    for info in infos:
        ip = info[4][0]
        if ip not in ips:
            ips.append(ip)
    return ips


def responds(ip, port):
    """(reachable, detail) - does the device answer HTTP on this port?"""
    try:
        with urllib.request.urlopen(f"http://{ip}:{port}/", timeout=3) as r:
            return True, r.read(120).decode("utf-8", "replace").strip()
    except urllib.error.HTTPError as e:
        return True, f"HTTP {e.code}"
    except Exception as e:
        return False, str(e)


def main():
    ap = argparse.ArgumentParser(description="Find the usage display's IP address.")
    ap.add_argument("--name", default="claude-display.local", help="device mDNS name")
    ap.add_argument("--port", type=int, default=8080, help="beacon port (default 8080)")
    args = ap.parse_args()

    print(f"Resolving {args.name} ...")
    ips = resolve(args.name)
    if not ips:
        print(f"\nCould not resolve {args.name}.", file=sys.stderr)
        print("- Is the device powered on and on the same Wi-Fi?", file=sys.stderr)
        print("- Its status line shows its name and IP - you can read the IP there.", file=sys.stderr)
        print("- On Windows, mDNS (.local) needs Apple Bonjour installed.", file=sys.stderr)
        sys.exit(1)

    ip = ips[0]
    print(f"Found it at: {ip}")
    if len(ips) > 1:
        print(f"(also resolved: {', '.join(ips[1:])})")

    reachable, detail = responds(ip, args.port)
    if reachable:
        print(f"Device responded on port {args.port}: {detail!r}")
    else:
        print(f"Resolved, but no answer on port {args.port} ({detail}).")
        print("The IP is likely still correct - make sure the firmware is flashed.")

    print("\n--- quick test (watch the screen) ---")
    print(f"  curl -m 2 -X POST http://{ip}:{args.port}/thinking/on    # -> working")
    print(f"  curl -m 2 -X POST http://{ip}:{args.port}/thinking/off   # -> idle")

    print("\n--- hook commands for ~/.claude/settings.json (use the IP) ---")
    base = f"curl -sf -m 1 -X POST http://{ip}:{args.port}"
    print(f"  UserPromptSubmit, PreToolUse:  {base}/thinking/on  >/dev/null 2>&1 || true")
    print(f"  Stop:                          {base}/thinking/off >/dev/null 2>&1 || true")

    print("\nTip: add a DHCP reservation in your router so this IP stays put.")


if __name__ == "__main__":
    main()
