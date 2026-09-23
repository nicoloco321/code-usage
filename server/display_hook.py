#!/usr/bin/env python3
"""Claude Code hooks -> usage display: exactly when Claude is working here.

Claude Code runs this on its lifecycle events and it tells the display
/thinking/on while any Claude Code session on this machine is working, and
/thinking/off once they have all finished or are waiting on you. The hooks are
installed as async, so they never slow Claude down.

    python3 server/display_hook.py --install --host 192.168.1.42   # add the hooks
    python3 server/display_hook.py --status                        # what it sees
    python3 server/display_hook.py --uninstall                     # remove them
    (on Windows: py -3 server\\display_hook.py ...)

Use the display's IP for --host - .local names can take seconds to resolve.
--install writes the hooks into ~/.claude/settings.json (user level, so every
project gets them) and replaces the older curl hooks from
claude-hooks.example.json. Claude Code picks the change up right away.

Each session is working, waiting (a permission prompt or question is up) or
idle:
  UserPromptSubmit                             -> working
  PreToolUse, PostToolUse(Failure)             -> working
  Notification: permission prompt / question   -> waiting
  Stop, StopFailure, SessionEnd, idle prompt   -> idle
SubagentStop and compaction only keep a working session alive: the desktop
app's own helper agent finishes a couple of seconds after every Stop, and must
not switch the display back on. Async hooks can also land out of order, so a
tool event that arrives within a moment of a stop is treated as a straggler
from before it, not as new work.

Hooks can't see two things: an Esc interrupt (Stop doesn't fire) and a tool
running longer than the display's 5-minute backstop. watch() covers both; the
Windows tray helper runs it, or run --watch yourself.

No third-party dependencies.
"""

import argparse
import contextlib
import json
import os
import shutil
import socket
import sys
import time
import urllib.parse
import urllib.request

if os.name == "nt":
    STATE_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "claude-display")
else:
    STATE_DIR = os.path.expanduser("~/.local/state/claude-display")
STATE_PATH = os.path.join(STATE_DIR, "hook-state.json")
LOCK_PATH = os.path.join(STATE_DIR, "hook-state.lock")
SETTINGS_PATH = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")

GRACE_SECS = 2.0          # activity this soon after a stop/wait is a late straggler
IDLE_AFTER_SECS = 300     # "working" with no events this long (and no tool running): it ended
TOOL_MAX_SECS = 1800      # one running tool keeps a session working at most this long
KEEPALIVE_SECS = 60       # watch() re-sends "on" this often (the display's backstop is 5 min)
OFFLINE_SECS = 30         # after a failed send, hooks don't retry for this long
SEND_TIMEOUT = 1.5
FORGET_SECS = 86400       # drop idle sessions after a day

WORKING, WAITING, IDLE = "working", "waiting", "idle"
STOP_EVENTS = {"Stop", "StopFailure", "SessionEnd"}
# Tool calls are the only events that prove Claude itself is working again -
# e.g. after being re-invoked by a finished background task, with no prompt.
TOOL_EVENTS = {"PreToolUse", "PostToolUse", "PostToolUseFailure"}
# These keep a working session alive but never wake an idle one: they can
# land after Stop (a subagent or helper finishing, a /compact you ran).
ALIVE_EVENTS = {"SubagentStop", "PreCompact", "PostCompact"}
AGENT_TOOLS = {"Task", "Agent"}  # tools that start a subagent
AGENT_STALE_SECS = 300    # an agent we haven't heard from this long is gone
HISTORY = 60              # recent events kept for --status
WAIT_NOTES = {"permission_prompt", "elicitation_dialog", "elicitation_url_dialog",
              "agent_needs_input"}
RESUME_NOTES = {"elicitation_response", "elicitation_complete"}  # you answered
HOOK_EVENTS = ["UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure",
               "Notification", "Stop", "StopFailure", "SubagentStop", "PreCompact",
               "SessionEnd"]
MARKER = "display_hook.py"  # how we recognise our own hooks in settings.json
INTERRUPTED = "[Request interrupted by user"


# ---------------------------------------------------------------- state file

def _retry(fn, secs=5):
    """Windows sometimes refuses a file for a moment (antivirus, the Store
    Python's file redirection) - try again rather than lose the event."""
    deadline = time.monotonic() + secs
    while True:
        try:
            return fn()
        except PermissionError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.02)


