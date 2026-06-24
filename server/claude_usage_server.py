#!/usr/bin/env python3
"""Companion server for the ESP32 Claude Code usage display.

LEGACY / OPTIONAL: the firmware now reads usage directly from Anthropic (see
README and `claude setup-token`), so this server is no longer required. It's
kept as a fallback and for its `--fake` demo mode. The only Mac-side helper you
need for the live display is server/beacon.py (the "Claude is thinking" LED).

Serves GET /usage as JSON:

    {
      "active": true,                     # Claude Code is doing something now
      "five_hour": {"pct": 42.0, "resets_at": "3:00 PM"},
      "seven_day": {"pct": 18.5, "resets_at": "Wed 9:00 AM"}
    }

Limits come from Anthropic's OAuth usage endpoint, authenticated with the
token Claude Code already stores on this Mac (Keychain item
"Claude Code-credentials", falling back to ~/.claude/.credentials.json).
Activity is detected by recent writes to ~/.claude/projects session logs.

Run:    python3 claude_usage_server.py          # real data
        python3 claude_usage_server.py --fake   # demo data, no credentials needed

No third-party dependencies.
"""

import datetime
import getpass
import json
import math
import os
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8787
CLAUDE_DIR = os.path.expanduser("~/.claude")
PROJECTS_DIR = os.path.join(CLAUDE_DIR, "projects")
CREDS_FILE = os.path.join(CLAUDE_DIR, ".credentials.json")
KEYCHAIN_SERVICE = "Claude Code-credentials"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"  # Claude Code's public OAuth client id
ACTIVE_WINDOW_SECS = 15   # transcript written this recently => "working"
USAGE_CACHE_SECS = 30     # don't hammer the usage API
FAKE = "--fake" in sys.argv

_cache = {"t": 0.0, "data": None}


def _make_ssl_context():
    """An SSL context with a usable CA store, even on bundled Pythons.

    Some portable interpreters (notably PlatformIO's ~/.platformio Python) ship
    with their default CA path pointing at the build machine, so it doesn't
    exist here and every HTTPS request fails with CERTIFICATE_VERIFY_FAILED. If
    the default store comes up empty, fall back to certifi's bundle when it can
    be imported; a normal system Python keeps using its own certs and needs no
    third-party packages.
    """
    ctx = ssl.create_default_context()
    if ctx.cert_store_stats().get("x509", 0) == 0:
        try:
            import certifi
            ctx.load_verify_locations(certifi.where())
        except Exception:
            pass
    return ctx


_SSL_CTX = _make_ssl_context()


def read_creds():
    """Return (creds_dict, source) from the macOS Keychain or the creds file."""
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, timeout=10)
        if out.returncode == 0 and out.stdout.strip():
            return json.loads(out.stdout.strip()), "keychain"
    except Exception:
        pass
    try:
        with open(CREDS_FILE) as f:
            return json.load(f), "file"
    except Exception:
        return None, None


