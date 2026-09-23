#!/usr/bin/env python3
"""Claude Code usage display - Raspberry Pi edition.

The ESP32 display (firmware/src/main.cpp) as a fullscreen app for a Raspberry
Pi - built for a Pi 2, fine on anything newer - on an HDMI monitor or the
official touchscreen:

  - your real 5-hour / weekly utilization, fetched straight from Anthropic's
    OAuth usage API with a dedicated login (server/device_login.py)
  - a spinner while Claude is working, driven by the same HTTP beacons
    (POST /thinking/on, /thinking/off) from Claude Code hooks or beacon.py
  - an optional Spotify now-playing screen (POST /mode/spotify, /mode/usage,
    /mode/toggle - or just tap the screen)

It speaks the firmware's HTTP API on the same port, so the hooks, beacon.py,
find_display.py and the /switch command work unchanged - point them at the Pi.
It also answers GET /usage with the numbers as JSON (the Windows tray helper
reads that).

    python3 pi/claude_display.py                      # fullscreen
    python3 pi/claude_display.py --windowed 800x480   # in a window, for testing
    python3 pi/claude_display.py --demo               # fake data, no logins needed

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
             "working, /thinking/off when done. POST /mode/usage, /mode/spotify or "
             "/mode/toggle to switch screens; GET /mode to ask; GET /usage for JSON.\n")

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
COL_EYE = (0, 0, 0)

SPIN_FRAME_MS = 90  # spinner step, same pace as the firmware

# Pixel-art Clawd, same grid as firmware/src/mascot.h (1 = body, 2 = eye).
MASCOT = [
    [0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0],
    [0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0],
    [1, 1, 1, 1, 2, 2, 1, 2, 2, 1, 1, 1, 1],
    [1, 1, 1, 1, 2, 2, 1, 2, 2, 1, 1, 1, 1],
    [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
    [0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0],
    [0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0],
    [0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0],
    [0, 0, 0, 1, 0, 1, 0, 1, 0, 1, 0, 0, 0],
    [0, 0, 0, 1, 0, 1, 0, 1, 0, 1, 0, 0, 0],
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
        self.usage_poll = max(30.0, num("anthropic", "poll_seconds", 90))
        self.sp_client_id = s("spotify", "client_id")
        self.sp_refresh_token = s("spotify", "refresh_token")
        self.sp_poll = max(1.0, num("spotify", "poll_seconds", 5))
        self.port = int(num("server", "port", 8080))
        self.beacon_ttl = num("server", "beacon_ttl_seconds", 300)
        self.size = parse_size(s("screen", "size"))
        self.rotate = int(num("screen", "rotate", 0)) % 360


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

    def __init__(self, cfg, state, spotify_ready):
        self.cfg, self.state, self.spotify_ready = cfg, state, spotify_ready
        self.lock = threading.Lock()
        self.mode = "spotify" if state.get("mode") == "spotify" and spotify_ready else "usage"

        self.usage = None          # {"five"/"week": (pct or None, reset datetime, iso)}
        self.usage_ok_at = 0.0     # monotonic time of the last good fetch
        self.usage_status = None   # (text, colour); None = nothing yet
        self.usage_version = 0

        self.np = None             # now-playing dict; None = no fetch yet
        self.np_at = 0.0           # monotonic time np["progress_ms"] was current
        self.np_version = 0
        self.sp_status = None
        self.art = None            # (url, image bytes) of the latest album art
        self.art_px = 300          # art size on screen, so we fetch a sharp enough variant

        self.last_beacon = 0.0     # monotonic time of the last "thinking" ping, 0 = off
        self.flash_msg = None      # (text, colour, until): short-lived status override
        self.host = socket.gethostname().split(".")[0]
        self.ip = ""
        self.usage_wake = threading.Event()
        self.spotify_wake = threading.Event()

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
        """Switch screens. False if Spotify was asked for but isn't set up."""
        if mode == "spotify" and not self.spotify_ready:
            return False
        with self.lock:
            if mode == self.mode:
                return True
            self.mode = mode
            if mode == "spotify" and self.np is None:
                self.sp_status = ("fetching spotify...", COL_DIM)
        self.state.put("mode", mode)
        if mode == "spotify":
            self.spotify_wake.set()
        return True

    def toggle_mode(self):
        if not self.set_mode("spotify" if self.mode == "usage" else "usage"):
            self.flash("spotify not set up - see config.ini", COL_YELLOW)

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

    def snapshot(self):
        now = time.monotonic()
        with self.lock:
            flash = self.flash_msg if self.flash_msg and self.flash_msg[2] > now else None
            return SimpleNamespace(
                mode=self.mode, usage=self.usage, usage_version=self.usage_version,
                usage_status=self.usage_status, np=self.np, np_at=self.np_at,
                np_version=self.np_version, sp_status=self.sp_status, art=self.art,
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


def usage_worker(model, login):
    while True:
        delay = model.cfg.usage_poll
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
    """--demo: moving numbers, a thinking spinner every other 8s and a fake song."""
    art = demo_art()
    model.set_art("demo:art", art)
    start = time.time()
    while True:
        t = time.time()
        now = datetime.datetime.now().astimezone()
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
        self._route()

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(min(length, 65536))  # drain it so the client isn't reset
        self._route()

    def _route(self):
        m = self.model
        path = urllib.parse.urlsplit(self.path).path.rstrip("/") or "/"
        if path in ("/thinking", "/thinking/on"):
            m.beacon_on()
            self._send(200, "on\n")
        elif path == "/thinking/off":
            m.beacon_off()
            self._send(200, "off\n")
        elif path == "/mode":
            self._send(200, m.mode + "\n")
        elif path in ("/mode/usage", "/mode/spotify", "/mode/toggle"):
            want = path.rsplit("/", 1)[1]
            if want == "toggle":
                want = "spotify" if m.mode == "usage" else "usage"
            if m.set_mode(want):
                self._send(200, want + "\n")
            else:
                self._send(409, "spotify not configured - run server/spotify_login.py "
                                "--config ~/.config/claude-display/config.ini\n")
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

    def bar(self, surf, rect, frac, color, radius):
        """A rounded track with the left `frac` of it filled."""
        w, h = rect.size
        fill = 0 if frac is None else int(round(w * max(0.0, min(1.0, frac))))
        r = self.n(radius)

        def draw(big, k):
            pygame.draw.rect(big, COL_CARD, big.get_rect(), border_radius=r * k)
            if fill:
                big.set_clip(pygame.Rect(0, 0, fill * k, h * k))
                pygame.draw.rect(big, color, big.get_rect(), border_radius=r * k)
                big.set_clip(None)

        surf.blit(self._ss(("bar", fill, color, r), w, h, draw), rect.topleft)

    def spinner(self, surf, cx, cy, size, angle, color):
        """The firmware's starburst: 8 rays in a 60px box, scaled to `size`."""
        px = self.n(size)

        def draw(big, k):
            c, u = px * k / 2, px * k / 60.0
            for i in range(8):
                a = math.radians(angle + i * 45)
                ca, sa = math.cos(a), math.sin(a)
                p1 = (c + ca * 7 * u, c + sa * 7 * u)
                p2 = (c + ca * 25 * u, c + sa * 25 * u)
                nx, ny = -sa * 1.5 * u, ca * 1.5 * u  # half of the 3px width
                pygame.draw.polygon(big, color, [(p1[0] + nx, p1[1] + ny), (p2[0] + nx, p2[1] + ny),
                                                 (p2[0] - nx, p2[1] - ny), (p1[0] - nx, p1[1] - ny)])
                pygame.draw.circle(big, color, p1, 1.5 * u)
                pygame.draw.circle(big, color, p2, 1.5 * u)
            pygame.draw.circle(big, color, (c, c), 3 * u)

        img = self._ss(("spin", round(angle % 45, 2), color), px, px, draw)
        surf.blit(img, (self.x(cx) - px // 2, self.y(cy) - px // 2))

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
            return key + (snap.usage_version, snap.usage_status, snap.thinking)
        playing = snap.np is not None and snap.np.get("has_track")
        return key + (snap.np_version, snap.sp_status, snap.art and snap.art[0],
                      self.progress_ms(snap) // 1000 if playing else -1)

    def draw(self, surf, snap, now):
        surf.fill(COL_BG)
        getattr(self, f"_{snap.mode}_{self.layout}")(surf, snap, now)
        self._status(surf, snap)

    def _status(self, surf, snap):
        addr = f"{snap.host}.local  {snap.ip}".rstrip()
        status = snap.flash or (snap.sp_status if snap.mode == "spotify" else snap.usage_status)
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

    # (cx, cy, size) of the spinner in each layout's design units
    SPINNER = {"landscape": (162, 246, 150), "portrait": (90, 108, 60),
               "bar": (70, 206, 104), "strip": (160, 410, 190)}

    @staticmethod
    def spin_frame(snap):
        """The spinner's animation step, or -1 while idle."""
        return int(snap.mono * 1000 / SPIN_FRAME_MS) % 48 if snap.thinking else -1

    def draw_spinner(self, surf, snap):
        """Draw just the spinner and return the rect it covers. Animation frames
        repaint and push only this square instead of the whole screen - on a
        Pi 2 under X, full-screen frames cost more CPU than everything else."""
        cx, cy, size = self.SPINNER[self.layout]
        px = self.n(size)
        rect = pygame.Rect(self.x(cx) - px // 2, self.y(cy) - px // 2, px, px)
        surf.fill(COL_BG, rect)
        frame = self.spin_frame(snap)
        self.spinner(surf, cx, cy, size, max(frame, 0) * 7.5,
                     COL_ORANGE if frame >= 0 else COL_CARD)
        return rect

    def _header_landscape(self, surf, now, spotify):
        if spotify:
            self.spotify_logo(surf, 58, 52, 26)
        else:
            self.mascot(surf, 32, 27, 5)
        self.text(surf, "Spotify" if spotify else "Claude Code", 119, 56, 30,
                  COL_SPOTIFY if spotify else COL_ORANGE, bold=True)
        self.text(surf, "now playing" if spotify else "usage monitor", 119, 82, 17, COL_DIM)
        self.text(surf, clock_str(now), 768, 58, 30, COL_TEXT, align="r")
        self.text(surf, f"{now.strftime('%a %b')} {now.day}", 768, 82, 17, COL_DIM, align="r")
        surf.fill(COL_CARD, self.rect(32, 104, 736, 2))

    def _usage_landscape(self, surf, snap, now):
        self._header_landscape(surf, now, spotify=False)
        active = snap.thinking
        self.draw_spinner(surf, snap)
        self.text(surf, "working..." if active else "idle", 162, 362, 26,
                  COL_ORANGE if active else COL_DIM, align="c")

        x0, x1 = 332, 768
        for i, (label, key) in enumerate((("5-HOUR", "five"), ("WEEKLY", "week"))):
            top = 132 + i * 150
            pct, when, _ = snap.usage[key] if snap.usage else (None, None, "")
            self.text(surf, label, x0, top + 22, 20, COL_DIM, bold=True)
            self.text(surf, "--" if pct is None else f"{round(pct)}%", x1, top + 30, 44,
                      COL_TEXT, bold=True, align="r")
            if when:
                self.text(surf, f"resets {fmt_reset(when, now)}  \u00b7  {fmt_until(when, now)}",
                          x0, top + 50, 17, COL_DIM)
            self.bar(surf, self.rect(x0, top + 62, x1 - x0, 40),
                     None if pct is None else pct / 100, bar_color(pct), 12)

    def _usage_portrait(self, surf, snap, now):
        self.mascot(surf, 12, 12, 4)
        self.text(surf, "Claude Code", 72, 30, 16, COL_ORANGE, bold=True)
        self.text(surf, "usage monitor", 72, 47, 11, COL_DIM)
        active = snap.thinking
        self.draw_spinner(surf, snap)
        self.text(surf, "working..." if active else "idle", 90, 158, 15,
                  COL_ORANGE if active else COL_DIM, align="c")
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
        self._header_landscape(surf, now, spotify=True)
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

    def _usage_bar(self, surf, snap, now):
        self.mascot(surf, 28, 40, 6)
        self.text(surf, "Claude Code", 124, 74, 30, COL_ORANGE, bold=True)
        self.text(surf, "usage monitor", 124, 99, 17, COL_DIM)
        active = snap.thinking
        self.draw_spinner(surf, snap)
        self.text(surf, "working..." if active else "idle", 132, 216, 28,
                  COL_ORANGE if active else COL_DIM)
        surf.fill(COL_CARD, self.rect(346, 40, 2, 196))

        x0, xb, x1 = 380, 1268, 1452  # label / bar start, bar end, % right edge
        for i, (label, key) in enumerate((("5-HOUR", "five"), ("WEEKLY", "week"))):
            top = 40 + i * 112
            pct, when, _ = snap.usage[key] if snap.usage else (None, None, "")
            self.text(surf, label, x0, top + 24, 22, COL_DIM, bold=True)
            if when:
                self.text(surf, f"resets {fmt_reset(when, now)}  ·  {fmt_until(when, now)}",
                          xb, top + 24, 20, COL_DIM, align="r")
            self.bar(surf, self.rect(x0, top + 36, xb - x0, 46),
                     None if pct is None else pct / 100, bar_color(pct), 14)
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
        self.mascot(surf, 82, 56, 12)
        self.text(surf, "Claude Code", 160, 232, 34, COL_ORANGE, bold=True, align="c")
        self.text(surf, "usage monitor", 160, 264, 20, COL_DIM, align="c")
        active = snap.thinking
        self.draw_spinner(surf, snap)
        self.text(surf, "working..." if active else "idle", 160, 556, 32,
                  COL_ORANGE if active else COL_DIM, align="c")
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
    args = ap.parse_args()

    if pygame.version.vernum[0] < 2:
        sys.exit(f"needs pygame 2 (found {pygame.version.ver}) - pi/install.sh installs "
                 "it (on Bullseye: python3 -m pip install --user pygame)")
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # systemctl stop -> clean exit

    cfg = Config(args.config)
    if args.port:
        cfg.port = args.port
    state = StateFile(STATE_PATH)
    spotify_ready = args.demo or bool(cfg.sp_client_id and cfg.sp_refresh_token)
    model = Model(cfg, state, spotify_ready)
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
    log(f"screen {sw}x{sh}, rotate {rotate}, {renderer.layout} layout")

    def present(rect=None):
        """Push the canvas - or just `rect` of it - to the screen."""
        if rotate:
            src = canvas.subsurface(rect) if rect else canvas
            rect = rotate_rect(rect, rotate, *canvas.get_size()) if rect else None
            screen.blit(pygame.transform.rotate(src, -rotate), rect.topleft if rect else (0, 0))
        if rect:
            pygame.display.update(rect)
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
            key = renderer.scene_key(snap, now)
            frame = renderer.spin_frame(snap) if snap.mode == "usage" else -1
            if key != last_key:
                last_key, last_frame = key, frame
                renderer.draw(canvas, snap, now)
                present()
            elif frame != last_frame:  # only the spinner moved
                last_frame = frame
                present(renderer.draw_spinner(canvas, snap))
            clock.tick(30 if snap.thinking and snap.mode == "usage" else 10)
    finally:
        pygame.quit()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
