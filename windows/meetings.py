#!/usr/bin/env python3
"""Meeting controls for the display's Meet, Zoom and Teams screens.

The screens on the display are buttons; this presses them, on the PC the
call is on. For each meeting screen installed on the display it waits on
GET /meeting/<app>/commands (the display answers the moment a button's
tapped), carries the tap out, and posts what the call's doing - muted? on
camera? - to POST /meeting/<app>/state, when it changes and every 10 s.

  Zoom   the meeting window's own buttons, through Windows UI Automation: no
         focus change, and their names say whether you're muted. Failing
         that, Zoom's shortcuts (Alt+A and friends), sent to its window.
  Teams  Teams' third-party app API (Settings > Privacy > Third-party app
         API) when it's on: real state, reactions, blur. Otherwise the
         meeting window's buttons through UI Automation, then its shortcuts.
  Meet   the browser extension in ../extension/meet, which this serves on
         http://127.0.0.1:47613: it presses Meet's buttons in its tab.

Push to talk: the display says "on" when the button goes down, again every
second while it's held, and "off" when it's let go. The mic opens on the
first, and shuts on the last - or 2.5 s after the display last said it's
held, so a lost "off" can't leave you talking.

The tray app runs this. On its own, for trying it out:

    py -3 windows\\meetings.py --host 192.168.1.42      # the display's address
    py -3 windows\\meetings.py --dump zoom              # the buttons UI Automation sees
"""

import argparse
import base64
import ctypes
import ctypes.wintypes as wt
import json
import logging
import os
import queue
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("meetings")

APPS = ("meet", "zoom", "teams")
APP_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "claude-display")
EXTENSION_PORT = 47613     # the Meet extension's localhost line to this
EXTENSION_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                              os.pardir, "extension", "meet"))
POLL_WAIT = 20             # s: how long the display may hold a request for taps
HEARTBEAT = 10             # s: the state goes to the display at least this often
LOOK_SHOWING = 1.0         # s: how often to look at the call while its screen is showing
LOOK_IDLE = 4.0            # s: ...and otherwise (a meeting starting should still show up)
PTT_GRACE = 2.5            # s: push to talk shuts this long after the display last said "held"
EXPECT_SECS = 2.0          # s: what a tap just set outweighs the app's older word this long
SCREENS_EVERY = 30         # s: re-read which meeting screens the display has


# ---------------------------------------------------------------- Windows

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
user32.EnumWindows.argtypes = [WNDENUMPROC, wt.LPARAM]
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.IsIconic.argtypes = [wt.HWND]
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.GetForegroundWindow.restype = wt.HWND
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]


def windows():
    """Every visible top-level window: [(hwnd, class, title, exe name)]."""
    out, names = [], {}

    def each(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            cls, title = ctypes.create_unicode_buffer(256), ctypes.create_unicode_buffer(512)
            user32.GetClassNameW(hwnd, cls, 256)
            user32.GetWindowTextW(hwnd, title, 512)
            pid = wt.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value not in names:
                names[pid.value] = exe_name(pid.value)
            out.append((hwnd, cls.value, title.value, names[pid.value]))
        return True
    user32.EnumWindows(WNDENUMPROC(each), 0)
    return out


def exe_name(pid):
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        buf, size = ctypes.create_unicode_buffer(1024), wt.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value).lower()
        return ""
    finally:
        kernel32.CloseHandle(handle)


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _U)]


VK = {"ctrl": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B, "space": 0x20, "enter": 0x0D, "esc": 0x1B,
      "nothing": 0xE8}  # (an unassigned key: input that no app acts on)
EXTENDED = {0x5B}


def press_keys(combo):
    """Press keys together and let them go, e.g. "alt+a", "ctrl+shift+m"."""
    vks = [VK.get(k) or ord(k.upper()) for k in combo.lower().split("+")]
    events = [(vk, 0) for vk in vks] + [(vk, 2) for vk in reversed(vks)]  # 2 = KEYEVENTF_KEYUP
    inputs = (INPUT * len(events))()
    for i, (vk, flags) in enumerate(events):
        inputs[i].type = 1  # INPUT_KEYBOARD
        inputs[i].ki = KEYBDINPUT(vk, 0, flags | (1 if vk in EXTENDED else 0), 0, 0)
    user32.SendInput(len(events), inputs, ctypes.sizeof(INPUT))


def focus(hwnd):
    """Bring a window to the front. Windows only lets the app that had the
    last input do that, so send a key no app uses first (Alt, the usual
    trick, can leave the window you come back to with its menu selected)."""
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    if user32.GetForegroundWindow() != hwnd:
        press_keys("nothing")
        user32.SetForegroundWindow(hwnd)
        for _ in range(20):
            if user32.GetForegroundWindow() == hwnd:
                break
            time.sleep(0.01)
    return user32.GetForegroundWindow() == hwnd


def keys_to(hwnd, combo):
    """A shortcut to a window that only takes it while it's in front: in front
    just for that, then back to what you were in."""
    before = user32.GetForegroundWindow()
    if not focus(hwnd):
        log.info("couldn't bring the meeting window forward for %s", combo)
    time.sleep(0.05)
    press_keys(combo)
    time.sleep(0.05)
    if before and before != hwnd:
        press_keys("nothing")
        user32.SetForegroundWindow(before)


# ---------------------------------------------------------------- UI Automation

