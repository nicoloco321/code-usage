#!/usr/bin/env python3
"""Claude Code usage display - Raspberry Pi edition.

The ESP32 display (firmware/src/main.cpp) as a fullscreen app for a Raspberry
Pi - built for a Pi 2, fine on anything newer - on an HDMI monitor or the
official touchscreen:

  - your real 5-hour / weekly utilization, fetched straight from Anthropic's
    OAuth usage API with a dedicated login (server/device_login.py)
  - a spinner while Claude is working, driven by the same HTTP beacons
    (POST /thinking/on, /thinking/off) from Claude Code hooks or beacon.py
  - an optional Spotify now-playing screen, and an optional 3D printer screen
    with a Bambu Lab print's progress (POST /mode/spotify, /mode/bambu,
    /mode/usage, /mode/toggle - or just tap the screen)

It speaks the firmware's HTTP API on the same port, so the hooks, beacon.py,
find_display.py and the /switch command work unchanged - point them at the Pi.
It also answers GET /usage with the numbers as JSON (the Windows tray helper
reads that).

    python3 pi/claude_display.py                      # fullscreen
    python3 pi/claude_display.py --windowed 800x480   # in a window, for testing
    python3 pi/claude_display.py --demo               # fake data, no logins needed
    python3 pi/claude_display.py --setup-bambu        # add your Bambu Lab printer

Settings live in ~/.config/claude-display/config.ini (see config.example.ini);
pi/install.sh sets everything up to start fullscreen on boot. Needs pygame 2
(sudo apt install python3-pygame); everything else is the standard library.

Keys: tap / click / space switches screens, Ctrl+Q quits (Esc too, windowed).
"""

import argparse
import configparser
import datetime
import hashlib
import io
import json
import math
import os
import re
import signal
import socket
import ssl
import struct
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
# piwheels' pygame (Bullseye) warns about NEON on every start; it's harmless.
warnings.filterwarnings("ignore", message="Your system is neon capable")
try:
    import pygame
except ImportError:
    sys.exit("pygame is missing - on the Pi run:  sudo apt install python3-pygame")

CONFIG_PATH = os.path.expanduser("~/.config/claude-display/config.ini")
STATE_PATH = os.path.expanduser("~/.local/state/claude-display/state.json")

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"  # Claude Code's public client
USER_AGENT = "claude-usage-display/1.0"  # Cloudflare 1010-blocks urllib's default UA

SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_NOW_URL = ("https://api.spotify.com/v1/me/player/currently-playing"
                   "?additional_types=episode")

ROOT_TEXT = ("Claude Code usage display (Raspberry Pi). POST /thinking/on while "
             "working, /thinking/off when done. POST /mode/usage, /mode/spotify, "
             "/mode/bambu or /mode/toggle to switch screens; GET /mode to ask; "
             "GET /usage for JSON.\n")

# ---- palette: the firmware's RGB565 colours, in full RGB ----
COL_BG = (18, 18, 18)
COL_CARD = (42, 42, 42)
COL_ORANGE = (217, 119, 87)   # Claude orange
COL_TEXT = (236, 236, 236)
COL_DIM = (123, 123, 123)
COL_SUB = (175, 175, 175)     # artist names: between the title and the dim details
COL_GREEN = (57, 186, 82)
COL_YELLOW = (222, 162, 66)
COL_RED = (230, 81, 74)
COL_SPOTIFY = (29, 185, 84)
COL_BAMBU = (35, 165, 67)     # Bambu Lab green
COL_EYE = (0, 0, 0)

# The screens, in the order a tap or /mode/toggle cycles through them.
MODES = ("usage", "spotify", "bambu")

# The Bambu Lab mark: two columns, each cut by a slanted gap - four panels,
# in units of a 442 x 574 box.
BAMBU_LOGO_SIZE = (442, 574)
BAMBU_LOGO = [
    [(0, 0), (205, 0), (205, 280), (0, 360)],
    [(0, 393), (205, 313), (205, 574), (0, 574)],
    [(236, 0), (442, 0), (442, 261), (236, 181)],
    [(236, 214), (442, 294), (442, 574), (236, 574)],
]

SPIN_FRAME_MS = 50  # spinner step: 20 fps
SPIN_FRAMES = 32    # one sweep around the spark: 1.6 s
COL_ORANGE_HI = (246, 178, 150)  # the crest of the spark and the text shimmer

