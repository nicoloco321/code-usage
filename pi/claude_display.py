#!/usr/bin/env python3
"""Claude Code usage display - Raspberry Pi edition.

The ESP32 display (firmware/src/main.cpp) as a fullscreen app for a Raspberry
Pi - built for a Pi 2, fine on anything newer - on an HDMI monitor or the
official touchscreen.

This file is the display itself: the screen, the HTTP API the hooks and
beacons talk to, and the machinery screens plug into. The screens all come
from the Screen Market (github.com/nicoloco321/screen-market): your Claude
Code usage, Spotify, a Bambu Lab printer, planes overhead, Formula 1, the
Washington Metro, weather, clocks... Pair the display with the market once (a
code shows up on the screen) and install the ones you want; they land in
~/.local/share/claude-display/screens. Until then it shows how to do that.

  - beacons: POST /thinking/on while Claude works, /thinking/off when done
    (Claude Code hooks or beacon.py); every screen shows a spinner then
  - screens: POST /mode/<screen>, /mode/toggle, or tap the screen; GET /mode
  - add-on screens: GET /screens, and the pairing and install calls the
    Screen Market makes (see BeaconHandler._screens)

It speaks the firmware's HTTP API on the same port, so the hooks, beacon.py,
find_display.py and the /switch command work unchanged - point them at the Pi.

    python3 pi/claude_display.py                      # fullscreen
    python3 pi/claude_display.py --windowed 800x480   # in a window, for testing
    python3 pi/claude_display.py --demo               # the installed screens with fake data
    python3 pi/claude_display.py --setup planes       # a screen's own setup (planes, bambu, metro)

Settings live in ~/.config/claude-display/config.ini (see config.example.ini);
pi/install.sh sets everything up to start fullscreen on boot. Needs pygame 2
(sudo apt install python3-pygame); everything else is the standard library.

Keys: tap / click / space switches screens, Ctrl+Q quits (Esc too, windowed).
A screen can have buttons of its own (Spotify's play / pause, say): a tap on
one presses it instead.
"""

import argparse
import base64
import configparser
import datetime
import hashlib
import importlib.util
import io
import json
import math
import os
import re
import secrets
import shutil
import signal
import socket
import ssl
import subprocess
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

# Native screens `from claude_display import *`, even when this runs as __main__.
sys.modules.setdefault("claude_display", sys.modules[__name__])

CONFIG_PATH = os.path.expanduser("~/.config/claude-display/config.ini")
STATE_PATH = os.path.expanduser("~/.local/state/claude-display/state.json")
CACHE_DIR = os.path.expanduser("~/.cache/claude-display")  # reference data, re-fetched monthly


ROOT_TEXT = ("Claude Code usage display (Raspberry Pi). POST /thinking/on while "
             "working, /thinking/off when done. POST /mode/<screen> or /mode/toggle to switch "
             "screens; GET /mode to ask; GET /screens for the installed screens (POST "
             "/screens/pair to pair the Screen Market, which installs them).\n")

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
COL_EYE = (0, 0, 0)

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
    """config.ini, with defaults for anything missing (a missing file is fine).
    The display reads [server] and [screen]; each screen reads its own section
    with get() / num() (see a native screen's configure())."""

    def __init__(self, path):
        self.path = path
        self.cp = configparser.ConfigParser(interpolation=None)
        self.cp.read(path, encoding="utf-8")
        self.port = int(self.num("server", "port", 8080))
        self.beacon_ttl = self.num("server", "beacon_ttl_seconds", 300)
        self.size = parse_size(self.get("screen", "size"))
        self.rotate = int(self.num("screen", "rotate", 0)) % 360

    def get(self, section, key, default=""):
        return self.cp.get(section, key, fallback=default).strip()

    def num(self, section, key, default):
        try:
            return float(self.get(section, key) or default)
        except ValueError:
            log(f"config: [{section}] {key} isn't a number - using {default}")
            return default


def save_to_config(path, section, values):
    """Set keys under [section] of config.ini, comments and all left as they
    are (server/device_login.py's writer, which the login tools use too)."""
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "server"))
    from device_login import save_to_config as save
    save(path, section, values)


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


def http(url, data=None, headers=None, timeout=10, method=None):
    """(status, body, headers). HTTP errors come back as a status; network
    errors raise (URLError / OSError)."""
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
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


def smootherstep(t):
    """Eases 0 -> 1 with zero speed and acceleration at both ends."""
    return t * t * t * (t * (t * 6 - 15) + 10)


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
    """Everything on screen, shared by the workers, the HTTP server and the
    renderer. The screens add their own state and methods (see NativeScreen)."""

    def __init__(self, cfg, state, demo=False, screens=None):
        self.cfg, self.state, self.demo = cfg, state, demo
        self.lock = threading.Lock()
        self.restart = False       # screens or settings changed: main() starts the app afresh
        self.version = display_version()
        self.remote_sessions = {}  # sender -> (monotonic time, [session summaries])
        self.last_beacon = 0.0     # monotonic time of the last "thinking" ping, 0 = off
        self.flash_msg = None      # (text, colour, until): short-lived status override
        self.host = socket.gethostname().split(".")[0]
        self.ip = ""
        self.layout = None         # the renderer's, for GET /screens
        self.pairing = Pairing(state)
        self.addons = ScreenStore(screens or SCREENS_DIR)  # the installed screens: see ScreenStore
        self.addons.load_all(self)
        saved = state.get("mode")
        self.mode = saved if self.is_ready(saved) else next(
            (m for m in self.modes() if self.is_ready(m)), WELCOME)

    # -- beacons: "thinking" is sticky between on and off; the TTL is a backstop
    def thinking(self, now=None):
        now = time.monotonic() if now is None else now
        return self.last_beacon > 0 and now - self.last_beacon < self.cfg.beacon_ttl

    def beacon_on(self):
        self.last_beacon = time.monotonic()

    def beacon_off(self):
        self.last_beacon = 0.0

    # -- screen mode (persisted, like the firmware's NVS "mode")
    def modes(self):
        """Every installed screen, in install order."""
        return self.addons.ids()

    def native(self, mode=None):
        """The native screen showing (or `mode`), if it is one."""
        return self.addons.native(self.mode if mode is None else mode)

    def is_ready(self, mode):
        """Installed, and set up enough to show."""
        screen = self.addons.get(mode)
        if isinstance(screen, NativeScreen):
            return self.demo or screen.ready(self.cfg)
        return screen is not None

    def set_mode(self, mode):
        """Switch screens. False if that screen isn't installed or set up."""
        if not self.is_ready(mode):
            return False
        with self.lock:
            if mode == self.mode:
                return True
            self.mode = mode
        self.state.put("mode", mode)
        screen = self.addons.get(mode)
        if isinstance(screen, NativeScreen):
            screen.hook("on_show", self)
        else:
            screen.wake.set()
        return True

    def next_mode(self):
        """The screen after this one, skipping any that aren't set up."""
        modes = self.modes()
        i = modes.index(self.mode) if self.mode in modes else -1
        return next((m for m in modes[i + 1:] + modes[:i + 1] if self.is_ready(m) and m != self.mode),
                    self.mode)

    def toggle_mode(self):
        nxt = self.next_mode()
        if nxt == self.mode:
            if self.modes():
                self.flash("no other screens set up", COL_YELLOW)
        else:
            self.set_mode(nxt)

    def flash(self, text, color, secs=4):
        with self.lock:
            self.flash_msg = (text, color, time.monotonic() + secs)

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
        """A consistent copy of what's on screen, for one frame. Each native
        screen adds its own fields (its snapshot() hook)."""
        now = time.monotonic()
        screen = self.addons.get(self.mode)
        addon = screen if isinstance(screen, AddonScreen) else None
        native = screen if isinstance(screen, NativeScreen) else None
        with self.lock:
            flash = self.flash_msg if self.flash_msg and self.flash_msg[2] > now else None
            fields = {}
            for n in self.addons.natives():
                fields.update(n.hook("snapshot", self) or {})
            return SimpleNamespace(
                mode=self.mode, sessions=self.sessions_now(now),
                addon=addon, addon_view=addon.view() if addon else None, native=native,
                pair_code=self.pairing.showing, screens=len(self.modes()),
                thinking=self.thinking(now), flash=flash and flash[:2],
                host=self.host, ip=self.ip, port=self.cfg.port, mono=now, **fields)


# ---------------------------------------------------------------- helpers for screens