class UIA:
    """Windows UI Automation (through comtypes): a window's buttons, by name,
    and pressing them - the way a screen reader does, so nothing moves."""

    def __init__(self):
        import comtypes
        import comtypes.client
        try:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        except OSError:
            pass  # (importing comtypes already set COM up on this thread: that's fine too)
        comtypes.client.GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient as uia
        self.u = uia
        self.auto = comtypes.client.CreateObject(uia.CUIAutomation, interface=uia.IUIAutomation)
        self.buttons_cond = self.auto.CreateOrCondition(
            self.auto.CreatePropertyCondition(uia.UIA_ControlTypePropertyId, uia.UIA_ButtonControlTypeId),
            self.auto.CreatePropertyCondition(uia.UIA_ControlTypePropertyId, uia.UIA_CheckBoxControlTypeId))
        self.panes_cond = self.auto.CreatePropertyCondition(uia.UIA_ControlTypePropertyId,
                                                            uia.UIA_PaneControlTypeId)

    def buttons(self, hwnd):
        """[(name, element)] for the window's buttons (and checkboxes)."""
        return self.named(hwnd, self.buttons_cond)

    def panes(self, hwnd):
        """[(name, element)] for the window's panes (Zoom's video tiles are)."""
        return self.named(hwnd, self.panes_cond)

    def named(self, hwnd, cond):
        try:
            found = self.auto.ElementFromHandle(hwnd).FindAll(self.u.TreeScope_Descendants, cond)
        except Exception as e:  # (a window closing under us)
            log.debug("UIA FindAll: %s", e)
            return []
        out = []
        for i in range(found.Length):
            el = found.GetElement(i)
            try:
                name = el.CurrentName or ""
            except Exception:
                continue
            if name:
                out.append((name.strip(), el))
        return out

    def press(self, el):
        """Press a button the way it wants: invoke it, flip it, or open (or
        shut) the menu or panel it's for (Teams' React, Share, Chat and
        People are those); False if it does none of them."""
        u = self.u
        for pattern, iface in ((u.UIA_InvokePatternId, u.IUIAutomationInvokePattern),
                               (u.UIA_TogglePatternId, u.IUIAutomationTogglePattern),
                               (u.UIA_ExpandCollapsePatternId, u.IUIAutomationExpandCollapsePattern),
                               (u.UIA_LegacyIAccessiblePatternId, u.IUIAutomationLegacyIAccessiblePattern)):
            try:
                p = el.GetCurrentPattern(pattern)
                if not p:
                    continue
                p = p.QueryInterface(iface)
                if pattern == u.UIA_InvokePatternId:
                    p.Invoke()
                elif pattern == u.UIA_TogglePatternId:
                    p.Toggle()
                elif pattern == u.UIA_ExpandCollapsePatternId:
                    if p.CurrentExpandCollapseState == u.ExpandCollapseState_Collapsed:
                        p.Expand()
                    else:
                        p.Collapse()
                else:
                    p.DoDefaultAction()
                return True
            except Exception as e:
                log.debug("UIA press: %s", e)
        return False


# ---------------------------------------------------------------- the apps

class App:
    """One meeting app: what it's doing (look()) and doing what's tapped (act()).
    look() and act() run on its own worker thread, which owns any COM objects."""
    name = ""
    via = ""

    def setup(self):
        pass

    def look(self):
        """The call's state: {"available", "in_meeting", "muted", ...} (None: not known)."""
        return {"available": False, "in_meeting": False}

    def act(self, action, cmd, state):
        """Carry out a tap. Returns a note when it can't."""

    def set_muted(self, want, state):
        if state.get("muted") is None or state.get("muted") != want:
            return self.act("mute", {}, state)

    def close(self):
        pass


