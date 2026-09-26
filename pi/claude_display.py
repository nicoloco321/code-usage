#!/usr/bin/env python3
"""Claude Code usage display - Raspberry Pi edition.

The ESP32 display (firmware/src/main.cpp) as a fullscreen app for a Raspberry
Pi - built for a Pi 2, fine on anything newer - on an HDMI monitor or the
official touchscreen:

  - your real 5-hour / weekly utilization, fetched straight from Anthropic's
    OAuth usage API with a dedicated login (server/device_login.py)
  - a spinner while Claude is working, driven by the same HTTP beacons
    (POST /thinking/on, /thinking/off) from Claude Code hooks or beacon.py
  - optional screens for Spotify's now playing, a Bambu Lab print's
    progress, the planes flying overhead, and Formula 1 (POST /mode/spotify,
    /mode/bambu, /mode/planes, /mode/f1, /mode/usage, /mode/toggle - or tap
    the screen)

It speaks the firmware's HTTP API on the same port, so the hooks, beacon.py,
find_display.py and the /switch command work unchanged - point them at the Pi.
It also answers GET /usage with the numbers as JSON (the Windows tray helper
reads that).

    python3 pi/claude_display.py                      # fullscreen
    python3 pi/claude_display.py --windowed 800x480   # in a window, for testing
    python3 pi/claude_display.py --demo               # fake data, no logins needed
    python3 pi/claude_display.py --setup-bambu        # add your Bambu Lab printer
    python3 pi/claude_display.py --setup-planes       # your location, for planes overhead

Settings live in ~/.config/claude-display/config.ini (see config.example.ini);
pi/install.sh sets everything up to start fullscreen on boot. Needs pygame 2
(sudo apt install python3-pygame); everything else is the standard library.

Keys: tap / click / space switches screens, Ctrl+Q quits (Esc too, windowed).
"""

import argparse
import base64
import bisect
import configparser
import csv
import datetime
import gzip
import hashlib
import html
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
CACHE_DIR = os.path.expanduser("~/.cache/claude-display")  # reference data, re-fetched monthly
LIVERY_DIR = os.path.expanduser("~/.local/share/claude-display/liveries")  # pi/build_liveries.py

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"  # Claude Code's public client
USER_AGENT = "claude-usage-display/1.0"  # Cloudflare 1010-blocks urllib's default UA

SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_NOW_URL = ("https://api.spotify.com/v1/me/player/currently-playing"
                   "?additional_types=episode")