@contextlib.contextmanager
def locked():
    """Serialize hook processes: async hooks can run concurrently."""
    os.makedirs(STATE_DIR, exist_ok=True)
    fd = _retry(lambda: os.open(LOCK_PATH, os.O_RDWR | os.O_CREAT, 0o600))
    try:
        if os.name == "nt":
            import msvcrt
            deadline = time.monotonic() + 10
            while True:
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise
                    time.sleep(0.01)
            try:
                yield
            finally:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield  # released when the fd closes
    finally:
        os.close(fd)


def load():
    """The saved state. Only a missing or garbled file means a fresh start -
    a file we can't read right now raises, so it never gets saved over."""
    def read():
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    try:
        st = _retry(read)
    except (FileNotFoundError, ValueError):
        st = {}
    st.setdefault("sessions", {})
    return st


def save(st):
    def write():
        with open(STATE_PATH, "w", encoding="utf-8") as f:  # only ever called under locked()
            json.dump(st, f, indent=1)
    _retry(write)


# ---------------------------------------------------------------- session logic

def set_state(s, state, now):
    if s["state"] != state:
        s["state"], s["changed"] = state, now


def describe_tool(name, inp):
    """A tool call in a few words for the display: "Editing main.cpp"."""
    inp = inp if isinstance(inp, dict) else {}

    def base(path):
        return os.path.basename(str(path or "").rstrip("/\\")) or "a file"

    if name == "Bash":
        text = str(inp.get("description") or inp.get("command") or "a command")
        return "Running: " + text.strip().splitlines()[0][:90] if text.strip() else "Running a command"
    if name in ("Edit", "MultiEdit", "NotebookEdit"):
        return f"Editing {base(inp.get('file_path') or inp.get('notebook_path'))}"
    if name == "Write":
        return f"Writing {base(inp.get('file_path'))}"
    if name == "Read":
        return f"Reading {base(inp.get('file_path'))}"
    if name in ("Grep", "Glob"):
        return f"Searching for {str(inp.get('pattern') or '')[:60]}".rstrip()
    if name == "WebFetch":
        host = urllib.parse.urlsplit(str(inp.get("url") or "")).hostname
        return f"Reading {host}" if host else "Reading a web page"
    if name == "WebSearch":
        return f"Searching the web: {str(inp.get('query') or '')[:60]}"
    if name in AGENT_TOOLS:
        return f"Starting an agent: {str(inp.get('description') or '')[:60]}".rstrip(": ")
    if name == "TodoWrite":
        return "Updating the plan"
    if name.startswith("mcp__"):
        parts = name.split("__")
        return f"Using {parts[-1]} ({parts[1]})" if len(parts) > 2 else f"Using {name}"
    return f"Using {name}"


def agent_event(s, ev, now, activity):
    """A subagent's own event: keep its entry (and what it's doing) fresh."""
    agents = s.setdefault("agents", {})
    aid, atype = ev["agent_id"], ev.get("agent_type") or "agent"
    entry = next((a for a in agents.values() if a.get("agent_id") == aid), None)
    if entry is None:  # the agent a launch is waiting for, or one we missed starting
        entry = next((a for a in agents.values()
                      if not a.get("agent_id") and a.get("type") == atype), None)
        if entry is None:
            entry = agents.setdefault(aid, {"label": atype, "type": atype})
        entry["agent_id"] = aid
    entry["seen"] = now
    if activity:
        entry["activity"] = activity