class ButtonApp(App):
    """An app whose meeting window has named buttons (Zoom, Teams without its
    API): UI Automation reads and presses them, its shortcuts are the fallback."""
    PATTERNS = {}     # action -> regex for the button that does it
    STATES = {}       # state key -> (regex meaning on, regex meaning off)
    SHORTCUTS = {}    # action -> keys, for when there's no button to press
    LEAVE_CONFIRM = None  # regex for the "Leave meeting" button a leave asks about

    def __init__(self):
        self.uia = None
        self.hwnd = None
        self.buttons = []

    def setup(self):
        try:
            self.uia = UIA()
        except Exception as e:
            log.warning("%s: no UI Automation (%s) - using shortcuts", self.name, e)

    def meeting_windows(self, wins):
        """The windows the call could be in, best first."""
        return []

    STICKY = 20  # s: a window just seen to be the call still is, while a menu hides its buttons

    def look(self):
        wins = windows()
        running = self.running(wins)
        self.hwnd, self.buttons = None, []
        for hwnd in self.meeting_windows(wins):
            buttons = self.uia.buttons(hwnd) if self.uia else []
            if self.is_meeting(hwnd, buttons):
                self.hwnd, self.buttons = hwnd, buttons
                break
        last_hwnd, last_st, last_at = getattr(self, "last", (None, {}, 0.0))
        if self.hwnd is None and last_hwnd in self.meeting_windows(wins) and time.monotonic() - last_at < self.STICKY:
            # (Teams hides the rest of its bar from UI Automation while a
            # menu of it is open: the call hasn't gone anywhere)
            self.hwnd, self.buttons = last_hwnd, self.uia.buttons(last_hwnd) if self.uia else []
            st = dict(last_st)
        elif self.hwnd is None:
            return {"available": running, "in_meeting": False}
        else:
            st = {"available": True, "in_meeting": True, "title": self.title(self.hwnd)}
            self.last = (self.hwnd, st, time.monotonic())
        seen = set()
        for key, (on_re, off_re) in self.STATES.items():
            if any(re.search(on_re, n, re.I) for n, _ in self.buttons):
                st[key] = True
            elif any(re.search(off_re, n, re.I) for n, _ in self.buttons):
                st[key] = False
            else:
                continue
            seen.add(key)
        for key in self.CARRY:  # (only showing now and then, in a menu: what it last said)
            if key not in seen and last_hwnd == self.hwnd:
                st[key] = last_st.get(key)
        self.more_state(st)
        self.via = "UI Automation" if self.uia and self.buttons else "keyboard"
        return st

    CARRY = ()  # state keys whose buttons are in a menu, so only seen while it's open

    def is_meeting(self, hwnd, buttons):
        """Is this window a call? By default: it has a mute button."""
        return self.uia is None or self.find("mute", buttons) is not None

    def more_state(self, st):
        """Fill in what the buttons didn't say (Zoom: from your video tile)."""

    def running(self, wins):
        return False

    def title(self, hwnd):
        return ""

    def find(self, action, buttons=None):
        pattern = self.PATTERNS.get(action)
        if not pattern:
            return None
        return next((el for n, el in (self.buttons if buttons is None else buttons) if re.search(pattern, n, re.I)),
                    None)

    def act(self, action, cmd, state):
        if self.hwnd is None:
            return f"not in a {self.name} meeting"
        el = self.find(action)
        if el is not None and self.uia.press(el):
            if action == "leave":
                self.confirm_leave()
            return None
        keys = self.SHORTCUTS.get(action)
        if keys:
            keys_to(self.hwnd, keys)
            if action == "leave":
                self.confirm_leave()
            return None
        return f"{self.name} has no {action} button showing"

    def confirm_leave(self):
        """Leaving asks "leave, or end it for everyone?": leave, never end."""
        if not (self.LEAVE_CONFIRM and self.uia):
            return
        for _ in range(15):
            time.sleep(0.1)
            for hwnd, _, _, exe in windows():
                if exe == self.EXE:
                    el = next((el for n, el in self.uia.buttons(hwnd) if re.search(self.LEAVE_CONFIRM, n, re.I)), None)
                    if el is not None:
                        self.uia.press(el)
                        return


class Zoom(ButtonApp):
    """Zoom (checked on Zoom Workplace 7.1). Its meeting toolbar hides when the
    mouse isn't over the call, and its buttons go with it, so: your own video
    tile says whether you're muted and on camera, the toolbar's buttons are
    pressed while it's showing, and Zoom's shortcuts do the rest. Its
    toolbar names them like "Mute, currently unmuted, Alt+A, ...", "Stop my
    video, Alt+V", "End, Alt+Q"; raise hand and record aren't on it."""
    name, EXE = "Zoom", "zoom.exe"
    MEETING = ("ConfMultiTabContentWndClass", "ZPContentViewWndClass")  # (7.x, then older)
    PATTERNS = {
        "mute": r"^(un)?mute\b", "video": r"^(start|stop) (my )?video\b", "share": r"^(share( screen)?|stop share)\b",
        "hand": r"^(raise|lower) hand", "chat": r"^chat\b",
        "people": r"^participants\b", "record": r"^(record|stop recording|pause recording)\b",
        "leave": r"^(leave|end)\b",
    }
    STATES = {"muted": (r"^unmute\b|currently muted", r"^mute\b|currently unmuted"),
              "video": (r"^stop (my )?video", r"^start (my )?video"),
              "sharing": (r"^stop shar", r"^share\b"), "hand": (r"^lower hand", r"^raise hand"),
              "recording": (r"stop recording|pause recording|^recording\b", r"^record\b")}
    SHORTCUTS = {"mute": "alt+a", "video": "alt+v", "share": "alt+s", "hand": "alt+y", "chat": "alt+h",
                 "people": "alt+u", "record": "alt+r", "leave": "alt+q"}
    LEAVE_CONFIRM = r"^leave (meeting|webinar)$"
    # your own video tile: "Video content you(Host, me), Computer audio unmuted,Video on, Active speaker"
    MY_TILE = r"^video content .*\((?:[^()]*,\s*)?me\)"

    def running(self, wins):
        return any(exe == "zoom.exe" for _, _, _, exe in wins)

    def meeting_windows(self, wins):
        # the meeting window, then the floating one while you share
        return [h for h, cls, _, exe in wins if exe == "zoom.exe" and cls in self.MEETING] + \
               [h for h, cls, _, exe in wins if exe == "zoom.exe" and cls == "ZPFloatToolbarClass"]

    def is_meeting(self, hwnd, buttons):
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        return cls.value in self.MEETING or super().is_meeting(hwnd, buttons)

    def more_state(self, st):
        if not self.uia or (st.get("muted") is not None and st.get("video") is not None):
            return
        tile = next((n for n, _ in self.uia.panes(self.hwnd) if re.search(self.MY_TILE, n, re.I)), "")
        audio = re.search(r"audio (muted|unmuted)", tile, re.I)
        video = re.search(r"video (on|off)", tile, re.I)
        if audio and st.get("muted") is None:
            st["muted"] = audio.group(1).lower() == "muted"
        if video and st.get("video") is None:
            st["video"] = video.group(1).lower() == "on"

    def title(self, hwnd):
        return ""  # (Zoom's window just says "Zoom Meeting")


