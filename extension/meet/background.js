// Claude display: Meet controls - the background half.
//
// meet.js, in each Meet tab, reports the call and presses Meet's buttons.
// This hands it the display's taps, from the helper on this computer (the
// tray app, listening on 127.0.0.1:47613), and hands the helper the call's
// state: whether you're in one, muted, on camera...

const HELPER = "http://127.0.0.1:47613";
const tabs = new Map();  // tab id -> {state, at}: what each Meet tab last said
let polling = false;
let sent = "", sentAt = 0;

chrome.runtime.onMessage.addListener((msg, sender) => {
  if (msg && msg.type === "state" && sender.tab) {
    tabs.set(sender.tab.id, {state: msg.state, at: Date.now()});
    report();
    poll();
  }
});
chrome.tabs.onRemoved.addListener((id) => {
  if (tabs.delete(id)) report(true);
});

// Ask each Meet tab how the call's going, every 2 s. The tab's own timer
// can't be relied on: Chrome slows a background tab's timers to once a
// minute, but answering a message is never held up. (The asking keeps this
// awake too; the alarm wakes it if it slept anyway.)
async function ask() {
  let found = [];
  try {
    found = await chrome.tabs.query({url: "https://meet.google.com/*"});
  } catch (e) {
    return;
  }
  for (const tab of found) {
    try {
      const state = await chrome.tabs.sendMessage(tab.id, {type: "state?"});
      if (state) tabs.set(tab.id, {state, at: Date.now()});
    } catch (e) {
      // No meet.js answering in it: the tab was open before the extension
      // was loaded (or reloaded). Put it in, rather than make you reload Meet.
      chrome.scripting.executeScript({target: {tabId: tab.id}, files: ["meet.js"]}).catch(() => {});
    }
  }
  for (const id of [...tabs.keys()]) {
    if (!found.some((t) => t.id === id)) tabs.delete(id);
  }
  report();
  if (tabs.size) poll();
}
setInterval(ask, 2000);
ask();
chrome.alarms.create("awake", {periodInMinutes: 0.5});
chrome.alarms.onAlarm.addListener(ask);

// The tab with the call: one in a meeting, the latest heard from.
function current() {
  let best = null;
  for (const [id, t] of tabs) {
    if (Date.now() - t.at > 10000) { tabs.delete(id); continue; }  // (closed, or asleep)
    const better = !best || (t.state.in_meeting && !best.t.state.in_meeting) ||
      (!!t.state.in_meeting === !!best.t.state.in_meeting && t.at > best.t.at);
    if (better) best = {id, t};
  }
  return best;
}

// The state to the helper: when it changes, and every 3 s so it knows we're here.
async function report(force) {
  const c = current();
  const text = JSON.stringify(c ? c.t.state : {in_meeting: false});
  if (!force && text === sent && Date.now() - sentAt < 3000) return;
  sent = text;
  sentAt = Date.now();
  try {
    await fetch(HELPER + "/meet/state", {method: "POST", body: text});  // (text/plain: no preflight)
  } catch (e) {
    // the helper isn't running: the next report tries again
  }
}

// The display's taps, for as long as there's a Meet tab.
async function poll() {
  if (polling) return;
  polling = true;
  try {
    while (tabs.size) {
      let reply;
      try {
        reply = await (await fetch(HELPER + "/meet/commands?wait=20")).json();
      } catch (e) {
        await new Promise((ok) => setTimeout(ok, 3000));  // the helper isn't there: try again soon
        continue;
      }
      for (const command of reply.commands || []) await run(command);
    }
  } finally {
    polling = false;
  }
}

async function run(command) {
  const c = current();
  if (!c) return;
  try {
    const state = await chrome.tabs.sendMessage(c.id, {type: "command", command});
    if (state) {
      tabs.set(c.id, {state, at: Date.now()});
      report(true);  // (the display shows the change straight away)
    }
  } catch (e) {
    // the tab went away mid-tap
  }
}