def apply_event(st, ev, now):
    """Update the per-session state from one hook event."""
    name = ev.get("hook_event_name", "")
    sid = ev.get("session_id") or "?"
    s = st["sessions"].setdefault(sid, {"state": IDLE, "changed": 0.0, "seen": now, "tools": {}})
    s["seen"] = now
    if ev.get("transcript_path"):
        s["transcript"] = ev["transcript_path"]
    if ev.get("cwd"):
        s["project"] = os.path.basename(str(ev["cwd"]).rstrip("/\\"))
    note = ev.get("notification_type") or ""
    st["last"] = {"event": name + (f":{note}" if note else ""), "session": sid[:8], "at": now}
    agents = s.setdefault("agents", {})
    if name == "SubagentStop" and ev.get("agent_id"):
        for key in [k for k, a in agents.items() if a.get("agent_id") == ev["agent_id"]]:
            del agents[key]

    if name in STOP_EVENTS or note == "idle_prompt":
        set_state(s, IDLE, now)
        s.update(tools={}, agents={}, activity="", waiting="")
        if name == "SessionEnd":
            del st["sessions"][sid]
    elif name == "Notification" and note in WAIT_NOTES:
        if s["state"] == WORKING:
            set_state(s, WAITING, now)
            s["waiting"] = ev.get("message") or "Claude needs your input"
    elif name == "UserPromptSubmit":
        set_state(s, WORKING, now)
        s.update(turn=now, activity="Thinking...", waiting="", agents={})
    elif note in RESUME_NOTES:
        if s["state"] == WAITING:
            set_state(s, WORKING, now)
            s["waiting"] = ""
    elif name in TOOL_EVENTS:
        if s["state"] != WORKING and now - s["changed"] < GRACE_SECS:
            return  # a straggler from just before the stop/wait - ignore it
        if s["state"] == IDLE:
            s["turn"] = now  # woken without a prompt, e.g. by a finished background task
        tool = ev.get("tool_use_id")
        done = s.setdefault("done", [])
        # A quick tool's Post hook can run before its Pre hook, so remember
        # finished ids - otherwise the late Pre looks like a tool still running.
        if name == "PreToolUse" and tool and tool not in done:
            s["tools"][tool] = now
        elif name in ("PostToolUse", "PostToolUseFailure") and tool:
            s["tools"].pop(tool, None)
            done.append(tool)
            del done[:-50]
        tool_name, tool_input = ev.get("tool_name") or "", ev.get("tool_input")
        if ev.get("agent_id"):  # a subagent working - main activity stays as it is
            agent_event(s, ev, now, describe_tool(tool_name, tool_input) if name == "PreToolUse" else "")
        elif name == "PreToolUse":
            s["activity"] = describe_tool(tool_name, tool_input)
            if tool_name in AGENT_TOOLS and tool:
                inp = tool_input if isinstance(tool_input, dict) else {}
                atype = inp.get("subagent_type") or "agent"
                desc = str(inp.get("description") or "")
                agents[tool] = {"label": f"{atype}: {desc}" if desc else atype,
                                "type": atype, "seen": now}
        else:  # the main agent's tool finished - back to thinking about the result
            s["activity"] = "Thinking..."
            if tool in agents and not agents[tool].get("agent_id"):
                del agents[tool]  # a foreground agent finished
        s["waiting"] = ""
        set_state(s, WORKING, now)
    # ALIVE_EVENTS and anything else: "seen" is refreshed above, which keeps a
    # working session from timing out, but they never wake an idle one.


def summary(st, now):
    """What the display's session panel shows: this machine's sessions that
    are working or waiting on you."""
    out = []
    for sid, s in sorted(st["sessions"].items(), key=lambda kv: -kv[1]["seen"]):
        if not session_working(s, now) and s["state"] != WAITING:
            continue
        agents = [{"label": a.get("label", "agent"), "activity": a.get("activity", "")}
                  for a in (s.get("agents") or {}).values()
                  if now - a.get("seen", now) < AGENT_STALE_SECS]
        out.append({"id": sid[:8], "project": s.get("project", ""), "state": s["state"],
                    "elapsed": round(now - s.get("turn", s["changed"])),
                    "activity": s.get("activity", ""), "waiting": s.get("waiting", ""),
                    "agents": agents[:4]})
    return {"host": socket.gethostname(), "sessions": out}


def record(st, now, text):
    """Keep a short history of what happened, for --status."""
    history = st.setdefault("history", [])
    history.append([now, text])
    del history[:-HISTORY]


def session_working(s, now):
    if s["state"] != WORKING:
        return False
    if s["tools"] and now - max(s["tools"].values()) < TOOL_MAX_SECS:
        return True  # a tool is running - builds and tests can take a while
    return now - s["seen"] < IDLE_AFTER_SECS


def any_working(st, now):
    return any(session_working(s, now) for s in st["sessions"].values())


def prune(st, now):
    for sid, s in list(st["sessions"].items()):
        if s["state"] != WORKING and now - s["seen"] > FORGET_SECS:
            del st["sessions"][sid]


# ---------------------------------------------------------------- talking to the display

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # LAN: no proxy


def split_host(host, port=None):
    host = host.strip()
    if host.count(":") == 1:
        host, _, p = host.partition(":")
        port = port or int(p)
    return host, port or 8080