class TeamsButtons(ButtonApp):
    """Teams without its API: the meeting window's buttons (it's a web page
    inside, which UI Automation can see into), then its shortcuts. Checked on
    new Teams 26288: the bar has "Mute mic" / "Unmute mic", "Turn camera on" /
    "Turn camera off", "Share", "React", "Chat", "People", "Record", "More"
    and "Leave"; React opens "Like", "Love", "Applause", "Laugh",
    "Surprised" and "Raise". While a menu's open, the rest of the bar is
    hidden from UI Automation (see ButtonApp.STICKY)."""
    name, EXE = "Teams", "ms-teams.exe"
    PATTERNS = {
        "mute": r"^(mute|unmute)( mic(rophone)?)?\b", "video": r"^turn camera (on|off)\b|^(start|stop) video\b",
        "share": r"^share\b|^stop sharing", "chat": r"^chat\b", "people": r"^people\b",
        "react": r"^react\b", "leave": r"^leave\b|^hang up",
    }
    STATES = {"muted": (r"^unmute\b", r"^mute\b"), "video": (r"^turn camera off|^stop video", r"^turn camera on|^start video"),
              "hand": (r"^lower( your| my)?( hand)?$", r"^raise( your| my)?( hand)?$"),
              "sharing": (r"^stop sharing", r"^share\b")}
    CARRY = ("hand",)
    SHORTCUTS = {"mute": "ctrl+shift+m", "video": "ctrl+shift+o", "share": "ctrl+shift+e", "hand": "ctrl+shift+k",
                 "blur": "ctrl+shift+p", "leave": "ctrl+shift+h"}
    REACTIONS = {"like": "Like", "love": "Love", "applause": "Applause", "laugh": "Laugh", "wow": "Surprised"}

    def act(self, action, cmd, state):
        # raise hand and the reactions are in React's menu: open it, press one
        if action in ("hand", "react") and self.hwnd and self.uia:
            target = (r"^(raise|lower)( your| my)?( hand)?$" if action == "hand" else
                      "^" + re.escape(self.REACTIONS.get(str(cmd.get("type")), "Like")) + "$")
            if self.from_menu("react", target):
                if action == "hand":  # (it's only in the menu: keep what it is now)
                    self.last[1]["hand"] = not state.get("hand")
                return None
        return super().act(action, cmd, state)

    MENU = r"^(like|love|applause|laugh|surprised|raise|lower)$"  # React's menu, open

    def more_state(self, st):
        # React's tooltip: "React or lower your hand. Your hand is raised. ..."
        u = self.uia and self.uia.u
        if u and self.hwnd:
            tips = self.uia.named(self.hwnd, self.uia.auto.CreatePropertyCondition(
                u.UIA_ControlTypePropertyId, u.UIA_ToolTipControlTypeId))
            said = next((re.search(r"your hand is (not )?raised", n, re.I) for n, _ in tips
                         if re.search(r"your hand is", n, re.I)), None)
            if said:
                st["hand"] = not said.group(1)

    def is_meeting(self, hwnd, buttons):
        # (with React's menu open, that menu is all UI Automation sees of the bar)
        return super().is_meeting(hwnd, buttons) or any(re.search(self.MENU, n, re.I) for n, _ in buttons)

    def from_menu(self, opener, target):
        """Open the menu `opener` opens, press `target` in it, and see it shut."""
        el = self.find(opener)
        if el is None or not self.uia.press(el):
            return False
        for _ in range(15):
            time.sleep(0.1)
            hit = next((b for n, b in self.uia.buttons(self.hwnd) if re.search(target, n, re.I)), None)
            if hit is not None:
                self.uia.press(hit)
                time.sleep(0.3)
                self.shut()
                return True
        self.shut()
        return False

    def shut(self):
        """Close React's menu if it's still open. It hides the button that
        opened it, so it's Escape (Teams comes forward for a moment)."""
        if self.hwnd and any(re.search(self.MENU, n, re.I) for n, _ in self.uia.buttons(self.hwnd)):
            keys_to(self.hwnd, "esc")

    def running(self, wins):
        return any(exe == "ms-teams.exe" for _, _, _, exe in wins)

    def meeting_windows(self, wins):
        return [h for h, _, title, exe in wins if exe == "ms-teams.exe" and title]

    def title(self, hwnd):
        title = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, title, 512)
        return re.sub(r"\s*\|\s*Microsoft Teams.*$", "", title.value).strip()


