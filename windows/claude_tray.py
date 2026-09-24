#!/usr/bin/env python3
"""Windows tray helper for the Claude Code usage display.

A notification-area (system tray) icon - the Windows version of a menu-bar
helper - that sits next to the clock and:

  - shows your 5-hour / weekly usage at a glance: a meter under Clawd in the
    icon (or the 5-hour % itself), the numbers in the tooltip and menu
  - walks Clawd while Claude is working, on any of your machines
  - switches the display between its usage, Spotify, 3D printer, planes and F1 screens
    (left-click the icon to cycle, or pick one from the menu)
  - tells the display exactly when Claude Code is working on this PC: one
    click installs the Claude Code hooks (server/display_hook.py), and the
    tray adds what hooks can't see - Esc interrupts and very long tool runs.
    Without hooks it falls back to guessing from transcript writes, like
    server/beacon.py
  - can start itself with Windows

Everything comes from the display itself (GET /usage), so there's no Anthropic
login on this PC and no extra load on the rate-limited usage API. Works with
the ESP32 display (re-flash it for the usage readout) and the Raspberry Pi app.

    py -3 -m pip install -r windows\\requirements.txt
    pyw -3 windows\\claude_tray.py                        # no console window
    pyw -3 windows\\claude_tray.py --host 192.168.1.42    # first run: where the display is

Settings live in %APPDATA%\\claude-display\\tray.json (errors go to tray.log
next to it). Right-click the icon for everything else.
"""

import argparse
import ctypes
import datetime
import json
import logging
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import winreg

try:
    import pystray
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pythonw has no console, so say it in a dialog
    ctypes.windll.user32.MessageBoxW(
        None, f"The tray helper needs two packages in this Python "
              f"({sys.version.split()[0]}). Install them with:\n\n"
              f"py -{sys.version_info.major}.{sys.version_info.minor} -m pip install pystray pillow",
        "Claude display", 0x10)
    sys.exit(1)

# The Claude Code hooks' logic lives next to the other helpers in server/.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "server"))
try:
    import display_hook
except ImportError:
    display_hook = None

APP_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "claude-display")
SETTINGS_PATH = os.path.join(APP_DIR, "tray.json")
LOG_PATH = os.path.join(APP_DIR, "tray.log")
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "ClaudeDisplayTray"
DEFAULTS = {
    "host": "claude-display.local",
    "port": 8080,
    "beacon": True,     # without hooks: guess activity from transcript writes
    "icon": "mascot",   # or "percent"
}

POLL_SECS = 5           # re-read /usage from the display this often (it's on the LAN)
TICK_SECS = 0.5         # worker loop; also the walking-Clawd frame rate
OFFLINE_AFTER = 2       # failed polls in a row before we call the display offline

# Activity detection - the same rule as server/beacon.py.
CLAUDE_DIR = os.path.join(os.path.expanduser("~"), ".claude")
CLAUDE_PROJECTS = os.path.join(CLAUDE_DIR, "projects")
ACTIVE_WINDOW_SECS = 8  # a transcript written this recently => Claude is working
PING_EVERY_SECS = 4     # cadence while active; well under the display's BEACON_TTL
IDLE_CHECK_SECS = 2     # how often to re-check while idle

ORANGE = (217, 119, 87, 255)
GREEN = (57, 186, 82, 255)
YELLOW = (222, 162, 66, 255)
RED = (230, 81, 74, 255)
GRAY = (140, 140, 140, 255)
TRACK = (80, 80, 80, 255)
DARK = (18, 18, 18, 255)

# Clawd on his native 12x8 grid, same as firmware/src/mascot.h (1 = body,
# 2 = eye). While Claude works he walks: alternate legs lift on each frame.
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
LIFTED_LEGS = {0: (), 1: (2, 7), 2: (4, 9)}  # walk frame -> leg columns off the ground

log = logging.getLogger("claude-tray")


# ---------------------------------------------------------------- helpers

def load_settings():
    settings = dict(DEFAULTS)
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            settings.update(json.load(f))
    except (OSError, ValueError):
        pass
    return settings


def save_settings(settings):
    os.makedirs(APP_DIR, exist_ok=True)
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)


def parse_address(text):
    """'192.168.1.42', 'claude-display.local:8080' or a pasted URL -> (host, port)."""
    text = re.sub(r"^\w+://", "", text.strip()).split("/")[0]
    host, _, port = text.partition(":")
    return host.strip(), int(port) if port.strip().isdigit() else DEFAULTS["port"]