# Pixel-art Clawd on his native 12x8 grid, same as firmware/src/mascot.h
# (1 = body, 2 = eye).
MASCOT = [
    [0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0],
    [0, 0, 1, 2, 1, 1, 1, 1, 2, 1, 0, 0],
    [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
    [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
    [0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0],
    [0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0],
    [0, 0, 1, 0, 1, 0, 0, 1, 0, 1, 0, 0],
    [0, 0, 1, 0, 1, 0, 0, 1, 0, 1, 0, 0],
]

# Regular / bold font pairs to try, first match wins. DejaVu ships with
# Raspberry Pi OS (install.sh makes sure); the others let you try the app on a
# desktop. Falls back to pygame's bundled font.
FONT_CANDIDATES = [
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    (r"C:\Windows\Fonts\segoeui.ttf", r"C:\Windows\Fonts\segoeuib.ttf"),
    ("/System/Library/Fonts/Supplemental/Arial.ttf",
     "/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
]


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- config / state

def parse_size(text):
    """'800x480' -> (800, 480); '' -> None."""
    m = re.fullmatch(r"\s*(\d+)\s*[xX*]\s*(\d+)\s*", text or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


class Config:
    """config.ini, with defaults for anything missing (a missing file is fine)."""

    def __init__(self, path):
        cp = configparser.ConfigParser(interpolation=None)
        cp.read(path, encoding="utf-8")

        def s(section, key, default=""):
            return cp.get(section, key, fallback=default).strip()

        def num(section, key, default):
            try:
                return float(s(section, key) or default)
            except ValueError:
                log(f"config: [{section}] {key} isn't a number - using {default}")
                return default

        self.refresh_token = s("anthropic", "refresh_token")
        self.usage_poll = max(60.0, num("anthropic", "poll_seconds", 180))
        self.sp_client_id = s("spotify", "client_id")
        self.sp_refresh_token = s("spotify", "refresh_token")
        self.sp_poll = max(1.0, num("spotify", "poll_seconds", 5))
        self.port = int(num("server", "port", 8080))
        self.beacon_ttl = num("server", "beacon_ttl_seconds", 300)
        self.size = parse_size(s("screen", "size"))
        self.rotate = int(num("screen", "rotate", 0)) % 360
        self.bambu_host = s("bambu", "host")
        self.bambu_serial = s("bambu", "serial")
        self.bambu_code = s("bambu", "access_code")
        self.bambu_name = s("bambu", "name")

    @property
    def bambu_ready(self):
        return bool(self.bambu_host and self.bambu_serial and self.bambu_code)


class StateFile:
    """What the firmware keeps in NVS: rotated OAuth tokens and the screen mode."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        try:
            with open(path, encoding="utf-8") as f:
                self.data = json.load(f)
        except (OSError, ValueError):
            self.data = {}

    def get(self, key, default=None):
        with self.lock:
            return self.data.get(key, default)

    def put(self, key, value):
        with self.lock:
            self.data[key] = value
            try:
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                tmp = self.path + ".tmp"
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(self.data, f)
                os.replace(tmp, self.path)
            except OSError as e:
                log(f"could not save {self.path}: {e}")


# ---------------------------------------------------------------- http / oauth

SSL_CTX = ssl.create_default_context()


def http(url, data=None, headers=None, timeout=10):
    """(status, body, headers). HTTP errors come back as a status; network
    errors raise (URLError / OSError)."""
    req = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
            return r.status, r.read(), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers


def retry_after(headers):
    try:
        return int(headers.get("Retry-After") or 0)
    except (TypeError, ValueError):
        return 0


def anthropic_exchange(refresh_token):
    body = json.dumps({"grant_type": "refresh_token", "refresh_token": refresh_token,
                       "client_id": OAUTH_CLIENT_ID}).encode()
    code, data, _ = http(TOKEN_URL, data=body, headers={
        "Content-Type": "application/json", "User-Agent": USER_AGENT})
    if code != 200:
        log(f"anthropic token refresh failed: HTTP {code}")
        return None
    return json.loads(data)


def spotify_exchange(client_id):
    # PKCE app, so the body is form-encoded and carries no client secret.
    def exchange(refresh_token):
        body = urllib.parse.urlencode({"grant_type": "refresh_token",
                                       "refresh_token": refresh_token,
                                       "client_id": client_id}).encode()
        code, data, _ = http(SPOTIFY_TOKEN_URL, data=body, headers={
            "Content-Type": "application/x-www-form-urlencoded"})
        if code != 200:
            log(f"spotify token refresh failed: HTTP {code}")
            return None
        return json.loads(data)
    return exchange


def token_id(token):
    return hashlib.sha256(token.encode()).hexdigest()[:16]


class OAuthLogin:
    """One OAuth login, refreshed the firmware's way.

    The config.ini token is only the seed: refresh tokens rotate, so the newest
    one lives in the state file (the firmware keeps it in NVS). If the config
    token changes - you minted a new login - the rotated one is dropped and the
    new seed takes over.
    """

    def __init__(self, name, seed, state, exchange, default_ttl):
        self.name, self.seed, self.state = name, seed, state
        self.exchange, self.default_ttl = exchange, default_ttl
        saved = state.get(name) or {}
        if not seed or saved.get("seed") != token_id(seed):
            saved = {}
        self.refresh = saved.get("refresh") or seed
        self.access = saved.get("access", "")
        self.expires_at = saved.get("expires_at", 0)

    @property
    def configured(self):
        return bool(self.seed)

    def ensure(self):
        """Make sure self.access is usable, refreshing if missing or about to
        expire. Falls back to the config token if the rotated one is rejected.
        Network errors propagate so callers can tell "offline" from "revoked"."""
        if self.access and time.time() < self.expires_at - 300:
            return True
        if self._try(self.refresh):
            return True
        return self.refresh != self.seed and self._try(self.seed)

    def invalidate(self):
        self.expires_at = 0

    def _try(self, refresh_token):
        if not refresh_token:
            return False
        tok = self.exchange(refresh_token)
        if not tok or not tok.get("access_token"):
            return False
        self.access = tok["access_token"]
        self.refresh = tok.get("refresh_token") or refresh_token  # rotation: keep the new one
        self.expires_at = time.time() + int(tok.get("expires_in") or self.default_ttl)
        self.state.put(self.name, {"seed": token_id(self.seed), "refresh": self.refresh,
                                   "access": self.access, "expires_at": self.expires_at})
        return True


# ---------------------------------------------------------------- time formatting

_ISO = re.compile(r"(\d{4})-(\d\d)-(\d\d)[T ](\d\d):(\d\d)(?::(\d\d)(?:\.\d+)?)?"
                  r"\s*(Z|[+-]\d\d:?\d\d)?$")


def parse_iso(text):
    """ISO-8601 -> aware datetime. Tolerates any fraction length, which
    datetime.fromisoformat doesn't before Python 3.11."""
    m = _ISO.match((text or "").strip())
    if not m:
        return None
    y, mo, d, h, mi, sec, tz = m.groups()
    offset = datetime.timedelta(0)
    if tz and tz != "Z":
        sign = -1 if tz[0] == "-" else 1
        tz = tz[1:].replace(":", "")
        offset = sign * datetime.timedelta(hours=int(tz[:2]), minutes=int(tz[2:]))
    return datetime.datetime(int(y), int(mo), int(d), int(h), int(mi), int(sec or 0),
                             tzinfo=datetime.timezone(offset))


def clock_str(dt):
    """'4:19 AM' - no leading zero, portable (no %-I)."""
    return f"{dt.hour % 12 or 12}:{dt.minute:02d} {'AM' if dt.hour < 12 else 'PM'}"


def fmt_reset(when, now):
    """'4:19 AM' if it's today, else 'Mon 1:00 PM' - same as the firmware."""
    if when is None:
        return ""
    local = when.astimezone()
    if local.date() == now.date():
        return clock_str(local)
    return f"{local.strftime('%a')} {clock_str(local)}"


def fmt_until(when, now):
    """'in 2h 13m' / 'in 3d 4h' / 'in 12m'."""
    mins = int((when - now).total_seconds() // 60)
    if mins < 1:
        return "any moment"
    days, rest = divmod(mins, 1440)
    hours, mins = divmod(rest, 60)
    if days:
        return f"in {days}d {hours}h"
    if hours:
        return f"in {hours}h {mins}m"
    return f"in {mins}m"


def fmt_ms(ms):
    s = max(0, int(ms)) // 1000
    if s >= 3600:
        return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"
    return f"{s // 60}:{s % 60:02d}"


def bar_color(pct):
    if pct is None or pct < 50:
        return COL_GREEN
    return COL_YELLOW if pct < 80 else COL_RED


def local_ip():
    """This machine's LAN address (no packets are sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return ""
    finally:
        s.close()


# ---------------------------------------------------------------- shared state

class Model:
    """Everything on screen, shared by the workers, the HTTP server and the renderer."""

    def __init__(self, cfg, state, spotify_ready, bambu_ready=False):
        self.cfg, self.state = cfg, state
        self.ready = {"usage": True, "spotify": spotify_ready, "bambu": bambu_ready}
        self.lock = threading.Lock()
        saved = state.get("mode")
        self.mode = saved if self.ready.get(saved) else "usage"

        self.usage = None          # {"five"/"week": (pct or None, reset datetime, iso)}
        self.usage_ok_at = 0.0     # monotonic time of the last good fetch
        self.backoff_until = 0.0   # rate limited: no usage calls before this
        self.usage_status = None   # (text, colour); None = nothing yet
        self.usage_version = 0

        self.np = None             # now-playing dict; None = no fetch yet
        self.np_at = 0.0           # monotonic time np["progress_ms"] was current
        self.np_version = 0
        self.sp_status = None
        self.art = None            # (url, image bytes) of the latest album art
        self.art_px = 300          # art size on screen, so we fetch a sharp enough variant

        self.printer = None        # Bambu "print" report, merged update by update
        self.printer_status = None
        self.remote_sessions = {}  # sender -> (monotonic time, [session summaries])

        self.last_beacon = 0.0     # monotonic time of the last "thinking" ping, 0 = off
        self.flash_msg = None      # (text, colour, until): short-lived status override
        self.host = socket.gethostname().split(".")[0]
        self.ip = ""
        self.usage_wake = threading.Event()
        self.spotify_wake = threading.Event()
        self.bambu_wake = threading.Event()

    # -- beacons: "thinking" is sticky between on and off; the TTL is a backstop
    def thinking(self, now=None):
        now = time.monotonic() if now is None else now
        return self.last_beacon > 0 and now - self.last_beacon < self.cfg.beacon_ttl

    def beacon_on(self):
        self.last_beacon = time.monotonic()

    def beacon_off(self):
        self.last_beacon = 0.0

    # -- screen mode (persisted, like the firmware's NVS "mode")
    def set_mode(self, mode):
        """Switch screens. False if that screen isn't set up."""
        if not self.ready.get(mode):
            return False
        with self.lock:
            if mode == self.mode:
                return True
            self.mode = mode
            if mode == "spotify" and self.np is None:
                self.sp_status = ("fetching spotify...", COL_DIM)
            if mode == "bambu" and self.printer is None:
                self.printer_status = ("connecting to the printer...", COL_DIM)
        self.state.put("mode", mode)
        if mode == "spotify":
            self.spotify_wake.set()
        elif mode == "bambu":
            self.bambu_wake.set()
        else:  # back on the usage screen: refresh numbers gone stale off-screen
            now = time.monotonic()
            if now >= self.backoff_until and now - self.usage_ok_at > self.cfg.usage_poll:
                self.usage_wake.set()
        return True

    def next_mode(self):
        """The screen after this one, skipping any that aren't set up."""
        i = MODES.index(self.mode)
        return next((m for m in MODES[i + 1:] + MODES[:i] if self.ready[m]), self.mode)

    def toggle_mode(self):
        nxt = self.next_mode()
        if nxt == self.mode:
            self.flash("no other screens set up - see config.ini", COL_YELLOW)
        else:
            self.set_mode(nxt)

    def flash(self, text, color, secs=4):
        with self.lock:
            self.flash_msg = (text, color, time.monotonic() + secs)

    # -- worker updates
    def set_usage(self, usage):
        with self.lock:
            self.usage = usage
            self.usage_ok_at = time.monotonic()
            self.usage_status = ("usage ok", COL_GREEN)
            self.usage_version += 1

    def set_usage_status(self, text, color):
        with self.lock:
            self.usage_status = (text, color)

    def set_now_playing(self, np):
        with self.lock:
            self.np = np
            self.np_at = time.monotonic()
            self.sp_status = ("spotify ok", COL_GREEN)
            self.np_version += 1

    def set_sp_status(self, text, color):
        with self.lock:
            self.sp_status = (text, color)

    def set_art(self, url, data):
        with self.lock:
            self.art = (url, data)

    def update_printer(self, report):
        """Merge one Bambu report - P1/A1 printers only send what changed."""
        with self.lock:
            self.printer = {**(self.printer or {}), **report}
            self.printer_status = ("printer ok", COL_GREEN)

    def set_printer_status(self, text, color):
        with self.lock:
            self.printer_status = (text, color)

    def set_sessions(self, sender, sessions):
        """Claude Code sessions reported by one machine's hooks (display_hook.py)."""
        def clean(s):
            text = lambda v, n=120: str(v or "")[:n]
            agents = s.get("agents") if isinstance(s.get("agents"), list) else []
            return {"project": text(s.get("project"), 40), "state": text(s.get("state"), 10),
                    "activity": text(s.get("activity")), "waiting": text(s.get("waiting")),
                    "elapsed": s.get("elapsed") if isinstance(s.get("elapsed"), (int, float)) else 0,
                    "agents": [{"label": text(a.get("label"), 80), "activity": text(a.get("activity"))}
                               for a in agents[:6] if isinstance(a, dict)]}
        with self.lock:
            self.remote_sessions[sender] = (time.monotonic(),
                                            [clean(s) for s in sessions[:8] if isinstance(s, dict)])

    def sessions_now(self, now):
        """Every machine's working / waiting sessions."""
        out = []
        for at, sessions in self.remote_sessions.values():
            if now - at < self.cfg.beacon_ttl:  # a machine that went quiet drops off
                out += [dict(s, elapsed=s["elapsed"] + (now - at)) for s in sessions
                        if s["state"] in ("working", "waiting")]
        # waiting on you first; otherwise the hooks' order, most recently active first
        return sorted(out, key=lambda s: s["state"] != "waiting")

    def snapshot(self):
        now = time.monotonic()
        with self.lock:
            flash = self.flash_msg if self.flash_msg and self.flash_msg[2] > now else None
            return SimpleNamespace(
                mode=self.mode, usage=self.usage, usage_version=self.usage_version,
                usage_status=self.usage_status, np=self.np, np_at=self.np_at,
                np_version=self.np_version, sp_status=self.sp_status, art=self.art,
                printer=self.printer, printer_status=self.printer_status,
                sessions=self.sessions_now(now),
                printer_name=self.cfg.bambu_name or "3D printer",
                thinking=self.thinking(now), flash=flash and flash[:2],
                host=self.host, ip=self.ip, mono=now)

    def usage_json(self):
        """GET /usage - the numbers on screen, for the tray helper and friends."""
        now = datetime.datetime.now().astimezone()
        with self.lock:
            u, ok_at, mode = self.usage, self.usage_ok_at, self.mode

        def window(key):
            if not u:
                return {"pct": None, "resets_at": "", "resets": ""}
            pct, when, iso = u[key]
            return {"pct": None if pct is None else round(pct, 1), "resets_at": iso,
                    "resets": fmt_reset(when, now)}

        return {"five_hour": window("five"), "seven_day": window("week"),
                "valid": u is not None, "thinking": self.thinking(), "mode": mode,
                "age_s": int(time.monotonic() - ok_at) if ok_at else None}


# ---------------------------------------------------------------- usage worker

def usage_request(access):
    """One GET to the usage endpoint -> (status, retry_after, usage or None)."""
    code, body, headers = http(USAGE_URL, headers={
        "Authorization": "Bearer " + access,
        "anthropic-beta": "oauth-2025-04-20",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    })
    if code != 200:
        return code, (retry_after(headers) if code in (429, 403) else 0), None
    raw = json.loads(body)

    def window(key):
        block = raw.get(key) or {}
        pct = block.get("utilization")
        iso = block.get("resets_at") or ""
        return (float(pct) if pct is not None else None, parse_iso(iso), iso)

    return 200, 0, {"five": window("five_hour"), "week": window("seven_day")}


def fetch_usage(login):
    """Usage with a fresh token; a 401 means it went stale mid-flight, so
    refresh once and retry. -3 = no valid token could be obtained."""
    if not login.ensure():
        return -3, 0, None
    code, retry, usage = usage_request(login.access)
    if code == 401:
        login.invalidate()
        if login.ensure():
            code, retry, usage = usage_request(login.access)
    return code, retry, usage


OFF_SCREEN_SLOWDOWN = 3  # poll this much less often while another screen is up


def usage_worker(model, login):
    while True:
        delay = model.cfg.usage_poll
        if model.mode != "usage":
            # Nobody's looking at the bars, so spend fewer calls on them;
            # switching back to the usage screen refreshes stale numbers.
            delay *= OFF_SCREEN_SLOWDOWN
        if not login.configured:
            model.set_usage_status("no login - run device_login.py --config", COL_YELLOW)
            model.usage_wake.wait(3600)
            model.usage_wake.clear()
            continue
        try:
            code, retry, usage = fetch_usage(login)
        except Exception as e:  # offline, DNS, timeout...
            log(f"usage fetch error: {e}")
            code, retry, usage = -1, 0, None

        if code == 200:
            model.set_usage(usage)
        elif code in (401, -3):
            model.set_usage_status("auth failed - run device_login.py", COL_RED)
        elif code in (429, 403):
            # A 403 here is the edge rate-limiter, not a real auth failure: a
            # valid token still gets it when hammered. Back off past the
            # cooldown (default 10 min without Retry-After) so it can expire.
            delay = (retry or 600) + 30
            model.backoff_until = time.monotonic() + delay
            model.set_usage_status(f"rate limited, retry in {int(delay)}s", COL_YELLOW)
        elif not model.usage_ok_at or time.monotonic() - model.usage_ok_at > 90:
            model.set_usage_status("network down - retrying" if code == -1
                                   else f"usage fetch failed ({code})", COL_RED)
        if model.usage is None and code in (-1, -3) and delay > 15:
            delay = 15  # no numbers yet (network still coming up at boot?) - retry soon
        model.usage_wake.wait(delay)
        model.usage_wake.clear()


# ---------------------------------------------------------------- spotify worker

def pick_art(images, want_px):
    """Smallest image at least want_px wide, else the largest (albums ship 640/300/64)."""
    imgs = [(int(i.get("width") or 0), i["url"]) for i in images if i.get("url")]
    if not imgs:
        return ""
    big = [im for im in imgs if im[0] >= want_px]
    return (min(big) if big else max(imgs))[1]


def spotify_request(access, art_px):
    """One GET to currently-playing -> (status, retry_after, now_playing or None).
    204 means nothing is playing - a success, just an empty one."""
    code, body, headers = http(SPOTIFY_NOW_URL, headers={"Authorization": "Bearer " + access})
    if code == 204:
        return 200, 0, {"has_track": False}
    if code != 200:
        return code, (retry_after(headers) if code == 429 else 0), None
    raw = json.loads(body or b"{}")
    item = raw.get("item")
    if not item:
        return 200, 0, {"has_track": False}
    album = item.get("album") or {}
    artists = ", ".join(a["name"] for a in item.get("artists") or [] if a.get("name"))
    if not artists:  # podcast episodes have a show, not artists
        artists = (item.get("show") or {}).get("name", "")
    return 200, 0, {
        "has_track": True,
        "playing": bool(raw.get("is_playing")),
        "progress_ms": int(raw.get("progress_ms") or 0),
        "duration_ms": int(item.get("duration_ms") or 0),
        "track": item.get("name") or "",
        "artist": artists,
        "album": album.get("name") or "",
        "art_url": pick_art(album.get("images") or item.get("images") or [], art_px),
    }


def fetch_now_playing(login, art_px):
    if not login.ensure():
        return -3, 0, None
    code, retry, np = spotify_request(login.access, art_px)
    if code == 401:
        login.invalidate()
        if login.ensure():
            code, retry, np = spotify_request(login.access, art_px)
    return code, retry, np


def spotify_worker(model, login):
    art_tried = ""
    while True:
        if model.mode != "spotify":  # sleep until someone switches to Spotify
            model.spotify_wake.wait()
            model.spotify_wake.clear()
            continue
        delay = model.cfg.sp_poll
        try:
            code, retry, np = fetch_now_playing(login, model.art_px)
        except Exception as e:
            log(f"spotify fetch error: {e}")
            code, retry, np = -1, 0, None

        if code == 200:
            model.set_now_playing(np)
            url = np.get("art_url") if np["has_track"] else ""
            if url and url != art_tried:
                art_tried = url  # one attempt per track
                try:
                    status, data, _ = http(url, timeout=8)
                    if status == 200 and len(data) < 4_000_000:
                        model.set_art(url, data)
                except Exception as e:
                    log(f"album art fetch error: {e}")
            if np["has_track"] and np["playing"] and np["duration_ms"]:
                # poll right as the track ends to catch the next one
                left = (np["duration_ms"] - np["progress_ms"]) / 1000
                delay = max(1.0, min(delay, left + 0.5))
        elif code in (401, 403, -3):
            # 403 usually means the Spotify app doesn't include this account
            # (Dashboard -> your app -> User Management).
            model.set_sp_status("spotify auth failed - spotify_login.py", COL_RED)
        elif code == 429:
            delay = (retry or 30) + 2
            model.set_sp_status(f"spotify rate limited, {int(delay)}s", COL_YELLOW)
        elif time.monotonic() - model.np_at > 30:
            model.set_sp_status("network down - retrying" if code == -1
                                else f"spotify fetch failed ({code})", COL_RED)
        model.spotify_wake.wait(delay)
        model.spotify_wake.clear()


# ---------------------------------------------------------------- bambu lab printer
#
# Bambu printers publish their status over MQTT on the LAN (TLS, port 8883,
# user "bblp", password = the access code on the printer's screen). The
# printer answers to its serial number: we subscribe to device/<serial>/report
# and ask once for a full status ("pushall"); after that P1 / A1 printers only
# send what changed.

class MQTTRefused(Exception):
    """The printer turned down the login - a wrong access code."""


def _mqtt_str(text):
    data = text.encode()
    return struct.pack("!H", len(data)) + data


def _mqtt_len(n):
    out = bytearray()
    while True:
        n, byte = divmod(n, 128)
        out.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(out)


class MiniMQTT:
    """Just enough MQTT 3.1.1 to follow one topic: log in, subscribe, publish a
    request, read messages. The printer's certificate is self-signed, so it's
    not verified (it's on your LAN, like the ESP32's own HTTPS)."""

    def __init__(self, host, port, username, password, tls=True, keepalive=60):
        self.addr, self.user, self.password = (host, port), username, password
        self.tls, self.keepalive = tls, keepalive
        self.sock = None
        self.packet_id = 0

    def connect(self, timeout=10):
        sock = socket.create_connection(self.addr, timeout=timeout)
        if self.tls:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            sock = ctx.wrap_socket(sock, server_hostname=self.addr[0])
        self.sock = sock
        flags = 0x02 | (0x80 if self.user else 0) | (0x40 if self.password else 0)
        body = (_mqtt_str("MQTT") + bytes([4, flags]) + struct.pack("!H", self.keepalive)
                + _mqtt_str(f"claude-display-{os.getpid()}"))
        if self.user:
            body += _mqtt_str(self.user)
        if self.password:
            body += _mqtt_str(self.password)
        self._send(0x10, body)
        kind, body = self._packet(timeout)
        if kind >> 4 != 2 or len(body) < 2:
            raise ConnectionError("the printer didn't answer the MQTT login")
        if body[1]:
            raise MQTTRefused(body[1])

    def subscribe(self, topic):
        self.packet_id = self.packet_id % 65535 + 1
        self._send(0x82, struct.pack("!H", self.packet_id) + _mqtt_str(topic) + b"\x00")

    def publish(self, topic, payload):
        self._send(0x30, _mqtt_str(topic) + payload)

    def ping(self):
        self._send(0xC0, b"")

    def read(self, timeout):
        """The next message as (topic, payload); ("", b"") for control packets
        (acks, pings); None if nothing arrived in time."""
        try:
            kind, body = self._packet(timeout)
        except socket.timeout:
            return None
        if kind >> 4 != 3:
            return "", b""
        n = struct.unpack("!H", body[:2])[0]
        topic, pos = body[2:2 + n].decode("utf-8", "replace"), 2 + n
        qos = (kind >> 1) & 3
        if qos:
            if qos == 1:
                self._send(0x40, body[pos:pos + 2])  # PUBACK
            pos += 2
        return topic, body[pos:]

    def close(self):
        if self.sock:
            try:
                self._send(0xE0, b"")  # DISCONNECT
            except OSError:
                pass
            self.sock.close()
            self.sock = None

    def _send(self, kind, body):
        self.sock.sendall(bytes([kind]) + _mqtt_len(len(body)) + body)

    def _packet(self, timeout):
        """One whole packet. Waits up to `timeout` for it to start (raising
        socket.timeout); once it has, reads the rest without giving up early."""
        self.sock.settimeout(timeout)
        kind = self._recv(1)[0]
        self.sock.settimeout(15)
        length, shift = 0, 0
        while True:
            byte = self._recv(1)[0]
            length |= (byte & 0x7F) << shift
            if not byte & 0x80:
                break
            shift += 7
        return kind, self._recv(length)

    def _recv(self, n):
        buf = bytearray()
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("the printer closed the connection")
            buf += chunk
        return bytes(buf)


def bambu_client(cfg):
    return MiniMQTT(cfg.bambu_host, 8883, "bblp", cfg.bambu_code)


PUSHALL = json.dumps({"pushing": {"sequence_id": "0", "command": "pushall",
                                  "version": 1, "push_target": 1}}).encode()


def bambu_worker(model):
    """While the printer screen is up, stay connected and merge its reports."""
    cfg = model.cfg
    while True:
        if model.mode != "bambu":  # sleep until someone switches to the printer
            model.bambu_wake.wait()
            model.bambu_wake.clear()
            continue
        client, delay = bambu_client(cfg), 10
        try:
            client.connect()
            client.subscribe(f"device/{cfg.bambu_serial}/report")
            client.publish(f"device/{cfg.bambu_serial}/request", PUSHALL)
            last_heard = last_ping = time.monotonic()
            while model.mode == "bambu":
                msg = client.read(timeout=1.0)
                now = time.monotonic()
                if msg is not None:
                    last_heard = now
                    if msg[1]:
                        try:
                            report = json.loads(msg[1]).get("print")
                        except (ValueError, AttributeError):
                            report = None
                        if isinstance(report, dict):
                            model.update_printer(report)
                if now - last_heard > 90:
                    raise ConnectionError("the printer went quiet")
                if now - last_ping > 30:
                    client.ping()
                    last_ping = now
            delay = 0  # left the printer screen
        except MQTTRefused as e:
            log(f"printer refused the login (MQTT code {e})")
            model.set_printer_status("printer refused the access code - run --setup-bambu", COL_RED)
            delay = 60
        except (OSError, ConnectionError) as e:
            log(f"printer connection: {e}")
            model.set_printer_status("printer unreachable - retrying", COL_RED)
        finally:
            client.close()
        if delay:
            model.bambu_wake.wait(delay)
            model.bambu_wake.clear()


BAMBU_STATES = {  # gcode_state -> (what the screen says, colour)
    "RUNNING": ("printing", COL_BAMBU), "PREPARE": ("preparing", COL_TEXT),
    "SLICING": ("slicing", COL_TEXT), "PAUSE": ("paused", COL_YELLOW),
    "FINISH": ("finished", COL_BAMBU), "FAILED": ("failed", COL_RED),
    "IDLE": ("idle", COL_DIM),
}


def fmt_minutes(mins):
    mins = int(mins)
    return f"{mins // 60}h {mins % 60}m" if mins >= 60 else f"{mins}m"


def printer_view(p, now):
    """What the printer screen shows, from the merged report."""
    p = p or {}
    state = str(p.get("gcode_state") or "").upper()
    word, color = BAMBU_STATES.get(state, (state.lower() or "idle", COL_DIM))
    job = str(p.get("subtask_name") or os.path.basename(str(p.get("gcode_file") or "")))
    for ext in (".gcode.3mf", ".3mf", ".gcode"):
        if job.lower().endswith(ext):
            job = job[:-len(ext)]
    pct = p.get("mc_percent")
    pct = max(0, min(100, int(pct))) if isinstance(pct, (int, float)) else None
    if state == "FINISH":
        pct = 100
    left = eta = ""
    rem = p.get("mc_remaining_time")
    if state in ("RUNNING", "PAUSE", "PREPARE") and isinstance(rem, (int, float)) and rem > 0:
        left = f"{fmt_minutes(rem)} left"
        if state != "PAUSE":  # a paused print's finish time slides - don't promise one
            eta = f"done {fmt_reset(now + datetime.timedelta(minutes=rem), now)}"
    layer, total = p.get("layer_num"), p.get("total_layer_num")
    layers = f"layer {layer} / {total}" if total else ""

    def temp(name, now_key, target_key):
        t, target = p.get(now_key), p.get(target_key)
        if not isinstance(t, (int, float)):
            return ""
        heating = isinstance(target, (int, float)) and target > 0 and abs(target - t) >= 3
        return f"{name} {round(t)}°" + (f" / {round(target)}°" if heating else "")

    temps = "  ·  ".join(x for x in (temp("nozzle", "nozzle_temper", "nozzle_target_temper"),
                                          temp("bed", "bed_temper", "bed_target_temper")) if x)
    bar = {"PAUSE": COL_YELLOW, "FAILED": COL_RED}.get(state, COL_BAMBU)
    return SimpleNamespace(state=state, word=word, color=color, bar=bar, job=job, pct=pct,
                           left=left, eta=eta, layers=layers, temps=temps,
                           has_job=state in ("RUNNING", "PAUSE", "PREPARE", "FINISH", "FAILED"))


def find_bambu(seconds=8):
    """Bambu printers announce themselves on UDP 2021 (SSDP-style); listen."""
    found = {}
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("", 2021))
    s.settimeout(0.5)
    end = time.monotonic() + seconds
    try:
        while time.monotonic() < end:
            try:
                data, addr = s.recvfrom(4096)
            except socket.timeout:
                continue
            head = {}
            for line in data.decode("utf-8", "replace").splitlines():
                key, _, value = line.partition(":")
                if value:
                    head[key.strip().lower()] = value.strip()
            serial = head.get("usn")
            if serial and serial not in found:
                found[serial] = {"serial": serial, "host": head.get("location") or addr[0],
                                 "name": head.get("devname.bambu.com", ""),
                                 "model": head.get("devmodel.bambu.com", "")}
    finally:
        s.close()
    return list(found.values())


def setup_bambu(config_path):
    """--setup-bambu: find the printer, ask for its access code, test, save."""
    import getpass
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "server"))
    from device_login import save_to_config

    print("Looking for Bambu Lab printers on the network (8 s)...")
    printers = find_bambu()
    for i, pr in enumerate(printers, 1):
        print(f"  {i}. {pr['name'] or 'printer'} ({pr['model']}) at {pr['host']}")
    if printers:
        pick = input(f"Which one? [1-{len(printers)}, default 1] ").strip() or "1"
        printer = printers[int(pick) - 1]
    else:
        print("None heard - enter it by hand (IP and serial are on the printer's screen).")
        printer = {"host": input("Printer IP: ").strip(), "serial": input("Serial number: ").strip(),
                   "name": input("A name for it (optional): ").strip()}
    code = getpass.getpass("Access code (on the printer's screen: Settings > WLAN, "
                           "or Network on an X1): ").strip()

    cfg = SimpleNamespace(bambu_host=printer["host"], bambu_code=code)
    client = bambu_client(cfg)
    try:
        client.connect()
        client.subscribe(f"device/{printer['serial']}/report")
        client.publish(f"device/{printer['serial']}/request", PUSHALL)
        report, end = None, time.monotonic() + 15
        while report is None and time.monotonic() < end:
            msg = client.read(timeout=1.0)
            if msg and msg[1]:
                report = json.loads(msg[1]).get("print")
        if report:
            v = printer_view(report, datetime.datetime.now().astimezone())
            print(f"Connected - the printer is {v.word}"
                  + (f", {v.job} at {v.pct}%" if v.has_job and v.pct is not None else "") + ".")
        else:
            print("Connected, but no status arrived yet - saving anyway.")
    except MQTTRefused:
        sys.exit("The printer rejected that access code. Check it on the printer's "
                 "screen and try again (newer firmware may also need LAN-only mode with "
                 "Developer Mode turned on).")
    except (OSError, ConnectionError) as e:
        print(f"Couldn't reach the printer ({e}) - saving anyway; check it's on and on this network.")
    finally:
        client.close()

    save_to_config(config_path, "bambu", {"host": printer["host"], "serial": printer["serial"],
                                          "access_code": code, "name": printer.get("name", "")})
    print(f"Saved to [bambu] in {config_path}.")
    print("Restart the display to pick it up (sudo systemctl restart claude-display, or "
          "reboot), then tap the screen to reach the printer screen.")


# ---------------------------------------------------------------- demo data

def demo_art():
    """A stand-in album cover, encoded like a download would be."""
    s = pygame.Surface((300, 300))
    for y in range(300):
        pygame.draw.line(s, (40 + y // 3, 30, 90 + y // 4), (0, y), (299, y))
    pygame.draw.circle(s, COL_ORANGE, (150, 150), 90)
    pygame.draw.circle(s, (40, 30, 90), (150, 150), 30)
    buf = io.BytesIO()
    pygame.image.save(s, buf, "art.png")
    return buf.getvalue()


def demo_worker(model):
    """--demo: moving numbers, a thinking spinner every other 8s, a fake song
    and a fake print."""
    art = demo_art()
    model.set_art("demo:art", art)
    start = time.time()
    while True:
        t = time.time()
        now = datetime.datetime.now().astimezone()
        done = ((t - start) / 600) % 1.0  # a 10-minute "print" on loop
        model.update_printer({
            "gcode_state": "RUNNING", "subtask_name": "Articulated Dragon.3mf",
            "mc_percent": int(done * 100), "mc_remaining_time": int((1 - done) * 154),
            "layer_num": int(done * 212), "total_layer_num": 212,
            "nozzle_temper": 219.6, "nozzle_target_temper": 220.0,
            "bed_temper": 55.1, "bed_target_temper": 55.0})
        model.set_usage({
            "five": (50 + 45 * math.sin(t / 30), now + datetime.timedelta(hours=2, minutes=13), ""),
            "week": (40 + 20 * math.sin(t / 90), now + datetime.timedelta(days=3, hours=4), ""),
        })
        if int(t / 8) % 2 == 0:
            model.beacon_on()
        else:
            model.beacon_off()
        pos = int((t - start) * 1000) % 214000
        if pos < 2000 or model.np is None:
            model.set_now_playing({
                "has_track": True, "playing": int(t / 20) % 4 != 3,
                "progress_ms": pos, "duration_ms": 214000,
                "track": "A Demo Track With a Title Long Enough to Wrap Onto Two Lines",
                "artist": "The Placeholders", "album": "Sample Sessions (Deluxe)",
                "art_url": "demo:art"})
        time.sleep(1)


# ---------------------------------------------------------------- http server

class BeaconHandler(BaseHTTPRequestHandler):
    """The firmware's HTTP API: beacons, mode switching, plus GET /usage."""
    model = None  # set before serving
    server_version = "claude-display"

    def do_GET(self):
        self.body = b""
        self._route()

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.body = self.rfile.read(min(length, 65536)) if length else b""
        self._route()

    def _take_sessions(self):
        """display_hook.py sends its session summary along with each beacon."""
        try:
            data = json.loads(self.body) if self.body else None
        except ValueError:
            return
        if isinstance(data, dict) and isinstance(data.get("sessions"), list):
            self.model.set_sessions(str(data.get("host") or self.client_address[0]),
                                    data["sessions"])

    def _route(self):
        m = self.model
        path = urllib.parse.urlsplit(self.path).path.rstrip("/") or "/"
        if path in ("/thinking", "/thinking/on"):
            m.beacon_on()
            self._take_sessions()
            self._send(200, "on\n")
        elif path == "/thinking/off":
            m.beacon_off()
            self._take_sessions()
            self._send(200, "off\n")
        elif path == "/mode":
            self._send(200, m.mode + "\n")
        elif path.startswith("/mode/") and path[6:] in MODES + ("toggle",):
            want = path[6:]
            if want == "toggle":
                want = m.next_mode()
            if m.set_mode(want):
                self._send(200, want + "\n")
            elif want == "spotify":
                self._send(409, "spotify not configured - run server/spotify_login.py "
                                "--config ~/.config/claude-display/config.ini\n")
            else:
                self._send(409, "printer not configured - run "
                                "python3 pi/claude_display.py --setup-bambu\n")
        elif path == "/usage":
            self._send(200, json.dumps(m.usage_json()) + "\n", "application/json")
        elif path == "/":
            self._send(200, ROOT_TEXT)
        else:
            self._send(404, "not found\n")

    def _send(self, code, text, ctype="text/plain"):
        body = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


# ---------------------------------------------------------------- drawing

SS = 3  # supersampling factor for anti-aliased shapes


def find_fonts():
    for regular, bold in FONT_CANDIDATES:
        if os.path.exists(regular):
            return regular, (bold if os.path.exists(bold) else None)
    return None, None


class Renderer:
    """Draws the two screens at any resolution.

    Each layout is written in its own design units and scaled uniformly to fit,
    centered. The screen's shape picks the layout:
      landscape  800x480   monitors, TVs, the official touchscreen
      portrait   180x320   the ESP32's stacked layout
      bar        1480x320  long bar panels on their side (Waveshare 11.9")
      strip      320x1480  the same bar standing up
    """

    LAYOUTS = {"landscape": (800, 480), "portrait": (180, 320),
               "bar": (1480, 320), "strip": (320, 1480)}
    ART_SIZE = {"landscape": 232, "portrait": 84, "bar": 236, "strip": 272}

    def __init__(self, size, fonts):
        self.W, self.H = size
        aspect = self.W / self.H
        if aspect >= 2.4:
            self.layout = "bar"
        elif aspect <= 1 / 2.4:
            self.layout = "strip"
        else:
            self.layout = "landscape" if aspect >= 1 else "portrait"
        bw, bh = self.LAYOUTS[self.layout]
        self.s = min(self.W / bw, self.H / bh)
        self.ox = (self.W - bw * self.s) / 2
        self.oy = (self.H - bh * self.s) / 2
        self.art_px = self.n(self.ART_SIZE[self.layout])  # so we fetch sharp enough art
        self.anim = 0.0      # 0 = usage bars full width, 1 = session panel open
        self.anim_at = 0.0
        self.spin_cache = {}  # (frame, px) -> the spark, drawn once
        self.font_paths = fonts
        self.fonts = {}
        self.text_cache = {}
        self.shape_cache = {}
        self.art_url = None
        self.art_img = None

    # design units -> pixels
    def x(self, v):
        return int(round(self.ox + v * self.s))

    def y(self, v):
        return int(round(self.oy + v * self.s))

    def n(self, v):
        return max(1, int(round(v * self.s)))

    def rect(self, x, y, w, h):
        x0, y0 = self.x(x), self.y(y)
        return pygame.Rect(x0, y0, self.x(x + w) - x0, self.y(y + h) - y0)

    # -- text
    def font(self, size, bold=False):
        key = (self.n(size), bold)
        f = self.fonts.get(key)
        if f is None:
            regular, bold_path = self.font_paths
            f = pygame.font.Font(bold_path if bold and bold_path else regular, key[0])
            if bold and not bold_path:
                f.set_bold(True)  # synthesize it
            self.fonts[key] = f
        return f

    def text(self, surf, s, x, baseline, size, color, bold=False, align="l"):
        """Draw s with its baseline at design y `baseline`; x is its left /
        centre / right edge for align l / c / r."""
        if not s:
            return
        f = self.font(size, bold)
        key = (s, id(f), color)
        img = self.text_cache.get(key)
        if img is None:
            if len(self.text_cache) > 400:
                self.text_cache.clear()
            img = self.text_cache[key] = f.render(s, True, color, COL_BG)
        px = self.x(x)
        if align == "c":
            px -= img.get_width() // 2
        elif align == "r":
            px -= img.get_width()
        surf.blit(img, (px, self.y(baseline) - f.get_ascent()))

    def fit(self, s, max_w, size, bold=False):
        """Shorten s with an ellipsis until it fits in max_w design units."""
        f, limit = self.font(size, bold), max_w * self.s
        if f.size(s)[0] <= limit:
            return s
        while s and f.size(s + "\u2026")[0] > limit:
            s = s[:-1]
        return s.rstrip() + "\u2026"

    def wrap2(self, s, max_w, size, bold=False):
        """Break s at a word boundary so line one fits; line two is ellipsized.
        A single overlong word gets hard-broken instead."""
        f, limit = self.font(size, bold), max_w * self.s
        if f.size(s)[0] <= limit:
            return s, ""
        words, line = s.split(" "), ""
        i = 0
        for i, word in enumerate(words):
            trial = f"{line} {word}" if line else word
            if f.size(trial)[0] > limit:
                break
            line = trial
        if not line:
            cut = len(s)
            while cut > 1 and f.size(s[:cut])[0] > limit:
                cut -= 1
            return s[:cut], self.fit(s[cut:], max_w, size, bold)
        return line, self.fit(" ".join(words[i:]), max_w, size, bold)

    # -- shapes
    def _ss(self, key, w, h, draw):
        """An anti-aliased shape: drawn SSx larger, smoothscaled down, cached.
        The background is transparent, so it blends into whatever is under it
        (smoothscale shifts colours a notch, which an opaque box would show)."""
        key = (key, w, h)
        img = self.shape_cache.get(key)
        if img is None:
            big = pygame.Surface((w * SS, h * SS), pygame.SRCALPHA, 32)
            big.fill((*COL_BG, 0))  # edges fade toward the background, not black
            draw(big, SS)
            img = pygame.transform.smoothscale(big, (w, h))
            if len(self.shape_cache) > 200:
                self.shape_cache.clear()
            self.shape_cache[key] = img
        return img

    def mascot(self, surf, x, y, cell):
        for r, row in enumerate(MASCOT):
            for c, v in enumerate(row):
                if v:
                    surf.fill(COL_ORANGE if v == 1 else COL_EYE,
                              self.rect(x + c * cell, y + r * cell, cell, cell))

    def bar(self, surf, rect, frac, color, radius, fast=False):
        """A rounded track with the left `frac` of it filled. `fast` skips the
        anti-aliasing - mid-animation every frame has a new width, and building
        a supersampled bar per frame is too slow on a Pi 2."""
        w, h = rect.size
        fill = 0 if frac is None else int(round(w * max(0.0, min(1.0, frac))))
        r = self.n(radius)
        if fast:
            pygame.draw.rect(surf, COL_CARD, rect, border_radius=r)
            if fill:
                clip = surf.get_clip()
                surf.set_clip(pygame.Rect(rect.x, rect.y, fill, h).clip(clip))
                pygame.draw.rect(surf, color, rect, border_radius=r)
                surf.set_clip(clip)
            return

        def draw(big, k):
            pygame.draw.rect(big, COL_CARD, big.get_rect(), border_radius=r * k)
            if fill:
                big.set_clip(pygame.Rect(0, 0, fill * k, h * k))
                pygame.draw.rect(big, color, big.get_rect(), border_radius=r * k)
                big.set_clip(None)

        surf.blit(self._ss(("bar", fill, color, r), w, h, draw), rect.topleft)

    def spinner(self, surf, cx, cy, size, frame):
        """Claude's spark: 12 teardrop rays, long and short alternating, fat end
        out. While working (frame >= 0) a crest sweeps around it - the rays it
        passes stretch, fatten and brighten; idle, it's a still grey spark.
        Every frame is drawn once and cached, so animating costs a blit."""
        px = self.n(size)
        key = ("spark", frame, px)
        img = self.spin_cache.get(key)
        if img is None:
            img = self.spin_cache[key] = self._ss(key, px, px, lambda big, k: self._spark(big, frame))
            self.shape_cache.pop((key, px, px), None)  # keep it out of the general cache
        surf.blit(img, (self.x(cx) - px // 2, self.y(cy) - px // 2))

    @staticmethod
    def _spark(big, frame):
        c = big.get_width() / 2
        R = c * 0.97
        active = frame >= 0
        for i in range(12):
            reach = 1.0 if i % 2 == 0 else 0.76
            if active:
                d = (i / 12 - frame / SPIN_FRAMES) % 1.0
                wave = (0.5 + 0.5 * math.cos(2 * math.pi * d)) ** 2  # 1 at the crest
                length = R * reach * (0.6 + 0.4 * wave)
                width = R * (0.15 + 0.07 * wave)
                t = 0.7 * wave
                color = tuple(round(a + (b - a) * t) for a, b in zip(COL_ORANGE, COL_ORANGE_HI))
            else:
                length, width, color = R * reach * 0.82, R * 0.16, COL_CARD
            a = 2 * math.pi * i / 12 - math.pi / 2
            ca, sa = math.cos(a), math.sin(a)

            def at(u, v):
                return c + u * ca - v * sa, c + u * sa + v * ca

            r0, end = R * 0.12, length - width / 2  # thin near the middle, round fat tip
            pygame.draw.polygon(big, color, [at(r0, width * 0.16), at(end, width / 2),
                                             at(end, -width / 2), at(r0, -width * 0.16)])
            pygame.draw.circle(big, color, at(end, 0), width / 2)
        pygame.draw.circle(big, COL_ORANGE if active else COL_CARD, (c, c), R * 0.15)

    def warm_spinner(self):
        """Draw every spark frame up front, so the first sweep doesn't stutter."""
        dummy = pygame.Surface((1, 1))
        for mode in ("usage", "spotify"):
            cx, cy, size = self.spinner_geometry(mode)
            for frame in range(-1, SPIN_FRAMES):
                self.spinner(dummy, cx, cy, size, frame)

    def _shimmer(self, surf, s, x, baseline, size, align, phase):
        """A soft light band sweeping across the text, like Claude Code's."""
        f = self.font(size)
        key = (s, id(f), "hi")
        hi = self.text_cache.get(key)
        if hi is None:
            hi = self.text_cache[key] = f.render(s, True, COL_ORANGE_HI)
        w, h = hi.get_size()
        band = max(8, int(h * 2))
        mask = self.shape_cache.get(("shimmer", band, h))
        if mask is None:
            mask = pygame.Surface((band, h), pygame.SRCALPHA)
            for col in range(band):
                alpha = round(255 * math.sin(math.pi * (col + 0.5) / band) ** 2)
                pygame.draw.line(mask, (255, 255, 255, alpha), (col, 0), (col, h - 1))
            self.shape_cache[("shimmer", band, h)] = mask
        left = self.x(x) - (w // 2 if align == "c" else w if align == "r" else 0)
        top = self.y(baseline) - f.get_ascent()
        pos = int(-band + (w + band) * phase)
        piece = pygame.Surface((band, h), pygame.SRCALPHA)
        piece.blit(hi, (-pos, 0))
        piece.blit(mask, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)
        surf.blit(piece, (left + pos, top))

    @staticmethod
    def _logo(big, cx, cy, radius, color, bg):
        # flat take on the three "sound wave" bars of the Spotify mark (firmware r=20)
        u = radius / 20.0
        pygame.draw.circle(big, color, (cx, cy), radius)
        for x0, y0, w in ((-13, -9, 27), (-11, -1, 22), (-8, 7, 17)):
            pygame.draw.rect(big, bg, pygame.Rect(cx + x0 * u, cy + y0 * u, w * u, 4 * u),
                             border_radius=int(2 * u))

    def spotify_logo(self, surf, cx, cy, radius):
        d = self.n(2 * radius)
        img = self._ss(("logo",), d, d,
                       lambda big, k: self._logo(big, d * k / 2, d * k / 2, d * k / 2,
                                                 COL_SPOTIFY, COL_BG))
        surf.blit(img, (self.x(cx) - d // 2, self.y(cy) - d // 2))

    def bambu_logo(self, surf, x, y, h):
        """The Bambu Lab mark, h design units tall, top-left at (x, y)."""
        lw, lh = BAMBU_LOGO_SIZE
        wpx, hpx = self.n(h * lw / lh), self.n(h)

        def draw(big, k):
            sx, sy = wpx * k / lw, hpx * k / lh
            for poly in BAMBU_LOGO:
                pygame.draw.polygon(big, COL_BAMBU, [(px * sx, py * sy) for px, py in poly])

        surf.blit(self._ss(("bambu",), wpx, hpx, draw), (self.x(x), self.y(y)))

    def art_placeholder(self, surf, rect):
        def draw(big, k):
            pygame.draw.rect(big, COL_CARD, big.get_rect(), border_radius=int(rect.w * k * 0.04))
            c = big.get_width() / 2
            self._logo(big, c, c, big.get_width() * 0.18, COL_DIM, COL_CARD)
        surf.blit(self._ss(("art",), rect.w, rect.h, draw), rect.topleft)

    def play_state(self, surf, cx, top, h, playing):
        """No font has a reliable play / pause glyph, so draw them (firmware shapes)."""
        wpx, hpx = self.n(h * 16 / 14), self.n(h)
        color = COL_SPOTIFY if playing else COL_DIM

        def draw(big, k):
            u, c = hpx * k / 14.0, wpx * k / 2
            if playing:
                pygame.draw.polygon(big, color, [(c - 5 * u, 0), (c - 5 * u, 14 * u), (c + 7 * u, 7 * u)])
            else:
                for x0 in (-8, 3):
                    pygame.draw.rect(big, color, pygame.Rect(c + x0 * u, 0, 5 * u, 14 * u),
                                     border_radius=int(u))

        surf.blit(self._ss(("play", playing), wpx, hpx, draw), (self.x(cx) - wpx // 2, self.y(top)))

    def album_art(self, surf, rect, snap):
        np = snap.np
        if snap.art and snap.art[0] != self.art_url:
            self.art_url, self.art_img = snap.art[0], None
            try:
                img = pygame.image.load(io.BytesIO(snap.art[1]), "art.jpg").convert()
                self.art_img = pygame.transform.smoothscale(img, rect.size)
            except Exception as e:
                log(f"could not decode album art: {e}")
        if self.art_img and self.art_url == np.get("art_url"):
            if self.art_img.get_size() != rect.size:
                self.art_img = pygame.transform.smoothscale(self.art_img, rect.size)
            surf.blit(self.art_img, rect.topleft)
        else:
            self.art_placeholder(surf, rect)

    # -- scene
    @staticmethod
    def progress_ms(snap):
        np = snap.np
        est = np["progress_ms"] + (int((snap.mono - snap.np_at) * 1000) if np["playing"] else 0)
        return min(est, np["duration_ms"]) if np["duration_ms"] else est

    def scene_key(self, snap, now):
        """Everything but the spinner's animation that changes the picture -
        redraw the whole screen only when this does."""
        key = (snap.mode, snap.flash, snap.host, snap.ip, now.strftime("%Y%m%d%H%M"))
        if snap.mode == "usage":
            panel = ()
            if self.anim > 0:  # the session panel's text, minute by minute
                panel = tuple((s["project"], s["state"], s["activity"], s["waiting"],
                               int(s["elapsed"] // 60),
                               tuple((a["label"], a["activity"]) for a in s["agents"]))
                              for s in snap.sessions)
            sliding = "sliding" if 0 < self.anim < 1 else self.anim
            return key + (snap.usage_version, snap.usage_status, snap.thinking, sliding, panel)
        if snap.mode == "bambu":
            # only what's drawn, so fan speeds and wifi strength don't cause redraws
            v = printer_view(snap.printer, now)
            return key + (snap.printer is None, v.word, v.job, v.pct, v.left, v.eta, v.layers,
                          v.temps, snap.printer_status, snap.thinking)
        playing = snap.np is not None and snap.np.get("has_track")
        return key + (snap.np_version, snap.sp_status, snap.art and snap.art[0], snap.thinking,
                      self.progress_ms(snap) // 1000 if playing else -1)

    def draw(self, surf, snap, now):
        surf.fill(COL_BG)
        getattr(self, f"_{snap.mode}_{self.layout}")(surf, snap, now)
        self._status(surf, snap)

    # where the status line starts: (x, baseline, text size)
    STATUS = {"landscape": (32, 454, 16), "portrait": (12, 315, 10),
              "bar": (28, 300, 19), "strip": (24, 1414, 18)}

    def _status(self, surf, snap):
        addr = f"{snap.host}.local  {snap.ip}".rstrip()
        status = snap.flash or {"usage": snap.usage_status, "spotify": snap.sp_status,
                                "bambu": snap.printer_status}[snap.mode]
        if self.working_note(snap):
            # Only the usage screen has the big spinner, so on the others Claude
            # working shows up here - the Pi's stand-in for the ESP32's LED.
            x, base, size = self.STATUS[self.layout]
            self.draw_spinner(surf, snap)
            self.text(surf, "Claude is working...", x + size * 2.05, base, size, COL_ORANGE)
            status = ("", COL_DIM)
        if self.layout == "portrait":
            if status is None:
                text, color = addr, COL_DIM  # what the firmware shows at boot
            else:
                text, color = status
                if color == COL_GREEN:
                    text = f"{text}  {snap.host}.local"
            self.text(surf, self.fit(text, 156, 10), 12, 315, 10, color)
            return
        text, color = status or ("starting...", COL_DIM)
        if self.layout == "strip":
            self.text(surf, self.fit(text, 272, 18), 24, 1414, 18, color)
            self.text(surf, self.fit(addr, 272, 18), 24, 1446, 18, COL_DIM)
        elif self.layout == "bar":
            self.text(surf, text, 28, 300, 19, color)
            self.text(surf, addr, 1268, 300, 19, COL_DIM, align="r")
        else:
            self.text(surf, text, 32, 454, 16, color)
            self.text(surf, addr, 768, 454, 16, COL_DIM, align="r")

    # (cx, cy, size) of the usage screen's spinner in each layout's design units
    SPINNER = {"landscape": (162, 246, 150), "portrait": (90, 108, 60),
               "bar": (70, 206, 104), "strip": (160, 410, 190)}

    # -- the session panel: while Claude works (or waits on you) the usage bars
    # slide to the right end and shrink, opening the middle of the screen.
    ANIM_SECS = 0.75
    PANEL_LAYOUTS = ("bar", "landscape")

    def session_open(self, snap):
        return (snap.mode == "usage" and self.layout in self.PANEL_LAYOUTS
                and (snap.thinking or bool(snap.sessions)))

    def advance(self, snap):
        """Step the open / close slide toward where it should be; True while it moves."""
        target = 1.0 if self.session_open(snap) else 0.0
        dt = min(0.05, snap.mono - self.anim_at) if self.anim_at else 0.0
        self.anim_at = snap.mono
        if self.anim == target:
            return False
        step = dt / self.ANIM_SECS
        self.anim = min(target, self.anim + step) if target > self.anim else max(target, self.anim - step)
        return True

    @property
    def eased(self):
        t = self.anim  # smootherstep: zero speed and acceleration at both ends
        return t * t * t * (t * (t * 6 - 15) + 10)

    SLIDE_REGION = {"bar": (350, 28, 1130, 232), "landscape": (298, 110, 502, 292)}

    def draw_slide(self, surf, snap, now):
        """Mid-slide frame: repaint only the moving region, return its rect."""
        rect = self.rect(*self.SLIDE_REGION[self.layout])
        surf.fill(COL_BG, rect)
        getattr(self, f"_slide_{self.layout}")(surf, snap, now)
        return rect

    def width(self, s, size, bold=False):
        """Width of s in design units."""
        return self.font(size, bold).size(s)[0] / self.s

    def _session_panel(self, surf, snap, x, y, w, h, k=1.0, visible=None):
        """What each Claude Code session is up to, laid out in the box
        (x, y, w, h) but only shown up to `visible` wide (the slide uncovers
        it); k scales the text for smaller layouts."""
        visible = w if visible is None else visible
        if visible < 4:
            return
        clip = surf.get_clip()
        surf.set_clip(self.rect(x, y - 8, visible, h + 16).clip(clip))
        try:
            sessions = snap.sessions
            if not sessions:  # a beacon without details (curl hooks, beacon.py)
                self.text(surf, "Claude is working", x, y + 26 * k, 26 * k, COL_TEXT, bold=True)
                self.text(surf, self.fit("session details come from the Claude Code hooks "
                                         "(display_hook.py)", w, 18 * k), x, y + 58 * k,
                          18 * k, COL_DIM)
                return
            single = len(sessions) == 1
            shown = sessions[:1 if single else 2]
            cy = y
            for s in shown:
                waiting = s["state"] == "waiting"
                mins = int(s["elapsed"] // 60)
                state = "needs you" if waiting else "working" + (f" {fmt_minutes(mins)}" if mins else "")
                if not single and s["agents"]:
                    n = len(s["agents"])
                    state += f"  ·  {n} agent{'s' if n > 1 else ''}"
                title = self.fit(s["project"] or "Claude", w * 0.55, 26 * k, bold=True)
                self.text(surf, title, x, cy + 26 * k, 26 * k, COL_TEXT, bold=True)
                self.text(surf, self.fit(f"  ·  {state}", w - self.width(title, 26 * k, True),
                                         22 * k),
                          x + self.width(title, 26 * k, True), cy + 26 * k, 22 * k,
                          COL_YELLOW if waiting else COL_ORANGE)
                line = s["waiting"] if waiting else s["activity"]
                self.text(surf, self.fit(line, w, 21 * k), x, cy + 58 * k, 21 * k,
                          COL_YELLOW if waiting else COL_SUB)
                cy += 58 * k
                if single:
                    for a in s["agents"][:3]:
                        cy += 27 * k
                        text = "› " + a["label"] + (f"  —  {a['activity']}" if a["activity"] else "")
                        self.text(surf, self.fit(text, w, 18 * k), x, cy, 18 * k, COL_DIM)
                    if len(s["agents"]) > 3:
                        cy += 27 * k
                        self.text(surf, f"+{len(s['agents']) - 3} more agents", x, cy, 18 * k, COL_DIM)
                cy += 40 * k
            if len(sessions) > len(shown):
                rest = len(sessions) - len(shown)
                self.text(surf, f"+{rest} more session{'s' if rest > 1 else ''} working",
                          x, cy - 8 * k, 18 * k, COL_DIM)
        finally:
            surf.set_clip(clip)

    @staticmethod
    def working_note(snap):
        """Does the status line show "Claude is working" (every screen but usage)?"""
        return snap.mode != "usage" and snap.thinking and not snap.flash

    def spin_frame(self, snap):
        """The spinner's animation step, or -1 when nothing is spinning."""
        if not snap.thinking or (snap.mode != "usage" and not self.working_note(snap)):
            return -1
        return int(snap.mono * 1000 / SPIN_FRAME_MS) % SPIN_FRAMES

    def spinner_geometry(self, mode):
        if mode == "usage":
            return self.SPINNER[self.layout]
        x, base, size = self.STATUS[self.layout]  # the small one in the status line
        return x + size * 0.8, base - size * 0.36, size * 1.6

    def draw_spinner(self, surf, snap):
        """Draw just the spinner and return the rect it covers. Animation frames
        repaint and push only this square instead of the whole screen - on a
        Pi 2 under X, full-screen frames cost more CPU than everything else."""
        cx, cy, size = self.spinner_geometry(snap.mode)
        px = self.n(size)
        rect = pygame.Rect(self.x(cx) - px // 2, self.y(cy) - px // 2, px, px)
        surf.fill(COL_BG, rect)
        self.spinner(surf, cx, cy, size, self.spin_frame(snap))
        return rect

    # the usage screen's activity word under / beside the spinner: (x, baseline, size, align)
    WORD = {"landscape": (162, 362, 26, "c"), "portrait": (90, 158, 15, "c"),
            "bar": (132, 216, 28, "l"), "strip": (160, 556, 32, "c")}

    def draw_activity(self, surf, snap):
        """The spinner plus, on the usage screen, its word ("working..." with a
        shimmer running through it). Returns the rects it painted, so animation
        frames push just those."""
        rects = [self.draw_spinner(surf, snap)]
        if snap.mode != "usage":
            return rects
        x, base, size, align = self.WORD[self.layout]
        f = self.font(size)
        widest = max(f.size(w)[0] for w in ("working...", "needs you", "idle")) + 2
        left = self.x(x) - (widest // 2 if align == "c" else 0)
        rect = pygame.Rect(left, self.y(base) - f.get_ascent(), widest, f.get_height())
        surf.fill(COL_BG, rect)
        word, color = self.activity_word(snap)
        self.text(surf, word, x, base, size, color, align=align)
        frame = self.spin_frame(snap)
        if frame >= 0:
            self._shimmer(surf, word, x, base, size, align, frame / SPIN_FRAMES)
        rects.append(rect)
        return rects

    def _header_landscape(self, surf, now, brand, subtitle=None):
        if brand == "spotify":
            self.spotify_logo(surf, 58, 52, 26)
            title, color, sub = "Spotify", COL_SPOTIFY, "now playing"
        elif brand == "bambu":
            self.bambu_logo(surf, 38, 24, 58)
            title, color, sub = "Bambu Lab", COL_BAMBU, subtitle
        else:
            self.mascot(surf, 32, 28, 6)
            title, color, sub = "Claude Code", COL_ORANGE, "usage monitor"
        self.text(surf, title, 119, 56, 30, color, bold=True)
        self.text(surf, self.fit(sub, 420, 17), 119, 82, 17, COL_DIM)
        self.text(surf, clock_str(now), 768, 58, 30, COL_TEXT, align="r")
        self.text(surf, f"{now.strftime('%a %b')} {now.day}", 768, 82, 17, COL_DIM, align="r")
        surf.fill(COL_CARD, self.rect(32, 104, 736, 2))

    def _usage_landscape(self, surf, snap, now):
        self._header_landscape(surf, now, "usage")
        self.draw_activity(surf, snap)
        self._slide_landscape(surf, snap, now)

    def _slide_landscape(self, surf, snap, now):
        """The moving part (see _slide_bar): the bars column and session panel."""
        e = self.eased
        x0, x1 = 332 + 208 * e, 768
        if e > 0.02:
            self._session_panel(surf, snap, 306, 130, 540 - 24 - 306, 280, k=0.72,
                                visible=x0 - 24 - 306)
            surf.fill(COL_CARD, self.rect(x0 - 12, 128, 2, 256))
        moving = 0 < self.anim < 1
        for i, (label, key) in enumerate((("5-HOUR", "five"), ("WEEKLY", "week"))):
            top = 132 + i * 150
            pct, when, _ = snap.usage[key] if snap.usage else (None, None, "")
            self.text(surf, label, x0, top + 22, 20, COL_DIM, bold=True)
            self.text(surf, "--" if pct is None else f"{round(pct)}%", x1, top + 30, 44,
                      COL_TEXT, bold=True, align="r")
            if when:
                self.text(surf, self.reset_text(when, now, x1 - x0, 17, short=self.anim > 0),
                          x0, top + 50, 17, COL_DIM)
            self.bar(surf, self.rect(x0, top + 62, x1 - x0, 40),
                     None if pct is None else pct / 100, bar_color(pct), 12, fast=moving)

    def _usage_portrait(self, surf, snap, now):
        self.mascot(surf, 12, 17, 4)
        self.text(surf, "Claude Code", 72, 30, 16, COL_ORANGE, bold=True)
        self.text(surf, "usage monitor", 72, 47, 11, COL_DIM)
        self.draw_activity(surf, snap)
        surf.fill(COL_CARD, self.rect(12, 168, 156, 1))

        for i, (label, key) in enumerate((("5-HOUR", "five"), ("WEEKLY", "week"))):
            top = 178 + i * 68
            pct, when, _ = snap.usage[key] if snap.usage else (None, None, "")
            self.text(surf, label, 12, top + 12, 13, COL_DIM, bold=True)
            self.text(surf, "--" if pct is None else f"{round(pct)}%", 168, top + 13, 18,
                      COL_TEXT, bold=True, align="r")
            if when:
                self.text(surf, f"resets {fmt_reset(when, now)}", 12, top + 27, 10, COL_DIM)
            self.bar(surf, self.rect(12, top + 33, 156, 22),
                     None if pct is None else pct / 100, bar_color(pct), 6)

    def _spotify_landscape(self, surf, snap, now):
        self._header_landscape(surf, now, "spotify")
        np = snap.np
        if not np or not np["has_track"]:
            self.text(surf, "nothing playing" if np else "loading...", 400, 290, 28,
                      COL_DIM, align="c")
            return
        self.album_art(surf, self.rect(32, 130, 232, 232), snap)
        x0, x1 = 300, 768
        l1, l2 = self.wrap2(np["track"], x1 - x0, 34, bold=True)
        y = 166
        self.text(surf, l1, x0, y, 34, COL_TEXT, bold=True)
        if l2:
            y += 42
            self.text(surf, l2, x0, y, 34, COL_TEXT, bold=True)
        y += 40
        self.text(surf, self.fit(np["artist"], x1 - x0, 24), x0, y, 24, COL_SUB)
        self.text(surf, self.fit(np["album"], x1 - x0, 18), x0, y + 30, 18, COL_DIM)

        ms, dur = self.progress_ms(snap), np["duration_ms"]
        self.bar(surf, self.rect(x0, 322, x1 - x0, 12), ms / dur if dur else 0, COL_SPOTIFY, 6)
        self.text(surf, fmt_ms(ms), x0, 360, 16, COL_DIM)
        self.text(surf, fmt_ms(dur), x1, 360, 16, COL_DIM, align="r")
        self.play_state(surf, (x0 + x1) / 2, 344, 18, np["playing"])

    def _spotify_portrait(self, surf, snap, now):
        self.spotify_logo(surf, 34, 32, 20)
        self.text(surf, "Spotify", 72, 30, 16, COL_SPOTIFY, bold=True)
        self.text(surf, "now playing", 72, 47, 11, COL_DIM)
        surf.fill(COL_CARD, self.rect(12, 62, 156, 1))
        np = snap.np
        if not np or not np["has_track"]:
            self.text(surf, "nothing playing" if np else "loading...", 90, 180, 15,
                      COL_DIM, align="c")
            return
        l1, l2 = self.wrap2(np["track"], 156, 15, bold=True)
        y = 84
        self.text(surf, l1, 12, y, 15, COL_TEXT, bold=True)
        if l2:
            y += 19
            self.text(surf, l2, 12, y, 15, COL_TEXT, bold=True)
        y += 19
        self.text(surf, self.fit(np["artist"], 156, 12), 12, y, 12, COL_SUB)
        self.text(surf, self.fit(np["album"], 156, 10), 12, y + 15, 10, COL_DIM)

        self.album_art(surf, self.rect(48, 148, 84, 84), snap)
        ms, dur = self.progress_ms(snap), np["duration_ms"]
        self.text(surf, fmt_ms(ms), 12, 250, 10, COL_DIM)
        self.text(surf, fmt_ms(dur), 168, 250, 10, COL_DIM, align="r")
        self.bar(surf, self.rect(12, 256, 156, 8), ms / dur if dur else 0, COL_SPOTIFY, 4)
        self.play_state(surf, 90, 272, 14, np["playing"])

    # -- bar: 1480x320. Two rows - brand + 5-hour, activity + weekly - with
    # long meters, so it reads left to right at a glance.
    def _clock_bar(self, surf, now):
        self.text(surf, clock_str(now), 1452, 300, 22, COL_TEXT, bold=True, align="r")

    @staticmethod
    def activity_word(snap):
        if snap.thinking:
            return "working...", COL_ORANGE
        if any(s["state"] == "waiting" for s in snap.sessions):
            return "needs you", COL_YELLOW
        return "idle", COL_DIM

    def reset_text(self, when, now, room, size, short=False):
        """The reset line, shortened to fit `room` design units - or always
        short, so it doesn't flip between forms mid-slide."""
        full = f"resets {fmt_reset(when, now)}  ·  {fmt_until(when, now)}"
        for text in ((fmt_until(when, now),) if short else (full, fmt_until(when, now))):
            if self.width(text, size) <= room:
                return text
        return ""

    def _usage_bar(self, surf, snap, now):
        self.mascot(surf, 28, 42, 7)
        self.text(surf, "Claude Code", 124, 74, 30, COL_ORANGE, bold=True)
        self.text(surf, "usage monitor", 124, 99, 17, COL_DIM)
        self.draw_activity(surf, snap)
        surf.fill(COL_CARD, self.rect(346, 40, 2, 196))
        self._slide_bar(surf, snap, now)

    def _slide_bar(self, surf, snap, now):
        """The moving part. While Claude works the bars slide right and shrink
        toward the end of the screen, uncovering the session panel in the
        middle; they slide back when it's done. The panel is laid out at its
        full width and revealed, so its text never reflows mid-slide."""
        e = self.eased
        x0, xb, x1 = 380 + 620 * e, 1268, 1452  # label / bar start, bar end, % right edge
        if e > 0.02:
            self._session_panel(surf, snap, 380, 40, 1000 - 36 - 380, 200, visible=x0 - 36 - 380)
            surf.fill(COL_CARD, self.rect(x0 - 18, 40, 2, 196))
        moving = 0 < self.anim < 1
        for i, (label, key) in enumerate((("5-HOUR", "five"), ("WEEKLY", "week"))):
            top = 40 + i * 112
            pct, when, _ = snap.usage[key] if snap.usage else (None, None, "")
            self.text(surf, label, x0, top + 24, 22, COL_DIM, bold=True)
            if when:
                room = xb - x0 - self.width(label, 22, True) - 24
                self.text(surf, self.reset_text(when, now, room, 20, short=self.anim > 0),
                          xb, top + 24, 20, COL_DIM, align="r")
            self.bar(surf, self.rect(x0, top + 36, xb - x0, 46),
                     None if pct is None else pct / 100, bar_color(pct), 14, fast=moving)
            self.text(surf, "--" if pct is None else f"{round(pct)}%", x1, top + 79, 54,
                      COL_TEXT, bold=True, align="r")
        self._clock_bar(surf, now)

    def _spotify_bar(self, surf, snap, now):
        self._clock_bar(surf, now)
        art = self.rect(28, 28, 236, 236)
        np = snap.np
        if not np or not np["has_track"]:
            self.art_placeholder(surf, art)
            self.text(surf, "nothing playing" if np else "loading...", 300, 160, 34, COL_DIM)
            return
        self.album_art(surf, art, snap)
        x0, x1 = 300, 1452
        self.spotify_logo(surf, 1428, 54, 24)
        width = 1380 - x0  # leave the logo its corner
        self.text(surf, self.fit(np["track"], width, 44, bold=True), x0, 82, 44,
                  COL_TEXT, bold=True)
        self.text(surf, self.fit(np["artist"], width, 28), x0, 126, 28, COL_SUB)
        self.text(surf, self.fit(np["album"], width, 21), x0, 162, 21, COL_DIM)

        ms, dur = self.progress_ms(snap), np["duration_ms"]
        self.bar(surf, self.rect(x0, 192, x1 - x0, 16), ms / dur if dur else 0, COL_SPOTIFY, 8)
        self.text(surf, fmt_ms(ms), x0, 244, 21, COL_DIM)
        self.text(surf, fmt_ms(dur), x1, 244, 21, COL_DIM, align="r")
        self.play_state(surf, (x0 + x1) / 2, 224, 24, np["playing"])

    # -- strip: 320x1480, the bar standing up. Everything stacks, big.
    def _clock_strip(self, surf, now):
        surf.fill(COL_CARD, self.rect(24, 1170, 272, 2))
        self.text(surf, clock_str(now), 160, 1262, 52, COL_TEXT, bold=True, align="c")
        self.text(surf, f"{now.strftime('%a %b')} {now.day}", 160, 1302, 22, COL_DIM, align="c")

    def _usage_strip(self, surf, snap, now):
        self.mascot(surf, 82, 70, 13)
        self.text(surf, "Claude Code", 160, 232, 34, COL_ORANGE, bold=True, align="c")
        self.text(surf, "usage monitor", 160, 264, 20, COL_DIM, align="c")
        self.draw_activity(surf, snap)
        surf.fill(COL_CARD, self.rect(24, 600, 272, 2))

        for i, (label, key) in enumerate((("5-HOUR", "five"), ("WEEKLY", "week"))):
            top = 636 + i * 270
            pct, when, _ = snap.usage[key] if snap.usage else (None, None, "")
            self.text(surf, label, 24, top + 30, 24, COL_DIM, bold=True)
            self.text(surf, "--" if pct is None else f"{round(pct)}%", 296, top + 50, 64,
                      COL_TEXT, bold=True, align="r")
            if when:
                self.text(surf, f"resets {fmt_reset(when, now)}", 24, top + 92, 21, COL_DIM)
                self.text(surf, fmt_until(when, now), 24, top + 120, 21, COL_DIM)
            self.bar(surf, self.rect(24, top + 138, 272, 56),
                     None if pct is None else pct / 100, bar_color(pct), 16)
        self._clock_strip(surf, now)

    def _spotify_strip(self, surf, snap, now):
        self.spotify_logo(surf, 160, 96, 40)
        self.text(surf, "Spotify", 160, 192, 34, COL_SPOTIFY, bold=True, align="c")
        self.text(surf, "now playing", 160, 224, 20, COL_DIM, align="c")
        art = self.rect(24, 256, 272, 272)
        self._clock_strip(surf, now)
        np = snap.np
        if not np or not np["has_track"]:
            self.art_placeholder(surf, art)
            self.text(surf, "nothing playing" if np else "loading...", 160, 590, 26,
                      COL_DIM, align="c")
            return
        self.album_art(surf, art, snap)
        l1, l2 = self.wrap2(np["track"], 272, 28, bold=True)
        y = 580
        self.text(surf, l1, 24, y, 28, COL_TEXT, bold=True)
        if l2:
            y += 36
            self.text(surf, l2, 24, y, 28, COL_TEXT, bold=True)
        self.text(surf, self.fit(np["artist"], 272, 22), 24, y + 38, 22, COL_SUB)
        self.text(surf, self.fit(np["album"], 272, 18), 24, y + 68, 18, COL_DIM)

        ms, dur = self.progress_ms(snap), np["duration_ms"]
        self.bar(surf, self.rect(24, 730, 272, 12), ms / dur if dur else 0, COL_SPOTIFY, 6)
        self.text(surf, fmt_ms(ms), 24, 776, 19, COL_DIM)
        self.text(surf, fmt_ms(dur), 296, 776, 19, COL_DIM, align="r")
        self.play_state(surf, 160, 756, 24, np["playing"])


    # -- the Bambu Lab printer screen
    @staticmethod
    def printer_word(snap, v):
        return ("connecting...", COL_DIM) if snap.printer is None else (v.word, v.color)

    @staticmethod
    def no_job_text(snap):
        return "waiting for the printer..." if snap.printer is None else "no print running"

    def _bambu_bar(self, surf, snap, now):
        v = printer_view(snap.printer, now)
        self.bambu_logo(surf, 30, 38, 66)
        self.text(surf, "Bambu Lab", 98, 74, 30, COL_BAMBU, bold=True)
        self.text(surf, self.fit(snap.printer_name, 236, 17), 98, 99, 17, COL_DIM)
        word, color = self.printer_word(snap, v)
        self.text(surf, word, 28, 206, 30, color)
        if v.temps:
            self.text(surf, self.fit(v.temps, 310, 19), 28, 244, 19, COL_DIM)
        surf.fill(COL_CARD, self.rect(346, 40, 2, 196))

        x0, xb, x1 = 380, 1268, 1452  # text / bar start, bar end, % right edge
        if v.has_job:
            self.text(surf, self.fit(v.job or "print", xb - x0, 30, bold=True), x0, 72, 30,
                      COL_TEXT, bold=True)
            self.bar(surf, self.rect(x0, 92, xb - x0, 62),
                     None if v.pct is None else v.pct / 100, v.bar, 16)
            self.text(surf, "--" if v.pct is None else f"{v.pct}%", x1, 148, 60, COL_TEXT,
                      bold=True, align="r")
            if v.layers:
                self.text(surf, v.layers, x0, 206, 22, COL_DIM)
            right = "  ·  ".join(x for x in (v.left, v.eta) if x)
            if right:
                self.text(surf, right, xb, 206, 22, COL_DIM, align="r")
        else:
            self.text(surf, self.no_job_text(snap), x0, 150, 34, COL_DIM)
        self._clock_bar(surf, now)

    def _bambu_landscape(self, surf, snap, now):
        self._header_landscape(surf, now, "bambu", snap.printer_name)
        v = printer_view(snap.printer, now)
        if not v.has_job:
            self.text(surf, self.no_job_text(snap), 400, 270, 28, COL_DIM, align="c")
            if v.temps:
                self.text(surf, v.temps, 400, 312, 18, COL_DIM, align="c")
            return
        x0, x1 = 32, 768
        word, color = self.printer_word(snap, v)
        self.text(surf, self.fit(v.job or "print", 560, 32, bold=True), x0, 190, 32, COL_TEXT,
                  bold=True)
        self.text(surf, "--" if v.pct is None else f"{v.pct}%", x1, 194, 56, COL_TEXT,
                  bold=True, align="r")
        self.text(surf, word, x0, 228, 22, color)
        if v.temps:
            self.text(surf, v.temps, x1, 228, 18, COL_DIM, align="r")
        self.bar(surf, self.rect(x0, 252, x1 - x0, 52),
                 None if v.pct is None else v.pct / 100, v.bar, 14)
        if v.layers:
            self.text(surf, v.layers, x0, 342, 20, COL_DIM)
        right = "  ·  ".join(x for x in (v.left, v.eta) if x)
        if right:
            self.text(surf, right, x1, 342, 20, COL_DIM, align="r")

    def _bambu_portrait(self, surf, snap, now):
        self.bambu_logo(surf, 23, 12, 40)
        self.text(surf, "Bambu Lab", 72, 30, 16, COL_BAMBU, bold=True)
        self.text(surf, self.fit(snap.printer_name, 96, 11), 72, 47, 11, COL_DIM)
        surf.fill(COL_CARD, self.rect(12, 62, 156, 1))
        v = printer_view(snap.printer, now)
        if not v.has_job:
            self.text(surf, self.no_job_text(snap), 90, 170, 13, COL_DIM, align="c")
            if v.temps:
                self.text(surf, self.fit(v.temps, 156, 10), 90, 190, 10, COL_DIM, align="c")
            return
        word, color = self.printer_word(snap, v)
        l1, l2 = self.wrap2(v.job or "print", 156, 15, bold=True)
        y = 84
        self.text(surf, l1, 12, y, 15, COL_TEXT, bold=True)
        if l2:
            y += 19
            self.text(surf, l2, 12, y, 15, COL_TEXT, bold=True)
        self.text(surf, word, 12, y + 21, 13, color)
        self.text(surf, "--" if v.pct is None else f"{v.pct}%", 90, 188, 40, COL_TEXT,
                  bold=True, align="c")
        self.bar(surf, self.rect(12, 204, 156, 20),
                 None if v.pct is None else v.pct / 100, v.bar, 6)
        if v.layers:
            self.text(surf, v.layers, 12, 244, 10, COL_DIM)
        if v.left:
            self.text(surf, v.left, 168, 244, 10, COL_DIM, align="r")
        if v.eta:
            self.text(surf, v.eta, 12, 262, 10, COL_DIM)
        if v.temps:
            self.text(surf, self.fit(v.temps, 156, 10), 12, 280, 10, COL_DIM)

    def _bambu_strip(self, surf, snap, now):
        self.bambu_logo(surf, 113, 60, 122)
        self.text(surf, "Bambu Lab", 160, 232, 34, COL_BAMBU, bold=True, align="c")
        self.text(surf, self.fit(snap.printer_name, 272, 20), 160, 264, 20, COL_DIM, align="c")
        v = printer_view(snap.printer, now)
        word, color = self.printer_word(snap, v)
        self.text(surf, word, 160, 340, 30, color, align="c")
        if v.temps:
            self.text(surf, self.fit(v.temps, 272, 20), 160, 378, 20, COL_DIM, align="c")
        surf.fill(COL_CARD, self.rect(24, 420, 272, 2))
        if v.has_job:
            l1, l2 = self.wrap2(v.job or "print", 272, 28, bold=True)
            self.text(surf, l1, 24, 486, 28, COL_TEXT, bold=True)
            if l2:
                self.text(surf, l2, 24, 522, 28, COL_TEXT, bold=True)
            self.text(surf, "--" if v.pct is None else f"{v.pct}%", 160, 700, 96, COL_TEXT,
                      bold=True, align="c")
            self.bar(surf, self.rect(24, 736, 272, 56),
                     None if v.pct is None else v.pct / 100, v.bar, 16)
            for i, line in enumerate(x for x in (v.layers, v.left, v.eta) if x):
                self.text(surf, line, 160, 842 + i * 34, 21, COL_DIM, align="c")
        else:
            self.text(surf, self.no_job_text(snap), 160, 560, 24, COL_DIM, align="c")
        self._clock_strip(surf, now)


# ---------------------------------------------------------------- main

def forever(fn, *args):
    """Run a worker in a daemon thread, restarting it if it ever crashes."""
    def run():
        while True:
            try:
                fn(*args)
            except Exception:
                log(traceback.format_exc())
                time.sleep(10)
    threading.Thread(target=run, daemon=True, name=fn.__name__).start()


def rotate_rect(r, rotate, cw, ch):
    """Where rect r of a cw x ch canvas lands once the canvas is turned
    `rotate` degrees clockwise."""
    if rotate == 90:
        return pygame.Rect(ch - r.bottom, r.x, r.h, r.w)
    if rotate == 180:
        return pygame.Rect(cw - r.right, ch - r.bottom, r.w, r.h)
    if rotate == 270:
        return pygame.Rect(r.y, cw - r.right, r.h, r.w)
    return r


def open_screen(args, cfg):
    pygame.display.init()
    pygame.font.init()
    if args.windowed:
        screen = pygame.display.set_mode(parse_size(args.windowed) or (800, 480))
    else:
        screen = pygame.display.set_mode(cfg.size or (0, 0), pygame.FULLSCREEN)
        pygame.mouse.set_visible(False)
    pygame.display.set_caption("Claude Code usage")
    # pygame 2 lets the desktop's screensaver run by default; a wall display
    # should keep the screen on (install.sh turns blanking off too).
    pygame.display.set_allow_screensaver(False)
    return screen


def main():
    ap = argparse.ArgumentParser(description="Claude Code usage display for a Raspberry Pi.")
    ap.add_argument("--config", default=CONFIG_PATH, help=f"config file (default {CONFIG_PATH})")
    ap.add_argument("--windowed", metavar="WxH", help="run in a window, e.g. 800x480 or 480x800")
    ap.add_argument("--demo", action="store_true", help="fake data - no logins or network needed")
    ap.add_argument("--port", type=int, help="HTTP port (overrides config.ini)")
    ap.add_argument("--setup-bambu", action="store_true",
                    help="find your Bambu Lab printer, ask for its access code, save it")
    args = ap.parse_args()

    if args.setup_bambu:
        setup_bambu(args.config)
        return
    if pygame.version.vernum[0] < 2:
        sys.exit(f"needs pygame 2 (found {pygame.version.ver}) - pi/install.sh installs "
                 "it (on Bullseye: python3 -m pip install --user pygame)")
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # systemctl stop -> clean exit

    cfg = Config(args.config)
    if args.port:
        cfg.port = args.port
    state = StateFile(STATE_PATH)
    spotify_ready = args.demo or bool(cfg.sp_client_id and cfg.sp_refresh_token)
    if args.demo and not cfg.bambu_name:
        cfg.bambu_name = "demo P1S"
    model = Model(cfg, state, spotify_ready, bambu_ready=args.demo or cfg.bambu_ready)
    model.ip = local_ip()

    BeaconHandler.model = model
    try:
        server = ThreadingHTTPServer(("", cfg.port), BeaconHandler)
        threading.Thread(target=server.serve_forever, daemon=True, name="http").start()
        log(f"listening on port {cfg.port}")
    except OSError as e:
        log(f"can't listen on port {cfg.port}: {e}")
        model.flash(f"port {cfg.port} busy - beacons off", COL_RED, secs=60)

    if args.demo:
        forever(demo_worker, model)
    else:
        if not cfg.refresh_token:
            log(f"no [anthropic] refresh_token in {args.config} - "
                "run server/device_login.py --config " + args.config)
        forever(usage_worker, model,
                OAuthLogin("anthropic", cfg.refresh_token, state, anthropic_exchange, 28800))
        if spotify_ready:
            forever(spotify_worker, model,
                    OAuthLogin("spotify", cfg.sp_refresh_token, state,
                               spotify_exchange(cfg.sp_client_id), 3600))
        if cfg.bambu_ready:
            forever(bambu_worker, model)

    try:
        screen = open_screen(args, cfg)
    except pygame.error as e:
        sys.exit(f"could not open the screen: {e}\n(on Raspberry Pi OS Lite this needs "
                 "SDL_VIDEODRIVER=kmsdrm and the video/render/input groups - "
                 "pi/install.sh sets that up)")

    rotate = cfg.rotate if cfg.rotate in (90, 180, 270) else 0
    sw, sh = screen.get_size()
    canvas = pygame.Surface((sh, sw) if rotate in (90, 270) else (sw, sh)).convert() \
        if rotate else screen
    renderer = Renderer(canvas.get_size(), find_fonts())
    model.art_px = renderer.art_px
    renderer.warm_spinner()  # draw the spark's frames now, not mid-animation
    log(f"screen {sw}x{sh}, rotate {rotate}, {renderer.layout} layout")

    def present(*rects):
        """Push the canvas - or just these rects of it - to the screen."""
        if rotate:
            if not rects:
                screen.blit(pygame.transform.rotate(canvas, -rotate), (0, 0))
            turned = []
            for rect in rects:
                dest = rotate_rect(rect, rotate, *canvas.get_size())
                screen.blit(pygame.transform.rotate(canvas.subsurface(rect), -rotate), dest.topleft)
                turned.append(dest)
            rects = turned
        if rects:
            pygame.display.update(list(rects))
        else:
            pygame.display.flip()

    clock = pygame.time.Clock()
    last_key, last_frame, last_ip_check = None, -1, 0.0
    try:
        while True:
            for ev in pygame.event.get():
                if ev.type == pygame.QUIT:
                    return
                if ev.type == pygame.KEYDOWN:
                    if ev.key == pygame.K_q and ev.mod & pygame.KMOD_CTRL:
                        return
                    if ev.key == pygame.K_ESCAPE and args.windowed:
                        return
                    if ev.key in (pygame.K_SPACE, pygame.K_TAB, pygame.K_RETURN):
                        model.toggle_mode()
                elif ev.type == pygame.MOUSEBUTTONUP and ev.button == 1:
                    model.toggle_mode()  # touchscreens send taps as clicks

            mono = time.monotonic()
            if mono - last_ip_check > 10:  # DHCP may hand us a new address
                last_ip_check, model.ip = mono, local_ip()

            snap = model.snapshot()
            now = datetime.datetime.now().astimezone()
            moving = renderer.advance(snap)  # the session panel's slide
            key = renderer.scene_key(snap, now)
            frame = renderer.spin_frame(snap)
            if key != last_key:
                last_key, last_frame = key, frame
                renderer.draw(canvas, snap, now)
                present()
            else:  # repaint and push only what moved
                rects = [renderer.draw_slide(canvas, snap, now)] if moving else []
                if frame != last_frame:
                    last_frame = frame
                    rects += renderer.draw_activity(canvas, snap)
                if rects:
                    present(*rects)
            clock.tick(60 if moving else 30 if frame >= 0 else 10)
    finally:
        pygame.quit()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