class TeamsAPI:
    """Teams' third-party app API: a WebSocket on 127.0.0.1:8124 that Teams
    opens when Settings > Privacy > Third-party app API is on. The first
    command after connecting makes Teams ask you to allow this app; it then
    sends a token, kept in teams_api.json, for next time."""
    PORT = 8124
    ACTIONS = {"mute": "toggle-mute", "video": "toggle-video", "hand": "toggle-hand", "blur": "toggle-background-blur",
               "leave": "leave-call", "chat": ("toggle-ui", "chat"), "share": ("toggle-ui", "share-tray"),
               "react": "send-reaction"}

    def __init__(self):
        self.path = os.path.join(APP_DIR, "teams_api.json")
        self.lock = threading.Lock()
        self.ws = None
        self.meeting = None   # the latest meetingUpdate's meetingState
        self.can = {}
        self.request = 0
        self.tried = 0.0
        self.problem = None   # why it isn't working, in words

    def token(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f).get("token") or ""
        except (OSError, ValueError):
            return ""

    def save_token(self, token):
        os.makedirs(APP_DIR, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"token": token}, f)

    def connect(self):
        """Open the socket if Teams is listening (tried every 10 s at most)."""
        if self.ws or time.monotonic() - self.tried < 10:
            return self.ws is not None
        self.tried = time.monotonic()
        q = urllib.parse.urlencode({"token": self.token(), "protocol-version": "2.0.0",
                                    "manufacturer": "Claude display", "device": "Screen Market screen",
                                    "app": "claude-display", "app-version": "1.0"})
        try:
            self.ws = WebSocket("127.0.0.1", self.PORT, "/?" + q)
        except OSError as e:
            self.problem = ("turn on Teams' third-party app API (Settings, Privacy) for its full controls"
                            if isinstance(e, ConnectionRefusedError) else f"Teams API: {e}")
            return False
        self.problem = None
        threading.Thread(target=self.listen, args=(self.ws,), daemon=True, name="teams-api").start()
        self.send("query-state")
        return True

    def listen(self, ws):
        try:
            while True:
                text = ws.recv(60)
                if text is None:
                    continue
                try:
                    msg = json.loads(text)
                except ValueError:
                    continue
                if msg.get("tokenRefresh"):
                    self.save_token(msg["tokenRefresh"])
                    log.info("Teams API: paired")
                if isinstance(msg.get("meetingUpdate"), dict):
                    up = msg["meetingUpdate"]
                    with self.lock:
                        self.meeting = up.get("meetingState") or {}
                        self.can = up.get("meetingPermissions") or {}
                if msg.get("errorMsg"):
                    log.info("Teams API: %s", msg["errorMsg"])
        except (OSError, ConnectionError) as e:
            log.info("Teams API closed: %s", e)
        finally:
            ws.close()
            with self.lock:
                if self.ws is ws:
                    self.ws, self.meeting = None, None

    def send(self, action, kind=None):
        with self.lock:
            ws = self.ws
            self.request += 1
            msg = {"action": action, "parameters": {"type": kind} if kind else {}, "requestId": self.request}
        if ws is None:
            return False
        try:
            ws.send(json.dumps(msg))
            return True
        except OSError:
            return False

    def look(self):
        with self.lock:
            m, can = self.meeting, dict(self.can)
        if m is None:
            return None
        st = {"available": True, "in_meeting": bool(m.get("isInMeeting"))}
        if st["in_meeting"]:
            st.update(muted=m.get("isMuted"), video=m.get("isVideoOn"), hand=m.get("isHandRaised"),
                      sharing=m.get("isSharing"), recording=m.get("isRecordingOn"),
                      blur=m.get("isBackgroundBlurred"))
            st["can"] = {"mute": can.get("canToggleMute", True), "video": can.get("canToggleVideo", True),
                         "hand": can.get("canToggleHand", True), "blur": can.get("canToggleBlur", True),
                         "leave": can.get("canLeave", True), "react": can.get("canReact", True),
                         "chat": can.get("canToggleChat", True), "share": can.get("canToggleShareTray", True)}
        return st

    def act(self, action, cmd):
        what = self.ACTIONS.get(action)
        if what is None:
            return False
        if action == "share" and (self.meeting or {}).get("isSharing"):
            return self.send("stop-sharing")
        if action == "react":
            return self.send("send-reaction", str(cmd.get("type") or "like"))
        if isinstance(what, tuple):
            return self.send(*what)
        return self.send(what)

    def close(self):
        if self.ws:
            self.ws.close()


class Teams(App):
    """Teams: its API when it's on, the meeting window's buttons when not."""
    name = "Teams"

    def __init__(self):
        self.api = TeamsAPI()
        self.buttons = TeamsButtons()

    def setup(self):
        self.buttons.setup()

    def look(self):
        self.api.connect()
        st = self.api.look()
        if st is not None:
            self.via = "Teams API"
            return st
        st = self.buttons.look()
        self.via = self.buttons.via
        if st.get("in_meeting") and self.api.problem and self.via == "keyboard":
            st["note"] = self.api.problem  # (with its buttons to press, it's fine without the API)
        return st

    def act(self, action, cmd, state):
        if self.api.look() is not None and self.api.act(action, cmd):
            return None
        return self.buttons.act(action, cmd, state)

    def close(self):
        self.api.close()