OFF_SCREEN_SLOWDOWN = 3  # poll this much less often while another screen is up


def fmt_minutes(mins):
    mins = int(mins)
    return f"{mins // 60}h {mins % 60}m" if mins >= 60 else f"{mins}m"


# planespotters wants a way to reach whoever runs a client in its User-Agent
PLANES_USER_AGENT = "claude-usage-display/1.0 (+https://github.com/nicoloco321/code-usage)"


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


def cached_download(url, name, max_age=30 * 86400, headers=None):
    """A reference file, kept in CACHE_DIR and fetched again once it's a
    month old - a stale copy beats none when the fetch fails."""
    path = os.path.join(CACHE_DIR, name)
    try:
        fresh = time.time() - os.path.getmtime(path) < max_age
    except OSError:
        fresh = None  # no copy yet
    if not fresh:
        try:
            code, raw, _ = http(url, headers={"User-Agent": PLANES_USER_AGENT, **(headers or {})},
                                timeout=20)
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


# ---------------------------------------------------------------- screens
#
# Every screen comes from the Screen Market (github.com/nicoloco321/screen-market),
# or is put there by hand: each lives in its own folder under SCREENS_DIR with
# a manifest.json and a screen.py. There are two kinds.
#
# An add-on screen (the usual kind) draws through ScreenUI. screen.py defines:
#
#     class Screen:
#         def __init__(self, settings): ...       # the manifest's settings, filled in
#         def fetch(self, ctx): return data       # optional: a worker thread calls it
#                                                 # every poll_seconds while it's showing
#         def draw(self, ui, data, now): ...      # paint the screen (see ScreenUI)
#
# A native screen ("kind": "native" in its manifest) plugs into the display
# itself - its state joins the Model, its drawing the Renderer - for screens
# that need more than ScreenUI: buttons, slides, moving maps, HTTP pages. The
# usage screen, Spotify, the printer, planes, F1 and the Metro are native. See
# NativeScreen. Installing, updating or removing one restarts the display.
#
# Screens are tapped through in the order they were installed, and answer
# POST /mode/<id>. Installing over HTTP (POST /screens/install) needs a token,
# which you get by pairing: POST /screens/pair puts a code on the screen, and
# POST /screens/pair/confirm trades that code for the token. A screen is
# Python running as you on the Pi - only install code you trust.

SCREENS_DIR = os.path.expanduser("~/.local/share/claude-display/screens")
SCREEN_API = 1     # what an add-on screen's manifest "api" may ask for
NATIVE_API = 1     # what a native screen's "native_api" may ask for: the hooks below
WELCOME = "welcome"  # the screen shown while none are installed (or set up)
SCREEN_ID = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
SCREEN_FILE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")
SCREEN_MAX_BYTES = 4 * 1024 * 1024  # one install, all files together
PAIR_SECS = 180
PAIR_TRIES = 5