def write_creds(creds, source):
    """Persist refreshed credentials so Claude Code keeps working too."""
    payload = json.dumps(creds)
    if source == "keychain":
        subprocess.run(
            ["security", "add-generic-password", "-U",
             "-a", getpass.getuser(), "-s", KEYCHAIN_SERVICE, "-w", payload],
            capture_output=True, timeout=10, check=True)
    else:
        fd = os.open(CREDS_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(payload)


def get_token(force_refresh=False):
    """A valid OAuth access token, refreshing it Claude Code-style if stale.

    Claude Code only refreshes its token when it needs one itself, so the
    stored token is often expired. We refresh with the stored refresh token
    and write the rotated credentials back so Claude Code stays logged in.
    """
    creds, source = read_creds()
    if not creds:
        return None
    oauth = creds.get("claudeAiOauth", {})
    token = oauth.get("accessToken")
    expires_ms = oauth.get("expiresAt") or 0
    if token and not force_refresh and expires_ms / 1000 > time.time() + 60:
        return token

    refresh = oauth.get("refreshToken")
    if not refresh:
        return token
    body = json.dumps({
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "client_id": CLIENT_ID,
    }).encode()
    req = urllib.request.Request(
        TOKEN_URL, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10, context=_SSL_CTX) as resp:
            tok = json.load(resp)
    except Exception:
        return token  # let the caller fail with the stale token

    oauth["accessToken"] = tok.get("access_token", token)
    if tok.get("refresh_token"):
        oauth["refreshToken"] = tok["refresh_token"]
    oauth["expiresAt"] = int((time.time() + int(tok.get("expires_in", 3600))) * 1000)
    creds["claudeAiOauth"] = oauth
    try:
        write_creds(creds, source)
    except Exception as e:
        print(f"warning: could not write refreshed credentials back: {e}",
              file=sys.stderr)
    return oauth["accessToken"]


def fmt_reset(iso):
    """ISO timestamp -> short local string like '3:00 PM' or 'Wed 9:00 AM'."""
    if not iso:
        return ""
    try:
        dt = datetime.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        dt = dt.astimezone()
        now = datetime.datetime.now().astimezone()
        if dt.date() == now.date():
            return dt.strftime("%-I:%M %p")
        return dt.strftime("%a %-I:%M %p")
    except Exception:
        return ""


def _usage_request(token):
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
        "Content-Type": "application/json",
        "User-Agent": "claude-usage-display/1.0",
    })
    with urllib.request.urlopen(req, timeout=10, context=_SSL_CTX) as resp:
        return json.load(resp)


def fetch_usage():
    now = time.time()
    if _cache["data"] is not None and now - _cache["t"] < USAGE_CACHE_SECS:
        return _cache["data"]
    _cache["t"] = now  # throttle attempts (even failures) so a blip can't hammer the API

    token = get_token()
    if not token:
        return _cache["data"] or {"error": "no_token"}

    try:
        try:
            raw = _usage_request(token)
        except urllib.error.HTTPError as e:
            if e.code != 401:
                raise
            token = get_token(force_refresh=True)  # stale token: refresh, retry
            raw = _usage_request(token)
    except urllib.error.HTTPError as e:
        return _cache["data"] or {"error": f"api_http_{e.code}"}
    except Exception as e:
        # Transient network failure (timeout, connection reset, DNS hiccup).
        # Keep serving the last good numbers instead of blanking the display;
        # only surface an error if we've never had a successful fetch.
        reason = getattr(e, "reason", None) or e
        return _cache["data"] or {"error": f"api: {e.__class__.__name__}: {reason}"}

    data = {}
    for key in ("five_hour", "seven_day"):
        block = raw.get(key) or {}
        util = block.get("utilization")
        data[key] = {
            "pct": round(float(util), 1) if util is not None else None,
            "resets_at": fmt_reset(block.get("resets_at")),
        }
    _cache["data"] = data
    return data


def is_active():
    """True if any Claude Code session transcript was written very recently."""
    cutoff = time.time() - ACTIVE_WINDOW_SECS
    try:
        for root, _dirs, files in os.walk(PROJECTS_DIR):
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


def fake_payload():
    t = time.time()
    return {
        "active": int(t / 8) % 2 == 0,
        "five_hour": {
            "pct": round(50 + 45 * math.sin(t / 30), 1),
            "resets_at": "3:00 PM",
        },
        "seven_day": {
            "pct": round(40 + 20 * math.sin(t / 90), 1),
            "resets_at": "Wed 9:00 AM",
        },
    }


def build_payload():
    if FAKE:
        return fake_payload()
    payload = {"active": is_active()}
    payload.update(fetch_usage())
    return payload


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.rstrip("/") not in ("", "/usage"):
            self.send_error(404)
            return
        body = json.dumps(build_payload()).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # keep the terminal quiet


def main():
    mode = "FAKE demo data" if FAKE else "live Claude Code data"
    print(f"Serving {mode} on http://0.0.0.0:{PORT}/usage")
    if not FAKE:
        print("Note: the first request may pop a Keychain dialog - "
              "click 'Always Allow'.")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