class Meet(App):
    """Google Meet: the browser extension does the pressing. This keeps its
    taps for it, and its word on the call, on 127.0.0.1:47613."""
    name, via = "Google Meet", "browser extension"
    FRESH = 15  # s: the extension's word is this old: it's gone (it says every 3 s; a closed tab, at once)

    def __init__(self):
        self.cond = threading.Condition()
        self.pending = []
        self.state = None
        self.state_at = 0.0
        self.server = None

    def setup(self):
        meet = self

        class Handler(BaseHTTPRequestHandler):
            def allowed(self):
                origin = self.headers.get("Origin") or ""
                # the extension, or a program on this PC - not a web page
                return not origin or origin.startswith(("chrome-extension://", "moz-extension://"))

            def reply(self, code, data):
                body = json.dumps(data).encode()
                self.send_response(code)
                origin = self.headers.get("Origin")
                if origin:
                    self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_OPTIONS(self):
                self.send_response(204 if self.allowed() else 403)
                if self.allowed():
                    self.send_header("Access-Control-Allow-Origin", self.headers.get("Origin") or "*")
                    self.send_header("Access-Control-Allow-Methods", "GET, POST")
                    self.send_header("Access-Control-Allow-Headers", "Content-Type")
                    self.send_header("Access-Control-Allow-Private-Network", "true")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self):
                if not self.allowed():
                    return self.reply(403, {"error": "not from the extension"})
                url = urllib.parse.urlsplit(self.path)
                if url.path == "/meet/commands":
                    wait = min(25.0, float((urllib.parse.parse_qs(url.query).get("wait") or ["20"])[0]))
                    self.reply(200, {"commands": meet.take(wait)})
                elif url.path == "/meet":
                    self.reply(200, {"helper": "windows", "extension_port": EXTENSION_PORT})
                else:
                    self.reply(404, {"error": "not found"})

            def do_POST(self):
                if not self.allowed():
                    return self.reply(403, {"error": "not from the extension"})
                path = urllib.parse.urlsplit(self.path).path
                if path == "/meet/inspect" and not self.headers.get("Origin"):
                    return self.reply(200, meet.inspect())  # (--inspect-meet, from this PC)
                if path != "/meet/state":
                    return self.reply(404, {"error": "not found"})
                try:
                    data = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                except ValueError:
                    return self.reply(400, {"error": "not JSON"})
                meet.got_state(data if isinstance(data, dict) else {})
                self.reply(200, {"ok": True})

            def log_message(self, *args):
                pass

        try:
            self.server = ThreadingHTTPServer(("127.0.0.1", EXTENSION_PORT), Handler)
            self.server.daemon_threads = True
        except OSError as e:
            log.warning("Meet: can't listen on 127.0.0.1:%s for the extension: %s", EXTENSION_PORT, e)
            return
        threading.Thread(target=self.server.serve_forever, daemon=True, name="meet-extension").start()

    on_change = None  # the relay's: a fresh word from the extension is posted at once

    def got_state(self, data):
        with self.cond:
            if "controls" in data:  # (the answer to an inspect: see inspect)
                self.inspected = data.pop("controls")
            changed = data != self.state
            self.state, self.state_at = data, time.monotonic()
            self.cond.notify_all()
        if changed and self.on_change:
            self.on_change()

    inspected = None

    def inspect(self):
        """What's on Meet's bar, as the extension sees it: for checking its
        selectors against Meet as it is now."""
        with self.cond:
            self.inspected = None
            self.pending.append({"action": "inspect"})
            self.cond.notify_all()
            deadline = time.monotonic() + 8
            while self.inspected is None and time.monotonic() < deadline:
                self.cond.wait(deadline - time.monotonic())
            return {"state": self.state, "controls": self.inspected}

    def take(self, wait):
        """The extension asking for taps: whatever's waiting, or [] after `wait`."""
        deadline = time.monotonic() + wait
        with self.cond:
            while not self.pending and time.monotonic() < deadline:
                self.cond.wait(deadline - time.monotonic())
            out, self.pending = self.pending, []
            return out

    def look(self):
        with self.cond:
            st, fresh = self.state, time.monotonic() - self.state_at < self.FRESH
        if not fresh or not isinstance(st, dict):
            return {"available": False, "in_meeting": False,
                    "note": "open Meet in a browser with the Claude display extension"}
        keys = ("in_meeting", "muted", "video", "hand", "sharing", "captions", "title")
        return dict({k: st.get(k) for k in keys}, available=True)

    def act(self, action, cmd, state):
        with self.cond:
            self.pending.append({"action": action, "on": cmd.get("on"), "type": cmd.get("type"),
                                 "want": cmd.get("want")})
            self.cond.notify_all()

    def set_muted(self, want, state):
        self.act("mute", {"want": want}, state)  # (the extension knows the state best: it says which way)

    def close(self):
        if self.server:
            self.server.shutdown()


def make_app(app):
    return {"zoom": Zoom, "teams": Teams, "meet": Meet}[app]()


# ---------------------------------------------------------------- WebSocket (for Teams)

class WebSocket:
    """Just enough of RFC 6455 for a text-message client, plain ws://."""

    def __init__(self, host, port, path, timeout=3):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
                           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
                          .encode())
        self.buf, self.parts, self.wlock = b"", [], threading.Lock()
        while b"\r\n\r\n" not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("closed during the handshake")
            self.buf += chunk
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise ConnectionError(head.split(b"\r\n")[0].decode(errors="replace"))

    def send(self, data, opcode=1):
        data = data.encode() if isinstance(data, str) else data
        n = len(data)
        head = bytes([0x80 | opcode])
        head += bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + n.to_bytes(2, "big") if n < 65536 \
            else bytes([0x80 | 127]) + n.to_bytes(8, "big")
        mask = os.urandom(4)
        with self.wlock:
            self.sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def recv(self, timeout):
        """The next text message, or None if none comes within `timeout`."""
        self.sock.settimeout(timeout)
        while True:
            msg = self.take()
            if msg is not None:
                return msg
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                return None
            if not chunk:
                raise ConnectionError("closed")
            self.buf += chunk

    def take(self):
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
            if len(b) < i + n:
                return None
            data, op, fin, self.buf = b[i:i + n], b[0] & 0x0F, b[0] & 0x80, b[i + n:]
            if op == 8:
                raise ConnectionError("closed by Teams")
            if op == 9:
                self.send(data, 10)
            elif op in (0, 1):
                self.parts.append(data)
                if fin:
                    text, self.parts = b"".join(self.parts).decode("utf-8", "replace"), []
                    return text

    def close(self):
        try:
            self.send(b"", 8)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------- the display