def recently_active():
    """True if any Claude Code transcript was written within ACTIVE_WINDOW_SECS."""
    cutoff = time.time() - ACTIVE_WINDOW_SECS
    for root, _dirs, files in os.walk(CLAUDE_PROJECTS):
        for name in files:
            if name.endswith(".jsonl"):
                try:
                    if os.path.getmtime(os.path.join(root, name)) > cutoff:
                        return True
                except OSError:
                    pass
    return False


_hooks = {"checked": 0.0, "ours": None, "curl": False}


def hooks_status(refresh=False):
    """What ~/.claude/settings.json does for the display: "ours" is the
    host:port our hooks (server/display_hook.py) report to, or None; "curl"
    means the older curl hooks from claude-hooks.example.json."""
    if refresh or time.monotonic() - _hooks["checked"] > 5:
        try:
            with open(os.path.join(CLAUDE_DIR, "settings.json"), encoding="utf-8") as f:
                text = f.read()
        except OSError:
            text = ""
        ours = None
        if display_hook and display_hook.MARKER in text:
            ours = display_hook.installed()
        _hooks.update(checked=time.monotonic(), ours=ours, curl="/thinking/on" in text)
    return _hooks


def startup_command():
    if getattr(sys, "frozen", False):  # a PyInstaller build
        return f'"{sys.executable}"'
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    exe = pythonw if os.path.exists(pythonw) else sys.executable
    return f'"{exe}" "{os.path.abspath(__file__)}"'


def startup_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, RUN_VALUE)
            return True
    except OSError:
        return False


def set_startup(enabled):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, startup_command())
        else:
            try:
                winreg.DeleteValue(key, RUN_VALUE)
            except FileNotFoundError:
                pass


# ---- time formatting (same rules as the display)

_ISO = re.compile(r"(\d{4})-(\d\d)-(\d\d)[T ](\d\d):(\d\d)(?::(\d\d)(?:\.\d+)?)?"
                  r"\s*(Z|[+-]\d\d:?\d\d)?$")


def parse_iso(text):
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
    return f"{dt.hour % 12 or 12}:{dt.minute:02d} {'AM' if dt.hour < 12 else 'PM'}"