def send(st, host, port, on, now):
    """POST /thinking/on or /off, carrying the session summary as JSON (the Pi
    app shows it; the ESP32 just ignores the body). Records the result in the
    state and returns a word for the history."""
    word = "on" if on else "off"
    url = f"http://{host}:{port}/thinking/{word}"
    body = json.dumps(summary(st, now)).encode()
    try:
        _opener.open(urllib.request.Request(url, data=body, method="POST",
                                            headers={"Content-Type": "application/json"}),
                     timeout=SEND_TIMEOUT).read()
        st["sent"], st["sent_at"], st["fail_at"] = word, now, 0
        st.pop("error", None)
        return word
    except OSError as e:
        st["fail_at"], st["error"] = now, str(e)[:200]
        return f"{word} FAILED ({str(e)[:60]})"


def handle(ev, host, port):
    """One hook event: update the state, then tell the display if it matters."""
    now = time.time()
    with locked():
        st = load()
        apply_event(st, ev, now)
        prune(st, now)
        on = any_working(st, now)
        stopping = ev.get("hook_event_name") in STOP_EVENTS
        # "on" goes out on every event (it doubles as a keep-alive); "off" only
        # when it changes, or on a stop in case the last one got lost.
        sent = ""
        if on or st.get("sent") != "off" or stopping:
            offline = now - st.get("fail_at", 0) < OFFLINE_SECS
            sent = send(st, host, port, on, now) if not offline or stopping else "skipped (offline)"
        session = st["sessions"].get(ev.get("session_id") or "?")
        record(st, now, f"{st['last']['event']} {st['last']['session']} -> "
                        f"{session['state'] if session else 'ended'}"
                        + (f", sent {sent}" if sent else ""))
        save(st)


# ---------------------------------------------------------------- watcher

_checked_mtime = {}


def interrupted(path):
    """True if the transcript's last message is Claude Code's Esc marker."""
    try:
        mtime = os.path.getmtime(path)
        if _checked_mtime.get(path) == mtime:
            return False  # nothing new since we last looked
        _checked_mtime[path] = mtime
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 65536))
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        return False
    for line in reversed(tail.splitlines()):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("type") not in ("user", "assistant"):
            continue  # bookkeeping lines
        content = (entry.get("message") or {}).get("content")
        if isinstance(content, list):
            content = " ".join(c.get("text", "") for c in content
                               if isinstance(c, dict) and c.get("type") == "text")
        return isinstance(content, str) and content.startswith(INTERRUPTED)
    return False


def watch_once(host, port):
    """One watcher pass: catch Esc interrupts, keep the display alive through
    long tool runs, and send "off" when working sessions time out. Returns
    whether this machine is working."""
    now = time.time()
    with locked():
        st = load()
        for sid, s in st["sessions"].items():
            if s["state"] != IDLE and s.get("transcript") and interrupted(s["transcript"]):
                set_state(s, IDLE, now)
                s["tools"] = {}
                st["last"] = {"event": "interrupted (Esc)", "session": sid[:8], "at": now}
                record(st, now, f"watcher: Esc interrupt in {sid[:8]} -> idle")
        on = any_working(st, now)
        if on and (st.get("sent") != "on" or now - st.get("sent_at", 0) > KEEPALIVE_SECS):
            record(st, now, f"watcher: keep-alive, sent {send(st, host, port, True, now)}")
        elif not on and st.get("sent") != "off":
            record(st, now, f"watcher: nothing working, sent {send(st, host, port, False, now)}")
        save(st)
    return on


def local_working():
    """Is any Claude Code session on this machine working right now?"""
    with locked():
        return any_working(load(), time.time())


# ---------------------------------------------------------------- settings.json

def _strip_ours(hooks):
    """Remove our hooks (and the old curl /thinking/ ones) from a hooks dict."""
    for event in list(hooks):
        groups = []
        for group in hooks[event]:
            kept = [h for h in group.get("hooks", [])
                    if MARKER not in h.get("command", "") and "/thinking/" not in h.get("command", "")]
            if kept:
                groups.append(dict(group, hooks=kept))
        if groups:
            hooks[event] = groups
        else:
            del hooks[event]