class Display:
    """The display's HTTP API, at an address the tray can change."""

    def __init__(self, address):
        self.address = address  # () -> (host, port)
        self.ip = None
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # (it's on the LAN)

    def call(self, path, data=None, timeout=5.0):
        host, port = self.address()
        if self.ip is None or self.ip[0] != host:
            self.ip = (host, socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)[0][4][0])
        body = None if data is None else json.dumps(data).encode()
        req = urllib.request.Request(f"http://{self.ip[1]}:{port}{path}", data=body,
                                     headers={"Content-Type": "application/json"} if body else {})
        try:
            with self.opener.open(req, timeout=timeout) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, None
        except (OSError, ValueError):
            self.ip = None  # (re-resolve: DHCP may have moved it)
            raise


class Relay:
    """One meeting screen: the taps from it, done in its app, and the app's
    state back to it. Two threads: one waits on the display for taps, the
    other owns the app (and its COM objects) and does everything to it."""

    def __init__(self, app, display):
        self.app_id = app
        self.display = display
        self.app = make_app(app)
        self.inbox = queue.Queue()
        self.stopping = threading.Event()
        self.showing = False
        self.connected = False
        self.state = {}
        self.posted, self.posted_at = None, 0.0
        self.since = None          # when this call started (time.time())
        self.ptt_until = 0.0       # push to talk is held till then (monotonic), or 0
        self.ptt_opened = False    # ...and it was push to talk that unmuted you
        self.expected = None       # (key, value, until): see expect
        self.note = None           # why the last tap didn't work, for the display

    def start(self):
        if hasattr(self.app, "on_change"):  # (the app says when it's changed: look then, not on the next tick)
            self.app.on_change = lambda: self.inbox.put({"action": "look"})
        threading.Thread(target=self.poll, daemon=True, name=f"{self.app_id}-taps").start()
        threading.Thread(target=self.work, daemon=True, name=f"{self.app_id}-app").start()

    def stop(self):
        self.stopping.set()
        self.inbox.put(None)

    def poll(self):
        """Wait on the display for taps; each goes to the worker."""
        boot, after, backoff = "", 0, 1.0
        while not self.stopping.is_set():
            try:
                code, r = self.display.call(f"/meeting/{self.app_id}/commands?" + urllib.parse.urlencode(
                    {"boot": boot, "after": after, "wait": POLL_WAIT}), timeout=POLL_WAIT + 10)
                if code != 200 or not isinstance(r, dict):
                    raise OSError(f"HTTP {code}")
            except (OSError, ValueError) as e:
                self.connected = False
                log.debug("%s: display: %s", self.app_id, e)
                self.stopping.wait(backoff)
                backoff = min(30.0, backoff * 2)
                continue
            backoff, self.connected = 1.0, True
            boot, after, self.showing = r.get("boot", ""), r.get("seq", after), bool(r.get("showing"))
            for cmd in r.get("commands") or []:
                self.inbox.put(cmd)

    def work(self):
        """The app's own thread: taps as they come, and a look at the call
        every second or few, posting what it sees."""
        try:
            self.app.setup()
        except Exception:
            log.exception("%s setup", self.app_id)
        next_look = 0.0
        while not self.stopping.is_set():
            now = time.monotonic()
            wait = max(0.0, min(next_look - now, (self.ptt_until - now) if self.ptt_until else 99))
            try:
                cmd = self.inbox.get(timeout=wait)
            except queue.Empty:
                cmd = False
            if cmd is None:
                break
            try:
                if cmd:
                    self.do(cmd)
                    next_look = 0.0  # look again straight away: the tap changed something
                if self.ptt_until and time.monotonic() > self.ptt_until:
                    log.info("%s: push to talk wasn't let go - shutting the mic", self.app_id)
                    self.ptt_off()
                if time.monotonic() >= next_look:
                    self.look()
                    next_look = time.monotonic() + (LOOK_SHOWING if self.showing or self.ptt_until else LOOK_IDLE)
            except Exception:
                log.exception("%s", self.app_id)
                next_look = time.monotonic() + LOOK_IDLE
        try:
            self.ptt_off()  # (quitting mid-sentence: shut the mic first)
        finally:
            self.app.close()

    def do(self, cmd):
        action = cmd.get("action")
        if action == "look":
            return  # (just a look, which follows every command)
        if action == "ptt":
            return self.ptt_on() if cmd.get("on") else self.ptt_off()
        self.note = self.app.act(action, cmd, self.state)
        if self.note:
            log.info("%s %s: %s", self.app_id, action, self.note)

    def ptt_on(self):
        first = not self.ptt_until
        self.ptt_until = time.monotonic() + PTT_GRACE
        if first:
            self.ptt_opened = self.state.get("muted") is not False  # (live already: leave it so)
            if self.ptt_opened:
                self.app.set_muted(False, self.state)
                self.expect("muted", False)

    def ptt_off(self):
        if self.ptt_until:
            self.ptt_until = 0.0
            if self.ptt_opened:
                self.app.set_muted(True, dict(self.state, muted=False))
                self.expect("muted", True)

    def expect(self, key, value):
        """What a tap just set, believed over the app's word for a moment: it
        takes a little while to say so, and another press straight after
        this one (or the display) should go by what it is now."""
        self.state = dict(self.state, **{key: value})
        self.expected = (key, value, time.monotonic() + EXPECT_SECS)

    def look(self):
        st = self.app.look()
        if self.expected:
            key, value, until = self.expected
            if st.get(key) == value or time.monotonic() > until or not st.get("in_meeting"):
                self.expected = None  # (it says so too now, or it never will)
            else:
                st[key] = value
        if st.get("in_meeting"):
            self.since = self.since or time.time()
        else:
            self.since = None
        st = dict(st, helper="windows", via=self.app.via if st.get("in_meeting") else "", since=self.since)
        if self.note and st.get("in_meeting") and not st.get("note"):
            st["note"] = self.note
        self.state = st
        if st != self.posted or time.monotonic() - self.posted_at > HEARTBEAT:
            try:
                code, r = self.display.call(f"/meeting/{self.app_id}/state", st)
                if code == 200:
                    self.posted, self.posted_at = st, time.monotonic()
                    self.showing = bool((r or {}).get("showing"))
            except (OSError, ValueError) as e:
                log.debug("%s: posting state: %s", self.app_id, e)

    def summary(self):
        """A line for the tray's menu."""
        name = self.app.name
        if not self.connected:
            return f"{name}: can't reach the display"
        st = self.state
        if not st.get("in_meeting"):
            return f"{name}: {st.get('note') or 'not in a meeting'}"
        bits = ["muted" if st.get("muted") else "live" if st.get("muted") is False else "in a meeting"]
        if self.app.via:
            bits.append(f"via {self.app.via}")
        return f"{name}: " + ", ".join(bits)