ROOT_TEXT = ("Claude Code usage display (Raspberry Pi). POST /thinking/on while "
             "working, /thinking/off when done. POST /mode/usage, /mode/spotify, "
             "/mode/bambu, /mode/planes, /mode/f1 or /mode/toggle to switch screens; GET /mode to ask; "
             "GET /planes/log for every plane the planes screen has shown; "
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
COL_SPOTIFY = (30, 215, 96)   # Spotify green (#1ED760)
COL_BAMBU = (35, 165, 67)     # Bambu Lab green
COL_EYE = (0, 0, 0)

# The screens, in the order a tap or /mode/toggle cycles through them.
MODES = ("usage", "spotify", "bambu", "planes", "f1")

# The Spotify mark's three strokes, measured off the logo, in units of the
# circle's radius from its centre: (start, bend, end, width at start, at end).
SPOTIFY_ARCS = [
    ((-0.59, -0.335), (0.02, -0.4765), (0.63, -0.20), 0.185, 0.205),
    ((-0.54, 0.0), (-0.015, -0.1355), (0.51, 0.115), 0.155, 0.172),
    ((-0.516, 0.29), (-0.058, 0.185), (0.40, 0.40), 0.121, 0.138),
]

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
        self.planes_lat = num("planes", "lat", 0) if s("planes", "lat") else None
        self.planes_lon = num("planes", "lon", 0) if s("planes", "lon") else None
        self.planes_radius = max(2.0, min(60.0, num("planes", "radius_nm", 15)))
        self.planes_poll = max(5.0, num("planes", "poll_seconds", 10))
        self.planes_liveries = os.path.expanduser(s("planes", "liveries") or LIVERY_DIR)
        self.f1_enabled = s("f1", "enabled").lower() not in ("no", "false", "off", "0")

    @property
    def planes_ready(self):
        return self.planes_lat is not None and self.planes_lon is not None

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

    def __init__(self, cfg, state, spotify_ready, bambu_ready=False, planes_ready=False,
                 f1_ready=False):
        self.cfg, self.state = cfg, state
        self.ready = {"usage": True, "spotify": spotify_ready, "bambu": bambu_ready,
                      "planes": planes_ready, "f1": f1_ready}
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
        self.sky = None            # planes overhead: see planes_worker
        self.planes_status = None
        self.f1 = None             # the F1 screen: see f1_worker
        self.f1_status = None
        self.remote_sessions = {}  # sender -> (monotonic time, [session summaries])

        self.last_beacon = 0.0     # monotonic time of the last "thinking" ping, 0 = off
        self.flash_msg = None      # (text, colour, until): short-lived status override
        self.host = socket.gethostname().split(".")[0]
        self.ip = ""
        self.usage_wake = threading.Event()
        self.spotify_wake = threading.Event()
        self.bambu_wake = threading.Event()
        self.planes_wake = threading.Event()
        self.f1_wake = threading.Event()

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
            if mode == "planes" and self.sky is None:
                self.planes_status = ("looking for planes...", COL_DIM)
            if mode == "f1" and self.f1 is None:
                self.f1_status = ("loading the F1 season...", COL_DIM)
        self.state.put("mode", mode)
        if mode == "spotify":
            self.spotify_wake.set()
        elif mode == "bambu":
            self.bambu_wake.set()
        elif mode == "planes":
            self.planes_wake.set()
        elif mode == "f1":
            self.f1_wake.set()
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

    def set_sky(self, sky):
        n = len(sky["planes"])
        with self.lock:
            self.sky = sky
            self.planes_status = (f"{n} plane{'s' if n != 1 else ''} within "
                                  f"{sky['radius']:.0f} nm  ·  data: {sky.get('source') or '?'}",
                                  COL_GREEN)

    def set_f1(self, view):
        with self.lock:
            self.f1 = view
            self.f1_status = (("F1 live timing" if view.get("phase") == "live"
                               else "F1 schedule from OpenF1"), COL_GREEN)

    def set_f1_status(self, text, color):
        with self.lock:
            self.f1_status = (text, color)

    def drop_plane_photo(self):
        with self.lock:
            if self.sky and self.sky.get("photo"):
                self.sky = dict(self.sky, photo=None)

    def set_planes_status(self, text, color):
        with self.lock:
            self.planes_status = (text, color)

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
                sky=self.sky, planes_status=self.planes_status,
                f1=self.f1, f1_status=self.f1_status,
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
    job = job.replace("_", " ").strip()  # file-style names read better with spaces
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


# ---------------------------------------------------------------- planes overhead
#
# Live positions: adsb.fi's open data (personal use, cite adsb.fi; 1 request a
# second allowed), with adsb.lol (ODbL) standing in when it's down. Routes:
# adsb.im, then adsbdb - both can be stale for a reused flight number, so a
# route only counts when the plane is actually near one of its legs. Airline
# and aircraft-type names come from two reference files cached in CACHE_DIR.
# Photos: planespotters.net, whose terms want the photographer credited next
# to the photo, a QR code of the photo's page on a screen nobody can click,
# and the image kept in memory only while it's on screen - never on disk.

PLANES_FEEDS = (
    ("adsb.fi", "https://opendata.adsb.fi/api/v3/lat/{lat:.4f}/lon/{lon:.4f}/dist/{radius}"),
    ("adsb.lol", "https://api.adsb.lol/v2/point/{lat:.4f}/{lon:.4f}/{radius}"),
)
ROUTESET_URL = "https://adsb.im/api/0/routeset"
ROUTE_URL = "https://api.adsbdb.com/v0/callsign/{callsign}"
AIRLINES_URL = ("https://raw.githubusercontent.com/vradarserver/standing-data/main/"
                "airlines/schema-01/airlines.csv")
TYPES_URL = "https://raw.githubusercontent.com/wiedehopf/tar1090-db/master/db/icao_aircraft_types2.js"
PHOTO_HEX_URL = "https://api.planespotters.net/pub/photos/hex/{hex}"
PHOTO_REG_URL = "https://api.planespotters.net/pub/photos/reg/{reg}"
# planespotters wants a way to reach whoever runs a client in its User-Agent
PLANES_USER_AGENT = "claude-usage-display/1.0 (+https://github.com/nicoloco321/code-usage)"
NM_PER_MILE = 0.868976
EARTH_NM = 3440.065
ROUTE_TTL, UNKNOWN_TTL, PHOTO_TTL = 6 * 3600, 30 * 60, 24 * 3600

# A top-down airliner, nose up, in units of its half-length - the radar's plane.
PLANE_SHAPE = [(0, -1), (0.1, -0.86), (0.12, -0.3), (0.95, 0.12), (0.95, 0.26),
               (0.12, 0.08), (0.09, 0.6), (0.38, 0.82), (0.38, 0.94), (0, 0.86),
               (-0.38, 0.94), (-0.38, 0.82), (-0.09, 0.6), (-0.12, 0.08), (-0.95, 0.26),
               (-0.95, 0.12), (-0.12, -0.3), (-0.1, -0.86)]
SQUAWKS = {"7500": "hijack squawk 7500", "7600": "radio failure (7600)",
           "7700": "emergency squawk 7700"}


def distance_nm(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_NM * math.asin(min(1.0, math.sqrt(a)))


def bearing_deg(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return math.degrees(math.atan2(y, x)) % 360


def compass(deg):
    return ("N", "NE", "E", "SE", "S", "SW", "W", "NW")[int((deg % 360 + 22.5) // 45) % 8]


def get_json(url, body=None, timeout=8):
    """GET (or POST `body` as JSON) and parse the reply: (status, data or
    None). Network errors raise."""
    headers = {"User-Agent": PLANES_USER_AGENT, "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    code, raw, _ = http(url, data=data, headers=headers, timeout=timeout)
    try:
        return code, json.loads(raw) if raw else None
    except ValueError:  # rate-limit and error pages come back as HTML
        return code, None


def fetch_planes(lat, lon, radius_nm, resting=None):
    """(feed name, aircraft within radius_nm of (lat, lon) as plain dicts).
    A feed that fails or rate-limits rests for a minute (in `resting`)
    while the other one stands in - neither is ever retried straight away."""
    resting = {} if resting is None else resting
    error = None
    for name, template in PLANES_FEEDS:
        if resting.get(name, 0) > time.monotonic():
            continue
        try:
            code, data = get_json(template.format(lat=lat, lon=lon, radius=max(1, round(radius_nm))))
        except OSError as e:
            code, data, error = None, None, e
        if code == 200 and isinstance(data, dict) and isinstance(data.get("ac"), list):
            return name, [p for p in (parse_plane(a, lat, lon) for a in data["ac"]) if p]
        if code is not None:
            error = OSError(f"{name}: HTTP {code}")
        resting[name] = time.monotonic() + 60
    raise error or OSError("both plane feeds are resting after errors")


def parse_plane(a, home_lat, home_lon):
    """One aircraft from the feed (readsb's format), or None without a position."""
    if not isinstance(a, dict):
        return None

    def num(*keys):
        return next((a[k] for k in keys if isinstance(a.get(k), (int, float))
                     and not isinstance(a.get(k), bool)), None)

    lat, lon = num("lat"), num("lon")
    if lat is None or lon is None:
        return None
    alt = num("alt_baro")
    raw_hex = str(a.get("hex") or "").lower()
    text = lambda k: str(a.get(k) or "").strip()
    return {
        "hex": raw_hex.lstrip("~"),
        "icao": not raw_hex.startswith("~"),  # "~" marks an address that isn't an ICAO hex
        "callsign": text("flight").upper(), "reg": text("r").upper(), "type": text("t").upper(),
        "desc": text("desc"), "year": text("year"),  # these two only from adsb.fi
        "lat": lat, "lon": lon,
        "alt": None if alt is None else max(0, int(alt)),  # baro reads < 0 on a high-pressure day
        "gs": num("gs"), "track": num("track", "true_heading", "mag_heading"),
        "rate": num("baro_rate", "geom_rate"), "squawk": text("squawk"),
        "seen": num("seen_pos"),
        "ground": a.get("alt_baro") == "ground",
        "dist": distance_nm(home_lat, home_lon, lat, lon),
        "bearing": bearing_deg(home_lat, home_lon, lat, lon),
    }


def airport(a):
    """adsb.im and adsbdb name the same things differently."""
    a = a if isinstance(a, dict) else {}
    lat, lon = a.get("lat", a.get("latitude")), a.get("lon", a.get("longitude"))
    return {"iata": a.get("iata") or a.get("iata_code") or "",
            "icao": a.get("icao") or a.get("icao_code") or "",
            "name": a.get("name") or "", "city": a.get("location") or a.get("municipality") or "",
            "lat": lat if isinstance(lat, (int, float)) else None,
            "lon": lon if isinstance(lon, (int, float)) else None}


def lookup_route(callsign, lat, lon):
    """The airports this callsign's flight stops at, in order, or None if
    nobody knows it. Whether it fits today's flight is route_leg's call."""
    try:
        code, data = get_json(ROUTESET_URL, {"planes": [{"callsign": callsign, "lat": lat, "lng": lon}]})
        r = data[0] if code == 200 and isinstance(data, list) and data else None
    except OSError:
        r = None
    if isinstance(r, dict) and isinstance(r.get("_airports"), list) and len(r["_airports"]) > 1:
        return {"airports": [airport(a) for a in r["_airports"]], "airline": "", "airline_iata": ""}
    code, data = get_json(ROUTE_URL.format(callsign=urllib.parse.quote(callsign)))
    fr = data.get("response") if isinstance(data, dict) else None
    fr = fr.get("flightroute") if isinstance(fr, dict) else None
    if not isinstance(fr, dict):
        if code in (200, 404):  # 404 = "unknown callsign"
            return None
        raise OSError(f"adsbdb: HTTP {code}")
    airline = fr.get("airline") if isinstance(fr.get("airline"), dict) else {}
    stops = [fr.get(k) for k in ("origin", "midpoint", "destination")]
    return {"airports": [airport(a) for a in stops if isinstance(a, dict)],
            "airline": airline.get("name") or "", "airline_iata": airline.get("iata") or ""}


def leg_offset(a, b, lat, lon):
    """How far (nm) the point is from the great-circle leg a -> b."""
    d13 = distance_nm(a["lat"], a["lon"], lat, lon)
    d12 = distance_nm(a["lat"], a["lon"], b["lat"], b["lon"])
    if d12 < 1:
        return d13
    t = math.radians(bearing_deg(a["lat"], a["lon"], lat, lon)
                     - bearing_deg(a["lat"], a["lon"], b["lat"], b["lon"]))
    xt = math.asin(max(-1.0, min(1.0, math.sin(d13 / EARTH_NM) * math.sin(t))))
    along = math.acos(max(-1.0, min(1.0, math.cos(d13 / EARTH_NM) / math.cos(xt)))) * EARTH_NM
    if math.cos(t) < 0 or along > d12:  # beyond an end: how far from the nearer end
        return min(d13, distance_nm(b["lat"], b["lon"], lat, lon))
    return abs(xt) * EARTH_NM


def route_leg(airports, lat, lon, track=None):
    """The leg of a (maybe multi-stop) route this plane is flying, as
    (origin, dest, progress 0..1, nm to go) - or None when it's nowhere near
    any leg, which means the route data doesn't fit today's flight."""
    best = None
    for a, b in zip(airports, airports[1:]):
        if None in (a["lat"], a["lon"], b["lat"], b["lon"]):
            continue
        leg = distance_nm(a["lat"], a["lon"], b["lat"], b["lon"])
        off = leg_offset(a, b, lat, lon)
        if off > max(50.0, 0.2 * leg):
            continue
        score = off
        if track is not None:  # at a stop both legs are close: take the one it's flying
            turn = abs((bearing_deg(lat, lon, b["lat"], b["lon"]) - track + 180) % 360 - 180)
            score += 25 if turn > 100 else 0
        flown = distance_nm(a["lat"], a["lon"], lat, lon)
        left = distance_nm(lat, lon, b["lat"], b["lon"])
        if best is None or score < best[0]:
            best = (score, a, b, flown / (flown + left) if flown + left else 0.0, left)
    return best and best[1:]


def airline_code(callsign):
    """"UAL1234" -> "UAL"; "" for registrations and other non-airline callsigns."""
    return callsign[:3] if re.match(r"^[A-Z]{3}\d", callsign or "") else ""


def cached_download(url, name, max_age=30 * 86400):
    """A reference file, kept in CACHE_DIR and fetched again once it's a
    month old - a stale copy beats none when the fetch fails."""
    path = os.path.join(CACHE_DIR, name)
    try:
        fresh = time.time() - os.path.getmtime(path) < max_age
    except OSError:
        fresh = None  # no copy yet
    if not fresh:
        try:
            code, raw, _ = http(url, headers={"User-Agent": PLANES_USER_AGENT}, timeout=20)
            if code != 200 or not raw:
                raise OSError(f"{name}: HTTP {code}")
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(path + ".tmp", "wb") as f:
                f.write(raw)
            os.replace(path + ".tmp", path)
            return raw
        except OSError:
            if fresh is None:
                raise
    with open(path, "rb") as f:
        return f.read()


def load_airlines():
    """Airline ICAO code -> {"name", "iata"}, from Virtual Radar Server's
    standing data (about 180 KB; fresher IATA codes than adsbdb's)."""
    text = cached_download(AIRLINES_URL, "airlines.csv").decode("utf-8-sig")
    airlines = {}
    for row in csv.DictReader(io.StringIO(text)):
        icao = (row.get("ICAO") or "").strip().upper()
        if icao and icao not in airlines:
            airlines[icao] = {"name": (row.get("Name") or "").strip(),
                              "iata": (row.get("IATA") or "").strip()}
    return airlines


def load_type_names():
    """ICAO type code -> "BOEING 737 MAX 8", from tar1090-db (about 33 KB).
    Only needed when the feed didn't say (adsb.lol doesn't)."""
    raw = cached_download(TYPES_URL, "aircraft_types.json.gz")
    if raw[:2] == b"\x1f\x8b":  # it's gzip despite the name
        raw = gzip.decompress(raw)
    data = json.loads(raw)
    return {k: v[0] for k, v in data.items() if isinstance(v, list) and v and isinstance(v[0], str)}


def nice_model(desc):
    """"BOEING 737 MAX 8" -> "Boeing 737 MAX 8": the registry shouts the
    maker's name; the model part is fine as it is."""
    words = (desc or "").split()
    for i, w in enumerate(words):
        if w == "DE":
            words[i] = "de"  # de Havilland
        elif w.isalpha() and w.isupper() and len(w) > 3:
            words[i] = "Mc" + w[2:].capitalize() if w.startswith("MC") else w.capitalize()
        else:
            break
    return " ".join(words)


# Regional airlines fly in a mainline brand's livery. These fly for just one;
# the shared ones (SkyWest, Republic, Air Wisconsin...) fly for several, and
# the planespotters page of the airframe says which ("n240jq-delta-connection-...").
REGIONAL_BRANDS = {"EDV": "delta-connection", "ENY": "american-eagle", "PDT": "american-eagle",
                   "JIA": "american-eagle", "ASH": "united-express", "GJS": "united-express",
                   "UCA": "united-express", "QXE": "ASA"}
SHARED_REGIONALS = {"SKW", "RPA", "AWI", "CPZ"}
BRAND_WORDS = (("united-express", "united-express"), ("delta-connection", "delta-connection"),
               ("american-eagle", "american-eagle"), ("alaska", "ASA"))


def load_liveries(folder):
    """The aircraft art pi/build_liveries.py made: its index.json plus the
    folder it's in, or None if it hasn't been built."""
    try:
        with open(os.path.join(folder, "index.json"), encoding="utf-8") as f:
            index = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(index, dict):
        return None
    return {"dir": folder, "liveries": index.get("liveries") or {}, "blanks": index.get("blanks") or {}}


def livery_brand(callsign, photo_link=""):
    """Whose paint this flight wears: the airline's code, a regional brand
    like united-express, or "" if there's no telling."""
    code = airline_code(callsign)
    if code in SHARED_REGIONALS:
        slug = (photo_link or "").lower()
        return next((brand for word, brand in BRAND_WORDS if word in slug), "")
    return REGIONAL_BRANDS.get(code, code)


def plane_art_for(liveries, brand, type_code):
    """The illustration for this plane: its airline's livery on this exact
    type, else the type unpainted - {"path", "kind"} - or None, and it's the
    planespotters photo instead."""
    if not liveries or not type_code:
        return None
    entry, kind = (liveries["liveries"].get(brand) or {}).get(type_code) if brand else None, "livery"
    if not entry:
        entry, kind = liveries["blanks"].get(type_code), "blank"
    if not isinstance(entry, dict) or not entry.get("file"):
        return None
    path = os.path.join(liveries["dir"], os.path.basename(entry["file"]))
    return {"path": path, "kind": kind} if os.path.exists(path) else None


def photo_key(plane):
    """What planespotters knows this airframe by: its hex, or failing a real
    ICAO address, its registration."""
    if plane["icao"] and plane["hex"]:
        return "hex", plane["hex"]
    return ("reg", plane["reg"]) if plane["reg"] else None


def lookup_photo(key):
    """planespotters' newest photo of this very airframe: {"src", "link",
    "photographer"}, or None if nobody has photographed it yet."""
    kind, value = key
    url = (PHOTO_HEX_URL if kind == "hex" else PHOTO_REG_URL).format(
        **{kind: urllib.parse.quote(value)})
    code, data = get_json(url)
    if code != 200 or not isinstance(data, dict):
        raise OSError(f"planespotters: HTTP {code}")
    photos = data.get("photos")
    if data.get("error") or not isinstance(photos, list) or not photos or not isinstance(photos[0], dict):
        return None
    p = photos[0]
    img = p.get("thumbnail_large") or p.get("thumbnail")
    if not (isinstance(img, dict) and img.get("src") and p.get("link")):
        return None
    return {"src": img["src"], "link": p["link"], "photographer": str(p.get("photographer") or "unknown")}


def download_photo(info):
    """The JPEG itself - kept in memory while it's on screen, never saved."""
    code, body, _ = http(info["src"], headers={"User-Agent": PLANES_USER_AGENT}, timeout=10)
    if code != 200 or not body:
        raise OSError(f"photo download: HTTP {code}")
    return body


# Every plane the screen shows, and what picture it got, so the gaps in the
# art can be filled: GET /planes/log is the summary, /planes/log.csv the lot.
PLANES_LOG = os.path.join(os.path.dirname(STATE_PATH), "planes_log.csv")
PLANES_LOG_FIELDS = ["time", "hex", "callsign", "reg", "type", "model", "livery", "airline",
                     "picture", "art", "from", "to"]
PICTURE_WORDS = {"livery": "its livery", "blank": "blank livery", "photo": "photo", "none": "nothing"}


def log_plane(path, row):
    """Add one plane to the log; past 4 MB the log moves to .old and starts over."""
    try:
        new = not os.path.exists(path)
        if not new and os.path.getsize(path) > 4_000_000:
            os.replace(path, path + ".old")
            new = True
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, PLANES_LOG_FIELDS, extrasaction="ignore")
            if new:
                w.writeheader()
            w.writerow(row)
    except OSError as e:
        log(f"planes log: {e}")


def read_plane_log(path):
    """Everything logged, oldest first (the .old part too)."""
    rows = []
    for part in (path + ".old", path):
        try:
            with open(part, newline="", encoding="utf-8", errors="replace") as f:
                rows += [r for r in csv.DictReader(f) if r.get("hex") is not None]
        except (OSError, csv.Error) as e:  # a damaged log shouldn't take the page down
            if not isinstance(e, FileNotFoundError):
                log(f"planes log {part}: {e}")
    return rows


def plane_log_groups(rows, by):
    """Sightings grouped by the fields in `by`, most seen first. The latest
    sighting says what picture the group gets now (art may have been added)."""
    groups = {}
    for r in rows:
        g = groups.setdefault(tuple(r.get(k) or "" for k in by),
                              {"seen": 0, "last": "", "examples": [], "airlines": []})
        g["seen"] += 1
        g.update({k: v for k, v in r.items() if v})
        g["last"] = r.get("time") or g["last"]
        for key, value in (("examples", r.get("reg") or r.get("callsign")), ("airlines", r.get("airline"))):
            if value and value not in g[key]:
                g[key] = (g[key] + [value])[-4:]
    return sorted(groups.values(), key=lambda g: (-g["seen"], g.get("livery", ""), g.get("type", "")))


def planes_log_html(rows):
    """The log as a page: what's missing art first, then everything recent."""
    esc = lambda v: html.escape(str(v or ""))
    when = lambda t: esc((t or "").replace("T", " ")[:16])
    tag = lambda k: f'<span class="tag {esc(k)}">{esc(PICTURE_WORDS.get(k, k))}</span>'
    out = ['<!doctype html><html><head><meta charset="utf-8">',
           '<meta name="viewport" content="width=device-width, initial-scale=1">',
           "<title>Planes log</title><style>",
           ":root{color-scheme:dark}body{background:#121212;color:#e6e6e6;margin:24px;",
           "font:15px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}",
           "h1{color:#d97757;font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:30px 0 4px}",
           "p{color:#8a8a8a;margin:0 0 12px}a{color:#9fc3e8}",
           "table{border-collapse:collapse;width:100%;max-width:1150px}",
           "th,td{text-align:left;padding:6px 10px;border-bottom:1px solid #262626;vertical-align:top}",
           "th{color:#8a8a8a;font-size:13px;font-weight:600}td.n{text-align:right;font-variant-numeric:tabular-nums}",
           "code{color:#b5c7d6;white-space:nowrap}.tag{padding:1px 8px;border-radius:9px;font-size:12px;white-space:nowrap}",
           ".livery{background:#1d3a22;color:#86d392}.blank{background:#3a3220;color:#e3c565}",
           ".photo{background:#3d2420;color:#ec9478}.none{background:#2e2e2e;color:#aaa}",
           "@media(max-width:700px){.wide{display:none}}</style></head><body>",
           "<h1>Planes overhead: the log</h1>"]
    if not rows:
        out.append("<p>Nothing yet - planes land here as the planes screen shows them.</p></body></html>")
        return "".join(out)
    hexes = {r.get("hex") for r in rows}
    out.append(f"<p>{len(rows)} sightings of {len(hexes)} planes since {when(rows[0].get('time'))}"
               ' &middot; <a href="/planes/log.csv">download the CSV</a></p>')
    groups = plane_log_groups(rows, ("livery", "type"))
    blank = [g for g in groups if g.get("picture") == "blank" and g.get("livery")]  # (private: blank is right)
    out.append(f"<h2>Blank livery shown: no art of the airline's livery on this type ({len(blank)})</h2>"
               "<p>Add these to LIVERIES in pi/build_liveries.py as (livery, type).</p>")
    out.append("<table><tr><th>Seen</th><th>(livery, type)</th><th>Airline</th><th>Aircraft</th>"
               '<th class="wide">Last seen</th><th class="wide">For example</th></tr>' if blank else
               "<p><i>None so far.</i></p><table>")
    for g in blank:
        out.append(f'<tr><td class="n">{g["seen"]}</td><td><code>("{esc(g.get("livery"))}", '
                   f'"{esc(g.get("type"))}")</code></td><td>{esc(", ".join(g["airlines"]) or "-")}</td>'
                   f'<td>{esc(g.get("model"))}</td><td class="wide">{when(g["last"])}</td>'
                   f'<td class="wide">{esc(", ".join(g["examples"]))}</td></tr>')
    out.append("</table>")
    latest = {r.get("type"): r.get("picture") for r in rows}  # (art may have been added since)
    missing = [g for g in plane_log_groups([r for r in rows if r.get("picture") in ("photo", "none")],
                                           ("type",))
               if latest.get(g.get("type")) in ("photo", "none")]
    out.append(f"<h2>No illustration of the type at all ({len(missing)})</h2>"
               "<p>Add these to BLANKS (or LIVERIES) in pi/build_liveries.py.</p>")
    out.append("<table><tr><th>Seen</th><th>Type</th><th>Aircraft</th><th>Showed</th><th>Airlines</th>"
               '<th class="wide">Last seen</th><th class="wide">For example</th></tr>' if missing else
               "<p><i>None so far.</i></p><table>")
    for g in missing:
        out.append(f'<tr><td class="n">{g["seen"]}</td><td><code>"{esc(g.get("type") or "?")}"</code></td>'
                   f'<td>{esc(g.get("model"))}</td><td>{tag(g.get("picture"))}</td>'
                   f'<td>{esc(", ".join(g["airlines"]) or "-")}</td><td class="wide">{when(g["last"])}</td>'
                   f'<td class="wide">{esc(", ".join(g["examples"]))}</td></tr>')
    out.append("</table><h2>Latest 150</h2><table><tr><th>When</th><th>Flight</th><th>Aircraft</th>"
               '<th>Picture</th><th class="wide">Airline</th><th class="wide">Route</th></tr>')
    for r in reversed(rows[-150:]):
        route = f'{esc(r.get("from"))} &rarr; {esc(r.get("to"))}' if r.get("to") else ""
        out.append(f'<tr><td>{when(r.get("time"))}</td><td>{esc(r.get("callsign") or r.get("reg"))}</td>'
                   f'<td>{esc(r.get("model") or r.get("type"))} <code>{esc(r.get("reg"))}</code></td>'
                   f'<td>{tag(r.get("picture"))}</td><td class="wide">{esc(r.get("airline"))}</td>'
                   f'<td class="wide">{route}</td></tr>')
    out.append("</table></body></html>")
    return "\n".join(out)


def planes_log_csv(rows):
    buf = io.StringIO()
    w = csv.DictWriter(buf, PLANES_LOG_FIELDS, extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()


def planes_worker(model):
    """While the planes screen is up, follow the nearest airborne aircraft."""
    cfg = model.cfg
    home = (cfg.planes_lat, cfg.planes_lon)
    trails, resting = {}, {}
    cache = {}  # (kind, key) -> (expires, value); a value of None = nobody knows
    refs = {"airlines": None, "types": None, "retry": 0.0}  # the two reference files
    focus = shown = None  # shown: (photo key, info, jpeg) of the plane on screen
    logged = None         # the plane last written to the log
    liveries = load_liveries(cfg.planes_liveries)
    log(f"plane art: {sum(map(len, liveries['liveries'].values()))} liveries, "
        f"{len(liveries['blanks'])} blank types" if liveries else
        f"plane art: none in {cfg.planes_liveries} - photos only (see pi/build_liveries.py)")

    def remember(kind, key, value, ttl):
        cache[(kind, key)] = (time.monotonic() + ttl, value)
        if len(cache) > 500:
            now = time.monotonic()
            for k in [k for k, (t, _) in cache.items() if t < now] or list(cache)[:100]:
                del cache[k]

    def recall(kind, key):
        """(known, value)"""
        hit = cache.get((kind, key))
        return (True, hit[1]) if hit and hit[0] > time.monotonic() else (False, None)

    while True:
        if model.mode != "planes":
            shown = None  # planespotters: in memory only while it's on screen
            model.drop_plane_photo()
            model.planes_wake.wait()
            model.planes_wake.clear()
            continue
        try:
            source, planes = fetch_planes(*home, cfg.planes_radius, resting)
        except Exception as e:
            log(f"plane feed: {e}")
            model.set_planes_status("plane feed unreachable - retrying", COL_RED)
            model.planes_wake.wait(20)
            model.planes_wake.clear()
            continue
        planes = [p for p in planes if not p["ground"] and (p["seen"] is None or p["seen"] <= 30)]
        seen = {p["hex"] for p in planes}
        for hex_id in [h for h in trails if h not in seen]:
            del trails[hex_id]
        for p in planes:
            trail = trails.setdefault(p["hex"], [])
            if not trail or trail[-1] != (p["lat"], p["lon"]):
                trail.append((p["lat"], p["lon"]))
                del trail[:-40]
        # the nearest plane - but don't flip back and forth between two
        nearest = min(planes, key=lambda p: p["dist"], default=None)
        current = next((p for p in planes if p["hex"] == focus), None)
        if current is None or (nearest and nearest["dist"] < current["dist"] - 1.0):
            current = nearest
        focus = current and current["hex"]
        cs = current["callsign"] if current else ""
        pkey = photo_key(current) if current else None
        if shown and shown[0] != pkey:
            shown = None
        # shared regionals: which brand they fly for comes from the photo's page
        needs_page = airline_code(cs) in SHARED_REGIONALS and pkey is not None

        def art_now():
            """The illustration to show, None while it depends on a lookup
            still to come, or False if there's none (so: the photo)."""
            known, info = recall("photo", pkey) if pkey else (True, None)
            if needs_page and not known:
                return None
            art = plane_art_for(liveries, livery_brand(cs, (info or {}).get("link")), current["type"])
            return art or False

        def publish():
            info = {}
            if current:
                types = refs["types"] or {}
                art = art_now()
                info = {"route": recall("route", cs)[1] if cs else None,
                        "airline": (refs["airlines"] or {}).get(airline_code(cs)),
                        "model_name": nice_model(current["desc"] or types.get(current["type"], "")),
                        "art": art or None,
                        "photo": (shown[1]["link"], shown[2], shown[1]["photographer"])
                                 if art is False and shown else None,
                        "no_photo": art is False and (pkey is None or
                                                      recall("photo", pkey) == (True, None))}
            model.set_sky({"home": home, "radius": cfg.planes_radius, "planes": planes,
                           "source": source,
                           "focus": current and dict(current, trail=list(trails[current["hex"]])),
                           **info})

        publish()
        if current:  # anything new to find out about this plane? (cached, so rarely)
            steps = []  # (registrations as callsigns never have a route)
            if airline_code(cs) and not recall("route", cs)[0]:
                steps.append(("route", cs, lambda: lookup_route(cs, current["lat"], current["lon"]),
                              ROUTE_TTL))
            if pkey and not recall("photo", pkey)[0] and (needs_page or not art_now()):
                steps.append(("photo", pkey, lambda: lookup_photo(pkey), PHOTO_TTL))
            for kind, key, lookup, ttl in steps:
                try:
                    value = lookup()
                except Exception as e:
                    log(f"plane lookup {kind} {key}: {e}")
                    continue  # try again next round
                remember(kind, key, value, ttl if value else UNKNOWN_TTL)
                publish()
            want = [(k, load) for k, load, needed in (
                ("airlines", load_airlines, airline_code(cs)),
                ("types", load_type_names, not current["desc"])) if needed and refs[k] is None]
            if want and time.monotonic() > refs["retry"]:
                try:
                    for k, load in want:
                        refs[k] = load()
                    publish()
                except Exception as e:
                    log(f"plane reference data: {e}")
                    refs["retry"] = time.monotonic() + 3600
            info = pkey and recall("photo", pkey)[1]
            if info and not shown and art_now() is False:  # no art for it: the photo
                try:
                    shown = (pkey, info, download_photo(info))
                    publish()
                except Exception as e:
                    log(f"plane photo: {e}")
            art = art_now()
            if current["hex"] != logged and art is not None:  # into the log, picture settled
                picture = (art["kind"] if art else "photo" if shown else
                           "none" if pkey is None or recall("photo", pkey) == (True, None) else None)
                if picture:
                    sky = model.sky or {}
                    v = plane_view(sky)
                    route = recall("route", cs)[1] if cs else None
                    log_plane(PLANES_LOG, {
                        "time": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                        "hex": current["hex"], "callsign": cs, "reg": current["reg"],
                        "type": current["type"], "model": sky.get("model_name") or "",
                        "livery": livery_brand(cs, (info or {}).get("link")),
                        "airline": ((refs["airlines"] or {}).get(airline_code(cs)) or {}).get("name")
                        or (route or {}).get("airline") or "",
                        "picture": picture, "art": os.path.basename(art["path"]) if art else "",
                        "from": v.origin if v else "", "to": v.dest if v else ""})
                    logged = current["hex"]
        model.planes_wake.wait(cfg.planes_poll)
        model.planes_wake.clear()


def plane_view(sky):
    """What the planes screen says about the plane it's following."""
    f = (sky or {}).get("focus")
    if not f:
        return None
    route, airline = sky.get("route") or {}, sky.get("airline") or {}
    leg = route_leg(route.get("airports") or [], f["lat"], f["lon"], f["track"])
    origin, dest, progress, left = leg or ({}, {}, None, None)
    # "UA 1234" when the callsign is an airline's plus a plain number; ATC-style
    # callsigns like UAL334K aren't the public flight number, so they stay as is
    m = re.match(r"^[A-Z]{3}0*(\d{1,4})$", f["callsign"])
    iata = airline.get("iata") or route.get("airline_iata") or ""
    flight = f"{iata} {m.group(1)}" if iata and m else f["callsign"] or f["reg"] or f["hex"].upper()
    stats = []
    if f["alt"] is not None:
        trend = ""
        if f["rate"] is not None and abs(f["rate"]) >= 300:
            trend = " ↑" if f["rate"] > 0 else " ↓"
        stats.append(f"{f['alt']:,} ft{trend}")
    if f["gs"] is not None:
        stats.append(f"{round(f['gs'] * 1.15078)} mph")
    if f["track"] is not None:
        stats.append(f"heading {compass(f['track'])}")
    miles = f["dist"] / NM_PER_MILE
    stats.append("right overhead" if miles < 0.6 else f"{miles:.1f} mi {compass(f['bearing'])} of you")
    return SimpleNamespace(
        origin=origin.get("iata") or origin.get("icao") or "",
        dest=dest.get("iata") or dest.get("icao") or "",
        dest_name=dest.get("name") or "", dest_city=dest.get("city") or "",
        progress=progress, to_go=None if left is None else left / NM_PER_MILE,
        flight=flight,
        airline=airline.get("name") or route.get("airline") or "",
        aircraft=" · ".join(x for x in (sky.get("model_name") or f["type"],
                                        f["reg"] if f["reg"] != flight else "",
                                        f"built {f['year']}" if f["year"] else "") if x),
        alert=SQUAWKS.get(f["squawk"]),
        stats=stats)


def setup_planes(config_path):
    """--setup-planes: where you are, so the screen knows what's overhead."""
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "server"))
    from device_login import save_to_config

    guess = None
    try:
        code, data = get_json("https://ipapi.co/json/", timeout=6)
        if code == 200 and data and data.get("latitude") is not None:
            guess = (round(float(data["latitude"]), 4), round(float(data["longitude"]), 4),
                     data.get("city") or "")
    except Exception:
        pass
    print("The planes screen needs your location (it stays in config.ini on this Pi).")
    print("Tip: right-click your home in Google Maps to copy its coordinates.")
    if guess:
        print(f"Your internet connection looks like it's near {guess[2] or 'here'}: "
              f"{guess[0]}, {guess[1]} (approximate).")
    answer = input("Latitude, longitude" + (" [Enter = use that]" if guess else "") + ": ").strip()
    if answer:
        try:
            lat, lon = (float(x) for x in answer.replace(" ", "").split(",")[:2])
        except ValueError:
            sys.exit("Couldn't read that - use the form 38.4496, -78.8689")
    elif guess:
        lat, lon = guess[0], guess[1]
    else:
        sys.exit("No location given.")
    radius = input("How far out to look, in nautical miles [15]: ").strip() or "15"
    try:
        radius_nm = max(2.0, min(60.0, float(radius)))
    except ValueError:
        sys.exit("Couldn't read that radius - use a number like 15")
    try:
        source, planes = fetch_planes(lat, lon, radius_nm)
        up = [p for p in planes if not p["ground"]]
        print(f"{source} sees {len(up)} aircraft in the air within {radius_nm:g} nm right now.")
    except Exception as e:
        print(f"Couldn't reach the plane feeds ({e}) - saving anyway.")
    save_to_config(config_path, "planes", {"lat": f"{lat:.4f}", "lon": f"{lon:.4f}",
                                           "radius_nm": f"{radius_nm:g}"})
    print(f"Saved to [planes] in {config_path}. Restart the display, then tap through to it.")


# ---------------------------------------------------------------- qr codes
# Planespotters asks that a photo shown on a screen nobody can click carries a
# QR code of its page. This is a small byte-mode encoder (ECC level L, versions
# 1-10: up to 271 bytes), after Project Nayuki's reference implementation.

# per version: (ECC codewords per block, [(blocks, data codewords per block), ...])
QR_BLOCKS_L = [None, (7, [(1, 19)]), (10, [(1, 34)]), (15, [(1, 55)]), (20, [(1, 80)]),
               (26, [(1, 108)]), (18, [(2, 68)]), (20, [(2, 78)]), (24, [(2, 97)]),
               (30, [(2, 116)]), (18, [(2, 68), (2, 69)])]
QR_ALIGN = [None, [], [6, 18], [6, 22], [6, 26], [6, 30], [6, 34], [6, 22, 38], [6, 24, 42],
            [6, 26, 46], [6, 28, 50]]


def _gf_mul(x, y):
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_ecc(data, degree):
    """Reed-Solomon error-correction codewords for one block."""
    gen, root = [0] * (degree - 1) + [1], 1
    for _ in range(degree):
        for j in range(degree):
            gen[j] = _gf_mul(gen[j], root)
            if j + 1 < degree:
                gen[j] ^= gen[j + 1]
        root = _gf_mul(root, 2)
    rem = [0] * degree
    for b in data:
        factor = b ^ rem.pop(0)
        rem.append(0)
        for i, g in enumerate(gen):
            rem[i] ^= _gf_mul(g, factor)
    return rem


def qr_matrix(data):
    """The QR code for `data` (bytes) as rows of booleans (True = dark)."""
    for version in range(1, 11):
        ecc_len, groups = QR_BLOCKS_L[version]
        capacity = sum(n * k for n, k in groups)
        count_bits = 8 if version < 10 else 16
        if 4 + count_bits + 8 * len(data) <= capacity * 8:
            break
    else:
        raise ValueError("too long for a QR code this encoder makes")
    # the bit stream: byte mode, length, data, terminator, padding
    bits = [int(c) for c in f"0100{len(data):0{count_bits}b}"]
    for b in data:
        bits += [(b >> i) & 1 for i in range(7, -1, -1)]
    bits += [0] * min(4, capacity * 8 - len(bits))
    bits += [0] * (-len(bits) % 8)
    words = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    words += [0xEC, 0x11] * ((capacity - len(words)) // 2) + [0xEC] * ((capacity - len(words)) % 2)
    # split into blocks, add error correction, interleave
    blocks, at = [], 0
    for n, k in groups:
        for _ in range(n):
            blocks.append(words[at:at + k])
            at += k
    eccs = [_rs_ecc(b, ecc_len) for b in blocks]
    stream = [b[i] for i in range(max(map(len, blocks))) for b in blocks if i < len(b)]
    stream += [e[i] for i in range(ecc_len) for e in eccs]

    size = version * 4 + 17
    grid = [[False] * size for _ in range(size)]
    fixed = [[False] * size for _ in range(size)]

    def put(x, y, dark):
        grid[y][x], fixed[y][x] = dark, True

    for i in range(size):  # timing patterns
        put(6, i, i % 2 == 0)
        put(i, 6, i % 2 == 0)
    for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):  # finders + separators
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                if 0 <= cx + dx < size and 0 <= cy + dy < size:
                    put(cx + dx, cy + dy, max(abs(dx), abs(dy)) not in (2, 4))
    align = QR_ALIGN[version]
    for i, ax in enumerate(align):
        for j, ay in enumerate(align):
            if (i, j) in ((0, 0), (0, len(align) - 1), (len(align) - 1, 0)):
                continue
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    put(ax + dx, ay + dy, max(abs(dx), abs(dy)) != 1)

    def format_bits(mask):
        data = 1 << 3 | mask  # 01 = level L
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        v = (data << 10 | rem) ^ 0x5412
        for i in range(6):
            put(8, i, (v >> i) & 1)
        put(8, 7, (v >> 6) & 1)
        put(8, 8, (v >> 7) & 1)
        put(7, 8, (v >> 8) & 1)
        for i in range(9, 15):
            put(14 - i, 8, (v >> i) & 1)
        for i in range(8):
            put(size - 1 - i, 8, (v >> i) & 1)
        for i in range(8, 15):
            put(8, size - 15 + i, (v >> i) & 1)
        put(8, size - 8, True)  # the dark module

    format_bits(0)  # reserve the format areas
    if version >= 7:
        rem = version
        for _ in range(12):
            rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
        v = version << 12 | rem
        for i in range(18):
            a, b = size - 11 + i % 3, i // 3
            put(a, b, (v >> i) & 1)
            put(b, a, (v >> i) & 1)

    # the data, zig-zagging up and down in two-module columns from the right
    i, total = 0, len(stream) * 8
    right = size - 1
    while right >= 1:
        if right == 6:
            right = 5
        upward = ((right + 1) & 2) == 0
        for vert in range(size):
            y = size - 1 - vert if upward else vert
            for x in (right, right - 1):
                if not fixed[y][x] and i < total:
                    grid[y][x] = bool((stream[i >> 3] >> (7 - (i & 7))) & 1)
                    i += 1
        right -= 2

    masks = (lambda x, y: (x + y) % 2 == 0, lambda x, y: y % 2 == 0, lambda x, y: x % 3 == 0,
             lambda x, y: (x + y) % 3 == 0, lambda x, y: (x // 3 + y // 2) % 2 == 0,
             lambda x, y: x * y % 2 + x * y % 3 == 0,
             lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
             lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0)

    def apply(mask):
        f = masks[mask]
        for y in range(size):
            for x in range(size):
                if not fixed[y][x] and f(x, y):
                    grid[y][x] = not grid[y][x]

    def penalty():
        score = 0
        lines = [row for row in grid] + [list(col) for col in zip(*grid)]
        for line in lines:  # runs of 5+, and finder look-alikes
            run, prev = 0, None
            for m in line:
                if m == prev:
                    run += 1
                else:
                    if run >= 5:
                        score += run - 2
                    run, prev = 1, m
            if run >= 5:
                score += run - 2
            s = "".join("1" if m else "0" for m in line)
            score += 40 * (s.count("10111010000") + s.count("00001011101"))
        for y in range(size - 1):  # 2x2 blocks
            for x in range(size - 1):
                if grid[y][x] == grid[y][x + 1] == grid[y + 1][x] == grid[y + 1][x + 1]:
                    score += 3
        dark = sum(map(sum, grid))
        score += 10 * (abs(dark * 20 - size * size * 10) // (size * size))
        return score

    best = None
    for mask in range(8):
        apply(mask)
        format_bits(mask)
        score = penalty()
        if best is None or score < best[0]:
            best = (score, mask)
        apply(mask)  # XOR again to undo
    apply(best[1])
    format_bits(best[1])
    return grid


# ---------------------------------------------------------------- formula 1
#
# The weekend's schedule and results come from OpenF1 (free outside a live
# session - their live data is paid), the track from MultiViewer (the outline
# in F1's own coordinates, with the time along a real lap for every point),
# standings and past winners from Jolpica (the Ergast successor). During a
# session the display listens to F1's own live timing feed, as the official
# app does. It's free without a login - all but the cars' GPS positions, so
# the map places each car from the mini-sector timing loops it has just
# crossed, and moves it on at its lap pace in between.

F1_OPENF1 = "https://api.openf1.org/v1/"
F1_CIRCUIT_URL = "https://api.multiviewer.app/api/v1/circuits/{circuit}/{year}"
F1_JOLPICA = "https://api.jolpi.ca/ergast/f1/"
F1_LIVE_NEGOTIATE = "https://livetiming.formula1.com/signalrcore/negotiate?negotiateVersion=1"
F1_LIVE_WS = "wss://livetiming.formula1.com/signalrcore?id={token}"
F1_ARCHIVE = "https://livetiming.formula1.com/static/"
F1_TOPICS = ["SessionInfo", "SessionStatus", "TrackStatus", "DriverList", "TimingData",
             "RaceControlMessages", "LapCount", "ExtrapolatedClock", "WeatherData"]
F1_LIVE_BEFORE, F1_LIVE_AFTER = 15 * 60, 30 * 60  # listen from 15 min before to 30 after
F1_PIT_SEGMENT = 2064  # a mini-sector status meaning "in the pit lane"
F1_FEED_LAG = 1.0      # s: loops reach the feed about a second after the car crossed them
COL_F1 = (225, 6, 0)
F1_FLAGS = {"1": ("GREEN FLAG", COL_GREEN), "2": ("YELLOW FLAG", COL_YELLOW),
            "4": ("SAFETY CAR", COL_YELLOW), "5": ("RED FLAG", COL_RED),
            "6": ("VIRTUAL SAFETY CAR", COL_YELLOW), "7": ("VSC ENDING", COL_YELLOW)}
F1_SHORT = {"Practice 1": "FP1", "Practice 2": "FP2", "Practice 3": "FP3", "Qualifying": "QUALI",
            "Sprint Qualifying": "SPRINT Q", "Sprint Shootout": "SHOOTOUT", "Sprint": "SPRINT",
            "Race": "RACE"}


def f1_json(url, name, max_age):
    """GET JSON through the CACHE_DIR copy (fetched again after max_age; a
    stale copy beats none - OpenF1 turns free requests away mid-session)."""
    return json.loads(cached_download(url, name, max_age))


def f1_events(year):
    """The season's meetings, each with its sessions, in date order."""
    meetings = f1_json(F1_OPENF1 + f"meetings?year={year}", f"f1_meetings_{year}.json", 6 * 3600)
    sessions = f1_json(F1_OPENF1 + f"sessions?year={year}", f"f1_sessions_{year}.json", 6 * 3600)
    by_meeting = {}
    for s in sessions if isinstance(sessions, list) else []:
        start, end = parse_iso(s.get("date_start") or ""), parse_iso(s.get("date_end") or "")
        if isinstance(s, dict) and start and end and not s.get("is_cancelled"):
            by_meeting.setdefault(s.get("meeting_key"), []).append(
                {"key": s.get("session_key"), "name": s.get("session_name") or "",
                 "type": s.get("session_type") or "", "start": start, "end": end})
    events = []
    for m in meetings if isinstance(meetings, list) else []:
        sess = sorted(by_meeting.get(m.get("meeting_key"), []), key=lambda s: s["start"])
        if isinstance(m, dict) and sess and not m.get("is_cancelled"):
            events.append({"key": m.get("meeting_key"), "name": m.get("meeting_name") or "",
                           "location": m.get("location") or "", "country": m.get("country_name") or "",
                           "circuit_key": m.get("circuit_key"), "circuit": m.get("circuit_short_name") or "",
                           "year": year, "sessions": sess, "start": sess[0]["start"], "end": sess[-1]["end"]})
    return sorted(events, key=lambda e: e["start"])


def f1_day_end(when, now):
    """Midnight after `when`, local time: results stay up the rest of that day."""
    local = when.astimezone(now.tzinfo)
    return local.replace(hour=23, minute=59, second=59)


def f1_weekend(events, now):
    """The meeting to show: the one on now, else the next one."""
    return next((e for e in events if now <= f1_day_end(e["end"], now)), None)


def f1_live_session(event, now):
    """The session whose live window (15 min before to 30 after) we're in."""
    return next((s for s in event["sessions"]
                 if s["start"] - datetime.timedelta(seconds=F1_LIVE_BEFORE) <= now
                 <= s["end"] + datetime.timedelta(seconds=F1_LIVE_AFTER)), None)


def f1_today_session(event, now):
    """The latest session that finished today - its results go on the right."""
    done = [s for s in event["sessions"]
            if s["end"] <= now and s["end"].astimezone(now.tzinfo).date() == now.date()]
    return done[-1] if done else None


def f1_next_session(event, now):
    return next((s for s in event["sessions"] if s["start"] > now), None)


class TrackMap:
    """MultiViewer's map of a circuit: the outline turned the way F1 draws
    it, and where along a lap each point is."""

    def __init__(self, data):
        xs, ys = data.get("x") or [], data.get("y") or []
        n = min(len(xs), len(ys))
        if n < 10:
            raise ValueError("no outline")
        a = math.radians(float(data.get("rotation") or 0))
        ca, sa = math.cos(a), math.sin(a)
        self.pts = [(x * ca - y * sa, -(x * sa + y * ca)) for x, y in zip(xs[:n], ys[:n])]  # y down
        dist = [0.0]
        for (x0, y0), (x1, y1) in zip(self.pts, self.pts[1:]):
            dist.append(dist[-1] + math.hypot(x1 - x0, y1 - y0))
        self.length_km = (dist[-1] + math.dist(self.pts[-1], self.pts[0])) / 10000  # decimetres
        ts = data.get("trackPositionTime") or []
        if len(ts) >= n and ts[n - 1] > ts[0]:  # by time along the lap, like the timing loops
            self.frac = [(t - ts[0]) / (ts[n - 1] - ts[0]) for t in ts[:n]]
            self.lap_time = ts[n - 1] - ts[0]
        else:
            self.frac = [d / dist[-1] for d in dist]
            self.lap_time = 90.0
        self.corners = len(data.get("corners") or [])
        try:
            self.pit_loss = float((data.get("pitLoss") or {}).get("normal"))
        except (TypeError, ValueError):
            self.pit_loss = None
        self.box = (min(p[0] for p in self.pts), min(p[1] for p in self.pts),
                    max(p[0] for p in self.pts), max(p[1] for p in self.pts))

    def at(self, frac):
        """The point `frac` of the way round the lap."""
        frac %= 1.0
        i = max(1, min(len(self.frac) - 1, bisect.bisect_left(self.frac, frac)))
        f0, f1 = self.frac[i - 1], self.frac[i]
        k = 0.0 if f1 <= f0 else (frac - f0) / (f1 - f0)
        (x0, y0), (x1, y1) = self.pts[i - 1], self.pts[i]
        return x0 + (x1 - x0) * k, y0 + (y1 - y0) * k


def f1_track(circuit_key, year):
    """The TrackMap for a circuit (last year's if this year's isn't out yet)."""
    for y in (year, year - 1):
        try:
            return TrackMap(f1_json(F1_CIRCUIT_URL.format(circuit=circuit_key, year=y),
                                    f"f1_circuit_{circuit_key}_{y}.json", 30 * 86400))
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    return None


class MiniWebSocket:
    """Just enough of RFC 6455 for a text-message client over TLS."""
    GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    def __init__(self, url, headers=None, timeout=15):
        u = urllib.parse.urlsplit(url)
        raw = socket.create_connection((u.hostname, u.port or 443), timeout=timeout)
        self.sock = SSL_CTX.wrap_socket(raw, server_hostname=u.hostname)
        self.buf, self.parts = b"", []
        key = base64.b64encode(os.urandom(16)).decode()
        head = [f"GET {u.path}{'?' + u.query if u.query else ''} HTTP/1.1", f"Host: {u.hostname}",
                "Upgrade: websocket", "Connection: Upgrade", f"Sec-WebSocket-Key: {key}",
                "Sec-WebSocket-Version: 13", f"User-Agent: {PLANES_USER_AGENT}"]
        head += [f"{k}: {v}" for k, v in (headers or {}).items()]
        self.sock.sendall(("\r\n".join(head) + "\r\n\r\n").encode())
        while b"\r\n\r\n" not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("websocket closed during the handshake")
            self.buf += chunk
        reply, self.buf = self.buf.split(b"\r\n\r\n", 1)
        accept = base64.b64encode(hashlib.sha1((key + self.GUID).encode()).digest())
        if b" 101 " not in reply.split(b"\r\n")[0] or accept not in reply:
            raise ConnectionError("websocket refused: " + reply.split(b"\r\n")[0].decode(errors="replace"))

    def send(self, data, opcode=1):
        data = data.encode() if isinstance(data, str) else data
        n = len(data)
        head = bytes([0x80 | opcode])
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + n.to_bytes(2, "big")
        else:
            head += bytes([0x80 | 127]) + n.to_bytes(8, "big")
        mask = os.urandom(4)
        self.sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def recv(self, timeout):
        """The next text message; socket.timeout if none comes in time."""
        self.sock.settimeout(timeout)
        while True:
            msg = self._take()
            if msg is not None:
                return msg
            chunk = self.sock.recv(65536)  # a timeout here leaves the buffer intact
            if not chunk:
                raise ConnectionError("websocket closed")
            self.buf += chunk

    def _take(self):
        """Whole frames off the buffer: a finished text message, or None."""
        while True:
            b = self.buf
            if len(b) < 2:
                return None
            n, i = b[1] & 0x7F, 2
            if n == 126:
                if len(b) < 4:
                    return None
                n, i = int.from_bytes(b[2:4], "big"), 4
            elif n == 127:
                if len(b) < 10:
                    return None
                n, i = int.from_bytes(b[2:10], "big"), 10
            mask = None
            if b[1] & 0x80:
                mask, i = b[i:i + 4], i + 4
            if len(b) < i + n:
                return None
            data, self.buf = b[i:i + n], b[i + n:]
            if mask:
                data = bytes(c ^ mask[k % 4] for k, c in enumerate(data))
            op = b[0] & 0x0F
            if op == 8:
                raise ConnectionError("websocket closed by the server")
            if op == 9:
                self.send(data, 10)
            elif op in (0, 1, 2):
                self.parts.append(data)
                if b[0] & 0x80:
                    text, self.parts = b"".join(self.parts).decode("utf-8"), []
                    return text

    def close(self):
        try:
            self.send(b"", 8)
            self.sock.close()
        except OSError:
            pass


def f1_connect():
    """Open F1's live timing feed and subscribe; returns the websocket."""
    code, body, headers = http(F1_LIVE_NEGOTIATE, data=b"", headers={"User-Agent": PLANES_USER_AGENT})
    if code != 200:
        raise OSError(f"live timing negotiate: HTTP {code}")
    token = json.loads(body)["connectionToken"]
    cookies = "; ".join(c.split(";")[0] for c in (headers.get_all("Set-Cookie") or []))
    ws = MiniWebSocket(F1_LIVE_WS.format(token=urllib.parse.quote(token)),
                       {"Cookie": cookies} if cookies else None)
    ws.send('{"protocol":"json","version":1}\x1e')
    ws.recv(15)  # the hub's "{}" handshake reply
    ws.send(json.dumps({"type": 1, "invocationId": "1", "target": "Subscribe",
                        "arguments": [F1_TOPICS]}) + "\x1e")
    return ws


def f1_messages(ws, timeout):
    """(topic, data, whole) for each update the feed sent within `timeout`
    - whole=True for the full state that answers the Subscribe."""
    try:
        text = ws.recv(timeout)
    except socket.timeout:
        return []
    out = []
    for part in text.split("\x1e"):
        if not part:
            continue
        m = json.loads(part)
        if m.get("type") == 3 and isinstance(m.get("result"), dict):
            out += [(topic, data, True) for topic, data in m["result"].items()]
        elif m.get("type") == 1 and m.get("target") == "feed" and len(m.get("arguments") or []) >= 2:
            out.append((m["arguments"][0], m["arguments"][1], False))
        elif m.get("type") == 7:
            raise ConnectionError("live timing closed the connection")
    return out


def deep_merge(target, patch):
    """Apply one live timing update: dicts merge key by key, and a dict of
    "index": value patches a list."""
    if isinstance(patch, dict) and isinstance(target, dict):
        for k, v in patch.items():
            if k == "_deleted":
                for gone in v if isinstance(v, list) else []:
                    target.pop(str(gone), None)
            elif isinstance(v, (dict, list)) and isinstance(target.get(k), (dict, list)):
                target[k] = deep_merge(target[k], v)
            else:
                target[k] = v
        return target
    if isinstance(patch, dict) and isinstance(target, list):
        for k, v in patch.items():
            try:
                i = int(k)
            except ValueError:
                continue
            while len(target) <= i:
                target.append({})
            target[i] = deep_merge(target[i], v) if isinstance(v, (dict, list)) and \
                isinstance(target[i], (dict, list)) else v
        return target
    return patch


def f1_items(x):
    """(key, value) pairs of a dict, or of a list by index - the feed uses both."""
    if isinstance(x, dict):
        return list(x.items())
    return list(enumerate(x)) if isinstance(x, list) else []


def lap_seconds(text):
    """"1:43.347" -> 103.347; "" -> None."""
    try:
        parts = str(text).split(":")
        return round(sum(float(p) * 60 ** i for i, p in enumerate(reversed(parts))), 3) if text else None
    except ValueError:
        return None


class CarTracker:
    """Where each car is around the lap, from the mini-sector timing loops
    it crosses. Between loops it moves on at its lap pace, so the dots glide
    round the map. The loops' places along the lap are learned from the laps
    themselves (or from an earlier session's timing - see f1_calibrate)."""

    def __init__(self, lap_time=90.0, fractions=None):
        self.counts = ()          # loops per sector, e.g. (8, 10, 9); the last is the line
        self.fractions = fractions  # lap fraction at each loop, if known in advance
        self.ref_lap = lap_time
        self.cars = {}            # number -> {"g", "t", "lap", "pit", "start", "seen"}
        self.samples = {}         # loop -> fractions seen this session
        self.best = None          # fastest full lap seen, to skip slow laps

    def learn_counts(self, timing):
        """Loops per sector: the highest one any update mentions (updates
        only list the loops that changed; the full state lists them all)."""
        counts = list(self.counts) or [0, 0, 0]
        for _, line in f1_items((timing or {}).get("Lines")):
            for si, sector in f1_items(line.get("Sectors") if isinstance(line, dict) else None):
                for gi, _ in f1_items(sector.get("Segments") if isinstance(sector, dict) else None):
                    if int(si) < 3:
                        counts[int(si)] = max(counts[int(si)], int(gi) + 1)
        if all(counts) and tuple(counts) != self.counts:
            self.counts, self.samples = tuple(counts), {}
            if self.fractions and len(self.fractions) != sum(counts):
                self.fractions = None

    def update(self, timing, t):
        """Take a TimingData update that arrived at time t."""
        self.learn_counts(timing)
        for num, line in f1_items((timing or {}).get("Lines")):
            if not isinstance(line, dict):
                continue
            car = self.cars.setdefault(str(num), {"g": None, "t": t, "lap": self.ref_lap,
                                                  "pit": False, "start": None, "seen": {}})
            if "InPit" in line:
                car["pit"] = bool(line["InPit"])
            if line.get("Retired") or line.get("Stopped"):
                car["out"] = True
            for si, sector in f1_items(line.get("Sectors")):
                for gi, seg in f1_items((sector or {}).get("Segments") if isinstance(sector, dict) else None):
                    status = (seg or {}).get("Status") if isinstance(seg, dict) else None
                    if status:
                        self.passed(car, int(si), int(gi), status, t)

    def passed(self, car, sector, seg, status, t):
        if sector >= len(self.counts) or seg >= self.counts[sector]:
            return
        n = sum(self.counts)
        g = sum(self.counts[:sector]) + seg
        car["pit"] = status == F1_PIT_SEGMENT  # a pit-lane loop, or back on track
        if g == n - 1:  # the line: a lap done, another begun
            if car["start"] is not None and not car["pit"]:
                lap = t - car["start"]
                if 0.5 * self.ref_lap < lap < 2.5 * self.ref_lap:
                    car["lap"] = lap
                    self.best = min(self.best or lap, lap)
                    if lap < self.best * 1.1 and len(car["seen"]) > n // 2:  # a proper lap
                        for k, at in car["seen"].items():
                            s = self.samples.setdefault(k, [])
                            s.append(at / lap)
                            del s[:-15]
            car["start"], car["seen"] = t, {}
        elif car["start"] is not None:
            car["seen"][g] = t - car["start"]
        car["g"], car["t"] = g, t

    def bounds(self):
        """The lap fraction at each loop (the last one, the line, is 1.0)."""
        n = sum(self.counts)
        if self.fractions and len(self.fractions) == n:
            return list(self.fractions)
        out, prev = [], 0.0
        for g in range(n - 1):
            seen = sorted(self.samples.get(g, []))
            f = seen[len(seen) // 2] if len(seen) >= 3 else (g + 1) / n
            prev = max(prev + 0.002, min(f, 0.998))  # in order, and short of the line
            out.append(prev)
        return out + [1.0] if n else []

    def snapshot(self):
        """What the renderer needs to place the cars at any moment."""
        return {"bounds": self.bounds(),
                "cars": {k: {"g": c["g"], "t": c["t"], "lap": c["lap"], "pit": c["pit"],
                             "out": c.get("out", False)}
                         for k, c in self.cars.items() if c["g"] is not None}}


def car_fractions(snap, now):
    """number -> lap fraction for each car on track at time `now` (monotonic)."""
    bounds = snap.get("bounds") or []
    n, out = len(bounds), {}
    for num, c in (snap.get("cars") or {}).items():
        if c["pit"] or c["out"] or not n or c["g"] >= n:
            continue
        f0 = 0.0 if c["g"] == n - 1 else bounds[c["g"]]
        f1 = bounds[0] if c["g"] == n - 1 else bounds[c["g"] + 1]
        ran = max(0.0, now - c["t"] + F1_FEED_LAG)  # (checked against the cars' GPS: ~1 s behind)
        out[num] = min(f0 + ran / max(20.0, c["lap"]), f1 - 0.002) % 1.0
    return out


def f1_calibrate(stream_text):
    """The loops' lap fractions from a TimingData.jsonStream (F1's archive of
    a session), or None if it has too few laps to tell."""
    lines = []
    for raw in stream_text.lstrip("﻿").splitlines():
        if len(raw) > 12 and raw[12] == "{":
            try:
                h, m, s = raw[:12].split(":")
                lines.append((int(h) * 3600 + int(m) * 60 + float(s), json.loads(raw[12:])))
            except ValueError:
                continue
    counts = [0, 0, 0]
    for _, data in lines:  # the most loops any sector ever lists
        for _, line in f1_items(data.get("Lines")):
            for si, sector in f1_items(line.get("Sectors") if isinstance(line, dict) else None):
                for gi, _ in f1_items(sector.get("Segments") if isinstance(sector, dict) else None):
                    if int(si) < 3:
                        counts[int(si)] = max(counts[int(si)], int(gi) + 1)
    if not all(counts):
        return None
    lap_guess = None
    tracker = CarTracker()
    tracker.counts = tuple(counts)
    for t, data in lines:
        if lap_guess is None:  # the pace, from the first lap times that turn up
            times = [lap_seconds((line.get("LastLapTime") or {}).get("Value"))
                     for _, line in f1_items(data.get("Lines")) if isinstance(line, dict)]
            times = [x for x in times if x]
            if times:
                lap_guess = tracker.ref_lap = min(times)
        tracker.update(data, t)
    if sum(len(s) >= 3 for s in tracker.samples.values()) < sum(counts) * 0.8:
        return None
    return {"counts": counts, "fractions": tracker.bounds()}


def f1_archive_path(event, year):
    """F1's archive folder of the most recent finished session at this
    circuit: earlier this weekend, else last year's race there."""
    now = datetime.datetime.now(datetime.timezone.utc)
    done = {s["key"] for s in event["sessions"] if s["end"] < now - datetime.timedelta(hours=1)}
    for y in (year, year - 1):
        index = json.loads(cached_download(F1_ARCHIVE + f"{y}/Index.json", f"f1_archive_{y}.json",
                                           6 * 3600).decode("utf-8-sig"))
        for m in reversed(index.get("Meetings") or []):
            if (m.get("Circuit") or {}).get("Key") != event["circuit_key"]:
                continue
            sessions = [s for s in m.get("Sessions") or [] if s.get("Path")]
            if y == year:
                sessions = [s for s in sessions if s.get("Key") in done]
            else:
                sessions = [s for s in sessions if s.get("Type") == "Race"] or sessions
            if sessions:
                return sessions[-1]["Path"]
    return None


def f1_segment_fractions(event, year):
    """Where each timing loop sits around this circuit: cached per circuit,
    worked out once from F1's archive of a session there."""
    path = os.path.join(CACHE_DIR, "f1_loops.json")
    try:
        with open(path) as f:
            known = json.load(f)
    except (OSError, ValueError):
        known = {}
    entry = known.get(str(event["circuit_key"]))
    if entry and (entry.get("fractions") or time.time() - entry.get("tried", 0) < 86400):
        return entry if entry.get("fractions") else None
    known[str(event["circuit_key"])] = {"tried": time.time()}  # (saved below either way)
    folder = f1_archive_path(event, year)
    entry = None
    if folder:
        code, body, _ = http(F1_ARCHIVE + folder + "TimingData.jsonStream",
                             headers={"User-Agent": PLANES_USER_AGENT}, timeout=60)
        if code == 200:
            entry = f1_calibrate(body.decode("utf-8-sig", errors="replace"))
            if entry:
                known[str(event["circuit_key"])] = entry
        else:
            log(f"f1 timing archive {folder}: HTTP {code}")
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(path + ".tmp", "w") as f:
        json.dump(known, f)
    os.replace(path + ".tmp", path)
    return entry


def f1_team_color(hexcode):
    try:
        return tuple(int(hexcode[i:i + 2], 16) for i in (0, 2, 4))
    except (TypeError, ValueError):
        return COL_DIM


def f1_results(session):
    """OpenF1's classification of a finished session as display rows:
    (position, TLA, team colour, time or gap text)."""
    key = session["key"]
    code, res = get_json(F1_OPENF1 + f"session_result?session_key={key}")
    if code != 200 or not isinstance(res, list) or not res:
        raise ValueError(f"no results yet (HTTP {code})")
    code, drivers = get_json(F1_OPENF1 + f"drivers?session_key={key}")
    who = {d.get("driver_number"): (d.get("name_acronym") or str(d.get("driver_number")),
                                    f1_team_color(d.get("team_colour")))
           for d in drivers if isinstance(d, dict)} if isinstance(drivers, list) else {}
    race = session["type"] == "Race" or session["name"] in ("Race", "Sprint")
    rows = []
    for r in sorted((r for r in res if isinstance(r, dict) and r.get("position")),
                    key=lambda r: r["position"]):
        tla, color = who.get(r.get("driver_number"), (str(r.get("driver_number")), COL_DIM))
        dur, gap = r.get("duration"), r.get("gap_to_leader")
        if isinstance(dur, list):  # qualifying: Q1, Q2, Q3 - the last one they ran
            done = [(d, g) for d, g in zip(dur, gap if isinstance(gap, list) else [None] * 3) if d]
            dur, gap = done[-1] if done else (None, None)
        if r.get("dnf") or r.get("dns") or r.get("dsq"):
            text = "DSQ" if r.get("dsq") else "DNS" if r.get("dns") else "DNF"
        elif r["position"] == 1:
            text = fmt_race_time(dur) if race else fmt_lap(dur)
        elif isinstance(gap, str):
            text = gap  # "+1 LAP"
        else:
            text = f"+{gap:.3f}" if isinstance(gap, (int, float)) else ""
        rows.append((r["position"], tla, color, text))
    return rows


def fmt_lap(sec):
    if not isinstance(sec, (int, float)):
        return ""
    return f"{int(sec // 60)}:{sec % 60:06.3f}"


def fmt_race_time(sec):
    if not isinstance(sec, (int, float)):
        return ""
    h, rest = divmod(sec, 3600)
    return f"{int(h)}:{int(rest // 60):02d}:{rest % 60:06.3f}" if h else fmt_lap(sec)


def f1_facts(event):
    """Things worth knowing about the weekend's circuit, from Jolpica: its
    full name, the round, last year's winner, the championship leader."""
    year = event["year"]
    facts = {}
    races = f1_json(F1_JOLPICA + f"{year}.json", f"f1_jolpica_{year}.json", 86400)
    race_day = next((s["start"].date() for s in reversed(event["sessions"]) if s["type"] == "Race"), None)
    for r in ((races.get("MRData") or {}).get("RaceTable") or {}).get("Races") or []:
        if race_day and r.get("date") == race_day.isoformat():
            circuit = r.get("Circuit") or {}
            facts.update(circuit=circuit.get("circuitName") or "", circuit_id=circuit.get("circuitId"),
                         round=r.get("round"))
    if facts.get("circuit_id"):
        try:
            last = f1_json(F1_JOLPICA + f"{year - 1}/circuits/{facts['circuit_id']}/results/1.json",
                           f"f1_winner_{facts['circuit_id']}_{year - 1}.json", 30 * 86400)
            race = (((last.get("MRData") or {}).get("RaceTable") or {}).get("Races") or [None])[0]
            d = ((race or {}).get("Results") or [{}])[0].get("Driver") or {}
            if d:
                facts["last_winner"] = (year - 1, f"{d.get('givenName', '')} {d.get('familyName', '')}".strip())
        except (OSError, ValueError, AttributeError, IndexError):
            pass
    try:
        st = f1_json(F1_JOLPICA + f"{year}/driverstandings.json", f"f1_standings_{year}.json", 6 * 3600)
        lists = ((st.get("MRData") or {}).get("StandingsTable") or {}).get("StandingsLists") or []
        top = (lists[0].get("DriverStandings") or [])[:3] if lists else []
        facts["leaders"] = [((s.get("Driver") or {}).get("familyName") or "?", s.get("points")) for s in top]
    except (OSError, ValueError, AttributeError):
        pass
    return facts


def f1_live_view(topics, cars, session):
    """What the live screen shows, from the feed's merged topics."""
    info = topics.get("SessionInfo") or {}
    timing = topics.get("TimingData") or {}
    drivers = topics.get("DriverList") or {}
    race = (info.get("Type") or session["type"]) == "Race" or session["name"] in ("Race", "Sprint")
    who = {}
    for num, d in f1_items(drivers):
        if isinstance(d, dict):
            who[str(num)] = (d.get("Tla") or str(num), f1_team_color(d.get("TeamColour")))
    tower, fastest = [], None
    for num, line in f1_items(timing.get("Lines")):
        if not isinstance(line, dict):
            continue
        try:
            pos = int(line.get("Position") or 0)
        except ValueError:
            pos = 0
        tla, color = who.get(str(num), (str(num), COL_DIM))
        best = lap_seconds((line.get("BestLapTime") or {}).get("Value"))
        if best and (fastest is None or best < fastest[1]):
            fastest = (tla, best, color)
        out, pit = line.get("Retired") or line.get("Stopped"), line.get("InPit")
        if race:  # a race tower says where they are: in the pits, out, or the interval
            gap = (line.get("IntervalToPositionAhead") or {}).get("Value") or line.get("GapToLeader") or ""
            text = "OUT" if out else "PIT" if pit else "LEADER" if pos == 1 else str(gap)
        else:  # practice and qualifying: the time stands, pits or not
            text = fmt_lap(best) if pos == 1 else (line.get("TimeDiffToFastest") or "")
            text = text or ("OUT" if out else "")
        if pos:
            tower.append((pos, tla, color, text, "out" if out else "pit" if pit else ""))
    tower.sort()
    status = F1_FLAGS.get(str((topics.get("TrackStatus") or {}).get("Status")), ("", COL_DIM))
    msgs = [m for _, m in f1_items((topics.get("RaceControlMessages") or {}).get("Messages")) if isinstance(m, dict)]
    clock = topics.get("ExtrapolatedClock") or {}
    laps = topics.get("LapCount") or {}
    w = topics.get("WeatherData") or {}
    weather = []
    if w.get("AirTemp"):
        weather.append(f"air {float(w['AirTemp']):.0f}°")
    if w.get("TrackTemp"):
        weather.append(f"track {float(w['TrackTemp']):.0f}°")
    if str(w.get("Rainfall") or "0") not in ("0", ""):
        weather.append("rain")
    return {"phase": "live", "session": session, "race": race,
            "state": (topics.get("SessionStatus") or {}).get("Status") or info.get("SessionStatus") or "",
            "flag": status, "tower": tower, "drivers": who,
            "message": (msgs[-1].get("Message") or "") if msgs else "",
            "fastest": fastest, "weather": "  ·  ".join(weather),
            "laps": (laps.get("CurrentLap"), laps.get("TotalLaps")) if laps.get("TotalLaps") else None,
            "clock": (clock.get("Remaining"), parse_iso(clock.get("Utc") or ""),
                      bool(clock.get("Extrapolating"))) if clock.get("Remaining") else None,
            "cars": cars if (topics.get("SessionStatus") or {}).get("Status") == "Started" else {}}


def f1_clock_left(clock, now):
    """Seconds left in the session, counting down from the feed's last word."""
    remaining, at, running = clock
    left = lap_seconds(remaining) or 0.0
    if running and at:
        left -= (now - at).total_seconds()
    return max(0.0, left)


def f1_worker(model):
    """While the F1 screen is up: the weekend, and live timing when a session is on."""
    track, track_key, facts, facts_at = None, None, {}, 0.0
    results = {}   # session key -> rows (from the live feed at the end, then OpenF1)
    while True:
        if model.mode != "f1":
            model.f1_wake.wait()
            model.f1_wake.clear()
            continue
        now = datetime.datetime.now().astimezone()
        try:
            events = f1_events(now.year)
            event = f1_weekend(events, now)
            if event is None:  # season over: the first race of the next one
                event = f1_weekend(f1_events(now.year + 1), now)
        except Exception as e:
            log(f"f1 schedule: {e}")
            model.set_f1_status("F1 schedule unreachable - retrying", COL_RED)
            model.f1_wake.wait(60)
            model.f1_wake.clear()
            continue
        if event is None:
            model.set_f1_status("no F1 schedule yet", COL_DIM)
            model.f1_wake.wait(3600)
            model.f1_wake.clear()
            continue
        if track_key != (event["circuit_key"], event["year"]):
            track_key, facts, facts_at = (event["circuit_key"], event["year"]), {}, 0.0
            track = f1_track(event["circuit_key"], event["year"])
        if time.monotonic() - facts_at > 6 * 3600:
            try:
                facts = f1_facts(event)
            except Exception as e:
                log(f"f1 facts: {e}")
            facts_at = time.monotonic()
        base = {"event": event, "track": track, "facts": facts}
        live = f1_live_session(event, now)
        if live:
            rows = f1_follow_live(model, event, live, track, base)
            if rows:
                results[live["key"]] = rows
            continue
        today = f1_today_session(event, now)
        shown = None
        if today:
            if today["key"] not in results or results[today["key"]][0] == "live":
                try:  # OpenF1 has it half an hour after the flag
                    results[today["key"]] = ("openf1", f1_results(today))
                except Exception as e:
                    log(f"f1 results {today['name']}: {e}")
            shown = results.get(today["key"])
        model.set_f1(dict(base, phase="off", now=now, next=f1_next_session(event, now),
                          results=(today, shown[1]) if shown else None))
        # get ready for the map: where the timing loops sit on this track
        try:
            f1_segment_fractions(event, event["year"])
        except Exception as e:
            log(f"f1 timing loops: {e}")
        model.f1_wake.wait(60)
        model.f1_wake.clear()


def f1_follow_live(model, event, session, track, base):
    """Stay on F1's live timing while this session's window lasts; returns
    the final running order as ("live", rows) for the results panel."""
    loops = None
    try:
        loops = f1_segment_fractions(event, event["year"])
    except Exception as e:
        log(f"f1 timing loops: {e}")
    tracker = CarTracker(track.lap_time if track else 90.0,
                         loops and loops.get("fractions"))
    topics, last = {}, None

    def window_open():
        now = datetime.datetime.now().astimezone()
        return model.mode == "f1" and now <= session["end"] + datetime.timedelta(seconds=F1_LIVE_AFTER)

    while window_open():
        try:
            ws = f1_connect()
        except Exception as e:
            log(f"f1 live timing: {e}")
            model.set_f1_status("live timing unreachable - retrying", COL_RED)
            model.f1_wake.wait(15)
            model.f1_wake.clear()
            continue
        model.set_f1_status("F1 live timing", COL_GREEN)
        published, pinged = 0.0, time.monotonic()
        try:
            while window_open():
                for topic, data, whole in f1_messages(ws, 0.5):
                    t = time.monotonic()
                    if topic == "TimingData":
                        tracker.update(data, t)
                    if whole or topic not in topics or not isinstance(topics[topic], (dict, list)):
                        topics[topic] = data
                    else:
                        deep_merge(topics[topic], data)
                t = time.monotonic()
                if t - pinged > 10:
                    ws.send('{"type":6}\x1e')
                    pinged = t
                if t - published >= 0.5 and topics:
                    last = f1_live_view(topics, tracker.snapshot(), session)
                    model.set_f1(dict(base, **last))
                    published = t
        except Exception as e:
            log(f"f1 live timing dropped: {e}")
        finally:
            ws.close()
    return ("live", last["tower"]) if last and last["tower"] else None


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


def demo_plane_photo():
    """A stand-in aircraft photo, encoded like a download would be."""
    s = pygame.Surface((420, 280))
    for y in range(280):
        pygame.draw.line(s, (70 + y // 5, 120 + y // 6, 190 - y // 8), (0, y), (419, y))
    Renderer.plane_icon(s, 210, 150, 150, 90, (235, 235, 240))
    buf = io.BytesIO()
    pygame.image.save(s, buf, "photo.png")
    return buf.getvalue()


def demo_sky(model, t, start, photo, liveries=None):
    """A United 737 crossing westbound about every 5 minutes, plus a neighbour
    - in its livery if pi/build_liveries.py's art is installed."""
    lat0, lon0 = model.cfg.planes_lat, model.cfg.planes_lon
    k = ((t - start) / 300) % 1.0
    plat, plon = lat0 + 0.05, lon0 + 0.25 - 0.5 * k
    plane = {"hex": "a4f0e5", "icao": True, "callsign": "UAL1234", "reg": "N37522",
             "type": "B39M", "desc": "BOEING 737 MAX 9", "year": "2018",
             "lat": plat, "lon": plon, "alt": 12500, "gs": 310.0, "track": 270.0, "rate": 1400.0,
             "squawk": "4312", "seen": 0.2, "ground": False,
             "dist": distance_nm(lat0, lon0, plat, plon),
             "bearing": bearing_deg(lat0, lon0, plat, plon)}
    other = dict(plane, hex="c0ffee", lat=lat0 - 0.12, lon=lon0 + 0.1, track=45.0,
                 dist=distance_nm(lat0, lon0, lat0 - 0.12, lon0 + 0.1),
                 bearing=bearing_deg(lat0, lon0, lat0 - 0.12, lon0 + 0.1))
    model.set_sky({
        "home": (lat0, lon0), "radius": 15.0, "planes": [plane, other], "source": "demo",
        "focus": dict(plane, trail=[(plat, plon + 0.02 * i) for i in range(8, -1, -1)]),
        "route": {"airports": [
            {"iata": "JFK", "icao": "KJFK", "city": "New York",
             "name": "John F Kennedy International Airport", "lat": 40.6398, "lon": -73.7789},
            {"iata": "LAX", "icao": "KLAX", "city": "Los Angeles",
             "name": "Los Angeles International Airport", "lat": 33.9425, "lon": -118.408}],
            "airline": "", "airline_iata": ""},
        "airline": {"name": "United Airlines", "iata": "UA"},
        "model_name": nice_model(plane["desc"]), "no_photo": False,
        "art": plane_art_for(liveries, "UAL", plane["type"]),
        "photo": ("https://www.planespotters.net/", photo, "demo photo")})


DEMO_GRID = [("VER", "3671C6"), ("NOR", "FF8000"), ("LEC", "E8002D"), ("PIA", "FF8000"),
             ("RUS", "27F4D2"), ("HAM", "E8002D"), ("ANT", "27F4D2"), ("ALO", "229971"),
             ("SAI", "64C4FF"), ("GAS", "0093CC"), ("ALB", "64C4FF"), ("HUL", "52E252"),
             ("TSU", "3671C6"), ("OCO", "B6BABD"), ("STR", "229971"), ("BOR", "52E252"),
             ("LAW", "6692FF"), ("HAD", "6692FF"), ("BEA", "B6BABD"), ("COL", "0093CC")]


def demo_f1(t, start, track, event):
    """--demo: a 20-car race on a made-up circuit, cars gliding round."""
    n, mono = 24, time.monotonic()
    bounds = [(g + 1) / n for g in range(n)]
    cars, runs = {}, []
    for i, (tla, _) in enumerate(DEMO_GRID):
        lap = 88.0 + i * 0.35
        run = (t - start) / lap + 0.9 - i * 0.012  # laps covered
        f = run % 1.0
        passed = int(f * n) - 1  # the last loop crossed; -1 = the line
        g = n - 1 if passed < 0 else passed
        f0 = 0.0 if g == n - 1 else bounds[g]
        cars[str(i + 1)] = {"g": g, "t": mono - (f - f0) * lap - F1_FEED_LAG, "lap": lap,
                            "pit": False, "out": False}
        runs.append((run, i))
    runs.sort(reverse=True)
    tower = [(pos, DEMO_GRID[i][0], f1_team_color(DEMO_GRID[i][1]),
              "LEADER" if pos == 1 else f"+{(runs[pos - 2][0] - run) * 89:.3f}", "")
             for pos, (run, i) in enumerate(runs, 1)]
    session = {"key": 0, "name": "Race", "type": "Race",
               "start": event["start"], "end": event["end"]}
    lap_no = int(runs[0][0]) + 1
    return {"phase": "live", "event": event, "track": track, "facts": {}, "session": session,
            "race": True, "state": "Started", "flag": ("GREEN FLAG", COL_GREEN) if lap_no % 6 else
            ("VIRTUAL SAFETY CAR", COL_YELLOW), "tower": tower,
            "drivers": {str(i + 1): (tla, f1_team_color(c)) for i, (tla, c) in enumerate(DEMO_GRID)},
            "message": f"DRS ENABLED" if lap_no % 6 else "VIRTUAL SAFETY CAR DEPLOYED",
            "fastest": (DEMO_GRID[0][0], 91.254, f1_team_color(DEMO_GRID[0][1])),
            "weather": "air 24°  ·  track 38°", "laps": (lap_no, 57), "clock": None,
            "cars": {"bounds": bounds, "cars": cars}}


def demo_f1_track():
    """A made-up circuit: a long straight, a hairpin and some esses."""
    xs, ys = [], []
    for i in range(480):
        a = 2 * math.pi * i / 480
        xs.append(9000 * math.cos(a) + 1400 * math.cos(3 * a))
        ys.append(4200 * math.sin(a) + 900 * math.sin(2 * a) - 700 * math.sin(4 * a))
    return TrackMap({"x": xs, "y": ys, "rotation": 0, "trackPositionTime": [i * 0.19 for i in range(480)],
                     "corners": [{}] * 14, "pitLoss": {"normal": "20.5"}})


def demo_worker(model):
    """--demo: moving numbers, a thinking spinner every other 8s, a fake song,
    a fake print and a fake plane."""
    art = demo_art()
    model.set_art("demo:art", art)
    photo = demo_plane_photo()
    liveries = load_liveries(model.cfg.planes_liveries)
    f1_track_map = demo_f1_track()
    today = datetime.datetime.now().astimezone()
    f1_event = {"key": 0, "name": "Demo Grand Prix", "location": "Demo City", "country": "", "circuit": "Demo",
                "circuit_key": 0, "year": today.year, "start": today, "end": today, "sessions": []}
    start = time.time()
    while True:
        t = time.time()
        now = datetime.datetime.now().astimezone()
        demo_sky(model, t, start, photo, liveries)
        model.set_f1(demo_f1(t, start, f1_track_map, f1_event))
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
            elif want == "planes":
                self._send(409, "planes screen not set up - run "
                                "python3 pi/claude_display.py --setup-planes\n")
            elif want == "f1":
                self._send(409, "the F1 screen is turned off ([f1] enabled in config.ini)\n")
            else:
                self._send(409, "printer not configured - run "
                                "python3 pi/claude_display.py --setup-bambu\n")
        elif path == "/usage":
            self._send(200, json.dumps(m.usage_json()) + "\n", "application/json")
        elif path == "/planes/log":
            self._send(200, planes_log_html(read_plane_log(PLANES_LOG)), "text/html; charset=utf-8")
        elif path == "/planes/log.csv":
            self._send(200, planes_log_csv(read_plane_log(PLANES_LOG)), "text/csv; charset=utf-8")
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
        self.photo_url = None  # the plane photo currently decoded
        self.photo_img = None
        self.qr_link = self.qr_grid = self.qr_key = self.qr_surf = None  # its page, as a QR
        self.plane_art_key = self.plane_art_img = None  # the plane's illustration, scaled
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
        """The Spotify mark: three curved strokes with round ends, drawn as
        circles stamped along a curve so each can thicken toward its right end."""
        pygame.draw.circle(big, color, (cx, cy), radius)
        for (x0, y0), (xc, yc), (x1, y1), w0, w1 in SPOTIFY_ARCS:
            for i in range(97):
                t, s = i / 96, 1 - i / 96  # quadratic Bezier from start to end
                x = s * s * x0 + 2 * s * t * xc + t * t * x1
                y = s * s * y0 + 2 * s * t * yc + t * t * y1
                pygame.draw.circle(big, bg, (cx + x * radius, cy + y * radius),
                                   radius * (w0 + (w1 - w0) * t) / 2)

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
        if snap.mode == "planes":
            sky = snap.sky or {}
            f = sky.get("focus")
            plane = f and (f["hex"], round(f["lat"], 4), round(f["lon"], 4), f["alt"],
                           f["gs"] and round(f["gs"]), f["track"] and round(f["track"]),
                           len(f.get("trail") or []))
            others = tuple((p["hex"], round(p["lat"], 3), round(p["lon"], 3))
                           for p in sky.get("planes") or [])
            return key + (snap.sky is None, plane, others, repr(sky.get("route")),
                          repr(sky.get("airline")), sky.get("model_name"), sky.get("no_photo"),
                          (sky.get("art") or {}).get("path"),
                          (sky.get("photo") or (None,))[0], snap.planes_status, snap.thinking)
        if snap.mode == "f1":
            f = snap.f1 or {}
            if f.get("phase") == "live":
                clock = int(f1_clock_left(f["clock"], now)) if f.get("clock") else None
                return key + ("live", f["session"]["key"], tuple(f.get("tower") or ()), f.get("message"),
                              f.get("flag"), f.get("state"), f.get("laps"), clock, f.get("fastest"),
                              f.get("weather"), id(f.get("track")), snap.f1_status, snap.thinking)
            ev = f.get("event") or {}
            return key + ("off", ev.get("key"), repr(f.get("results")), repr(f.get("next")),
                          repr(f.get("facts")), id(f.get("track")), snap.f1_status, snap.thinking)
        if snap.mode == "bambu":
            # only what's drawn, so fan speeds and wifi strength don't cause redraws
            v = printer_view(snap.printer, now)
            return key + (snap.printer is None, v.word, v.job, v.pct, v.left, v.eta, v.layers,
                          v.temps, snap.printer_status, snap.thinking)
        playing = snap.np is not None and snap.np.get("has_track")
        return key + (snap.np_version, snap.sp_status, snap.art and snap.art[0], snap.thinking,
                      self.progress_ms(snap) // 1000 if playing else -1)

    def draw(self, surf, snap, now):
        if snap.mode != "planes" and self.photo_img is not None:
            self.photo_url = self.photo_img = None  # planespotters: only while it's on screen
        surf.fill(COL_BG)
        getattr(self, f"_{snap.mode}_{self.layout}")(surf, snap, now)
        self._status(surf, snap)

    # where the status line starts: (x, baseline, text size)
    STATUS = {"landscape": (32, 454, 16), "portrait": (12, 315, 10),
              "bar": (28, 300, 19), "strip": (24, 1414, 18)}

    def _status(self, surf, snap):
        addr = f"{snap.host}.local  {snap.ip}".rstrip()
        status = snap.flash or {"usage": snap.usage_status, "spotify": snap.sp_status,
                                "bambu": snap.printer_status,
                                "planes": snap.planes_status,
                                "f1": snap.f1_status}[snap.mode]
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
        return self.layout in self.PANEL_LAYOUTS and (snap.thinking or bool(snap.sessions))

    def advance(self, snap):
        """Step the open / close slide toward where it should be; True while it moves."""
        target = 1.0 if self.session_open(snap) else 0.0
        dt = min(0.05, snap.mono - self.anim_at) if self.anim_at else 0.0
        self.anim_at = snap.mono
        if snap.mode != "usage":
            # Only the usage screen has the slide: elsewhere, just be where it
            # should be. (Sliding here would paint the usage bars' slide frames
            # over the other screen, and coming back finds the panel as it was.)
            self.anim = target
            return False
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

    def fit_size(self, s, max_w, size, bold=False, smallest=24):
        """The largest font size up to `size` at which s fits in max_w."""
        while size > smallest and self.width(s, size, bold) > max_w:
            size -= 2
        return size

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
        elif brand == "planes":
            s = self.n(56)
            icon = self._ss(("title-plane",), s, s,
                            lambda big, k: self.plane_icon(big, s * k / 2, s * k / 2, s * k * 0.48,
                                                           45, COL_ORANGE))
            surf.blit(icon, (self.x(60) - s // 2, self.y(52) - s // 2))
            title, color, sub = "Planes overhead", COL_ORANGE, subtitle
        elif brand == "f1":
            self.text(surf, "F1", 34, 66, 44, COL_F1, bold=True)
            title, color, sub = "Formula 1", COL_F1, subtitle
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
            label = "--" if pct is None else f"{round(pct)}%"
            size = self.fit_size(label, x1 - xb - 16, 54, bold=True)  # "100%" in a wide font
            self.text(surf, label, x1, top + 79, size, COL_TEXT, bold=True, align="r")
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
            label = "--" if v.pct is None else f"{v.pct}%"
            size = self.fit_size(label, x1 - xb - 16, 60, bold=True)  # "100%" in a wide font
            self.text(surf, label, x1, 148, size, COL_TEXT, bold=True, align="r")
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


    # -- the planes-overhead screen: exact airframe photo | route and flight | radar
    @staticmethod
    def plane_icon(big, cx, cy, size, track, color):
        """PLANE_SHAPE turned to `track` (degrees clockwise from north); size
        is its half-length in pixels."""
        a = math.radians(track or 0)
        ca, sa = math.cos(a), math.sin(a)
        pygame.draw.polygon(big, color, [(cx + (x * ca - y * sa) * size, cy + (x * sa + y * ca) * size)
                                         for x, y in PLANE_SHAPE])

    QR_LIGHT = (236, 236, 236)

    def plane_qr(self, link, size):
        """The photo's page as a QR code at most `size` design units square,
        in whole pixels per module so it stays crisp - or None if it can't be
        drawn big enough (2 px a module) to scan."""
        if link != self.qr_link:
            self.qr_link, self.qr_grid = link, None
            try:
                self.qr_grid = qr_matrix(link.encode())
            except ValueError as e:
                log(f"photo link: {e}")
        if not self.qr_grid:
            return None
        n = len(self.qr_grid) + 4  # a 2-module light margin
        m = int(size * self.s) // n
        if m < 2:
            return None
        if self.qr_key != (link, m):
            img = pygame.Surface((n * m, n * m))
            img.fill(self.QR_LIGHT)
            for y, row in enumerate(self.qr_grid):
                for x, dark in enumerate(row):
                    if dark:
                        img.fill(COL_BG, ((x + 2) * m, (y + 2) * m, m, m))
            self.qr_key, self.qr_surf = (link, m), img
        return self.qr_surf

    def plane_photo(self, surf, rect, sky, qr_at=None, empty=None):
        """The photo of this exact aircraft, cover-cropped into rect, with
        the QR code of its planespotters page at qr_at (x, y, max size) -
        both or neither, so where a scannable code won't fit (or qr_at is
        None) the placeholder shows instead, in `empty` if given. Returns
        the QR code's size in design units, or 0 if it was the placeholder."""
        sky = sky or {}
        photo = sky.get("photo") if sky.get("focus") else None
        qr = photo and qr_at and self.plane_qr(photo[0], qr_at[2])
        if not qr:
            photo = None
        if photo and photo[0] != self.photo_url:
            self.photo_url, self.photo_img = photo[0], None
            try:
                img = pygame.image.load(io.BytesIO(photo[1]), "photo.jpg").convert()
                iw, ih = img.get_size()
                want = rect.w / rect.h
                if iw / ih > want:
                    cw = int(ih * want)
                    crop = pygame.Rect((iw - cw) // 2, 0, cw, ih)
                else:
                    ch = int(iw / want)
                    crop = pygame.Rect(0, (ih - ch) // 2, iw, ch)
                self.photo_img = pygame.transform.smoothscale(img.subsurface(crop), rect.size)
            except Exception as e:
                log(f"could not decode the plane photo: {e}")
        if photo and self.photo_img is not None and self.photo_url == photo[0]:
            if self.photo_img.get_size() != rect.size:
                self.photo_img = pygame.transform.smoothscale(self.photo_img, rect.size)
            surf.blit(self.photo_img, rect.topleft)
            surf.blit(qr, (self.x(qr_at[0]), self.y(qr_at[1])))
            return qr.get_width() / self.s

        rect = empty or rect

        def draw(big, k):
            pygame.draw.rect(big, COL_CARD, big.get_rect(), border_radius=int(rect.h * k * 0.05))
            w, h = big.get_size()
            self.plane_icon(big, w / 2, h / 2, h * 0.34, 90, COL_DIM)

        surf.blit(self._ss(("photo-placeholder",), rect.w, rect.h, draw), rect.topleft)
        return 0

    def radar(self, surf, sky, cx, cy, r, label=16):
        """Top-down, north up, you in the middle: range rings, the other
        planes, and the followed plane with its trail and the next few
        minutes of its heading."""
        px = self.n(2 * r)
        big = pygame.Surface((px * SS, px * SS), pygame.SRCALPHA)
        big.fill((*COL_BG, 0))
        c, R = px * SS / 2, px * SS / 2 * 0.94
        sky = sky or {}
        radius, home = sky.get("radius") or 15, sky.get("home")
        w = max(2, round(SS * self.s * 1.3))
        for frac in (1.0, 0.5):
            pygame.draw.circle(big, COL_CARD, (c, c), R * frac, w)
        pygame.draw.line(big, COL_DIM, (c, c - R), (c, c - R * 0.9), w)

        def to_xy(lat, lon):  # flat-earth is fine over a few dozen miles
            dx = (lon - home[1]) * 60 * math.cos(math.radians(home[0]))
            dy = (lat - home[0]) * 60
            return c + dx / radius * R, c - dy / radius * R

        focus, dest_label = sky.get("focus"), None
        if home:
            for p in sky.get("planes") or []:
                if not focus or p["hex"] != focus["hex"]:
                    self.plane_icon(big, *to_xy(p["lat"], p["lon"]), R * 0.055, p["track"], COL_DIM)
            if focus:
                x0, y0 = to_xy(focus["lat"], focus["lon"])
                trail = [to_xy(*ll) for ll in focus.get("trail") or []]
                if len(trail) > 1:
                    pygame.draw.lines(big, (150, 84, 62), False, trail, w)
                if focus["track"] is not None and focus["gs"]:
                    ahead = min(focus["gs"] * 4 / 60, radius * 2)  # the next 4 minutes
                    a = math.radians(focus["track"])
                    dx, dy = math.sin(a) * ahead / radius * R, -math.cos(a) * ahead / radius * R
                    for i in range(0, 12, 2):  # dashed
                        pygame.draw.line(big, COL_ORANGE, (x0 + dx * i / 12, y0 + dy * i / 12),
                                         (x0 + dx * (i + 1) / 12, y0 + dy * (i + 1) / 12), w)
                self.plane_icon(big, x0, y0, R * 0.12, focus["track"], COL_ORANGE)
                leg = route_leg((sky.get("route") or {}).get("airports") or [], focus["lat"],
                                focus["lon"], focus["track"])
                dest = leg[1] if leg else {}
                code = dest.get("iata") or dest.get("icao")
                if code:
                    dest_label = (code, bearing_deg(home[0], home[1], dest["lat"], dest["lon"]))
                    # a notch on the ring pointing the way its destination lies
                    a = math.radians(dest_label[1])
                    ux, uy = math.sin(a), -math.cos(a)
                    tip, back, half = R * 1.05, R * 0.9, R * 0.07
                    pygame.draw.polygon(big, COL_ORANGE, [
                        (c + ux * tip, c + uy * tip),
                        (c + ux * back - uy * half, c + uy * back + ux * half),
                        (c + ux * back + uy * half, c + uy * back - ux * half)])
        pygame.draw.circle(big, COL_TEXT, (c, c), R * 0.04)  # you
        surf.blit(pygame.transform.smoothscale(big, (px, px)),
                  (self.x(cx) - px // 2, self.y(cy) - px // 2))
        self.text(surf, "N", cx, cy - r * 0.94 + label * 1.45, label, COL_DIM, align="c")
        self.text(surf, f"{radius:.0f} nm", cx + r * 0.94, cy + r * 0.94, label * 0.8, COL_DIM,
                  align="r")
        if dest_label:
            a = math.radians(dest_label[1])
            lx, ly = cx + math.sin(a) * r * 0.72, cy - math.cos(a) * r * 0.72
            self.text(surf, dest_label[0], lx, ly + label * 0.35, label * 0.85, COL_ORANGE,
                      bold=True, align="c")

    def plane_head(self, surf, v, x0, x1, base, size, sub):
        """The headline: where it's going, big, with that airport's name under
        it, and the flight number and airline on the right. With no route
        known the flight itself is the headline."""
        fsize, asize = size * 0.46, sub * 0.9
        right_w = max(self.width(v.flight, fsize, True) if v.dest else 0,
                      min(self.width(v.airline, asize), (x1 - x0) * 0.45))
        big = v.dest or v.flight
        room = x1 - x0 - (right_w + size * 0.4 if right_w else 0)
        bsize = self.fit_size(big, room, size, bold=True, smallest=int(size * 0.5))
        self.text(surf, self.fit(big, room, bsize, bold=True), x0, base, bsize, COL_TEXT, bold=True)
        if v.dest:
            self.text(surf, v.flight, x1, base - size * 0.36, fsize, COL_ORANGE, bold=True, align="r")
        if v.airline:
            self.text(surf, self.fit(v.airline, right_w, asize), x1, base, asize, COL_DIM, align="r")
        if v.dest:
            name = v.dest_name
            if v.dest_city and v.dest_city.lower() not in name.lower():
                name = f"{name}, {v.dest_city}" if name else v.dest_city
            under, color = name, COL_SUB
        else:
            under, color = "route unknown", COL_DIM
        self.text(surf, self.fit(under, x1 - x0, sub), x0, base + sub * 1.7, sub, color)

    def route_progress(self, surf, v, x0, x1, base, size):
        """from JFK ━━━━✈──── 1,832 mi to go: the plane sits as far along the
        line as it is along its route."""
        if not v.origin:
            return
        left = f"from {v.origin}"
        right = f"{v.to_go:,.0f} mi to go" if v.to_go is not None else ""
        gap = size * 0.7
        wl = self.width(left, size)
        wr = self.width(right, size) if right else 0
        if x1 - x0 - wl - wr - 2 * gap < size * 5:
            right, wr = "", 0
        self.text(surf, left, x0, base, size, COL_DIM)
        if right:
            self.text(surf, right, x1, base, size, COL_DIM, align="r")
        lx0, lx1 = x0 + wl + gap, x1 - (wr + gap if right else 0)
        s = self.n(size * 1.5)
        span = lx1 - lx0 - s / self.s
        if span < size:
            return
        ly = base - size * 0.34
        at = lx0 + s / self.s / 2 + span * (v.progress if v.progress is not None else 0.5)
        t = max(1.5, size * 0.12)
        surf.fill(COL_CARD, self.rect(lx0, ly - t / 2, lx1 - lx0, t))
        if v.progress is not None:
            surf.fill(COL_ORANGE, self.rect(lx0, ly - t / 2, at - lx0, t))
        icon = self._ss(("route-plane", s), s, s,
                        lambda big, k: self.plane_icon(big, s * k / 2, s * k / 2, s * k * 0.46, 90,
                                                       COL_ORANGE))
        surf.blit(icon, (self.x(at) - s // 2, self.y(ly) - s // 2))

    def plane_stats(self, surf, v, x0, x1, base, size, lines=1, step=0):
        """Altitude, speed, heading, how far from you - on one line, or one
        per line. An emergency squawk leads, in red."""
        items = ([v.alert] if v.alert else []) + v.stats
        if lines == 1:
            items = ["   ·   ".join(items)]
        for i, item in enumerate(items[:lines]):
            color = COL_RED if v.alert and i == 0 else COL_DIM
            self.text(surf, self.fit(item, x1 - x0, size), x0, base + i * step, size, color)

    def plane_art(self, surf, rect, sky):
        """pi/build_liveries.py's side view of this plane - its livery, or
        the type unpainted - fitted into rect. True if there was one."""
        art = sky.get("art") if sky.get("focus") else None
        if not art:
            return False
        key = (art["path"], rect.size)
        if self.plane_art_key != key:
            self.plane_art_key, self.plane_art_img = key, None
            try:
                img = pygame.image.load(art["path"]).convert_alpha()
                k = min(rect.w / img.get_width(), rect.h / img.get_height())
                self.plane_art_img = pygame.transform.smoothscale(
                    img, (max(1, round(img.get_width() * k)), max(1, round(img.get_height() * k))))
            except Exception as e:
                log(f"could not load {art['path']}: {e}")
        img = self.plane_art_img
        if img is None:
            return False
        surf.blit(img, (rect.x + (rect.w - img.get_width()) // 2,
                        rect.y + (rect.h - img.get_height()) // 2))
        return True

    def plane_picture(self, surf, sky, art, photo, qr_at, credit, label=None, empty=None):
        """The plane's picture: its illustration in the rect `art`; failing
        that, the planespotters photo in `photo` with the QR code of its page
        at qr_at, which their terms ask for (label(qr size) places "scan for
        the full photo"). credit = (x, base, width, size) of the credit line."""
        sky = sky or {}
        x, base, w, size = credit
        if self.plane_art(surf, art, sky):
            text = ("illustration © Norebbo" if sky["art"]["kind"] == "livery"
                    else "blank livery · illustration © Norebbo")
            self.text(surf, self.fit(text, w, size), x, base, size, COL_DIM)
            return
        qr = self.plane_photo(surf, photo, sky, qr_at=qr_at, empty=empty)
        if qr and label:
            self.qr_label(surf, *label(qr))
        self.plane_credit(surf, sky, qr, x, base, w, size)

    def plane_credit(self, surf, sky, qr, x, base, w, size):
        """planespotters' terms: the photographer, by name, next to the photo."""
        if qr:
            self.text(surf, self.fit(f"© {sky['photo'][2]} / Planespotters.net", w, size), x, base,
                      size, COL_DIM)
        elif (sky or {}).get("focus") and sky.get("no_photo"):
            self.text(surf, "no photo of this one yet", x, base, size, COL_DIM)

    def qr_label(self, surf, x, base, size, align="l"):
        self.text(surf, "scan for the", x, base, size, COL_DIM, align=align)
        self.text(surf, "full photo", x, base + size * 1.3, size, COL_DIM, align=align)

    def quiet_sky(self, surf, snap, x, base, size, align="l"):
        sky = snap.sky or {}
        self.text(surf, "quiet skies" if snap.sky else "looking for planes...", x, base, size,
                  COL_DIM, bold=True, align=align)
        if snap.sky:
            self.text(surf, f"nothing flying within {sky['radius']:.0f} nm right now", x,
                      base + size * 0.95, max(9, size * 0.5), COL_DIM, align=align)

    def _planes_bar(self, surf, snap, now):
        sky = snap.sky or {}
        v = plane_view(sky)
        self.plane_picture(surf, sky, self.rect(24, 26, 428, 186), self.rect(24, 30, 300, 172),
                           (340, 30, 100), (24, 236, 428, 14),
                           label=lambda qr: (340 + qr / 2, 30 + qr + 22, 13, "c"),
                           empty=self.rect(24, 30, 428, 172))
        surf.fill(COL_CARD, self.rect(470, 40, 2, 196))
        surf.fill(COL_CARD, self.rect(1124, 40, 2, 196))
        self.radar(surf, sky, 1290, 150, 124)
        x0, x1 = 498, 1098
        if not v:
            self.quiet_sky(surf, snap, x0, 130, 44)
        else:
            self.plane_head(surf, v, x0, x1, 96, 66, 20)
            self.route_progress(surf, v, x0, x1, 166, 18)
            self.text(surf, self.fit(v.aircraft, x1 - x0, 22), x0, 202, 22, COL_TEXT)
            self.plane_stats(surf, v, x0, x1, 236, 20)
        self._clock_bar(surf, now)

    def _planes_landscape(self, surf, snap, now):
        sky = snap.sky or {}
        self._header_landscape(surf, now, "planes",
                               f"within {sky.get('radius', self.PLANES_DEFAULT_NM):.0f} nm of you")
        v = plane_view(sky)
        self.plane_picture(surf, sky, self.rect(32, 126, 228, 152), self.rect(32, 126, 228, 152),
                           (32, 312, 100), (32, 298, 228, 12),
                           label=lambda qr: (32 + qr + 12, 312 + qr / 2 - 4, 13))
        self.radar(surf, sky, 668, 262, 100, label=14)
        x0, x1 = 282, 556
        if not v:
            self.quiet_sky(surf, snap, x0, 200, 30)
            return
        self.plane_head(surf, v, x0, x1, 170, 46, 14)
        self.route_progress(surf, v, x0, x1, 226, 13)
        self.text(surf, self.fit(v.aircraft, x1 - x0, 15), x0, 256, 15, COL_TEXT)
        self.plane_stats(surf, v, x0, x1, 286, 15, lines=4, step=21)

    def _planes_portrait(self, surf, snap, now):
        # the illustration if there is one, else the radar: too small for a
        # photo's QR code (see plane_photo)
        sky = snap.sky or {}
        s = self.n(34)
        icon = self._ss(("title-plane",), s, s,
                        lambda big, k: self.plane_icon(big, s * k / 2, s * k / 2, s * k * 0.48, 45,
                                                       COL_ORANGE))
        surf.blit(icon, (self.x(38) - s // 2, self.y(32) - s // 2))
        self.text(surf, "Overhead", 72, 30, 16, COL_ORANGE, bold=True)
        self.text(surf, f"within {sky.get('radius', self.PLANES_DEFAULT_NM):.0f} nm", 72, 47, 11,
                  COL_DIM)
        v = plane_view(sky)
        if self.plane_art(surf, self.rect(12, 66, 156, 100), sky):
            self.text(surf, "illustration © Norebbo", 12, 178, 8, COL_DIM)
        else:
            self.radar(surf, sky, 90, 124, 58, label=9)
        if not v:
            self.quiet_sky(surf, snap, 90, 222, 15, align="c")
            return
        self.plane_head(surf, v, 12, 168, 222, 30, 9)
        self.route_progress(surf, v, 12, 168, 252, 9)
        self.text(surf, self.fit(v.aircraft, 156, 10), 12, 270, 10, COL_TEXT)
        self.plane_stats(surf, v, 12, 168, 287, 9)

    def _planes_strip(self, surf, snap, now):
        sky = snap.sky or {}
        s = self.n(90)
        icon = self._ss(("title-plane",), s, s,
                        lambda big, k: self.plane_icon(big, s * k / 2, s * k / 2, s * k * 0.48, 45,
                                                       COL_ORANGE))
        surf.blit(icon, (self.x(160) - s // 2, self.y(96) - s // 2))
        self.text(surf, "Overhead", 160, 192, 34, COL_ORANGE, bold=True, align="c")
        self.text(surf, f"within {sky.get('radius', self.PLANES_DEFAULT_NM):.0f} nm of you", 160,
                  224, 20, COL_DIM, align="c")
        v = plane_view(sky)
        self.plane_picture(surf, sky, self.rect(24, 250, 272, 181), self.rect(24, 250, 272, 181),
                           (24, 476, 100), (24, 458, 272, 14),
                           label=lambda qr: (24 + qr + 14, 476 + qr / 2 - 4, 16))
        self.radar(surf, sky, 160, 1022, 132, label=18)
        if v:
            self.plane_head(surf, v, 24, 296, 642, 60, 18)
            self.route_progress(surf, v, 24, 296, 712, 16)
            self.text(surf, self.fit(v.aircraft, 272, 19), 24, 748, 19, COL_TEXT)
            self.plane_stats(surf, v, 24, 296, 786, 18, lines=3, step=28)
        else:
            self.quiet_sky(surf, snap, 160, 660, 26, align="c")
        self._clock_strip(surf, now)

    PLANES_DEFAULT_NM = 15

    # -- the F1 screen: the track (cars on it when live) | live timing or the circuit | results
    F1_MAP = {"bar": (24, 18, 420, 256), "landscape": (32, 118, 330, 300),
              "portrait": (12, 58, 156, 104), "strip": (24, 250, 272, 330)}

    def f1_geometry(self, track, rect):
        """Scale and offset that fit the track into rect (physical pixels)."""
        m = self.n(10)
        bx0, by0, bx1, by1 = track.box
        k = min((rect.w - 2 * m) / max(1.0, bx1 - bx0), (rect.h - 2 * m) / max(1.0, by1 - by0))
        ox = rect.x + (rect.w - (bx1 - bx0) * k) / 2 - bx0 * k
        oy = rect.y + (rect.h - (by1 - by0) * k) / 2 - by0 * k
        return k, ox, oy

    def f1_track_surface(self, track, rect):
        """The track drawn once onto the background, anti-aliased."""
        key = ("f1-track", id(track), rect.size)
        img = self.shape_cache.get(key)
        if img is None:
            k, ox, oy = self.f1_geometry(track, pygame.Rect(0, 0, *rect.size))
            big = pygame.Surface((rect.w * SS, rect.h * SS))
            big.fill(COL_BG)
            pts = [((x * k + ox) * SS, (y * k + oy) * SS) for x, y in track.pts]
            w = max(3, round(self.s * 4.5 * SS))
            for color, width in (((70, 70, 76), w + 2 * SS), ((205, 205, 212), w)):
                pygame.draw.lines(big, color, True, pts, width)
                for p in pts[::2]:
                    pygame.draw.circle(big, color, p, width / 2)
            (x0, y0), (x1, y1) = pts[0], pts[min(3, len(pts) - 1)]  # the start/finish line
            d = math.hypot(x1 - x0, y1 - y0) or 1
            nx, ny = -(y1 - y0) / d * w * 1.6, (x1 - x0) / d * w * 1.6
            pygame.draw.line(big, COL_F1, (x0 - nx, y0 - ny), (x0 + nx, y0 + ny), max(2, w // 2))
            img = pygame.transform.smoothscale(big, rect.size)
            self.shape_cache[key] = img
        return img

    def car_frame(self, snap):
        """The cars' animation step while they're on the map, else -1."""
        f = snap.f1 or {}
        if snap.mode != "f1" or f.get("phase") != "live" or not (f.get("cars") or {}).get("cars"):
            return -1
        return int(snap.mono * 10)

    def draw_f1_map(self, surf, snap):
        """The map with the cars where they are right now; returns its rect
        (live, only this is repainted ten times a second)."""
        f = snap.f1 or {}
        mx, my, mw, mh = self.F1_MAP[self.layout]
        rect = self.rect(mx, my, mw, mh)
        track = f.get("track")
        surf.fill(COL_BG, rect)
        if not track:
            self.text(surf, "no map yet", mx + mw / 2, my + mh / 2, 16, COL_DIM, align="c")
            return rect
        surf.blit(self.f1_track_surface(track, rect), rect.topleft)
        cars = f.get("cars") or {}
        if f.get("phase") == "live" and cars.get("cars"):
            k, ox, oy = self.f1_geometry(track, rect)
            where = car_fractions(cars, snap.mono)
            order = {row[1]: row[0] for row in f.get("tower") or []}
            who = f.get("drivers") or {}
            r = max(3, self.n(min(6.0, mw * 0.014)))
            spots = []
            for num, frac in where.items():
                tla, color = who.get(num, (num, COL_DIM))
                x, y = track.at(frac)
                spots.append((order.get(tla, 99), tla, color, round(x * k + ox), round(y * k + oy)))
            for pos, tla, color, x, y in sorted(spots, reverse=True):  # the leader on top
                def dot(big, s, c=color):
                    mid = big.get_width() / 2
                    pygame.draw.circle(big, COL_BG, (mid, mid), (r + 1) * s)  # keeps close cars apart
                    pygame.draw.circle(big, c, (mid, mid), r * s)
                surf.blit(self._ss(("car", color, r), 2 * r + 2, 2 * r + 2, dot), (x - r - 1, y - r - 1))
                if pos <= 3:
                    self.text(surf, tla, (x + r + 2 - self.ox) / self.s, (y - r - self.oy) / self.s, 12,
                              COL_TEXT, bold=True)
        return rect

    def f1_row(self, surf, x0, x1, base, row, size):
        """One line of a timing tower: position, team colour, driver, time
        (yellow while in the pits, dim once out)."""
        pos, tla, color, text = row[:4]
        state = row[4] if len(row) > 4 else ""
        self.text(surf, str(pos), x0 + size * 1.1, base, size * 0.85, COL_DIM, align="r")
        surf.fill(color, self.rect(x0 + size * 1.4, base - size * 0.78, max(2, size * 0.18), size * 0.9))
        self.text(surf, tla, x0 + size * 1.8, base, size, COL_TEXT, bold=True)
        tone = COL_YELLOW if state == "pit" or text == "PIT" else             COL_DIM if state == "out" or text in ("OUT", "DNF", "DNS", "DSQ") else COL_SUB
        self.text(surf, self.fit(text, x1 - x0 - size * 4.6, size * 0.9), x1, base, size * 0.9, tone,
                  align="r")

    def f1_list(self, surf, rows, x0, x1, top, step, size, cols=1, per_col=5):
        colw = (x1 - x0 - (cols - 1) * size) / cols
        for i, row in enumerate(rows[:cols * per_col]):
            cx = x0 + (i // per_col) * (colw + size)
            self.f1_row(surf, cx, cx + colw, top + (i % per_col) * step, row, size)

    @staticmethod
    def f1_when(when, now):
        """"Fri 8:00 AM" (or "tomorrow 8:00 AM", "8:00 AM" today)."""
        local = when.astimezone(now.tzinfo)
        days = (local.date() - now.date()).days
        day = "" if days == 0 else "tomorrow " if days == 1 else local.strftime("%a ")
        return day + clock_str(local)

    @staticmethod
    def f1_countdown(when, now):
        secs = int((when - now).total_seconds())
        if secs < 3600:
            return f"in {max(1, secs // 60)}m"
        if secs < 86400:
            return f"in {secs // 3600}h {secs % 3600 // 60:02d}m"
        return f"in {secs // 86400}d {secs % 86400 // 3600}h"

    def f1_schedule_rows(self, f, now):
        """The weekend's sessions as rows for the right-hand list."""
        rows = []
        for s in f["event"]["sessions"]:
            done = s["end"] <= now
            rows.append((F1_SHORT.get(s["name"], s["name"].upper()[:8]), self.f1_when(s["start"], now), done))
        return rows

    def f1_panel_right(self, surf, f, now, x0, x1, top, size, step, cols, per_col):
        """Right: the live running order, today's results, or the weekend's schedule."""
        if f.get("phase") == "live":
            self.text(surf, "RUNNING ORDER", x0, top - size * 1.5, size * 0.75, COL_DIM, bold=True)
            self.f1_list(surf, f.get("tower") or [], x0, x1, top, step, size, cols, per_col)
        elif f.get("results"):
            session, rows = f["results"]
            self.text(surf, f"{session['name'].upper()} RESULT", x0, top - size * 1.5, size * 0.75,
                      COL_DIM, bold=True)
            self.f1_list(surf, rows, x0, x1, top, step, size, cols, per_col)
        else:
            self.text(surf, "THIS WEEKEND", x0, top - size * 1.5, size * 0.75, COL_DIM, bold=True)
            nxt = f.get("next")
            for i, (name, when, done) in enumerate(self.f1_schedule_rows(f, now)[:cols * per_col]):
                cx = x0 + (i // per_col) * ((x1 - x0) / cols)
                base = top + (i % per_col) * step
                is_next = nxt and name == F1_SHORT.get(nxt["name"], nxt["name"].upper()[:8])
                color = COL_F1 if is_next else COL_DIM if done else COL_TEXT
                self.text(surf, name, cx, base, size * 0.85, color, bold=True)
                self.text(surf, when, cx + size * 5.2, base, size * 0.85, color)

    def f1_live_middle(self, surf, f, now, x0, x1, top, size):
        """Middle, live: session and flag, the clock or laps, race control, fastest lap."""
        name = f["session"]["name"].upper()
        r = size * 0.3
        pygame.draw.circle(surf, COL_F1, (self.x(x0 + r), self.y(top - size * 0.35)), self.n(r))
        self.text(surf, "LIVE", x0 + r * 2.8, top, size * 0.8, COL_F1, bold=True)
        flag, color = f.get("flag") or ("", COL_DIM)
        state = f.get("state") or ""
        if state in ("Finished", "Finalised", "Ends"):
            flag, color = "CHEQUERED FLAG", COL_TEXT
        elif state == "Aborted":
            flag, color = "SESSION STOPPED", COL_RED
        self.text(surf, flag, x1, top, size * 0.7, color, bold=True, align="r")
        nx = x0 + r * 2.8 + self.width("LIVE", size * 0.8, True) + size * 0.5
        room = x1 - nx - self.width(flag, size * 0.7, True) - size * 0.5
        self.text(surf, self.fit(name, room, size * 0.8, bold=True), nx, top, size * 0.8, COL_TEXT,
                  bold=True)
        if f.get("laps") and f["laps"][0]:
            big = f"LAP {f['laps'][0]}/{f['laps'][1]}"
        elif f.get("clock"):
            left = int(f1_clock_left(f["clock"], now))
            big = f"{left // 3600}:{left % 3600 // 60:02d}:{left % 60:02d}" if left >= 3600 else \
                f"{left // 60}:{left % 60:02d}"
        else:
            big = "--"
        self.text(surf, big, x0, top + size * 2.15, size * 1.9, COL_TEXT, bold=True)
        if f.get("laps") is None and f.get("clock"):
            self.text(surf, "left", x0 + self.width(big, size * 1.9, True) + size * 0.4,
                      top + size * 2.15, size * 0.7, COL_DIM)
        l1, l2 = self.wrap2(f.get("message") or "", x1 - x0, size * 0.62)
        self.text(surf, l1, x0, top + size * 3.3, size * 0.62, COL_SUB)
        if l2:
            self.text(surf, l2, x0, top + size * 4.1, size * 0.62, COL_SUB)
        bits = []
        if f.get("fastest"):
            tla, best, _ = f["fastest"]
            bits.append(f"fastest {tla} {fmt_lap(best)}")
        if f.get("weather"):
            bits.append(f["weather"])
        self.text(surf, self.fit("   ·   ".join(bits), x1 - x0, size * 0.6), x0, top + size * 5.3,
                  size * 0.6, COL_DIM)

    def f1_off_middle(self, surf, f, now, x0, x1, top, size, lines=4):
        """Middle, between sessions: the circuit's name, big, and the weekend at a glance."""
        ev, facts, track = f["event"], f.get("facts") or {}, f.get("track")
        name = facts.get("circuit") or ev["circuit"] or ev["location"]
        big = self.fit_size(name, x1 - x0, int(size * 2.1), bold=True, smallest=int(size * 1.1))
        self.text(surf, self.fit(name, x1 - x0, big, bold=True), x0, top, big, COL_TEXT, bold=True)
        sub = ev["name"] + (f"  ·  Round {facts['round']}" if facts.get("round") else "")
        self.text(surf, self.fit(sub, x1 - x0, size * 0.75), x0, top + size * 1.25, size * 0.75, COL_SUB)
        out = []
        nxt = f.get("next")
        if nxt:
            out.append((f"{nxt['name']} {self.f1_countdown(nxt['start'], now)}  ·  "
                        f"{self.f1_when(nxt['start'], now)}", COL_F1))
        else:
            out.append(("race weekend over", COL_DIM))
        if track:
            bits = [f"{track.length_km:.2f} km"]
            if track.corners:
                bits.append(f"{track.corners} corners")
            if track.pit_loss:
                bits.append(f"pit stop costs {track.pit_loss:.0f} s")
            out.append(("  ·  ".join(bits), COL_DIM))
        bits = []
        if facts.get("last_winner"):
            bits.append(f"{facts['last_winner'][0]} winner {facts['last_winner'][1]}")
        if facts.get("leaders"):
            name0, pts = facts["leaders"][0]
            bits.append(f"leader {name0} {pts} pts")
        if bits:
            out.append(("  ·  ".join(bits), COL_DIM))
        for i, (text, color) in enumerate(out[:lines]):
            s = size * (0.75 if i == 0 else 0.62)
            s = self.fit_size(text, x1 - x0, s, smallest=s * 0.8)  # shrink a little before cutting
            self.text(surf, self.fit(text, x1 - x0, s), x0, top + size * (2.55 + i * 1.12), s, color)

    def f1_waiting(self, surf, snap, x, base, size, align="l"):
        self.text(surf, "Formula 1", x, base, size, COL_F1, bold=True, align=align)
        self.text(surf, "loading the season...", x, base + size * 0.95, size * 0.5, COL_DIM, align=align)

    def _f1_bar(self, surf, snap, now):
        f = snap.f1
        self.draw_f1_map(surf, snap)
        surf.fill(COL_CARD, self.rect(462, 40, 2, 196))
        surf.fill(COL_CARD, self.rect(1030, 40, 2, 196))
        if not f:
            self.f1_waiting(surf, snap, 490, 130, 44)
        elif f.get("phase") == "live":
            self.f1_live_middle(surf, f, now, 490, 1010, 62, 32)
        else:
            self.f1_off_middle(surf, f, now, 490, 1010, 84, 32)
        if f:
            self.f1_panel_right(surf, f, now, 1054, 1456, 80, 20, 38, 2, 5)
        self._clock_bar(surf, now)

    def _f1_landscape(self, surf, snap, now):
        f = snap.f1
        ev = (f or {}).get("event")
        self._header_landscape(surf, now, "f1", ev["name"] if ev else "loading the season...")
        self.draw_f1_map(surf, snap)
        if not f:
            return
        x0, x1 = 384, 768
        if f.get("phase") == "live":
            self.f1_live_middle(surf, f, now, x0, x1, 148, 22)
            self.f1_list(surf, f.get("tower") or [], x0, x1, 300, 23, 15, cols=2, per_col=6)
        else:
            self.f1_off_middle(surf, f, now, x0, x1, 150, 20, lines=3)
            self.f1_panel_right(surf, f, now, x0, x1, 300, 15, 23, 2, 6)

    def _f1_portrait(self, surf, snap, now):
        f = snap.f1
        self.text(surf, "F1", 12, 34, 22, COL_F1, bold=True)
        ev = (f or {}).get("event")
        self.text(surf, self.fit(ev["name"] if ev else "loading...", 120, 11), 48, 32, 11, COL_SUB)
        self.draw_f1_map(surf, snap)
        if not f:
            return
        if f.get("phase") == "live":
            self.f1_live_middle(surf, f, now, 12, 168, 186, 11)
            self.f1_list(surf, f.get("tower") or [], 12, 168, 262, 12, 9, per_col=4)
        else:
            self.f1_off_middle(surf, f, now, 12, 168, 190, 10, lines=2)
            self.f1_panel_right(surf, f, now, 12, 168, 262, 9, 12, 1, 4)

    def _f1_strip(self, surf, snap, now):
        f = snap.f1
        self.text(surf, "F1", 160, 120, 80, COL_F1, bold=True, align="c")
        ev = (f or {}).get("event")
        self.text(surf, self.fit(ev["name"] if ev else "loading the season...", 272, 20), 160, 180, 20,
                  COL_SUB, align="c")
        self.draw_f1_map(surf, snap)
        if f:
            if f.get("phase") == "live":
                self.f1_live_middle(surf, f, now, 24, 296, 640, 26)
            else:
                self.f1_off_middle(surf, f, now, 24, 296, 650, 24, lines=3)
            self.f1_panel_right(surf, f, now, 24, 296, 880, 20, 28, 1, 10)
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
    ap.add_argument("--setup-planes", action="store_true",
                    help="set your location for the planes-overhead screen")
    ap.add_argument("--setup-bambu", action="store_true",
                    help="find your Bambu Lab printer, ask for its access code, save it")
    args = ap.parse_args()

    if args.setup_planes:
        setup_planes(args.config)
        return
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
    if args.demo and not cfg.planes_ready:
        cfg.planes_lat, cfg.planes_lon = 40.70, -73.86  # between JFK and LGA
    model = Model(cfg, state, spotify_ready, bambu_ready=args.demo or cfg.bambu_ready,
                  planes_ready=cfg.planes_ready, f1_ready=cfg.f1_enabled)
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
        if cfg.planes_ready:
            forever(planes_worker, model)
        if cfg.f1_enabled:
            forever(f1_worker, model)

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
    last_key, last_frame, last_cars, last_ip_check = None, -1, -1, 0.0
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
            cars = renderer.car_frame(snap)
            if key != last_key:
                last_key, last_frame, last_cars = key, frame, cars
                renderer.draw(canvas, snap, now)
                present()
            else:  # repaint and push only what moved
                rects = [renderer.draw_slide(canvas, snap, now)] if moving else []
                if frame != last_frame:
                    last_frame = frame
                    rects += renderer.draw_activity(canvas, snap)
                if cars != last_cars:
                    last_cars = cars
                    rects.append(renderer.draw_f1_map(canvas, snap))
                if rects:
                    present(*rects)
            clock.tick(60 if moving else 30 if frame >= 0 or cars >= 0 else 10)
    finally:
        pygame.quit()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