def _read_settings(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def _write_settings(path, settings):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        shutil.copy2(path, path + ".bak")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")


def hook_command(host, port):
    script = os.path.abspath(__file__).replace("\\", "/")
    if os.name == "nt" and shutil.which("py"):
        # The Microsoft Store python alias won't start from Git Bash, so go
        # through the py launcher - pinned to this Python, because the Store
        # build keeps its files in a private per-version folder and the hooks
        # must share the tray helper's state file.
        python = f"py -{sys.version_info.major}.{sys.version_info.minor}"
    else:
        python = '"' + sys.executable.replace("\\", "/") + '"'
    return f'{python} "{script}" --host {host}:{port}'


def install(host, port, path=SETTINGS_PATH):
    settings = _read_settings(path)
    hooks = settings.setdefault("hooks", {})
    _strip_ours(hooks)
    command = hook_command(host, port)
    for event in HOOK_EVENTS:
        hooks.setdefault(event, []).append(
            {"hooks": [{"type": "command", "command": command, "async": True}]})
    _write_settings(path, settings)
    return command


def uninstall(path=SETTINGS_PATH):
    settings = _read_settings(path)
    _strip_ours(settings.get("hooks", {}))
    if not settings.get("hooks"):
        settings.pop("hooks", None)
    _write_settings(path, settings)


def installed(path=SETTINGS_PATH):
    """The display address our installed hooks point at, or None."""
    try:
        text = json.dumps(_read_settings(path))
    except (OSError, ValueError):
        return None
    if MARKER not in text:
        return None
    for hooks in _read_settings(path).get("hooks", {}).values():
        for group in hooks:
            for h in group.get("hooks", []):
                cmd = h.get("command", "")
                if MARKER in cmd and "--host " in cmd:
                    return cmd.split("--host ", 1)[1].split()[0]
    return ""


# ---------------------------------------------------------------- main

def print_status():
    now = time.time()
    with locked():
        st = load()
    print(f"hooks installed: {installed() or 'no'}   (settings: {SETTINGS_PATH})")
    print(f"this machine is {'WORKING' if any_working(st, now) else 'idle'}; "
          f"last sent to the display: {st.get('sent', '-')}"
          + (f" ({now - st['sent_at']:.0f}s ago)" if st.get("sent_at") else ""))
    if st.get("error"):
        print(f"last send failed {now - st.get('fail_at', now):.0f}s ago: {st['error']}")
    if st.get("last"):
        last = st["last"]
        print(f"last event: {last['event']} {last['session']} ({now - last['at']:.0f}s ago)")
    for sid, s in sorted(st["sessions"].items(), key=lambda kv: -kv[1]["seen"]):
        tools = f", {len(s['tools'])} tool(s) running" if s["tools"] else ""
        print(f"  {sid[:8]}  {s['state']:<8} for {now - s['changed']:.0f}s, "
              f"last event {now - s['seen']:.0f}s ago{tools}")
        for line in [s.get("project"), s.get("waiting") or s.get("activity")] +                 [f"agent {a.get('label')}: {a.get('activity', '')}" for a in (s.get("agents") or {}).values()]:
            if line:
                print(f"            {line}")
    if st.get("history"):
        print("recent:")
        for at, text in st["history"][-20:]:
            print(f"  {time.strftime('%H:%M:%S', time.localtime(at))}  {text}")


def main():
    ap = argparse.ArgumentParser(description="Claude Code hooks for the usage display.")
    ap.add_argument("--host", default=os.environ.get("CLAUDE_DISPLAY_HOST", "claude-display.local"),
                    help="display IP or name, optionally :port (default claude-display.local)")
    ap.add_argument("--port", type=int, help="display port (default 8080)")
    action = ap.add_mutually_exclusive_group()
    action.add_argument("--install", action="store_true", help="add the hooks to ~/.claude/settings.json")
    action.add_argument("--uninstall", action="store_true", help="remove them")
    action.add_argument("--status", action="store_true", help="show what the hooks have seen")
    action.add_argument("--watch", action="store_true",
                        help="run the interrupt / long-tool watcher (the tray helper does this)")
    args = ap.parse_args()
    host, port = split_host(args.host, args.port)

    if args.install:
        print("Installed. Hook command:\n  " + install(host, port))
        print(f"Written to {SETTINGS_PATH} (previous version saved as settings.json.bak).")
    elif args.uninstall:
        uninstall()
        print(f"Removed the display hooks from {SETTINGS_PATH}.")
    elif args.status:
        print_status()
    elif args.watch:
        print(f"Watching for interrupts and long tool runs -> {host}:{port}. Ctrl-C to stop.")
        while True:
            watch_once(host, port)
            time.sleep(2)
    elif sys.stdin is None or sys.stdin.isatty():
        ap.print_help()
    else:  # called by Claude Code: one event on stdin. Never fail loudly.
        try:
            handle(json.load(sys.stdin), host, port)
        except Exception:
            pass


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
