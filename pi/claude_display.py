#!/usr/bin/env python3
"""Claude Code usage display - Raspberry Pi edition.

The ESP32 display (firmware/src/main.cpp) as a fullscreen app for a Raspberry
Pi - built for a Pi 2, fine on anything newer - on an HDMI monitor or the
official touchscreen.

This file is the display itself: the screen, the HTTP API the hooks and
beacons talk to, and the machinery screens plug into. The screens all come
from the Screen Market (github.com/nicoloco321/screen-market): your Claude
Code usage, Spotify, a Bambu Lab printer, planes overhead, Formula 1, the
Washington Metro, weather, clocks... Link the display to your account there
once (it shows a code to type in) and install the ones you want from any
browser; the display checks in with the market for them (MarketLink), so
nothing on your network needs opening up. They land in
~/.local/share/claude-display/screens. Until then it shows how to do that.

  - beacons: POST /thinking/on while Claude works, /thinking/off when done
    (Claude Code hooks or beacon.py); every screen shows a spinner then
  - screens: POST /mode/<screen>, /mode/toggle, tap the screen, or pick one
    from the home menu (swipe down from the top edge); GET /mode
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
one presses it instead. Swipe down from the top edge (or drag with the mouse,
or press the down arrow or M) for the home menu: every screen, to tap the one
you want, and Settings with the display's address, its logins and the Pi's
commands. Swipe it back up, or press Esc.
"""

import argparse
import base64
import configparser
import datetime
import getpass
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
        self.link = None           # the MarketLink, if this display links to a Screen Market
        self.reporting = False     # telling the market what a job did: a restart waits
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
                link_code=self.link and self.link.code, link_url=self.link and self.link.code_url,
                market=self.link and self.link.site,
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


# QR codes, for screens that point you somewhere: a page on the display, a
# photo's source. A small byte-mode encoder (ECC level L, versions 1-10: up
# to 271 bytes), after Project Nayuki's reference implementation.

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
NATIVE_API = 2     # what a native screen's "native_api" may ask for: the hooks below (2: + qr_matrix)
WELCOME = "welcome"  # the screen shown while none are installed (or set up)
SCREEN_ID = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
SCREEN_FILE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")
SCREEN_MAX_BYTES = 4 * 1024 * 1024  # one install, all files together
PAIR_SECS = 180
PAIR_TRIES = 5
# The Screen Market this display links to ([market] url in config.ini
# overrides it; set that to nothing to turn linking off).
MARKET_URL = ""


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


# ---------------------------------------------------------------- screen calls

def screens_info(m):
    """What's installed, and what this display is (GET /screens, and what it
    reports to the Screen Market)."""
    return {
        "api": SCREEN_API, "native_api": NATIVE_API, "version": m.version,
        "host": m.host, "ip": m.ip, "mode": m.mode,
        "layout": m.layout, "design_size": Renderer.LAYOUTS.get(m.layout),
        "installed": [a.summary(m) for a in map(m.addons.get, m.addons.ids()) if a],
        "failed": m.addons.failed, "restarting": m.restart}


def show_screen(m, want):
    """Switch to a screen (or "toggle"): (HTTP status, message)."""
    if want == "toggle":
        want = m.next_mode()
    if want == m.mode or m.set_mode(want):
        return 200, want
    if not m.modes():
        return 409, "no screens installed yet - add some from the Screen Market"
    if want not in m.modes():
        return 404, f"no {want} screen on this display"
    native = m.native(want)
    return 409, getattr(native and native.mod, "NOT_READY", f"the {want} screen isn't set up yet")


def screen_action(m, path, body):
    """Install, configure or remove a screen: (HTTP status, reply). The paths
    are the HTTP API's - POST /screens/install, /screens/<id>/settings,
    /screens/<id>/uninstall - which MarketLink's jobs use too."""
    if path == "/screens/install":
        try:
            screen = m.addons.install(body.get("manifest"), body.get("files"), body.get("settings"), m)
        except ValueError as e:
            return 400, {"error": str(e)}
        except OSError as e:
            return 500, {"error": f"couldn't save it: {e}"}
        if screen is None:  # a native screen: it's in, once the display restarts
            if body.get("show", True):
                m.state.put("mode", body["manifest"]["id"])
            return 200, {"ok": True, "restarting": True}
        if body.get("show", True):
            m.set_mode(screen.id)
        m.flash(f"installed {screen.name}", COL_GREEN)
        return 200, {"ok": True, "screen": screen.summary(m)}
    parts = path.split("/")  # ["", "screens", id, action]
    if len(parts) == 4 and (m.addons.get(parts[2]) or parts[2] in m.addons.failed):
        sid, action = parts[2], parts[3]
        if action == "uninstall" and not m.addons.get(sid):  # one that didn't load
            shutil.rmtree(os.path.join(m.addons.folder, sid), ignore_errors=True)
            m.addons.failed.pop(sid, None)
            m.addons.remember_order(sid, keep=False)
            return 200, {"ok": True}
        if action == "uninstall":
            if m.mode == sid:
                nxt = m.next_mode()
                if nxt == sid or not m.set_mode(nxt):
                    with m.lock:
                        m.mode = WELCOME
            m.addons.uninstall(sid, m)
            return 200, {"ok": True, "restarting": m.restart}
        if action == "settings" and m.addons.get(sid):
            try:
                screen = m.addons.configure(sid, body.get("settings") or {}, m)
            except Exception as e:
                return 400, {"error": f"{type(e).__name__}: {e}"}
            return 200, {"ok": True, "restarting": m.restart, "screen": screen.summary(m)}
    return 404, {"error": "no such screen or action"}