class MeetingHelper:
    """The relays for the meeting screens the display has, started and
    stopped as they're installed and removed."""

    def __init__(self, address):
        self.display = Display(address)
        self.relays = {}
        self.stopping = threading.Event()
        self.lock = threading.Lock()

    def start(self):
        threading.Thread(target=self.watch, daemon=True, name="meetings").start()

    def watch(self):
        while not self.stopping.is_set():
            try:
                code, info = self.display.call("/screens")
                if code == 200 and isinstance(info, dict):
                    self.follow({s.get("id") for s in info.get("installed") or []
                                 if s.get("id") in APPS and s.get("enabled", True)})
            except (OSError, ValueError) as e:
                log.debug("meetings: /screens: %s", e)
            self.stopping.wait(SCREENS_EVERY)

    def follow(self, wanted):
        with self.lock:
            for app in wanted - set(self.relays):
                log.info("meetings: %s screen found - following its taps", app)
                self.relays[app] = Relay(app, self.display)
                self.relays[app].start()
            for app in set(self.relays) - wanted:
                self.relays.pop(app).stop()

    def summary(self):
        with self.lock:
            return [r.summary() for _, r in sorted(self.relays.items())]

    def stop(self):
        self.stopping.set()
        with self.lock:
            for r in self.relays.values():
                r.stop()
            self.relays.clear()


# ---------------------------------------------------------------- trying it out

def dump(app):
    """The buttons UI Automation sees in the app's meeting windows: for
    checking the names the patterns above look for."""
    a = {"zoom": Zoom, "teams": TeamsButtons}[app]()
    a.setup()
    wins = windows()
    found = a.meeting_windows(wins) or [h for h, _, _, exe in wins if exe == a.EXE]
    if not found:
        print(f"no {a.name} windows open")
    for hwnd in found:
        title = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, title, 512)
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        print(f"--- window {hwnd:#x} {cls.value!r} {title.value!r}")
        for name, _ in a.uia.buttons(hwnd):
            hits = [act for act, pat in a.PATTERNS.items() if re.search(pat, name, re.I)]
            print(f"  {name!r}" + (f"   -> {', '.join(hits)}" if hits else ""))
    print("state:", json.dumps(a.look()))


def main():
    ap = argparse.ArgumentParser(description="Meeting controls for the display's Meet, Zoom and Teams screens.")
    ap.add_argument("--host", default="claude-display.local", help="the display's address (host or host:port)")
    ap.add_argument("--dump", choices=("zoom", "teams"), help="list the buttons in that app's meeting window")
    ap.add_argument("--inspect-meet", action="store_true",
                    help="list what's on Meet's bar, as the extension sees it (the tray must be running)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(threadName)s %(message)s")
    if args.dump:
        return dump(args.dump)
    if args.inspect_meet:
        req = urllib.request.Request(f"http://127.0.0.1:{EXTENSION_PORT}/meet/inspect", data=b"", method="POST")
        got = json.load(urllib.request.urlopen(req, timeout=15))
        print("state:", json.dumps(got.get("state")))
        for c in got.get("controls") or []:
            print(" ", json.dumps(c, ensure_ascii=False))
        return None
    host, _, port = args.host.partition(":")
    helper = MeetingHelper(lambda: (host, int(port or 8080)))
    helper.start()
    try:
        while True:
            time.sleep(5)
            for line in helper.summary():
                log.info("%s", line)
    except KeyboardInterrupt:
        helper.stop()


if __name__ == "__main__":
    main()