def display_version():
    """The code-usage commit this display runs (empty outside a git checkout)."""
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        out = subprocess.run(["git", "-C", here, "log", "-1", "--format=%h %cs"],
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def parse_color(value, default=COL_ORANGE):
    """'#d97757', 'd97757' or [217, 119, 87] -> an RGB tuple."""
    if isinstance(value, (list, tuple)) and len(value) == 3:
        return tuple(max(0, min(255, int(c))) for c in value)
    text = str(value or "").lstrip("#")
    if re.fullmatch(r"[0-9a-fA-F]{6}", text):
        return tuple(int(text[i:i + 2], 16) for i in (0, 2, 4))
    return default


def screen_settings(manifest, saved):
    """The manifest's settings with their defaults, overlaid with `saved`,
    each coerced to its declared type."""
    out = {}
    for field in manifest.get("settings") or []:
        key = field.get("key")
        if not key:
            continue
        kind, value = field.get("type", "text"), saved.get(key, field.get("default"))
        try:
            if kind == "number":
                value = float(value) if value not in (None, "") else None
                if value is not None and value.is_integer() and not isinstance(field.get("default"), float):
                    value = int(value)
            elif kind == "boolean":
                value = value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes", "on")
            elif kind == "select":
                options = [o["value"] if isinstance(o, dict) else o for o in field.get("options") or []]
                value = value if value in options else field.get("default")
            else:
                value = "" if value is None else str(value)
        except (TypeError, ValueError):
            value = field.get("default")
        out[key] = value
    return out


def check_manifest(manifest):
    """Why a manifest won't do, or None if it's fine."""
    if not isinstance(manifest, dict):
        return "manifest must be a JSON object"
    sid = manifest.get("id")
    if not isinstance(sid, str) or not SCREEN_ID.match(sid):
        return "id must be 2-32 lowercase letters, digits or dashes, starting with a letter"
    if sid in ("toggle", WELCOME):
        return f"{sid!r} is reserved"
    if not str(manifest.get("name") or "").strip():
        return "the screen needs a name"
    try:
        api = int(manifest.get("api", SCREEN_API))
    except (TypeError, ValueError):
        return "api must be a number"
    if api > SCREEN_API:
        return f"this screen needs screen API {api}; this display has {SCREEN_API} - update the display"
    if manifest.get("kind") == "native":
        try:
            need = int(manifest.get("native_api", 1))
        except (TypeError, ValueError):
            return "native_api must be a number"
        if need > NATIVE_API:
            return (f"this screen needs native API {need}; this display has {NATIVE_API} - "
                    "update the display (git pull in code-usage)")
    if not isinstance(manifest.get("settings") or [], list):
        return "settings must be a list"
    return None


class ScreenContext:
    """What fetch() gets: settings, the network, and a way to say how it's going."""

    def __init__(self, screen):
        self._screen = screen
        self.settings = screen.settings
        self.folder = screen.folder

    def get(self, url, headers=None, timeout=10):
        """GET url -> body bytes. Raises on network errors and non-2xx answers."""
        code, body, _ = http(url, headers={"User-Agent": PLANES_USER_AGENT, **(headers or {})},
                             timeout=timeout)
        if not 200 <= code < 300:
            raise RuntimeError(f"HTTP {code} from {urllib.parse.urlsplit(url).netloc}")
        return body

    def get_json(self, url, headers=None, timeout=10):
        return json.loads(self.get(url, {"Accept": "application/json", **(headers or {})}, timeout))

    def status(self, text, color=COL_DIM):
        """Put a line on the status bar (None to clear it)."""
        self._screen.set_status(text and (text, parse_color(color, COL_DIM)))

    def log(self, msg):
        log(f"screen {self._screen.id}: {msg}")


class AddonScreen:
    """One installed screen: its code, its settings, and its fetch worker."""

    def __init__(self, folder):
        self.folder = folder
        with open(os.path.join(folder, "manifest.json"), encoding="utf-8") as f:
            self.manifest = json.load(f)
        problem = check_manifest(self.manifest)
        if problem:
            raise ValueError(problem)
        self.id = self.manifest["id"]
        self.name = str(self.manifest["name"])
        self.color = parse_color(self.manifest.get("color"))
        self.poll = max(5.0, float(self.manifest.get("poll_seconds") or 300))
        self.fps = max(0.0, min(10.0, float(self.manifest.get("fps") or 0)))
        try:
            with open(os.path.join(folder, "settings.json"), encoding="utf-8") as f:
                saved = json.load(f)
        except (OSError, ValueError):
            saved = {}
        self.settings = screen_settings(self.manifest, saved)
        self.lock = threading.Lock()
        self.data = None
        self.version = 0
        self.status = ("loading...", COL_DIM)
        self.draw_error = None
        self.wake = threading.Event()
        self.alive = True
        self.obj = self._load()
        if not callable(getattr(self.obj, "fetch", None)):
            self.status = None

    def _load(self):
        path = os.path.join(self.folder, self.manifest.get("entry") or "screen.py")
        name = f"claude_screen_{self.id.replace('-', '_')}_{time.monotonic_ns()}"
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.path.insert(0, self.folder)  # so a screen can import its own helper files
        try:
            spec.loader.exec_module(module)
        finally:
            sys.path.remove(self.folder)
        cls = getattr(module, "Screen", None)
        if cls is None or not callable(getattr(cls, "draw", None)):
            raise ValueError("screen.py needs a class Screen with a draw(ui, data, now) method")
        try:
            return cls(dict(self.settings))
        except TypeError:
            return cls()  # one that takes no settings

    def set_status(self, status):
        with self.lock:
            self.status = status

    def view(self):
        with self.lock:
            return self.data, self.version, self.status

    def worker(self, model):
        """Fetch while showing, every poll seconds; on a switch to it, at once."""
        fetch = getattr(self.obj, "fetch", None)
        if not callable(fetch):
            return
        ctx, fetched_at = ScreenContext(self), 0.0
        while self.alive:
            showing = model.mode == self.id
            due = time.monotonic() - fetched_at >= (self.poll if showing else self.poll * OFF_SCREEN_SLOWDOWN)
            if showing and due:
                try:
                    data = fetch(ctx)
                    with self.lock:
                        self.data, self.version = data, self.version + 1
                        if self.status and (self.status[0] == "loading..." or self.status[1] == COL_RED):
                            self.status = None  # (a status the screen set itself stays)
                except Exception as e:
                    log(f"screen {self.id}: fetch failed\n{traceback.format_exc()}")
                    with self.lock:
                        self.version += 1
                        self.status = (f"{self.name}: {e}"[:120], COL_RED)
                fetched_at = time.monotonic()
            self.wake.wait(min(self.poll, 30) if showing else 30)
            self.wake.clear()

    def start(self, model):
        threading.Thread(target=self.worker, args=(model,), daemon=True,
                         name=f"screen-{self.id}").start()

    def stop(self):
        self.alive = False
        self.wake.set()
        close = getattr(self.obj, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                log(traceback.format_exc())

    def summary(self, model=None):
        m = self.manifest
        return {"id": self.id, "name": self.name, "version": m.get("version", ""),
                "author": m.get("author", ""), "icon": m.get("icon", ""), "kind": "addon", "ready": True,
                "settings": self.settings,
                "status": self.status[0] if self.status and self.status[0] != "loading..." else None,
                "error": self.draw_error}


def native_values(manifest, settings):
    """A native screen's settings from the market, as config.ini text. Blank
    secrets are left out (blank = keep the one saved). Raises ValueError."""
    values = {}
    for field in manifest.get("settings") or []:
        key, kind = field.get("key"), field.get("type", "text")
        if key not in settings:
            continue
        v = settings[key]
        if kind == "secret" and not v:
            continue
        if kind == "number":
            try:
                v = f"{float(v):g}" if v not in (None, "") else ""
            except (TypeError, ValueError):
                raise ValueError(f"{key} must be a number")
        elif kind == "boolean":
            v = "yes" if v in (True, "true", "yes", "on", "1", 1) else "no"
        else:
            v = str(v if v is not None else "").strip()
            if "\n" in v or "\r" in v:
                raise ValueError(f"{key} can't have line breaks")
        values[key] = v
    return values


def mix_in(cls, part):
    """Add a native screen's methods and attributes to the display's class."""
    for name, value in vars(part).items():
        if not (name.startswith("__") and name.endswith("__")):
            setattr(cls, name, value)


class NativeScreen:
    """A screen that plugs into the display itself. Its screen.py is imported
    as a module, and may define (all optional but draw, or a _<id>_<layout>
    method for each layout):

        configure(cfg)               read its config.ini section into cfg attributes
        ready(cfg)                   set up enough to show? NOT_READY says how to
        needs_setup(cfg)             shows, but isn't set up yet (the usage screen with no login)
        demo_config(cfg)             fill in what --demo needs
        ModelPart, RendererPart      classes whose members join Model / Renderer
        init_model(model), init_renderer(r), on_layout(model, r)
        start(model), demo(model)    start its workers: real, or --demo's
        on_show(model)               it was switched to
        snapshot(model) -> dict      its fields in each frame's snapshot
        draw(r, surf, snap, now)     paint it (default: r._<id>_<layout>)
        scene_key(r, snap, now)      what, changing, means a redraw
        status(snap)                 (text, colour) for the status line
        brand(r, surf, subtitle)     its logo for the landscape header -> (title, colour, sub)
        advance(r, snap, dt)         step a slide; True while moving (every screen, every frame)
        draw_slide(r, surf, snap, now)    repaint just the slide -> rect
        frame(r, snap), draw_frame(r, surf, snap)   a moving map: its step, and repaint -> rect
        OWN_SPINNER, spinner_geometry(r), activity(r, surf, snap)   a spinner of its own
        hidden(r)                    another screen is showing (drop big images...)
        press(model, button)         one of its buttons was tapped (see Renderer.buttons)
        route(req, model, path)      answer an HTTP request; True if it did
        setup(config_path)           python3 pi/claude_display.py --setup <id>
        apply_settings(model, values) -> restart?   the market changed its settings
    """

    def __init__(self, folder):
        self.folder = folder
        with open(os.path.join(folder, "manifest.json"), encoding="utf-8") as f:
            self.manifest = json.load(f)
        problem = check_manifest(self.manifest)
        if problem:
            raise ValueError(problem)
        self.id = self.manifest["id"]
        self.name = str(self.manifest["name"])
        self.draw_error = None
        name = f"claude_screen_{self.id.replace('-', '_')}"
        spec = importlib.util.spec_from_file_location(
            name, os.path.join(folder, self.manifest.get("entry") or "screen.py"))
        self.mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = self.mod
        sys.path.insert(0, folder)
        try:
            spec.loader.exec_module(self.mod)
        finally:
            sys.path.remove(folder)
        for cls, part in ((Model, "ModelPart"), (Renderer, "RendererPart")):
            if hasattr(self.mod, part):
                mix_in(cls, getattr(self.mod, part))

    def hook(self, name, *args, default=None):
        fn = getattr(self.mod, name, None)
        return fn(*args) if callable(fn) else default

    def has(self, name):
        return callable(getattr(self.mod, name, None))

    def ready(self, cfg):
        return bool(self.hook("ready", cfg, default=True))

    @property
    def section(self):
        return self.manifest.get("config_section") or self.id

    def settings_values(self, cfg):
        """Its settings as config.ini has them (secrets only as set or not)."""
        out = {}
        for field in self.manifest.get("settings") or []:
            key, kind = field.get("key"), field.get("type", "text")
            if not key:
                continue
            raw = cfg.get(self.section, key)
            if kind == "secret":
                out[key] = bool(raw)
            elif raw == "":
                out[key] = field.get("default")
            elif kind == "number":
                try:
                    out[key] = float(raw)
                except ValueError:
                    out[key] = None
            elif kind == "boolean":
                out[key] = raw.lower() not in ("no", "false", "off", "0")
            else:
                out[key] = raw
        return out

    def summary(self, model):
        m = self.manifest
        ready = model.demo or self.ready(model.cfg)
        return {"id": self.id, "name": self.name, "version": m.get("version", ""),
                "author": m.get("author", ""), "icon": m.get("icon", ""), "kind": "native", "ready": ready,
                "needs_setup": not model.demo and (not ready or bool(self.hook("needs_setup", model.cfg))),
                "settings": self.settings_values(model.cfg), "setup": m.get("setup") or None,
                "status": None, "error": self.draw_error}


class ScreenStore:
    """The installed screens, in the order they were installed."""

    def __init__(self, folder=SCREENS_DIR):
        self.folder = folder
        self.state = None  # the display's state file, for the screens' order (see load_all)
        self.screens = {}
        self.lock = threading.Lock()
        self.failed = {}  # id -> why it didn't load

    def load_all(self, model):
        """Load every installed screen; native ones get their config read and
        their state added to the model."""
        self.state = model.state
        order = self.state.get("screen_order") or []  # install order: the order you tap through

        def place(n):
            manifest = os.path.join(self.folder, n, "manifest.json")
            mtime = os.path.getmtime(manifest) if os.path.exists(manifest) else 0
            return (order.index(n), 0) if n in order else (len(order), mtime)

        try:
            names = sorted(os.listdir(self.folder), key=place)
        except OSError:
            return
        for name in names:
            path = os.path.join(self.folder, name)
            if name.startswith(".") or not os.path.isfile(os.path.join(path, "manifest.json")):
                continue
            try:
                with open(os.path.join(path, "manifest.json"), encoding="utf-8") as f:
                    native = json.load(f).get("kind") == "native"
                screen = (NativeScreen if native else AddonScreen)(path)
                if native:
                    screen.hook("configure", model.cfg)
                    if model.demo:
                        screen.hook("demo_config", model.cfg)
                    screen.hook("init_model", model)
                self.screens[screen.id] = screen
                if screen.id not in order:  # (one from before the order was kept)
                    self.remember_order(screen.id)
                    order = self.state.get("screen_order")
                log(f"loaded screen {screen.id} ({screen.name})")
            except Exception as e:
                self.failed[name] = str(e)
                log(f"screen {name} didn't load: {e}\n{traceback.format_exc()}")

    def ids(self):
        with self.lock:
            return tuple(self.screens)

    def get(self, sid):
        with self.lock:
            return self.screens.get(sid)

    def remember_order(self, sid, keep=True):
        """A new screen goes last in the taps; an update keeps its place."""
        if self.state is None:
            return
        order = [x for x in self.state.get("screen_order") or [] if x != sid or keep]
        if keep and sid not in order:
            order.append(sid)
        self.state.put("screen_order", order)

    def native(self, sid):
        screen = self.get(sid)
        return screen if isinstance(screen, NativeScreen) else None

    def natives(self):
        with self.lock:
            return [s for s in self.screens.values() if isinstance(s, NativeScreen)]

    def install(self, manifest, files, settings, model):
        """Write the screen to disk and load it, replacing any old version.
        Returns the new AddonScreen; raises ValueError with what's wrong."""
        problem = check_manifest(manifest)
        if problem:
            raise ValueError(problem)
        sid = manifest["id"]
        entry = manifest.get("entry") or "screen.py"
        if not isinstance(files, dict) or entry not in files:
            raise ValueError(f"the install has no {entry}")
        blobs, total = {}, 0
        for name, content in files.items():
            if not SCREEN_FILE.match(name) or name in ("manifest.json", "settings.json"):
                raise ValueError(f"bad file name {name!r}")
            if isinstance(content, dict) and "base64" in content:
                blob = base64.b64decode(content["base64"])
            elif isinstance(content, dict) and "text" in content:
                blob = str(content["text"]).encode()
            elif isinstance(content, str):
                blob = content.encode()
            else:
                raise ValueError(f"{name}: give its text or base64")
            total += len(blob)
            blobs[name] = blob
        if total > SCREEN_MAX_BYTES:
            raise ValueError("the screen's files are too big (4 MB at most)")

        os.makedirs(self.folder, exist_ok=True)
        staging = os.path.join(self.folder, f".{sid}.new")
        final = os.path.join(self.folder, sid)
        old_dir = os.path.join(self.folder, f".{sid}.old")
        for d in (staging, old_dir):
            if os.path.isdir(d):
                shutil.rmtree(d)
        os.makedirs(staging)
        for name, blob in blobs.items():
            with open(os.path.join(staging, name), "wb") as f:
                f.write(blob)
        with open(os.path.join(staging, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)
        if settings is None and os.path.isfile(os.path.join(final, "settings.json")):
            with open(os.path.join(final, "settings.json"), encoding="utf-8") as f:
                settings = json.load(f)  # an update keeps the settings you had
        with open(os.path.join(staging, "settings.json"), "w", encoding="utf-8") as f:
            json.dump(settings or {}, f, indent=2)

        if manifest.get("kind") == "native":
            # It joins the display's own classes, so it can't be tried out
            # here: check the code compiles, swap it in, and start afresh.
            for name, blob in blobs.items():
                if name.endswith(".py"):
                    try:
                        compile(blob, name, "exec")
                    except SyntaxError as e:
                        shutil.rmtree(staging, ignore_errors=True)
                        raise ValueError(f"{name} line {e.lineno}: {e.msg}")
            with self.lock:
                if os.path.isdir(final):
                    os.replace(final, old_dir)
                os.replace(staging, final)
                shutil.rmtree(old_dir, ignore_errors=True)
            if settings:
                save_to_config(model.cfg.path, manifest.get("config_section") or sid,
                               native_values(manifest, settings))
            self.remember_order(sid)
            log(f"installed native screen {sid} ({manifest.get('name')} {manifest.get('version', '')})")
            model.restart = True
            return None

        try:
            screen = AddonScreen(staging)  # does it even load? (before touching the old one)
        except Exception as e:
            shutil.rmtree(staging, ignore_errors=True)
            raise ValueError(f"the screen didn't load: {type(e).__name__}: {e}")
        screen.stop()

        with self.lock:
            old = self.screens.pop(sid, None)
            if old:
                old.stop()
            if os.path.isdir(final):
                os.replace(final, old_dir)
            os.replace(staging, final)
            shutil.rmtree(old_dir, ignore_errors=True)
            screen = AddonScreen(final)  # load it from where it lives now
            self.screens[sid] = screen
            self.failed.pop(sid, None)
        self.remember_order(sid)
        screen.start(model)
        log(f"installed screen {sid} ({screen.name} {manifest.get('version', '')})")
        return screen

    def uninstall(self, sid, model):
        with self.lock:
            screen = self.screens.pop(sid, None)
        if isinstance(screen, NativeScreen):
            model.restart = True  # its code is part of the display now: start afresh without it
        elif screen:
            screen.stop()
        path = os.path.join(self.folder, sid)
        if not SCREEN_ID.match(sid) or not os.path.isdir(path):
            return screen is not None
        shutil.rmtree(path, ignore_errors=True)
        self.remember_order(sid, keep=False)
        log(f"uninstalled screen {sid}")
        return True

    def configure(self, sid, settings, model):
        """New settings for an installed screen: saved, then the screen reloaded.
        A native screen's go to its config.ini section, and the display
        restarts unless the screen can take them as they are."""
        screen = self.get(sid)
        if not screen:
            return None
        if isinstance(screen, NativeScreen):
            values = native_values(screen.manifest, settings or {})
            save_to_config(model.cfg.path, screen.section, values)
            if not model.cfg.cp.has_section(screen.section):
                model.cfg.cp.add_section(screen.section)
            for key, value in values.items():
                model.cfg.cp.set(screen.section, key, value)
            log(f"config: [{screen.section}] {', '.join(values) or 'nothing'} changed from the market")
            if screen.hook("apply_settings", model, values, default=True):
                model.state.put("mode", sid)  # come back up on the screen just set up
                model.restart = True
            return screen
        merged = {**screen.settings, **(settings or {})}
        with open(os.path.join(screen.folder, "settings.json"), "w", encoding="utf-8") as f:
            json.dump(merged, f, indent=2)
        fresh = AddonScreen(screen.folder)
        with self.lock:
            screen.stop()
            self.screens[sid] = fresh
        fresh.start(model)
        return fresh


class Pairing:
    """Pairing a marketplace with the display, like a Bluetooth keyboard: the
    display shows a code, you type it in, and the marketplace gets a token."""

    def __init__(self, state):
        self.state = state
        self.lock = threading.Lock()
        self.code = None
        self.until = 0.0
        self.tries = 0

    def start(self):
        with self.lock:
            if not self.code or time.monotonic() > self.until:
                self.code = f"{secrets.randbelow(1000000):06d}"
                self.tries = 0
            self.until = time.monotonic() + PAIR_SECS
            return self.code

    def confirm(self, code, client):
        with self.lock:
            if not self.code or time.monotonic() > self.until:
                return None, "no pairing in progress - start again"
            if str(code).strip().replace(" ", "") != self.code:
                self.tries += 1
                if self.tries >= PAIR_TRIES:
                    self.code = None
                    return None, "too many wrong codes - start again"
                return None, "that's not the code on the screen"
            self.code = None
        token = secrets.token_urlsafe(32)
        tokens = dict(self.state.get("screen_tokens") or {})
        tokens[hashlib.sha256(token.encode()).hexdigest()] = {
            "client": str(client or "marketplace")[:60], "at": int(time.time())}
        self.state.put("screen_tokens", tokens)
        return token, None

    def allowed(self, header):
        token = (header or "").removeprefix("Bearer ").strip()
        if not token:
            return False
        return hashlib.sha256(token.encode()).hexdigest() in (self.state.get("screen_tokens") or {})

    @property
    def showing(self):
        with self.lock:
            return self.code if self.code and time.monotonic() <= self.until else None


class ScreenUI:
    """What an add-on screen draws with. Coordinates are design units for the
    current layout (ui.w x ui.h: 800x480 landscape, 180x320 portrait, 1480x320
    bar, 320x1480 strip), scaled to the real screen for you; y on text() is the
    baseline. ui.top / ui.bottom bound the room left between the header and
    the status line."""

    BG, CARD, TEXT, DIM, SUB = COL_BG, COL_CARD, COL_TEXT, COL_DIM, COL_SUB
    ORANGE, GREEN, YELLOW, RED = COL_ORANGE, COL_GREEN, COL_YELLOW, COL_RED
    # room under the status line, per layout
    BOTTOM = {"landscape": 428, "portrait": 300, "bar": 276, "strip": 1384}

    def __init__(self, renderer, surf, screen):
        self.r, self.surface, self.screen = renderer, surf, screen
        self.layout = renderer.layout
        self.w, self.h = Renderer.LAYOUTS[self.layout]
        self.accent = screen.color
        self.settings = screen.settings
        self.top = 0
        self.bottom = self.BOTTOM[self.layout]
        self.pygame = pygame

    # -- text
    def text(self, s, x, y, size=20, color=COL_TEXT, bold=False, align="l"):
        """Draw s with its baseline at y; x is its left / centre / right edge
        for align l / c / r. The background shows through, so text works on cards."""
        s = str(s)
        if not s:
            return
        r, f, color = self.r, self.r.font(size, bold), parse_color(color, COL_TEXT)
        key = ("alpha", s, id(f), color)
        img = r.text_cache.get(key)
        if img is None:
            if len(r.text_cache) > 400:
                r.text_cache.clear()
            img = r.text_cache[key] = f.render(s, True, color)
        px = r.x(x)
        if align == "c":
            px -= img.get_width() // 2
        elif align == "r":
            px -= img.get_width()
        self.surface.blit(img, (px, r.y(y) - f.get_ascent()))

    def width(self, s, size=20, bold=False):
        return self.r.width(str(s), size, bold)

    def fit(self, s, max_w, size=20, bold=False):
        return self.r.fit(str(s), max_w, size, bold)

    def fit_size(self, s, max_w, size, bold=False, smallest=10):
        return self.r.fit_size(str(s), max_w, size, bold, smallest)

    def wrap(self, s, max_w, size=20, bold=False, lines=2):
        """Break s at spaces into at most `lines` lines that fit max_w; the last
        one is ellipsized if the text runs on."""
        words, out, line = str(s).split(), [], ""
        for i, word in enumerate(words):
            trial = f"{line} {word}" if line else word
            if not line or self.width(trial, size, bold) <= max_w:
                line = trial  # (an overlong single word is ellipsized below)
                continue
            out.append(line)
            line = word
            if len(out) == lines - 1:
                line = " ".join(words[i:])
                break
        if line:
            out.append(line)
        return [self.fit(t, max_w, size, bold) for t in out[:lines]]

    # -- shapes
    def _px(self, x, y):
        return self.r.x(x), self.r.y(y)

    def rect(self, x, y, w, h, color=COL_CARD, radius=0, width=0):
        pygame.draw.rect(self.surface, parse_color(color, COL_CARD), self.r.rect(x, y, w, h),
                         width=self.r.n(width) if width else 0,
                         border_radius=self.r.n(radius) if radius else 0)

    def bar(self, x, y, w, h, frac, color=None, radius=None):
        """A rounded progress bar, like the usage screen's (frac 0..1, None = empty)."""
        self.r.bar(self.surface, self.r.rect(x, y, w, h), frac,
                   parse_color(color, self.accent) if color is not None else self.accent,
                   h / 2 if radius is None else radius)

    def line(self, x1, y1, x2, y2, color=COL_CARD, width=1):
        pygame.draw.line(self.surface, parse_color(color, COL_CARD), self._px(x1, y1),
                         self._px(x2, y2), self.r.n(width))

    def lines(self, points, color=COL_TEXT, width=1, closed=False):
        if len(points) > 1:
            pygame.draw.lines(self.surface, parse_color(color, COL_TEXT), closed,
                              [self._px(x, y) for x, y in points], self.r.n(width))

    def circle(self, cx, cy, radius, color=COL_TEXT, width=0):
        pygame.draw.circle(self.surface, parse_color(color, COL_TEXT), self._px(cx, cy),
                           self.r.n(radius), self.r.n(width) if width else 0)

    def polygon(self, points, color=COL_TEXT, width=0):
        pygame.draw.polygon(self.surface, parse_color(color, COL_TEXT),
                            [self._px(x, y) for x, y in points], self.r.n(width) if width else 0)

    def arc(self, cx, cy, radius, start_deg, end_deg, color=COL_TEXT, width=4):
        """A thick arc clockwise from start_deg to end_deg (0 = 12 o'clock),
        `radius` to the middle of the stroke - a ring gauge, say."""
        steps = max(2, int(abs(end_deg - start_deg) / 3))
        outer, inner = radius + width / 2, radius - width / 2

        def at(r, deg):
            a = math.radians(deg)
            return cx + math.sin(a) * r, cy - math.cos(a) * r

        angles = [start_deg + (end_deg - start_deg) * i / steps for i in range(steps + 1)]
        self.polygon([at(outer, a) for a in angles] + [at(inner, a) for a in reversed(angles)], color)

    def image(self, data, x, y, w, h, key=None):
        """Draw an image (bytes of a PNG/JPEG, or a file in the screen's folder)
        scaled to fit the box, centred. Decoded images are cached."""
        cache = self.r.addon_images
        if isinstance(data, str) and not key:
            key = ("file", self.screen.id, data)
        key = key or ("bytes", hashlib.sha1(data).hexdigest())
        box = self.r.rect(x, y, w, h)
        ck = (key, box.size)
        img = cache.get(ck)
        if img is None:
            if isinstance(data, str):
                src = pygame.image.load(os.path.join(self.screen.folder, os.path.basename(data)))
            else:
                src = pygame.image.load(io.BytesIO(data))
            iw, ih = src.get_size()
            k = min(box.w / iw, box.h / ih)
            img = pygame.transform.smoothscale(src.convert_alpha() if pygame.display.get_init()
                                               and pygame.display.get_surface() else src,
                                               (max(1, int(iw * k)), max(1, int(ih * k))))
            if len(cache) > 40:
                cache.clear()
            cache[ck] = img
        self.surface.blit(img, img.get_rect(center=box.center))

    # -- pieces of the built-in screens
    def bar_color(self, pct):
        """Green under 50, yellow under 80, red above - the usage bars' colours."""
        return bar_color(pct)

    def clock(self, now):
        return clock_str(now)

    def header(self, title, subtitle="", color=None, icon=None):
        """The title block the built-in screens have, in the accent colour: a
        badge with the first letter (or `icon`, a short text), the title and
        subtitle, and on landscape the clock. Sets and returns ui.top."""
        color = parse_color(color, self.accent) if color is not None else self.accent
        badge = str(icon or title[:1]).upper()
        r, s, now = self.r, self.surface, datetime.datetime.now().astimezone()
        if self.layout == "landscape":
            self.rect(32, 26, 58, 58, color, radius=14)
            self.text(badge, 61, 68, self.fit_size(badge, 46, 34, True, 14), COL_BG, True, "c")
            self.text(r.fit(title, 440, 30, True), 112, 56, 30, color, bold=True)
            self.text(r.fit(subtitle, 420, 17), 112, 82, 17, COL_DIM)
            self.text(clock_str(now), 768, 58, 30, COL_TEXT, align="r")
            self.text(f"{now.strftime('%a %b')} {now.day}", 768, 82, 17, COL_DIM, align="r")
            s.fill(COL_CARD, r.rect(32, 104, 736, 2))
            self.top = 120
        elif self.layout == "portrait":
            self.rect(12, 12, 30, 30, color, radius=7)
            self.text(badge, 27, 34, self.fit_size(badge, 24, 18, True, 8), COL_BG, True, "c")
            self.text(r.fit(title, 124, 15, True), 50, 26, 15, color, bold=True)
            self.text(r.fit(subtitle, 124, 10), 50, 40, 10, COL_DIM)
            s.fill(COL_CARD, r.rect(12, 52, 156, 1))
            self.top = 62
        elif self.layout == "bar":
            self.rect(28, 40, 72, 72, color, radius=16)
            self.text(badge, 64, 92, self.fit_size(badge, 58, 42, True, 16), COL_BG, True, "c")
            self.text(r.fit(title, 210, 30, True), 118, 74, 30, color, bold=True)
            self.text(r.fit(subtitle, 210, 17), 118, 99, 17, COL_DIM)
            s.fill(COL_CARD, r.rect(346, 40, 2, 196))
            self.text(clock_str(now), 1452, 300, 22, COL_TEXT, bold=True, align="r")
            self.top = 40  # bar screens put their content right of x = 370
        else:  # strip
            self.rect(110, 60, 100, 100, color, radius=22)
            self.text(badge, 160, 132, self.fit_size(badge, 80, 58, True, 20), COL_BG, True, "c")
            self.text(r.fit(title, 272, 34, True), 160, 214, 34, color, bold=True, align="c")
            self.text(r.fit(subtitle, 272, 20), 160, 246, 20, COL_DIM, align="c")
            s.fill(COL_CARD, r.rect(24, 276, 272, 2))
            self.top = 300
        return self.top

    @property
    def content_left(self):
        """Where content starts across: right of the title block on a bar."""
        return 370 if self.layout == "bar" else {"landscape": 32, "portrait": 12, "strip": 24}[self.layout]

    @property
    def content_right(self):
        return {"landscape": 768, "portrait": 168, "bar": 1452, "strip": 296}[self.layout]


# ---------------------------------------------------------------- http server

class BeaconHandler(BaseHTTPRequestHandler):
    """The firmware's HTTP API: beacons and mode switching, plus the screens'
    own pages (GET /usage comes from the usage screen) and the Screen Market's
    calls."""
    model = None  # set before serving
    server_version = "claude-display"

    def do_GET(self):
        self.body = b""
        self._route()

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        limit = SCREEN_MAX_BYTES * 2 if self.path.startswith("/screens/") else 65536
        if length > limit:
            self.close_connection = True
            self._send(413, "too big\n")
            return
        self.body = self.rfile.read(length) if length else b""
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
        elif path.startswith("/screens"):
            self._screens(path)
        elif path.startswith("/mode/") and path[6:] in m.modes() + ("toggle",):
            want = path[6:]
            if want == "toggle":
                want = m.next_mode()
            if want == m.mode or m.set_mode(want):
                self._send(200, want + "\n")
            elif not m.modes():
                self._send(409, "no screens installed yet - add some from the Screen Market\n")
            else:
                native = m.native(want)
                self._send(409, getattr(native and native.mod, "NOT_READY",
                                        f"the {want} screen isn't set up yet") + "\n")
        elif any(n.hook("route", self, m, path) for n in m.addons.natives()):
            pass  # a screen's own page (the Metro's station picker, the planes log...)
        elif path == "/":
            self._send(200, ROOT_TEXT)
        else:
            self._send(404, "not found\n")

    def _json(self, code, data):
        self._send(code, json.dumps(data) + "\n", "application/json")

    def _screens(self, path):
        """The screen API, which the Screen Market talks to:
          GET  /screens                    what's installed, and what this display is
          POST /screens/pair               show a pairing code on the screen
          POST /screens/pair/confirm       {"code", "client"} -> {"token"}
        and, with "Authorization: Bearer <token>":
          POST /screens/install            {"manifest", "files", "settings"?, "show"?}
          POST /screens/<id>/settings      {"settings"}
          POST /screens/<id>/uninstall
        """
        m = self.model
        if path == "/screens" and self.command == "GET":
            r = Renderer.LAYOUTS
            self._json(200, {
                "api": SCREEN_API, "native_api": NATIVE_API, "version": m.version,
                "host": m.host, "ip": m.ip, "mode": m.mode,
                "layout": m.layout, "design_size": r.get(m.layout),
                "installed": [a.summary(m) for a in map(m.addons.get, m.addons.ids()) if a],
                "failed": m.addons.failed, "restarting": m.restart,
                "paired": self._authed()})
            return
        if self.command != "POST":
            self._send(405, "use POST\n")
            return
        try:
            body = json.loads(self.body) if self.body else {}
        except ValueError:
            self._json(400, {"error": "the body isn't JSON"})
            return
        if not isinstance(body, dict):
            self._json(400, {"error": "the body should be a JSON object"})
            return
        if path == "/screens/pair":
            code = m.pairing.start()
            log(f"pairing code {code} (for {self.client_address[0]})")
            self._json(200, {"ok": True, "expires_in": PAIR_SECS})
            return
        if path == "/screens/pair/confirm":
            token, err = m.pairing.confirm(body.get("code", ""), body.get("client"))
            if token:
                m.flash("paired with " + str(body.get("client") or "the marketplace")[:40], COL_GREEN)
                self._json(200, {"token": token, "host": m.host})
            else:
                self._json(403, {"error": err})
            return
        if not self._authed():
            self._json(401, {"error": "not paired - pair with the display first"})
            return
        if path == "/screens/install":
            try:
                screen = m.addons.install(body.get("manifest"), body.get("files"),
                                          body.get("settings"), m)
            except ValueError as e:
                self._json(400, {"error": str(e)})
                return
            except OSError as e:
                self._json(500, {"error": f"couldn't save it: {e}"})
                return
            if screen is None:  # a native screen: it's in, once the display restarts
                if body.get("show", True):
                    m.state.put("mode", body["manifest"]["id"])
                self._json(200, {"ok": True, "restarting": True})
                return
            if body.get("show", True):
                m.set_mode(screen.id)
            m.flash(f"installed {screen.name}", COL_GREEN)
            self._json(200, {"ok": True, "screen": screen.summary(m)})
            return
        parts = path.split("/")  # ["", "screens", id, action]
        if len(parts) == 4 and (m.addons.get(parts[2]) or parts[2] in m.addons.failed):
            sid, action = parts[2], parts[3]
            if action == "uninstall" and not m.addons.get(sid):  # one that didn't load
                shutil.rmtree(os.path.join(m.addons.folder, sid), ignore_errors=True)
                m.addons.failed.pop(sid, None)
                m.addons.remember_order(sid, keep=False)
                self._json(200, {"ok": True})
                return
            if action == "uninstall":
                if m.mode == sid:
                    nxt = m.next_mode()
                    if nxt == sid or not m.set_mode(nxt):
                        with m.lock:
                            m.mode = WELCOME
                m.addons.uninstall(sid, m)
                self._json(200, {"ok": True, "restarting": m.restart})
                return
            if action == "settings" and m.addons.get(sid):
                try:
                    screen = m.addons.configure(sid, body.get("settings") or {}, m)
                except Exception as e:
                    self._json(400, {"error": f"{type(e).__name__}: {e}"})
                    return
                self._json(200, {"ok": True, "restarting": m.restart, "screen": screen.summary(m)})
                return
        self._json(404, {"error": "no such screen or action"})

    def _authed(self):
        return self.model.pairing.allowed(self.headers.get("Authorization"))

    def _send(self, code, text, ctype="text/plain"):
        body = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, where):
        self.send_response(303)
        self.send_header("Location", where)
        self.send_header("Content-Length", "0")
        self.end_headers()

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
    """Draws the screens at any resolution. The native screens add their own
    drawing methods (see NativeScreen); add-on screens draw through ScreenUI.

    Each layout is written in its own design units and scaled uniformly to fit,
    centered. The screen's shape picks the layout:
      landscape  800x480   monitors, TVs, the official touchscreen
      portrait   180x320   the ESP32's stacked layout
      bar        1480x320  long bar panels on their side (Waveshare 11.9")
      strip      320x1480  the same bar standing up
    """

    LAYOUTS = {"landscape": (800, 480), "portrait": (180, 320),
               "bar": (1480, 320), "strip": (320, 1480)}
    PRESS_SECS = 0.25  # how long a tapped button stays lit

    def __init__(self, size, fonts, natives=()):
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
        self.buttons = (None, [])  # (screen, [(rect, button)]) as last drawn: see button_at
        self.lit = (None, 0.0)     # (button, until): the one just tapped
        self.fit_cache = {}        # fit()'s answers: a slide asks the same ones every frame
        self.anim_at = 0.0         # when the slides last stepped (see advance)
        self.spin_cache = {}  # (frame, px) -> the spark, drawn once
        self.font_paths = fonts
        self.fonts = {}
        self.text_cache = {}
        self.shape_cache = {}
        self.addon_images = {}  # add-on screens' decoded images: see ScreenUI.image
        self.natives = list(natives)
        for native in self.natives:
            native.hook("init_renderer", self)

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
        key = (s, max_w, size, bold)
        out = self.fit_cache.get(key)
        if out is None:
            f, limit, out = self.font(size, bold), max_w * self.s, s
            if f.size(s)[0] > limit:
                while s and f.size(s + "\u2026")[0] > limit:
                    s = s[:-1]
                out = s.rstrip() + "\u2026"
            if len(self.fit_cache) > 600:
                self.fit_cache.clear()
            self.fit_cache[key] = out
        return out

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
        spots = [self.status_spinner()] + [n.hook("spinner_geometry", self) for n in self.natives
                                           if n.has("spinner_geometry")]
        for cx, cy, size in spots:
            for frame in range(-1, SPIN_FRAMES):
                self.spinner(dummy, cx, cy, size, frame)


    def press(self, button, mono):
        self.lit = (button, mono + self.PRESS_SECS)

    def lit_button(self, snap):
        return self.lit[0] if snap.mono < self.lit[1] else None

    def button_at(self, pos, mode):
        """The button at canvas pixel `pos`, as the `mode` screen was last drawn."""
        screen, buttons = self.buttons
        return next((b for rect, b in buttons if rect.collidepoint(pos)), None) if screen == mode else None


    def scene_key(self, snap, now):
        """Everything but the spinner's animation that changes the picture -
        redraw the whole screen only when this does."""
        key = (snap.mode, snap.flash, snap.host, snap.ip, now.strftime("%Y%m%d%H%M"), snap.pair_code)
        if snap.addon:
            a = snap.addon
            tick = int(snap.mono * a.fps) if a.fps else None
            return key + (id(a), snap.addon_view[1:], tick, snap.thinking)
        if snap.native:
            return key + tuple(snap.native.hook("scene_key", self, snap, now, default=()))
        return key + (WELCOME, snap.screens, snap.thinking)

    def draw(self, surf, snap, now):
        for native in self.natives:
            if native is not snap.native:
                native.hook("hidden", self)  # e.g. planespotters' photos: only while on screen
        self.buttons = (snap.mode, [])  # the screen's drawing adds its own
        surf.fill(COL_BG)
        if snap.addon:
            self._addon(surf, snap, now)
        elif snap.native:
            self._native(surf, snap, now)
        else:
            self._welcome(surf, snap, now)
        self._status(surf, snap)
        if snap.pair_code:
            self._pair_card(surf, snap.pair_code)

    def _native(self, surf, snap, now):
        """A native screen draws itself; if it throws, say so on the screen."""
        n = snap.native
        try:
            if n.has("draw"):
                n.hook("draw", self, surf, snap, now)
            else:
                getattr(self, f"_{n.id.replace('-', '_')}_{self.layout}")(surf, snap, now)
            n.draw_error = None
        except Exception as e:
            if n.draw_error != repr(e):
                log(f"screen {n.id}: draw failed\n{traceback.format_exc()}")
            n.draw_error = repr(e)
            surf.fill(COL_BG)
            ui = ScreenUI(self, surf, SimpleNamespace(id=n.id, color=COL_ORANGE, settings={},
                                                      folder=n.folder))
            ui.header(n.name, "this screen hit an error")
            w = ui.content_right - ui.content_left
            for i, line in enumerate(ui.wrap(f"{type(e).__name__}: {e}", w, 16, lines=4)):
                ui.text(line, ui.content_left, ui.top + 30 + i * 24, 16, COL_RED)

    def _welcome(self, surf, snap, now):
        """No screens yet (or none set up): how to get some."""
        ui = ScreenUI(self, surf, SimpleNamespace(id=WELCOME, color=COL_ORANGE, settings={}, folder=""))
        L, x0, x1 = self.layout, ui.content_left, ui.content_right
        title = "No screens yet" if not snap.screens else "Your screens need setting up"
        lines = ("Add some from the Screen Market: pair this display with it, then pick "
                 "the ones you want - your Claude usage, Spotify, weather, clocks and more.",
                 f"This display is {snap.host}.local  ({snap.ip or 'no network yet'})")
        if L == "bar":
            self.mascot(surf, 40, 70, 12)
            x0, top, size = 260, 100, 40
        elif L == "strip":
            self.mascot(surf, 82, 90, 13)
            top, size = 300, 32
        else:
            k = 1 if L == "landscape" else 0.42
            self.mascot(surf, x0, 30 * k + 12, 7 * k)
            top, size = (150, 34) if L == "landscape" else (76, 15)
        ui.text(ui.fit(title, x1 - x0, size, True), x0, top, size, COL_ORANGE, bold=True)
        y = top + size * 0.6
        for i, text in enumerate(lines):
            for line in ui.wrap(text, x1 - x0, size * 0.55, lines=2 if L in ("landscape", "bar") else 6):
                y += size * 0.85
                ui.text(line, x0, y, size * 0.55, COL_SUB if i == 0 else COL_DIM)
            y += size * 0.3

    def _addon(self, surf, snap, now):
        """An add-on screen draws itself; if it throws, say so on the screen."""
        a = snap.addon
        data = snap.addon_view[0]
        ui = ScreenUI(self, surf, a)
        try:
            a.obj.draw(ui, data, now)
            a.draw_error = None
        except Exception as e:
            if a.draw_error != repr(e):
                log(f"screen {a.id}: draw failed\n{traceback.format_exc()}")
            a.draw_error = repr(e)
            surf.fill(COL_BG)
            ui = ScreenUI(self, surf, a)
            ui.header(a.name, "this screen hit an error")
            w = ui.content_right - ui.content_left
            for i, line in enumerate(ui.wrap(f"{type(e).__name__}: {e}", w, 16, lines=4)):
                ui.text(line, ui.content_left, ui.top + 30 + i * 24, 16, COL_RED)

    def _pair_card(self, surf, code):
        """Pairing with a marketplace: the code to type in, over whatever's showing."""
        w, h = self.LAYOUTS[self.layout]
        cw, ch = min(w - 24, 520), min(h - 24, 230)
        cx, cy = (w - cw) / 2, (h - ch) / 2
        k = ch / 230
        pygame.draw.rect(surf, COL_CARD, self.rect(cx, cy, cw, ch), border_radius=self.n(18 * k))
        pygame.draw.rect(surf, COL_ORANGE, self.rect(cx, cy, cw, ch), self.n(3 * k), self.n(18 * k))
        mid = cx + cw / 2
        size = self.fit_size("Pair with the screen marketplace", cw - 30, 22 * k, smallest=8)
        self.text_on(surf, "Pair with the screen marketplace", mid, cy + 52 * k, size, COL_TEXT)
        spaced = f"{code[:3]} {code[3:]}"
        size = self.fit_size(spaced, cw - 30, 84 * k, bold=True, smallest=12)
        self.text_on(surf, spaced, mid, cy + 150 * k, size, COL_ORANGE, bold=True)
        size = self.fit_size("type this code in to pair", cw - 30, 18 * k, smallest=7)
        self.text_on(surf, "type this code in to pair", mid, cy + 196 * k, size, COL_DIM)

    def text_on(self, surf, s, x, baseline, size, color, bold=False):
        """Centred text with a transparent background, for drawing on cards."""
        f = self.font(size, bold)
        img = f.render(s, True, color)
        surf.blit(img, (self.x(x) - img.get_width() // 2, self.y(baseline) - f.get_ascent()))

    # where the status line starts: (x, baseline, text size)
    STATUS = {"landscape": (32, 454, 16), "portrait": (12, 315, 10),
              "bar": (28, 300, 19), "strip": (24, 1414, 18)}

    def _status(self, surf, snap):
        addr = f"{snap.host}.local  {snap.ip}".rstrip()
        status = snap.flash or (snap.addon_view[2] if snap.addon else
                                snap.native.hook("status", snap) if snap.native else None)
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
        text, color = status or ((snap.addon.name, COL_DIM) if snap.addon else
                                 ("starting...", COL_DIM) if snap.native else
                                 ("screens come from the Screen Market", COL_DIM))
        if self.layout == "strip":
            self.text(surf, self.fit(text, 272, 18), 24, 1414, 18, color)
            self.text(surf, self.fit(addr, 272, 18), 24, 1446, 18, COL_DIM)
        elif self.layout == "bar":
            self.text(surf, text, 28, 300, 19, color)
            self.text(surf, addr, 1268, 300, 19, COL_DIM, align="r")
        else:
            self.text(surf, text, 32, 454, 16, color)
            self.text(surf, addr, 768, 454, 16, COL_DIM, align="r")


    def advance(self, snap):
        """Step the screens' slides (the usage screen's session panel, the
        Spotify queue) toward where they should be; True while one moves."""
        dt = min(0.05, snap.mono - self.anim_at) if self.anim_at else 0.0
        self.anim_at = snap.mono
        moving = False
        for native in self.natives:  # (each also settles its slide while it's not showing)
            moving = bool(native.hook("advance", self, snap, dt)) or moving
        return moving

    @staticmethod
    def _step(value, target, step):
        return min(target, value + step) if target > value else max(target, value - step)

    def draw_slide(self, surf, snap, now):
        """Mid-slide frame: the screen repaints only the moving region; its rect."""
        return snap.native.hook("draw_slide", self, surf, snap, now)

    def width(self, s, size, bold=False):
        """Width of s in design units."""
        return self.font(size, bold).size(s)[0] / self.s

    def fit_size(self, s, max_w, size, bold=False, smallest=24):
        """The largest font size up to `size` at which s fits in max_w."""
        while size > smallest and self.width(s, size, bold) > max_w:
            size -= 2
        return size


    @staticmethod
    def own_spinner(snap):
        """Does this screen have a spinner of its own (the usage screen does)?"""
        return bool(snap.native and getattr(snap.native.mod, "OWN_SPINNER", False))

    def working_note(self, snap):
        """Does the status line show "Claude is working" (screens without a spinner)?"""
        return not self.own_spinner(snap) and snap.thinking and not snap.flash

    def spin_frame(self, snap):
        """The spinner's animation step, or -1 when nothing is spinning."""
        if not snap.thinking or (not self.own_spinner(snap) and not self.working_note(snap)):
            return -1
        return int(snap.mono * 1000 / SPIN_FRAME_MS) % SPIN_FRAMES

    def status_spinner(self):
        x, base, size = self.STATUS[self.layout]  # the small one in the status line
        return x + size * 0.8, base - size * 0.36, size * 1.6

    def spinner_geometry(self, snap):
        if self.own_spinner(snap) and snap.native.has("spinner_geometry"):
            return snap.native.hook("spinner_geometry", self)
        return self.status_spinner()

    def draw_spinner(self, surf, snap):
        """Draw just the spinner and return the rect it covers. Animation frames
        repaint and push only this square instead of the whole screen - on a
        Pi 2 under X, full-screen frames cost more CPU than everything else."""
        cx, cy, size = self.spinner_geometry(snap)
        px = self.n(size)
        rect = pygame.Rect(self.x(cx) - px // 2, self.y(cy) - px // 2, px, px)
        surf.fill(COL_BG, rect)
        self.spinner(surf, cx, cy, size, self.spin_frame(snap))
        return rect

    def draw_activity(self, surf, snap):
        """The spinner, plus whatever a screen with its own spinner animates
        alongside it. Returns the rects it painted, so animation frames push
        just those."""
        rects = [self.draw_spinner(surf, snap)]
        if self.own_spinner(snap):
            rects += snap.native.hook("activity", self, surf, snap, default=[])
        return rects

    def _header_landscape(self, surf, now, brand, subtitle=None):
        """The landscape layout's title block: the screen's logo, title and
        subtitle (its brand() hook), the clock and date."""
        native = next((n for n in self.natives if n.id == brand), None)
        title, color, sub = native.hook("brand", self, surf, subtitle) if native else (brand, COL_TEXT, subtitle)
        self.text(surf, title, 119, 56, 30, color, bold=True)
        self.text(surf, self.fit(sub, 420, 17), 119, 82, 17, COL_DIM)
        self.text(surf, clock_str(now), 768, 58, 30, COL_TEXT, align="r")
        self.text(surf, f"{now.strftime('%a %b')} {now.day}", 768, 82, 17, COL_DIM, align="r")
        surf.fill(COL_CARD, self.rect(32, 104, 736, 2))


    # -- bar: 1480x320. Two rows - brand + 5-hour, activity + weekly - with
    # long meters, so it reads left to right at a glance.
    def _clock_bar(self, surf, now):
        self.text(surf, clock_str(now), 1452, 300, 22, COL_TEXT, bold=True, align="r")


    # -- strip: 320x1480, the bar standing up. Everything stacks, big.
    def _clock_strip(self, surf, now):
        surf.fill(COL_CARD, self.rect(24, 1170, 272, 2))
        self.text(surf, clock_str(now), 160, 1262, 52, COL_TEXT, bold=True, align="c")
        self.text(surf, f"{now.strftime('%a %b')} {now.day}", 160, 1302, 22, COL_DIM, align="c")


    def car_frame(self, snap):
        """A moving map's animation step (F1's cars, the Metro's trains), else -1."""
        return snap.native.hook("frame", self, snap, default=-1) if snap.native else -1

    def draw_live_map(self, surf, snap):
        """Repaint the moving map; its rect."""
        return snap.native.hook("draw_frame", self, surf, snap)


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


def unrotate_point(p, rotate, cw, ch):
    """Where screen point p (a tap) is on a cw x ch canvas turned `rotate`
    degrees clockwise onto the screen - rotate_rect backwards."""
    x, y = p
    if rotate == 90:
        return y, ch - 1 - x
    if rotate == 180:
        return cw - 1 - x, ch - 1 - y
    if rotate == 270:
        return cw - 1 - y, x
    return x, y


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


def run_setup(config_path, name):
    """python3 pi/claude_display.py --setup <screen>: the screen's own setup."""
    cfg = Config(config_path)
    store = ScreenStore()
    path = os.path.join(store.folder, name)
    if not os.path.isfile(os.path.join(path, "manifest.json")):
        sys.exit(f"no {name!r} screen installed - add it from the Screen Market first "
                 f"(it goes in {store.folder})")
    screen = NativeScreen(path)
    if not screen.has("setup"):
        sys.exit(f"the {screen.name} screen has nothing to set up here - see its page in the Screen Market")
    screen.hook("configure", cfg)
    screen.hook("setup", config_path)


def main():
    ap = argparse.ArgumentParser(description="Claude Code usage display for a Raspberry Pi.")
    ap.add_argument("--config", default=CONFIG_PATH, help=f"config file (default {CONFIG_PATH})")
    ap.add_argument("--windowed", metavar="WxH", help="run in a window, e.g. 800x480 or 480x800")
    ap.add_argument("--demo", action="store_true",
                    help="the installed screens with fake data - no logins or network needed")
    ap.add_argument("--port", type=int, help="HTTP port (overrides config.ini)")
    ap.add_argument("--setup", metavar="SCREEN",
                    help="run an installed screen's own setup, e.g. planes, bambu or metro")
    for old in ("planes", "bambu", "metro"):  # the flags from before screens came from the market
        ap.add_argument(f"--setup-{old}", dest="setup", action="store_const", const=old,
                        help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.setup:
        run_setup(args.config, args.setup)
        return
    if pygame.version.vernum[0] < 2:
        sys.exit(f"needs pygame 2 (found {pygame.version.ver}) - pi/install.sh installs "
                 "it (on Bullseye: python3 -m pip install --user pygame)")
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # systemctl stop -> clean exit

    cfg = Config(args.config)
    if args.port:
        cfg.port = args.port
    state = StateFile(STATE_PATH)
    model = Model(cfg, state, demo=args.demo)
    model.ip = local_ip()
    natives = model.addons.natives()
    if not model.modes():
        log("no screens installed yet - add some from the Screen Market")

    BeaconHandler.model = model
    try:
        server = ThreadingHTTPServer(("", cfg.port), BeaconHandler)
        threading.Thread(target=server.serve_forever, daemon=True, name="http").start()
        log(f"listening on port {cfg.port}")
    except OSError as e:
        log(f"can't listen on port {cfg.port}: {e}")
        model.flash(f"port {cfg.port} busy - beacons off", COL_RED, secs=60)

    for native in natives:  # each screen's workers: real, or --demo's fakes
        if args.demo:
            if native.has("demo"):
                forever(native.mod.demo, model)
        elif native.ready(cfg):
            native.hook("start", model)
    for sid in model.addons.ids():  # add-on screens fetch in demo mode too: they're real
        screen = model.addons.get(sid)
        if isinstance(screen, AddonScreen):
            screen.start(model)

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
    renderer = Renderer(canvas.get_size(), find_fonts(), natives)
    model.layout = renderer.layout
    for native in natives:
        native.hook("on_layout", model, renderer)  # e.g. how big Spotify's album art is
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
                    # Touchscreens send taps as clicks. A tap on a button
                    # presses it; anywhere else switches screens.
                    button = renderer.button_at(unrotate_point(ev.pos, rotate, *canvas.get_size()),
                                                model.mode)
                    native = model.native()
                    if button and native:
                        renderer.press(button, time.monotonic())
                        native.hook("press", model, button)
                    else:
                        model.toggle_mode()

            if model.restart:  # screens or settings changed: start again with them
                log("restarting with the new screens / settings")
                time.sleep(0.5)  # let the HTTP reply go out
                pygame.quit()
                os.execv(sys.executable, [sys.executable] + sys.argv)

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
                    rects.append(renderer.draw_live_map(canvas, snap))
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