class MarketLink:
    """The display's line to the Screen Market, from the inside out - so it
    works behind any home router, with nothing to open up.

    Not linked yet: it asks the market for a code, shows it (with a QR code)
    until someone signed in there types it in, and gets a token for it. Linked:
    it checks in - what's installed, what it just did - and the market answers
    as soon as there's something to do (install, settings, show, uninstall),
    or after LINK_WAIT seconds of nothing. Unlinked from the website, it starts
    over with a new code.
    """
    LINK_WAIT = 25  # the market's long-poll; our timeout is longer

    def __init__(self, model, url):
        self.m, self.url = model, url.rstrip("/")
        self.site = urllib.parse.urlsplit(self.url).netloc or self.url
        self.code = None      # the code to type in, while not linked
        self.code_url = None  # the same, as a link (the QR code)

    @property
    def token(self):
        return self.m.state.get("market_token")

    def call(self, path, body, token=None, timeout=None):
        headers = {"Content-Type": "application/json", "User-Agent": "claude-display"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        code, raw, _ = http(self.url + path, json.dumps(body).encode(), headers,
                            timeout=timeout or self.LINK_WAIT + 20, method="POST")
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            data = {}
        return code, data if isinstance(data, dict) else {}

    def run(self):
        wait = 5
        while True:
            try:
                if self.token:
                    self.check_in()
                else:
                    self.link()
                wait = 5
            except (OSError, ValueError) as e:  # offline, the market's down...
                log(f"screen market: {e} - trying again in {wait}s")
                time.sleep(wait)
                wait = min(wait * 2, 300)

    def link(self):
        code, r = self.call("/api/device/link", {"host": self.m.host, "layout": self.m.layout,
                                                 "version": self.m.version}, timeout=20)
        if code != 200 or not r.get("code"):
            raise OSError(f"asking for a link code: HTTP {code} {r.get('error', '')}".strip())
        self.code, self.code_url = r["code"], r.get("url")
        log(f"screen market: sign in at {self.url} and link this display with code {self.code}")
        until = time.monotonic() + int(r.get("expires_in") or 900) - 10
        while time.monotonic() < until:
            code, got = self.call("/api/device/link/poll", {"poll": r["poll"]})
            if code == 200 and got.get("token"):
                self.m.state.put("market_token", got["token"])
                self.code = self.code_url = None
                self.m.flash("linked to the Screen Market", COL_GREEN)
                log("screen market: linked")
                return
            if code == 404:
                return  # expired: ask for a new one
            if code != 200:
                raise OSError(f"waiting to be linked: HTTP {code}")

    def check_in(self):
        results = []
        while True:
            self.m.reporting = bool(results)  # a restart waits until the market has heard
            try:
                code, r = self.call("/api/device/sync", {"status": screens_info(self.m), "results": results,
                                                         "wait": not results}, token=self.token)
            finally:
                self.m.reporting = False
            if code == 401:
                log("screen market: unlinked from the website - showing a new code")
                self.m.state.put("market_token", None)
                return
            if code != 200:
                raise OSError(f"checking in: HTTP {code}")
            jobs = r.get("jobs") or []
            if jobs:
                self.m.reporting = True  # hold any restart a job asks for
            results = [self.do(job) for job in jobs if isinstance(job, dict)]

    def do(self, job):
        """Carry out one job from the market: {"id", "code", "reply"}."""
        action, sid = job.get("action"), str(job.get("screen") or "")
        try:
            if action == "install":
                code, reply = screen_action(self.m, "/screens/install", {
                    "manifest": job.get("manifest"), "files": job.get("files"),
                    "settings": job.get("settings"), "show": job.get("show", True)})
            elif action in ("settings", "uninstall") and SCREEN_ID.match(sid):
                code, reply = screen_action(self.m, f"/screens/{sid}/{action}",
                                            {"settings": job.get("settings") or {}})
            elif action == "show" and SCREEN_ID.match(sid):
                code, text = show_screen(self.m, sid)
                reply = {"ok": True} if code == 200 else {"error": text}
            else:
                code, reply = 400, {"error": f"this display doesn't know how to {action!r}"}
        except Exception as e:
            log(f"screen market: {action} {sid} failed\n{traceback.format_exc()}")
            code, reply = 500, {"error": f"{type(e).__name__}: {e}"}
        log(f"screen market: {action} {sid} -> {code}")
        return {"id": job.get("id"), "code": code, "reply": reply}


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
            code, text = show_screen(m, path[6:])
            self._send(code, text + "\n")
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
            self._json(200, dict(screens_info(m), paired=self._authed()))
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
        self._json(*screen_action(m, path, body))

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
        key = (snap.mode, snap.flash, snap.host, snap.ip, now.strftime("%Y%m%d%H%M"), snap.pair_code,
               snap.link_code)
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
        """No screens yet (or none set up): how to get some - with the code to
        link this display to the Screen Market, while it isn't."""
        ui = ScreenUI(self, surf, SimpleNamespace(id=WELCOME, color=COL_ORANGE, settings={}, folder=""))
        L, x0, x1 = self.layout, ui.content_left, ui.content_right
        title = "No screens yet" if not snap.screens else "Your screens need setting up"
        if snap.link_code:
            lines = (f"Sign in at {snap.market}, go to My display and type in this code (or scan "
                     "the QR code), then pick the screens you want.",)
        elif snap.market:
            lines = (f"Pick some at {snap.market} - your Claude usage, Spotify, weather, clocks "
                     "and more - and they show up here.",)
        else:
            lines = ("Add some from the Screen Market: pair this display with it, then pick "
                     "the ones you want - your Claude usage, Spotify, weather, clocks and more.",)
        if not (snap.link_code and L == "bar"):  # no room on a bar next to the code
            lines += (f"This display is {snap.host}.local  ({snap.ip or 'no network yet'})",)
        qr = None  # (x, y, size) of the QR code
        if L == "bar":
            self.mascot(surf, 40, 70, 12)
            x0, top, size = 260, 100, 40
            if snap.link_url:
                qr = (x1 - 230, 45, 230)
        elif L == "strip":
            self.mascot(surf, 82, 90, 13)
            top, size = 300, 32
            if snap.link_url:
                qr = (x0, 1050, x1 - x0)
        else:
            k = 1 if L == "landscape" else 0.42
            self.mascot(surf, x0, 30 * k + 12, 7 * k)
            top, size = (150, 34) if L == "landscape" else (76, 15)
            if snap.link_url and L == "landscape":
                qr = (x1 - 210, 130, 210)
        if qr and L != "strip":
            x1 = qr[0] - 30
        ui.text(ui.fit(title, x1 - x0, size, True), x0, top, size, COL_ORANGE, bold=True)
        y = top + size * 0.6
        for i, text in enumerate(lines):
            for line in ui.wrap(text, x1 - x0, size * 0.55, lines=3 if L in ("landscape", "bar") else 7):
                y += size * 0.85
                ui.text(line, x0, y, size * 0.55, COL_SUB if i == 0 else COL_DIM)
            y += size * 0.3
            if i == 0 and snap.link_code:  # the code, big, under what to do with it
                y += size * (1.3 if L == "bar" else 1.7)
                code_size = self.fit_size(snap.link_code, x1 - x0, size * (1.3 if L == "bar" else 1.6),
                                          bold=True, smallest=10)
                ui.text(snap.link_code, x0, y, code_size, COL_TEXT, bold=True)
                y += size * 0.2
        if qr:
            self.qr_code(surf, snap.link_url, *qr)

    def qr_code(self, surf, link, x, y, size):
        """A QR code of `link`, `size` design units square, top-left at (x, y)."""
        px = self.n(size)
        cache = getattr(self, "qr_cache", {})
        img = cache.get((link, px))
        if img is None:
            grid = qr_matrix(link.encode())
            n = len(grid) + 8  # with its quiet zone
            cell = max(1, px // n)
            img = pygame.Surface((n * cell, n * cell))
            img.fill((255, 255, 255))
            for gy, row in enumerate(grid):
                for gx, dark in enumerate(row):
                    if dark:
                        img.fill((0, 0, 0), (cell * (gx + 4), cell * (gy + 4), cell, cell))
            self.qr_cache = {(link, px): img}
        surf.blit(img, (self.x(x) + (px - img.get_width()) // 2, self.y(y) + (px - img.get_height()) // 2))

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
        if status is None and snap.link_code and (snap.addon or snap.native):
            # screens installed, but not linked to the market: where to do that
            status = (f"Screen Market code {snap.link_code}", COL_ORANGE)
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


# ---------------------------------------------------------------- the home menu

MENU_SECS = 0.25     # sliding all the way down or back up
MENU_IDLE_SECS = 60  # left down with nobody touching it, it goes back up
# For the commands on the Settings page. DejaVu's ships with Raspberry Pi OS;
# the others let you try the menu on a desktop. Falls back to the regular font.
MONO_CANDIDATES = ["/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
                   r"C:\Windows\Fonts\consola.ttf",
                   "/System/Library/Fonts/Supplemental/Andale Mono.ttf"]
REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # the code-usage checkout


def mix(a, b, t):
    """Colour a, t of the way to colour b."""
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def tilde(path):
    """A path with your home folder as ~, the way you'd type it."""
    path, home = os.path.abspath(path), os.path.expanduser("~")
    return "~" + path[len(home):] if path.startswith(home + os.sep) else path


class HomeMenu:
    """The home menu: every installed screen in equal sections. Swipe down
    from the top edge (or press the down arrow, or M) and it slides down over
    whatever's showing; tap a section to show that screen, or swipe it back up.
    Its Settings page has the details you'll want later: where the display
    is, how to point Claude Code at it, the logins its screens need, and how
    to look after the Pi.

    The sections sit side by side on wide layouts (landscape, bar) and stack
    on tall ones (portrait, strip). The main loop hands it every touch first
    (press / drag / release) and, while it's down, lets it draw (frame)."""

    # Where it all goes, in each layout's design units (the title row is in
    # header): the screens' sections or the settings' cards, the gap between
    # them, the handle at the bottom edge (centre x, centre y, w, h), and the
    # biggest text the settings' cards use.
    BODY = {"landscape": (32, 100, 736, 336), "bar": (28, 76, 1424, 214),
            "portrait": (12, 46, 156, 252), "strip": (24, 256, 272, 1164)}
    GAP = {"landscape": 12, "bar": 16, "portrait": 6, "strip": 14}
    NARROWEST = {"landscape": 80, "bar": 96, "portrait": 26, "strip": 110}  # a section, before a 2nd row
    GRIP = {"landscape": (400, 458, 64, 6), "bar": (740, 305, 72, 6),
            "portrait": (90, 310, 36, 4), "strip": (160, 1450, 80, 8)}
    TEXT = {"landscape": 14, "bar": 15, "portrait": 8, "strip": 16}

    def __init__(self, model, renderer):
        self.m, self.r = model, renderer
        self.W, self.H = renderer.W, renderer.H  # the canvas, in pixels
        short = min(self.W, self.H)
        self.slop = max(8, short * 0.035)       # a touch that moves less is a tap
        self.travel = short * 0.65              # a pull this long brings it all the way down
        self.zone = min(self.H * 0.2, self.W * 0.3)  # a pull starts this near the top edge
        self.pos = 0.0          # 0 = up out of sight, 1 = all the way down
        self.slide = None       # (from, to, when) while it slides on its own
        self.touch = None       # the touch being followed (see press)
        self.fresh = False      # just coming down: keep a copy of the screen under it first
        self.stale = False      # going back up: draw that screen afresh (it may be another one now)
        self.page = "screens"   # or "settings"
        self.sheet = 0          # which page of cards Settings is on (portrait has a few)
        self.surf = None        # the menu, drawn
        self.under = None       # the screen it slides over
        self.key = None         # what self.surf shows
        self.drawn = None       # how far down it was last put on the canvas
        self.hits = []          # [(rect, what tapping there does)] as last drawn
        self.touched = 0.0      # when anyone last touched it
        self.icons = {}         # (screen id, px, ready) -> its icon
        self.backs = {}         # (colour, size, radius, showing) -> a section's card
        self.logo_r = None      # (scale, a landscape Renderer) for drawing logos (see logo)
        self.mono_path = next((p for p in MONO_CANDIDATES if os.path.exists(p)), None)
        self.mono_fonts = {}
        self.health = (-1e9, None)
        depth = self.r.n(18)    # the shadow it casts on the screen while it slides
        self.shadow = pygame.Surface((self.W, depth), pygame.SRCALPHA)
        for i in range(depth):
            self.shadow.fill((0, 0, 0, round(130 * (1 - i / depth) ** 2)), (0, i, self.W, 1))

    @property
    def out(self):
        """Is it on the screen, or on its way?"""
        return self.pos > 0 or self.fresh or self.moving

    @property
    def moving(self):
        return self.slide is not None or bool(self.touch and self.touch.pull)

    def open(self):
        if not self.out:
            self.fresh, self.page = True, "screens"
        self.touched = time.monotonic()
        self.glide(1.0)

    def close(self):
        self.glide(0.0)

    def glide(self, to):
        self.slide = (self.pos, to, time.monotonic())
        self.stale = self.stale or to == 0

    # -- touches, in canvas pixels
    def press(self, pos):
        x, y = pos
        self.touch = SimpleNamespace(x0=x, y0=y, x=x, y=y, pull=None, trail=[(time.monotonic(), y)])
        self.touched = time.monotonic()

    def drag(self, pos):
        """The finger moved. A pull down from the top edge brings the menu
        down with it; a pull up takes it back."""
        t = self.touch
        if t is None:
            return
        now = time.monotonic()
        t.x, t.y = pos
        t.trail = [p for p in t.trail if now - p[0] < 0.1] + [(now, t.y)]
        self.touched = now
        dx, dy = t.x - t.x0, t.y - t.y0
        if t.pull is None and abs(dy) > self.slop and abs(dy) > abs(dx):
            if dy > 0 and not self.out and t.y0 < self.zone:
                t.pull, self.fresh, self.page = "down", True, "screens"
            elif dy < 0 and self.pos == 1 and not self.slide:
                t.pull, self.stale = "up", True
        if t.pull:
            self.pos = max(0.0, min(1.0, (0 if t.pull == "down" else 1) + dy / self.travel))

    def release(self, pos):
        """The finger lifted. True if the menu took the touch (a pull, or a
        tap while it's down); False for a tap the screen should have."""
        if self.touch is None:
            return self.out
        self.drag(pos)  # (some touchscreens send nothing between down and up)
        t, self.touch = self.touch, None
        if t.pull:
            (t0, y0), (t1, y1) = t.trail[0], t.trail[-1]
            fling = (y1 - y0) / (t1 - t0) / self.travel if t1 - t0 > 0.01 else 0  # travels a second
            if t.pull == "down":
                self.glide(1.0 if self.pos > 0.3 or fling > 2 else 0.0)
            else:
                self.glide(0.0 if self.pos < 0.7 or fling < -2 else 1.0)
            return True
        if not self.out:
            return False
        if abs(t.x - t.x0) < self.slop and abs(t.y - t.y0) < self.slop and not self.slide:
            what = next((w for rect, w in self.hits if rect.collidepoint(t.x, t.y)), None)
            if what and what[0] == "show":
                self.choose(what[1])
            elif what and what[0] == "page":
                self.page, self.sheet = what[1], 0
            elif what and what[0] == "sheet":
                self.sheet += 1
        return True

    def choose(self, sid):
        """A screen's section was tapped: show that screen and go back up -
        or, if it isn't set up yet, go to Settings, which says what it needs."""
        if not self.m.is_ready(sid):
            self.page, self.sheet = "settings", 0
            return
        self.m.set_mode(sid)
        self.close()

    # -- drawing
    def frame(self, canvas, snap, now):
        """Put the menu on the canvas as it is now; True if that changed it."""
        if self.fresh:  # coming down: keep the screen it covers
            self.fresh, self.under, self.key = False, canvas.copy(), None
        if self.stale and self.under is not None:  # going up: to the screen as it is now
            self.stale = False
            self.r.draw(self.under, snap, now)
        if self.slide:
            start, end, at = self.slide
            k = min(1.0, (snap.mono - at) / (MENU_SECS * max(0.4, abs(end - start))))
            self.pos = start + (end - start) * (1 - (1 - k) ** 3)  # fast, then settling
            if k >= 1:
                self.slide, self.pos = None, end
        elif self.pos == 1 and not self.touch and snap.mono - self.touched > MENU_IDLE_SECS:
            self.close()
        key = self.scene_key(snap, now)
        if key != self.key:
            self.key, self.drawn = key, None
            self.render(snap, now)
        if self.drawn == self.pos:
            return False
        self.drawn = self.pos
        if self.pos >= 1:
            canvas.blit(self.surf, (0, 0))
        else:
            edge = round(self.pos * self.H)
            canvas.blit(self.under, (0, 0))
            canvas.blit(self.surf, (0, edge - self.H))
            canvas.blit(self.shadow, (0, edge))
            if self.pos <= 0 and not self.moving:
                self.under = None  # all the way up: the screen gets drawn afresh
        return True

    def scene_key(self, snap, now):
        """Everything that changes the menu's picture."""
        screens = tuple((s.id, self.m.is_ready(s.id), self.tile_status(s, snap)) for s in self.installed())
        key = (self.page, snap.mode, screens, now.strftime("%H%M"), snap.flash, snap.thinking,
               snap.pair_code)
        if self.page == "settings":
            key += (self.sheet, snap.ip, snap.link_code, snap.market, self.health_line())
        return key

    def installed(self):
        return [s for s in map(self.m.addons.get, self.m.modes()) if s]

    def render(self, snap, now):
        if self.surf is None:
            self.surf = pygame.Surface((self.W, self.H)).convert()
        self.surf.fill(COL_BG)
        self.hits = []
        ui = ScreenUI(self.r, self.surf, SimpleNamespace(id="menu", color=COL_ORANGE, settings={}, folder=""))
        if self.page == "settings":
            self.settings(ui, snap, now)
        else:
            self.screens(ui, snap, now)
        gx, gy, gw, gh = self.GRIP[self.r.layout]  # the handle: swipe up here
        ui.rect(gx - gw / 2, gy - gh / 2, gw, gh, (74, 74, 74), radius=gh / 2)
        if snap.pair_code:
            self.r._pair_card(self.surf, snap.pair_code)

    def header(self, ui, title, sub, now, page, sheets=None):
        """The title row: the title and a line under it (sub: (text, colour)),
        the clock, and the button to the other page. sheets = (this one,
        how many) puts a button for the next one beside it."""
        L = self.r.layout
        text, color = sub
        label = "Settings" if page == "settings" else "Screens"
        if L == "landscape":
            self.pill(ui, 768, 27, 50, page, label)
            ui.text(title, 32, 58, 30, COL_TEXT, bold=True)
            ui.text(ui.fit(text, 400, 16), 32, 83, 16, color)
            ui.text(clock_str(now), 590, 58, 26, COL_TEXT, align="r")
            ui.text(f"{now.strftime('%a %b')} {now.day}", 590, 81, 15, COL_DIM, align="r")
        elif L == "bar":
            self.pill(ui, 1452, 14, 48, page, label)
            ui.text(title, 28, 50, 30, COL_TEXT, bold=True)
            x = 28 + ui.width(title, 30, True) + 24
            ui.text(ui.fit(text, 1080 - x, 18), x, 50, 18, color)
            ui.text(clock_str(now), 1268, 49, 24, COL_TEXT, bold=True, align="r")
        elif L == "strip":
            ui.text(clock_str(now), 160, 64, 22, COL_DIM, align="c")
            ui.text(title, 160, 118, 40, COL_TEXT, bold=True, align="c")
            for i, line in enumerate(ui.wrap(text, 272, 18, lines=2)):
                ui.text(line, 160, 150 + i * 24, 18, color, align="c")
            self.pill(ui, 160, 182, 52, page, label, align="c")
        else:  # portrait: room for the title and the buttons, no more
            ui.text(title, 12, 29, 16, COL_TEXT, bold=True)
            self.pill(ui, 168, 9, 28, page, label, align="icon")
            if sheets:  # (only portrait's settings come in pages)
                self.pill(ui, 130, 9, 28, "sheet", f"{sheets[0] + 1}/{sheets[1]}", align="icon")

    def pill(self, ui, x, y, h, what, label, align="r"):
        """A rounded button with an icon and a label: its right edge at x
        (align "r"), or its centre ("c"); "icon" makes a square one with just
        the icon, right edge at x - or for "sheet", just the label."""
        s = h * 0.44
        if align == "icon":
            w = h * 1.2
            x -= w
            ui.rect(x, y, w, h, COL_CARD, radius=h * 0.3)
            if what == "sheet":
                ui.text(label, x + w / 2, y + h * 0.66, h * 0.42, COL_TEXT, bold=True, align="c")
            else:
                self.glyph(ui, what, x + w / 2, y + h / 2, s)
        else:
            size = h * 0.38
            w = h * 0.42 + s + h * 0.24 + ui.width(label, size, True) + h * 0.5
            x -= w if align == "r" else w / 2
            ui.rect(x, y, w, h, COL_CARD, radius=h / 2)
            self.glyph(ui, what, x + h * 0.42 + s / 2, y + h / 2, s)
            ui.text(label, x + h * 0.42 + s + h * 0.24, y + h * 0.64, size, COL_TEXT, bold=True)
        self.hits.append((self.r.rect(x, y, w, h), ("sheet",) if what == "sheet" else ("page", what)))

    def glyph(self, ui, what, cx, cy, size):
        """The buttons' icons: a gear for Settings, four squares for Screens."""
        px = self.r.n(size)

        def draw(big, k):
            p = px * k
            c = p / 2
            if what == "settings":
                for i in range(8):  # the teeth, then the wheel, then its hole
                    a = i * math.pi / 4
                    ca, sa = math.cos(a), math.sin(a)
                    pygame.draw.polygon(big, COL_TEXT, [
                        (c + ca * r - sa * w, c + sa * r + ca * w)
                        for r, w in ((c * 0.6, -c * 0.2), (c, -c * 0.15), (c, c * 0.15), (c * 0.6, c * 0.2))])
                pygame.draw.circle(big, COL_TEXT, (c, c), c * 0.74)
                pygame.draw.circle(big, COL_CARD, (c, c), c * 0.3)
            else:
                g = p * 0.42
                for i, j in ((0, 0), (1, 0), (0, 1), (1, 1)):
                    pygame.draw.rect(big, COL_TEXT, (i * (p - g), j * (p - g), g, g),
                                     border_radius=round(g * 0.3))

        img = self.r._ss(("menu-glyph", what), px, px, draw)
        ui.surface.blit(img, (self.r.x(cx) - px // 2, self.r.y(cy) - px // 2))

    # -- the screens
    def screens(self, ui, snap, now):
        L = self.r.layout
        hint = ("tap one to show it  ·  swipe up to go back", COL_DIM)
        self.header(ui, "Screens", snap.flash or (("Claude is working...", COL_ORANGE)
                                                  if snap.thinking else hint), now, "settings")
        screens = self.installed()
        x, y, w, h = self.BODY[L]
        if not screens:
            k = min(w, h) / 220
            self.r.mascot(ui.surface, x + w / 2 - 36 * k, y + h / 2 - 66 * k, 6 * k)
            ui.text("No screens yet", x + w / 2, y + h / 2 + 22 * k, 26 * k, COL_TEXT, bold=True, align="c")
            ui.text(ui.fit("Add some from the Screen Market: Settings says how", w, 15 * k),
                    x + w / 2, y + h / 2 + 50 * k, 15 * k, COL_DIM, align="c")
            return
        n = len(screens)
        gap = self.GAP[L] * (0.6 if n > 7 else 1)
        across = L in ("landscape", "bar")
        long, short = (w, h) if across else (h, w)  # the screens share out the long way
        lanes = 1  # (a second row, or column, only once there are too many to read)
        while lanes < 3 and (long - gap * (-(-n // lanes) - 1)) / -(-n // lanes) < self.NARROWEST[L]:
            lanes += 1
        per = -(-n // lanes)
        each, depth = (long - gap * (per - 1)) / per, (short - gap * (lanes - 1)) / lanes
        look = self.tile_look(ui, screens, *((each, depth) if across else (depth, each)))
        for i, screen in enumerate(screens):
            lane, i = divmod(i, per)
            along, aside = i * (each + gap), lane * (depth + gap)
            box = (x + along, y + aside, each, depth) if across else (x + aside, y + along, depth, each)
            self.tile(ui, screen, box, snap, look)
            self.hits.append((self.r.rect(*box), ("show", screen.id)))

    def tile_look(self, ui, screens, w, h):
        """How the sections are laid out - the same for all of them, so they
        line up: the icon beside the words if they're wide, else above them,
        and the sizes, as big as fits every name (on two lines if need be)."""
        wide = w >= h * 2
        if wide:
            icon = min(h * 0.62, w * 0.3, 108)
            pad = min((h - icon) / 2, 28)
            room, size = w - pad * 2.7 - icon - 12, max(9, min(26, h * 0.22))
        else:
            pad = max(6, w * 0.06)
            icon = min(w * 0.72, h * 0.4, 112)
            room, size = w - 2 * pad, max(9, min(24, w * 0.13))
        small, lines = size * 0.7, 1
        for k in (1, 0.94, 0.88, 0.82, 0.76):
            if all(ui.width(s.name, size * k, True) <= room for s in screens):
                size *= k
                break
        else:
            size, lines = size * 0.76, 2
        return SimpleNamespace(wide=wide, icon=icon, pad=pad, room=room, size=size, small=small, lines=lines)

    def tile(self, ui, screen, box, snap, look):
        """One screen's section: its icon, its name and how it's doing, on
        its own colour - lit up while it's the one showing."""
        x, y, w, h = box
        accent = self.accent(screen)
        here = screen.id == snap.mode
        rect = self.r.rect(x, y, w, h)
        radius = self.r.n(min(18, w * 0.14, h * 0.14))
        ui.surface.blit(self.tile_back(accent, rect.size, radius, here), rect)
        if here:
            pygame.draw.rect(ui.surface, accent, rect, self.r.n(max(1.5, min(3, w * 0.05, h * 0.05))),
                             border_radius=radius)
        size, small = look.size, look.small
        names = (ui.wrap(screen.name, look.room, size, bold=True, lines=2) if look.lines > 1
                 else [ui.fit(screen.name, look.room, size, True)])
        words = look.lines * size * 1.2 + small * 1.6  # (the same height in every section)
        if look.wide:
            self.icon(ui, screen, x + look.pad, y + (h - look.icon) / 2, look.icon, accent)
            at, base, align = x + look.pad * 1.7 + look.icon, y + (h - words) / 2 + size * 0.95, "l"
        else:  # (leaving room at the bottom for the bar that marks the one showing)
            top = y + (h - 12 - look.icon - size * 0.55 - words) / 2
            self.icon(ui, screen, x + (w - look.icon) / 2, top, look.icon, accent)
            at, base, align = x + w / 2, top + look.icon + size * 1.5, "c"
        for line in names:
            ui.text(line, at, base, size, COL_TEXT, bold=True, align=align)
            base += size * 1.2
        text, color = self.tile_status(screen, snap)
        if text:
            below = y + h - 16 - (base + small * 0.4)  # the room under the status's first line
            self.status_text(ui, text, color, at, base + small * 0.4, small, look.room, align,
                             lines=2 if not look.wide and below > small * 1.6 else 1)
        if here:  # and a bar in its colour, like a dock's running light
            if look.wide:
                bar = min(40, h * 0.42)
                ui.rect(x + w - 14, y + (h - bar) / 2, 5, bar, accent, radius=2.5)
            else:
                bar = min(44, w * 0.3)
                ui.rect(x + (w - bar) / 2, y + h - 14, bar, 5, accent, radius=2.5)

    def tile_back(self, accent, size, radius, here):
        """A section's card: its colour, fading as it goes down (cached)."""
        key = (accent, size, radius, here)
        img = self.backs.get(key)
        if img is None:
            w, h = size
            top, bottom = (0.3, 0.12) if here else (0.17, 0.04)
            img = self.backs[key] = pygame.Surface(size, pygame.SRCALPHA)
            for i in range(h):
                img.fill(mix(COL_CARD, accent, top + (bottom - top) * i / max(1, h - 1)), (0, i, w, 1))
            corners = pygame.Surface(size, pygame.SRCALPHA)
            pygame.draw.rect(corners, (255, 255, 255, 255), corners.get_rect(), border_radius=radius)
            img.blit(corners, (0, 0), special_flags=pygame.BLEND_RGBA_MIN)
        return img

    @staticmethod
    def accent(screen):
        return getattr(screen, "color", None) or parse_color(screen.manifest.get("color"))

    def tile_status(self, screen, snap):
        """What a section says under the screen's name: its status line,
        if it has one, else what kind of screen it is. (text, colour)"""
        if not self.m.is_ready(screen.id):
            return "not set up yet", COL_YELLOW
        try:
            if isinstance(screen, NativeScreen):
                if not self.m.demo and screen.hook("needs_setup", self.m.cfg):
                    return "not set up yet", COL_YELLOW
                status = screen.hook("status", snap)
            else:
                status = screen.view()[2]
        except Exception:
            status = None
        if status and status[0] and status[0] != "loading...":
            return str(status[0]), parse_color(status[1], COL_DIM)
        kind = str(screen.manifest.get("category") or "")
        return ("" if kind.lower() == screen.name.lower() else kind), COL_DIM

    def status_text(self, ui, text, color, x, base, size, max_w, align, lines=1):
        """A status under a name: a dot in its colour, then the words (a dim
        one is just the words), over as many lines as it may take."""
        d = 0 if color == COL_DIM else size * 0.55
        room = max_w - d * 1.7
        for i, part in enumerate(ui.wrap(text, room, size, lines=lines) if lines > 1 else [ui.fit(text, room, size)]):
            left = x - (ui.width(part, size) + d * 1.7) / 2 if align == "c" else x
            if d and not i:
                ui.circle(left + d / 2, base - size * 0.36, d / 2, color)
            ui.text(part, left + d * 1.7, base, size, COL_SUB if d else COL_DIM)
            base += size * 1.3

    def icon(self, ui, screen, x, y, size, accent):
        px = self.r.n(size)
        ready = self.m.is_ready(screen.id)
        key = (screen.id, px, ready)
        img = self.icons.get(key)
        if img is None:
            img = self.icons[key] = self.make_icon(screen, px, accent)
            if not ready:  # faded, like a greyed-out app
                img.fill((255, 255, 255, 105), special_flags=pygame.BLEND_RGBA_MULT)
        ui.surface.blit(img, (self.r.x(x), self.r.y(y)))

    def make_icon(self, screen, px, accent):
        """A screen's icon, px square: a native screen's logo (the one in its
        own header) on a dark tile, else its initial on a tile of its colour,
        the badge an add-on screen's header has."""
        logo = self.logo(screen, px * 0.6) if isinstance(screen, NativeScreen) and screen.has("brand") else None
        back = COL_BG if logo else accent
        img = self.r._ss(("menu-icon", back, px), px, px, lambda big, k: pygame.draw.rect(
            big, back, big.get_rect(), border_radius=round(px * k * 0.24))).copy()
        if logo:
            lw, lh = logo.get_size()
            k = min(px * 0.62 / lw, px * 0.6 / lh)
            logo = pygame.transform.smoothscale(logo, (max(1, round(lw * k)), max(1, round(lh * k))))
            img.blit(logo, logo.get_rect(center=(px // 2, px // 2)))
        else:
            mark = str(screen.manifest.get("icon") or "")
            letter = (mark if re.fullmatch(r"[A-Za-z0-9]{1,2}", mark) else
                      next((c for c in screen.name if c.isalnum()), "?")).upper()
            f = self.r.font(px * (0.5 if len(letter) == 1 else 0.38) / self.r.s, True)
            glyph = f.render(letter, True, COL_BG)
            img.blit(glyph, glyph.get_rect(center=(px // 2, px // 2 + px // 40)))
        return img

    def logo(self, native, px):
        """A native screen's logo, about px pixels tall: what its brand() hook
        draws in the landscape header (about 56 units tall, left of x = 112),
        drawn off-screen by a landscape renderer at the right scale and cut out
        of the background."""
        k = px / 56
        if not self.logo_r or self.logo_r[0] != k:
            self.logo_r = (k, Renderer((round(800 * k), round(480 * k)), self.r.font_paths, self.r.natives))
        lr = self.logo_r[1]
        surf = pygame.Surface((lr.n(112), lr.n(100)))
        surf.fill(COL_BG)
        try:
            native.hook("brand", lr, surf, "")
        except Exception:
            log(f"screen {native.id}: brand failed\n{traceback.format_exc()}")
            return None
        mask = pygame.mask.from_threshold(surf, COL_BG, (4, 4, 4, 255))
        mask.invert()
        found = mask.get_bounding_rects()
        return surf.subsurface(found[0].unionall(found[1:])).copy() if found else None

    # -- settings
    def settings(self, ui, snap, now):
        cards = self.cards(snap)
        sheets = self.arrange(len(cards))
        self.sheet %= len(sheets)
        self.header(ui, "Settings", ("the details you'll want later", COL_DIM), now, "screens",
                    (self.sheet, len(sheets)) if len(sheets) > 1 else None)
        rows = sheets[self.sheet]
        x, y, w, h = self.BODY[self.r.layout]
        gap = self.GAP[self.r.layout]
        plan = []  # each row: its cards' widths, shared out by weight, and how tall it'd like to be
        for row in rows:
            across = (w - gap * (len(row) - 1)) / sum(cards[i][2] for i in row)
            widths = [across * cards[i][2] for i in row]
            plan.append((row, widths, max(self.card_height(ui, *cards[i][:2], wide)
                                          for i, wide in zip(row, widths))))
        k = (h - gap * (len(rows) - 1)) / sum(want for _, _, want in plan)
        for row, widths, want in plan:
            left = x
            for i, wide in zip(row, widths):
                self.card(ui, (left, y, wide, want * k), *cards[i][:2])
                left += wide + gap
            y += want * k + gap

    def arrange(self, n):
        """The cards' places: pages of rows of card numbers."""
        L, every = self.r.layout, list(range(n))
        if L == "bar":
            return [[every]]
        if L == "strip":
            return [[[i] for i in every]]
        if L == "portrait":
            return [[[i] for i in every[p:p + 2]] for p in range(0, n, 2)]
        top = (n + 1) // 2 if n > 3 else n  # landscape: two rows once there are four
        return [[every[:top], every[top:]]] if top < n else [[every]]

    def cards(self, snap):
        """What Settings shows: [(title, lines, how wide)]. A line is ("big",
        text[, colour]), ("text", text), ("dim", text), ("code", command),
        ("item", label, value, as code?) or ("dot", text, colour)."""
        m, cards = self.m, []
        rotate = m.cfg.rotate if m.cfg.rotate in (90, 180, 270) else 0
        market = ("off ([market] url)" if not m.link else "not linked yet" if snap.link_code
                  else snap.market)
        display = [("big", f"{snap.host}.local"),
                   ("item", "IP", snap.ip or "no network yet", False),
                   ("item", "Port", str(snap.port), False),
                   ("item", "Market", market, False),
                   ("item", "Screen", f"{self.W}×{self.H} {self.r.layout}" + (
                       f", turned {rotate}°" if rotate else ""), False),
                   ("item", "Version", m.version.replace(" ", ", ") or "not from git", False)]
        health = self.health_line()
        if health:
            display.append(("dot",) + health)
        cards.append(("This display", display, 0.85))

        if snap.link_code:  # (what the welcome screen says, for when there are screens)
            cards.append(("Screen Market", [
                ("dot", "Not linked to this display yet", COL_YELLOW),
                ("text", f"Sign in at {snap.market}, open My display and type in:"),
                ("big", snap.link_code, COL_ORANGE)], 1))

        cards.append(("Claude Code", [
            ("text", "On each computer with Claude Code, in code-usage:"),
            ("code", f"python3 server/display_hook.py --install --host {snap.ip or snap.host + '.local'}"),
            ("dim", "so the display knows when Claude's working. Can't find it? "
                    "python3 server/find_display.py")], 1))

        screens, ready = [], 0
        for screen in self.installed():
            native = screen if isinstance(screen, NativeScreen) else None
            command = native and (native.manifest.get("setup") or {}).get("command")
            if command:  # a login made on the Pi: the thing you'll need again
                fine = m.is_ready(screen.id) and (m.demo or not native.hook("needs_setup", m.cfg))
                screens += [("dot", screen.name, COL_GREEN if fine else COL_YELLOW), ("code", command)]
            elif not m.is_ready(screen.id):
                screens += [("dot", f"{screen.name}: not set up yet", COL_YELLOW),
                            ("dim", getattr(native and native.mod, "NOT_READY", "")
                             or "Set it up on its page in the Screen Market.")]
            else:
                ready += 1
        if not screens:
            screens = [("dot", f"All {ready} set up" if ready else "None yet", COL_GREEN if ready else COL_DIM),
                       ("dim", "Screens and their settings come from the Screen Market.")]
        cards.append(("Screens", screens, 1.1))

        service = os.path.exists("/etc/systemd/system/claude-display.service")
        pi = [("item", "SSH", f"ssh {getpass.getuser()}@{snap.host}.local", True),
              ("item", "Folder", tilde(REPO_DIR), True),
              ("item", "Config", tilde(m.cfg.path), True),
              ("item", "Update", "git pull", True),
              ("item", "Restart", "sudo systemctl restart claude-display" if service else "sudo reboot", True)]
        if service:
            pi.append(("item", "Logs", "journalctl -u claude-display -f", True))
        cards.append(("On the Pi", pi, 1.15))
        return cards

    def health_line(self):
        """How the Pi is doing - temperature, time up, power - as (text,
        colour), or None where it can't tell. Looked at every 30 s at most."""
        at, line = self.health
        if time.monotonic() - at < 30:
            return line
        bits, color = [], COL_GREEN
        try:
            with open("/sys/class/thermal/thermal_zone0/temp") as f:
                bits.append(f"{int(f.read()) / 1000:.0f}°C")
        except (OSError, ValueError):
            pass
        try:
            with open("/proc/uptime") as f:
                mins = int(float(f.read().split()[0]) // 60)
            bits.append("up " + (f"{mins // 1440}d {mins % 1440 // 60}h" if mins >= 1440 else fmt_minutes(mins)))
        except (OSError, ValueError, IndexError):
            pass
        try:  # the Pi's own word on its power supply
            out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=2)
            flags = int(out.stdout.strip().split("=")[1], 16)
            if flags & 0x1:
                bits.append("low voltage now")
                color = COL_RED
            elif flags & 0x10000:
                bits.append("low voltage since boot")
                color = COL_YELLOW
            else:
                bits.append("power ok")
        except (OSError, ValueError, IndexError, subprocess.SubprocessError):
            pass
        line = ("  ·  ".join(bits), color) if bits else None
        self.health = (time.monotonic(), line)
        return line

    def card_height(self, ui, title, lines, w):
        """How tall a card w wide would like to be: its text all at full size."""
        pad = self.TEXT[self.r.layout] * 1.1
        return self.lay_out(ui, title, lines, w - 2 * pad, self.TEXT[self.r.layout])[1] + 1.6 * pad

    def card(self, ui, box, title, lines):
        """One card of Settings, its text as big as fits."""
        x, y, w, h = box
        ui.rect(x, y, w, h, COL_CARD, radius=min(16, w * 0.06, h * 0.08))
        pad = self.TEXT[self.r.layout] * 1.1
        x, y, w, h = x + pad, y + pad * 0.8, w - 2 * pad, h - 1.6 * pad
        most = self.TEXT[self.r.layout]
        size = most
        while True:
            ops, used = self.lay_out(ui, title, lines, w, size)
            if used <= h or size <= most * 0.6:
                break
            size -= 0.5
        clip = ui.surface.get_clip()
        ui.surface.set_clip(self.r.rect(x, y, w, h + pad * 0.6))  # (if even the smallest text won't fit)
        for kind, *op in ops:
            if kind == "chip":
                ui.rect(x + op[0], y + op[1], op[2], op[3], COL_BG, radius=min(8, op[3] / 2))
            elif kind == "text":
                ui.text(op[0], x + op[2], y + op[1], op[3], op[4], bold=op[5])
            elif kind == "mono":
                self.mono_text(ui.surface, op[0], x + op[2], y + op[1], op[3], op[4])
            elif kind == "dot":
                ui.circle(x + op[2], y + op[1], op[3], op[4])
        ui.surface.set_clip(clip)

    def lay_out(self, ui, title, lines, w, s):
        """Where a card's lines go at text size s in w: ([drawing op], height).
        Ops are ("text" / "mono", text, baseline, x, size, colour[, bold]),
        ("dot", "", centre y, centre x, radius, colour) and the dark backing
        of a command, ("chip", x, top, w, h)."""
        ops, y = [], 0.0

        def row(size):  # a line of text at size: its baseline
            nonlocal y
            y += size * 1.4
            return y - size * 0.35

        ts = self.TEXT[self.r.layout] * 1.12  # (the same in every card, however small its text)
        ops.append(("text", ui.fit(title, w, ts, True), row(ts), 0, ts, COL_ORANGE, True))
        y += s * 0.3
        label_w = max((ui.width(line[1], s * 0.9) for line in lines if line[0] == "item"), default=0) + s
        for line in lines:
            kind = line[0]
            if kind == "big":
                bs = s * 1.3
                size = next((bs * k for k in (1, 0.9, 0.8, 0.7) if ui.width(line[1], bs * k, True) <= w), bs * 0.7)
                ops.append(("text", ui.fit(line[1], w, size, True), row(bs), 0, size,
                            line[2] if len(line) > 2 else COL_TEXT, True))
            elif kind in ("text", "dim"):
                size, color = (s, COL_SUB) if kind == "text" else (s * 0.9, COL_DIM)
                for part in ui.wrap(line[1], w, size, lines=4):
                    ops.append(("text", part, row(size), 0, size, color, False))
            elif kind == "dot":
                d = s * 0.27
                for i, part in enumerate(ui.wrap(line[1], w - s, s, lines=3)):
                    base = row(s)
                    if not i:
                        ops.append(("dot", "", base - s * 0.36, d, d, line[2]))
                    ops.append(("text", part, base, s, s, COL_TEXT, False))
            elif kind == "code":
                cs = s * 0.88
                top = y + s * 0.15
                y = top + cs * 0.15
                parts = [("mono", part, row(cs), cs * 0.55, cs, COL_TEXT)
                         for part in self.wrap_code(line[1], w - cs * 1.1, cs)]
                y += cs * 0.3
                ops += [("chip", 0, top, w, y - top)] + parts
                y += s * 0.2
            elif kind == "item":
                vs = s * 0.88 if line[3] else s
                parts = (self.wrap_code(line[2], w - label_w, vs) if line[3]
                         else ui.wrap(line[2], w - label_w, vs, lines=3))
                for i, part in enumerate(parts):
                    base = row(s)
                    if not i:
                        ops.append(("text", line[1], base, 0, s * 0.9, COL_DIM, False))
                    ops.append(("mono", part, base, label_w, vs, COL_TEXT) if line[3]
                               else ("text", part, base, label_w, vs, COL_TEXT, False))
        return ops, y

    def mono_font(self, size):
        px = self.r.n(size)
        f = self.mono_fonts.get(px)
        if f is None:
            f = self.mono_fonts[px] = pygame.font.Font(self.mono_path or self.r.font_paths[0], px)
        return f

    def mono_text(self, surf, s, x, baseline, size, color):
        f = self.mono_font(size)
        key = ("mono", s, id(f), color)
        img = self.r.text_cache.get(key)
        if img is None:
            img = self.r.text_cache[key] = f.render(s, True, color)
        surf.blit(img, (self.r.x(x), self.r.y(baseline) - f.get_ascent()))

    def wrap_code(self, text, max_w, size):
        """A command broken into lines that fit max_w: at its spaces, and
        inside anything too long after a slash or an @."""
        f = self.mono_font(size)
        width = lambda s: f.size(s)[0] / self.r.s
        pieces = []  # (text, glued to the one before it - no space between)
        for word in text.split():
            glued = False
            while width(word) > max_w and len(word) > 1:
                cut = max(1, int(len(word) * max_w / width(word)))
                slash = max(word.rfind("/", 0, cut), word.rfind("@", 0, cut))
                cut = slash + 1 if slash > 0 else cut
                pieces.append((word[:cut], glued))
                word, glued = word[cut:], True
            pieces.append((word, glued))
        out, line = [], ""
        for piece, glued in pieces:
            trial = line + ("" if glued or not line else " ") + piece
            if line and width(trial) > max_w:
                out.append(line)
                line = piece
            else:
                line = trial
        return out + [line] if line else out


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
    menu = HomeMenu(model, renderer)
    menu.render(model.snapshot(), datetime.datetime.now().astimezone())  # and the menu's icons
    market = cfg.get("market", "url", MARKET_URL)  # once the layout's known: the market shows it
    if market and not args.demo:
        model.link = MarketLink(model, market)
        forever(model.link.run)
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

    def on_canvas(pos):
        return unrotate_point(pos, rotate, *canvas.get_size())

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
                    if menu.out:
                        if ev.key in (pygame.K_ESCAPE, pygame.K_UP, pygame.K_m, pygame.K_SPACE,
                                      pygame.K_TAB, pygame.K_RETURN):
                            menu.close()
                    elif ev.key == pygame.K_ESCAPE and args.windowed:
                        return
                    elif ev.key in (pygame.K_DOWN, pygame.K_m):
                        menu.open()
                    elif ev.key in (pygame.K_SPACE, pygame.K_TAB, pygame.K_RETURN):
                        model.toggle_mode()
                elif ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
                    menu.press(on_canvas(ev.pos))
                elif ev.type == pygame.MOUSEMOTION:
                    menu.drag(on_canvas(ev.pos))
                elif ev.type == pygame.MOUSEBUTTONUP and ev.button == 1:
                    if menu.release(on_canvas(ev.pos)):
                        continue  # a pull on the home menu, or a tap on it
                    # Touchscreens send taps as clicks. A tap on a button
                    # presses it; anywhere else switches screens.
                    button = renderer.button_at(on_canvas(ev.pos), model.mode)
                    native = model.native()
                    if button and native:
                        renderer.press(button, time.monotonic())
                        native.hook("press", model, button)
                    else:
                        model.toggle_mode()

            if model.restart and not model.reporting:  # screens or settings changed: start again with them
                log("restarting with the new screens / settings")
                time.sleep(0.5)  # let the HTTP reply go out
                pygame.quit()
                os.execv(sys.executable, [sys.executable] + sys.argv)

            mono = time.monotonic()
            if mono - last_ip_check > 10:  # DHCP may hand us a new address
                last_ip_check, model.ip = mono, local_ip()

            snap = model.snapshot()
            now = datetime.datetime.now().astimezone()
            if menu.out:  # the home menu draws; the screen waits under it
                if menu.frame(canvas, snap, now):
                    present()
                last_key = None  # once it's gone back up, the screen's drawn afresh
                clock.tick(60 if menu.moving else 15)
                continue
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