def describe_reset(window):
    """'resets 4:19 AM (in 2h 13m)' from a /usage window, in this PC's timezone."""
    when = parse_iso(window.get("resets_at"))
    if when is None:
        return f"resets {window['resets']}" if window.get("resets") else ""
    now = datetime.datetime.now().astimezone()
    local = when.astimezone()
    day = "" if local.date() == now.date() else local.strftime("%a ")
    mins = int((when - now).total_seconds() // 60)
    days, rest = divmod(max(mins, 0), 1440)
    hours, mins = divmod(rest, 60)
    until = f"{days}d {hours}h" if days else f"{hours}h {mins}m" if hours else f"{mins}m"
    return f"resets {day}{clock_str(local)} (in {until})"


def bar_color(pct):
    if pct < 50:
        return GREEN
    return YELLOW if pct < 80 else RED


# ---------------------------------------------------------------- icon

_fonts = {}


def icon_font(size):
    if size not in _fonts:
        for name in ("segoeuib.ttf", "arialbd.ttf"):
            try:
                _fonts[size] = ImageFont.truetype(name, size)
                break
            except OSError:
                continue
        else:
            _fonts[size] = ImageFont.load_default()
    return _fonts[size]


def draw_icon(style, pct, online, frame):
    """64x64 tray icon. The mascot is drawn on a 4px grid so Windows' 32 and
    16 px versions stay pixel-sharp (2px and 1px per cell)."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if style == "percent":
        d.rounded_rectangle([0, 0, 63, 63], radius=12, fill=(32, 32, 32, 255))
        if online and pct is not None:
            text, color = str(min(round(pct), 100)), bar_color(pct)
        else:
            text, color = "--", GRAY
        d.text((32, 29), text, font=icon_font(46 if len(text) < 3 else 32), fill=color, anchor="mm")
        if frame == 1:  # blinking underline while Claude works
            d.rectangle([12, 54, 51, 59], fill=ORANGE)
        return img

    body = ORANGE if online else GRAY
    lifted = LIFTED_LEGS[frame]
    for r, row in enumerate(MASCOT):
        for c, v in enumerate(row):
            if not v or (r == 7 and c in lifted):
                continue
            x, y = 8 + c * 4, 8 + r * 4  # 48x32, centred above the meter
            d.rectangle([x, y, x + 3, y + 3], fill=body if v == 1 else DARK)
    if online and pct is not None:  # 5-hour meter under Clawd
        d.rounded_rectangle([0, 48, 63, 59], radius=4, fill=TRACK)
        width = round(64 * min(pct, 100) / 100 / 4) * 4
        if width:
            d.rounded_rectangle([0, 48, width - 1, 59], radius=4, fill=bar_color(pct))
    return img


# ---------------------------------------------------------------- the app

class TrayApp:
    def __init__(self, settings):
        self.settings = settings
        self.lock = threading.Lock()
        self.data = None        # the display's last /usage answer
        self.mode = None        # "usage" / "spotify", None until known
        self.online = False
        self.legacy = False     # it answers, but its firmware predates GET /usage
        self.failures = OFFLINE_AFTER
        self.ip = None          # settings["host"] resolved once (mDNS is slow)
        self.beaconing = False  # transcript beacon: we've told the display this PC is busy
        self.local_working = False  # hooks say Claude is working on this PC
        self.next_beacon = 0.0
        self.tick = 0
        self.icon_key = self.menu_key = None
        self.dialog_open = False
        self.stopping = threading.Event()
        self.poll_soon = threading.Event()
        # LAN requests only: skip any system proxy.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.icon = pystray.Icon("claude-display", draw_icon(settings["icon"], None, False, 0),
                                 "Claude display", menu=self.build_menu())

    # -- talking to the display
    def request(self, path, method="GET", timeout=2.0):
        host, port = self.settings["host"], self.settings["port"]
        if self.ip is None:
            try:
                self.ip = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]
            except OSError:
                raise OSError(f"can't resolve {host}") from None
        req = urllib.request.Request(f"http://{self.ip}:{port}{path}", method=method,
                                     data=b"" if method == "POST" else None)
        try:
            with self.opener.open(req, timeout=timeout) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except OSError:
            self.ip = None  # re-resolve next time - DHCP may have moved it
            raise

    def poll(self):
        try:
            code, body = self.request("/usage")
            if code == 200:
                data, legacy = json.loads(body), False
                mode = data.get("mode")
            elif code == 404:  # older firmware: no /usage, but /mode still works
                data, legacy = None, True
                code, body = self.request("/mode")
                mode = body.strip() if code == 200 else None
            else:
                raise OSError(f"HTTP {code}")
        except (OSError, ValueError) as e:
            with self.lock:
                self.failures += 1
                # The ESP32 can't answer while it's mid-fetch, so one miss isn't "offline".
                if self.failures >= OFFLINE_AFTER:
                    self.online = False
            log.debug("poll failed: %s", e)
            return
        with self.lock:
            self.data, self.mode, self.legacy = data, mode, legacy
            self.online, self.failures = True, 0

    def post_async(self, path, on_status=None):
        """POST from a worker thread - menu callbacks run on the UI thread."""
        def go():
            try:
                code, _ = self.request(path, "POST")
                if on_status:
                    on_status(code)
            except OSError:
                self.notify(f"Couldn't reach the display at {self.settings['host']}.")
            self.poll_soon.set()
        threading.Thread(target=go, daemon=True).start()

    def notify(self, message):
        try:
            self.icon.notify(message, "Claude display")
        except Exception:
            log.exception("notify failed")

    # -- this PC's activity -> beacons
    def beacon_enabled(self):
        return self.settings.get("beacon") is not False

    def send_beacon(self, on):
        try:
            self.request("/thinking/on" if on else "/thinking/off", "POST", timeout=1.5)
        except OSError:
            pass
        if on != self.beaconing:
            self.beaconing = on
            self.poll_soon.set()  # show the change in the icon right away

    def beacon_tick(self, now):
        if now < self.next_beacon:
            return
        hooks = hooks_status()
        if hooks["ours"] is not None:
            # The hooks report every event themselves; the watcher adds Esc
            # interrupts and keep-alives for long tool runs.
            host, port = display_hook.split_host(hooks["ours"] or self.settings["host"],
                                                 None if hooks["ours"] else self.settings["port"])
            working = display_hook.watch_once(host, port)
            if working != self.local_working:
                self.local_working = working
                self.poll_soon.set()
            self.next_beacon = now + IDLE_CHECK_SECS
            return
        self.local_working = False
        if not hooks["curl"] and self.beacon_enabled() and recently_active():
            self.send_beacon(True)
            self.next_beacon = now + PING_EVERY_SECS
        else:
            if self.beaconing:  # just went idle (or beacons were switched off)
                self.send_beacon(False)
            self.next_beacon = now + IDLE_CHECK_SECS

    # -- what we show
    def thinking(self):
        return self.local_working or (self.online and bool(self.data and self.data.get("thinking")))

    def five_pct(self):
        if self.online and self.data and self.data.get("valid"):
            return (self.data.get("five_hour") or {}).get("pct")
        return None

    def usage_line(self, key):
        label = "5-hour" if key == "five_hour" else "Weekly"
        window = (self.data or {}).get(key) or {}
        if window.get("pct") is None:
            return f"{label}: --"
        reset = describe_reset(window)
        return f"{label}: {round(window['pct'])}%" + (f"   {reset}" if reset else "")

    def status_line(self):
        if not self.online:
            return f"Display not reachable at {self.settings['host']}"
        if self.legacy:
            return "Re-flash the display firmware to see usage here"
        if not (self.data and self.data.get("valid")):
            return "Waiting for the display's first usage fetch..."
        return self.usage_line("five_hour")

    def tooltip(self):
        if not self.online:
            text = "Claude display - offline"
        elif self.data and self.data.get("valid"):
            parts = []
            for key, label in (("five_hour", "5h"), ("seven_day", "week")):
                pct = (self.data.get(key) or {}).get("pct")
                parts.append(f"{label} {'--' if pct is None else round(pct)}%")
            text = "Claude usage: " + " · ".join(parts)
        else:
            text = "Claude display"
        if self.thinking():
            text += "\nClaude is working..."
        return text[:127]  # the tray's hard limit

    def refresh_ui(self):
        with self.lock:
            walking = self.thinking()
            frame = 1 + self.tick % 2 if walking else 0
            pct = self.five_pct()
            icon_key = (self.settings["icon"], None if pct is None else round(pct), self.online, frame)
            tooltip = self.tooltip()
            menu_key = (self.status_line(), self.usage_line("seven_day"), walking, self.online,
                        self.mode, self.beacon_enabled(), self.settings["icon"],
                        hooks_status()["ours"], hooks_status()["curl"],
                        self.settings["host"], startup_enabled())
        self.tick += 1
        if icon_key != self.icon_key:
            self.icon_key = icon_key
            self.icon.icon = draw_icon(self.settings["icon"], pct, self.online, frame)
        if tooltip != self.icon.title:
            self.icon.title = tooltip
        if menu_key != self.menu_key:
            self.menu_key = menu_key
            self.icon.update_menu()

    def worker(self):
        next_poll = 0.0
        while not self.stopping.is_set():
            now = time.monotonic()
            try:
                if now >= next_poll or self.poll_soon.is_set():
                    self.poll_soon.clear()
                    self.poll()
                    next_poll = now + POLL_SECS
                self.beacon_tick(now)
                self.refresh_ui()
            except Exception:
                log.exception("worker")
            self.stopping.wait(TICK_SECS)

    # -- menu
    def build_menu(self):
        Item, Menu = pystray.MenuItem, pystray.Menu
        usage_known = lambda _: self.online and bool(self.data and self.data.get("valid"))
        return Menu(
            Item(lambda _: self.status_line(), None, enabled=False),
            Item(lambda _: self.usage_line("seven_day"), None, enabled=False, visible=usage_known),
            Item(lambda _: "Claude is working..." if self.thinking() else "Claude is idle",
                 None, enabled=False, visible=lambda _: self.online),
            Menu.SEPARATOR,
            Item("Switch display screen", self.on_mode("toggle"), default=True,
                 enabled=lambda _: self.online),
            Item("Usage screen", self.on_mode("usage"), radio=True,
                 checked=lambda _: self.mode == "usage", enabled=lambda _: self.online),
            Item("Spotify screen", self.on_mode("spotify"), radio=True,
                 checked=lambda _: self.mode == "spotify", enabled=lambda _: self.online),
            Item("3D printer screen", self.on_mode("bambu"), radio=True,
                 checked=lambda _: self.mode == "bambu", enabled=lambda _: self.online),
            Item("Planes overhead screen", self.on_mode("planes"), radio=True,
                 checked=lambda _: self.mode == "planes", enabled=lambda _: self.online),
            Item("Formula 1 screen", self.on_mode("f1"), radio=True,
                 checked=lambda _: self.mode == "f1", enabled=lambda _: self.online),
            Menu.SEPARATOR,
            Item("Track Claude with hooks (exact)", self.on_hooks,
                 checked=lambda _: hooks_status()["ours"] is not None,
                 enabled=lambda _: display_hook is not None),
            Item("Guess activity from transcripts", self.on_beacon,
                 checked=lambda _: self.beacon_enabled(),
                 visible=lambda _: hooks_status()["ours"] is None and not hooks_status()["curl"]),
            Item("Show 5-hour % in icon", self.on_icon_style,
                 checked=lambda _: self.settings["icon"] == "percent"),
            Item("Start with Windows", self.on_startup, checked=lambda _: startup_enabled()),
            Item(lambda _: f"Display address: {self.settings['host']}...", self.on_address),
            Menu.SEPARATOR,
            Item("Refresh", lambda: self.poll_soon.set()),
            Item("Quit", self.on_quit),
        )

    def on_mode(self, what):
        def switch():
            def result(code):
                if code == 409 and what == "bambu":
                    self.notify("The printer isn't set up on the display yet - run "
                                "claude_display.py --setup-bambu on the Pi.")
                elif code == 409 and what == "f1":
                    self.notify("The F1 screen is turned off in the display's config.ini.")
                elif code == 409 and what == "planes":
                    self.notify("The planes screen needs your location first - run "
                                "claude_display.py --setup-planes on the Pi.")
                elif code == 409:
                    self.notify("Spotify isn't set up on the display yet - see the README.")
                elif code == 404:
                    self.notify("This display doesn't have that screen (the 3D printer, "
                                "planes and F1 screens are in the Raspberry Pi app).")
            self.post_async(f"/mode/{what}", result)
        return switch

    def on_beacon(self):
        self.settings["beacon"] = not self.beacon_enabled()
        save_settings(self.settings)
        self.next_beacon = 0.0  # apply now (sends "off" if we were beaconing)

    def on_hooks(self):
        def go():
            try:
                if hooks_status(refresh=True)["ours"] is not None:
                    display_hook.uninstall()
                    self.notify("Removed the Claude Code hooks.")
                else:
                    display_hook.install(self.settings["host"], self.settings["port"])
                    self.notify("Claude Code hooks installed - the display now follows "
                                "Claude exactly.")
            except (OSError, ValueError) as e:
                self.notify(f"Couldn't update ~/.claude/settings.json: {e}")
            hooks_status(refresh=True)
        threading.Thread(target=go, daemon=True).start()

    def on_icon_style(self):
        self.settings["icon"] = "mascot" if self.settings["icon"] == "percent" else "percent"
        save_settings(self.settings)

    def on_startup(self):
        try:
            set_startup(not startup_enabled())
        except OSError as e:
            self.notify(f"Couldn't change the startup setting: {e}")

    def on_address(self):
        if self.dialog_open:
            return
        self.dialog_open = True
        threading.Thread(target=self.ask_address, daemon=True).start()

    def ask_address(self):
        try:
            import tkinter as tk
            from tkinter import simpledialog
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            current = self.settings["host"]
            if self.settings["port"] != DEFAULTS["port"]:
                current += f":{self.settings['port']}"
            answer = simpledialog.askstring(
                "Claude display",
                "Display address - its IP or name, plus :port if not 8080.\n"
                "The display shows it on its status line.",
                initialvalue=current, parent=root)
            root.destroy()
        finally:
            self.dialog_open = False
        if answer and answer.strip():
            host, port = parse_address(answer)
            if host:
                self.settings.update(host=host, port=port)
                save_settings(self.settings)
                if hooks_status(refresh=True)["ours"] is not None:
                    display_hook.install(host, port)  # point the hooks at it too
                self.ip = None
                self.failures = OFFLINE_AFTER - 1  # one miss now means offline
                self.poll_soon.set()

    def on_quit(self):
        self.stopping.set()
        if self.beaconing:
            self.send_beacon(False)  # don't leave the display stuck on "working"
        self.icon.stop()

    def setup(self, icon):
        icon.visible = True
        threading.Thread(target=self.worker, daemon=True, name="worker").start()


def single_instance():
    """Hold a named mutex; False if another copy already has it."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    single_instance.handle = kernel32.CreateMutexW(None, False, "Local\\ClaudeDisplayTray")
    return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS


def main():
    ap = argparse.ArgumentParser(description="Tray helper for the Claude Code usage display.")
    ap.add_argument("--host", help="display address (IP or name, optionally :port) - saved")
    args = ap.parse_args()

    os.makedirs(APP_DIR, exist_ok=True)
    logging.basicConfig(filename=LOG_PATH, level=logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s")
    if not single_instance():
        return  # already running - its icon is in the tray
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # crisp menus and dialog
    except (AttributeError, OSError):
        pass

    settings = load_settings()
    if args.host:
        settings["host"], settings["port"] = parse_address(args.host)
        save_settings(settings)
    app = TrayApp(settings)
    app.icon.run(setup=app.setup)


if __name__ == "__main__":
    main()
