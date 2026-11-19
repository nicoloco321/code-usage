// Claude display: Meet controls - in the Meet tab.
//
// Finds Meet's own call controls, tells background.js how the call is going
// (in one? muted? camera on? hand up?), and presses them when the display's
// buttons are tapped. It clicks the buttons Meet shows, so nothing else is
// touched. Meet changes its page now and then: the ways it finds them are
// all here, at the top.

(() => {
  const visible = (el) => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
  const label = (el) => (el.getAttribute("aria-label") || el.getAttribute("data-tooltip") || "").trim();
  const sleep = (ms) => new Promise((ok) => setTimeout(ok, ms));

  // The mic and camera carry data-is-muted, and their labels end in their
  // shortcut, "(ctrl + d)" or "(⌘ + e)", whatever the language.
  function toggle(key, word) {
    const els = [...document.querySelectorAll("[data-is-muted]")].filter(visible);
    const shortcut = new RegExp("\\+\\s*" + key + "\\s*\\)\\s*$", "i");
    const el = els.find((e) => shortcut.test(label(e))) || els.find((e) => label(e).toLowerCase().includes(word));
    if (!el) return null;
    const v = el.getAttribute("data-is-muted");
    return {el, off: v === "true" ? true : v === "false" ? false : null};
  }

  // The rest, checked against Meet in November 2026: by the shortcut their
  // label ends in (any language), then their label, then the icon in a
  // <button> (Meet's icons are ligatures, e.g. "call_end"), then their text.
  // Labels before icons: with a hand up, Meet's raised-hands panel has a
  // hand icon in it too. Its bar: "Raise hand (ctrl + alt + h)" / "Lower hand
  // (...)", "Captions and translation" (a menu: "Turn on captions", "Turn off
  // live captions"), "Share screen", "Chat with everyone", "Leave call", and
  // People, which has no label, just the word.
  const CONTROLS = {
    hand: {key: /\+\s*alt\s*\+\s*h\s*\)\s*$/i, words: /^(raise|lower) hand\b/i, icons: ["back_hand"]},
    chat: {words: /chat with everyone|^chat\b/i, icons: ["chat", "chat_bubble"]},
    people: {words: /^(show everyone|people|participants)\b/i, icons: ["people", "group"], text: /^people$/i},
    share: {words: /^(share screen|present now|present)\b/i, icons: ["computer_arrow_up", "present_to_all", "screen_share"]},
    unshare: {words: /^stop (presenting|sharing)/i, icons: ["cancel_presentation", "stop_screen_share"]},
    leave: {words: /^leave call/i, icons: ["call_end"]},
  };
  const all = () => [...document.querySelectorAll("button, [role='button']")].filter(visible);
  const iconsOf = (el) => [...el.querySelectorAll("i, span")].filter((i) => !i.children.length)
    .map((i) => i.textContent.trim());

  function find(what) {
    const c = CONTROLS[what], els = all();
    return (c.key && els.find((b) => c.key.test(label(b)))) || els.find((b) => c.words.test(label(b))) ||
      els.find((b) => b.tagName === "BUTTON" && iconsOf(b).some((i) => c.icons.includes(i))) ||
      (c.text && els.find((b) => c.text.test(b.textContent.trim()))) || null;
  }

  // Captions: a button that opens a menu (pressed while they're on), and the
  // switch in the menu. (Older Meet: a button that switches them itself.)
  const captionsButton = () => all().find((b) => b.hasAttribute("aria-pressed") && /caption/i.test(label(b)));
  const captionsSwitch = () => all().find((b) => /^turn (on|off)( live)? captions/i.test(label(b)));

  async function toggleCaptions() {
    let sw = captionsSwitch();
    const button = captionsButton();
    if (!sw && button) {
      const was = button.getAttribute("aria-pressed");
      button.click();
      await sleep(400);
      sw = captionsSwitch();
      if (!sw && button.getAttribute("aria-pressed") !== was) return;  // (it switched them itself)
    }
    if (!sw) return;
    sw.click();
    await sleep(300);
    const open = captionsButton();
    if (open && open.getAttribute("aria-expanded") === "true") open.click();  // (shut the menu again)
  }

  const handUp = (el) => /^lower hand\b/i.test(label(el)) || el.getAttribute("aria-pressed") === "true";

  // What the display shows. A control that's gone for a moment (the bar
  // hiding) keeps what it last said, rather than flickering to "not known".
  let last = {};
  function state() {
    const mic = toggle("d", "microphone"), cam = toggle("e", "camera");
    const leave = find("leave");
    const inMeeting = !!leave;
    const st = {
      in_meeting: inMeeting,
      muted: mic ? mic.off : inMeeting ? last.muted ?? null : null,
      video: cam ? (cam.off === null ? null : !cam.off) : inMeeting ? last.video ?? null : null,
      title: document.title.replace(/^(google )?meet\s*[-–:|]\s*/i, "").trim(),
    };
    if (inMeeting) {
      const hand = find("hand"), cc = captionsButton();
      st.hand = hand ? handUp(hand) : last.hand ?? null;
      st.captions = cc ? cc.getAttribute("aria-pressed") === "true" : last.captions ?? null;
      st.sharing = !!find("unshare");
    }
    last = inMeeting ? st : {};
    return st;
  }

  // Meet can hide its bar: a mouse move brings it back.
  function wake() {
    document.dispatchEvent(new MouseEvent("mousemove", {bubbles: true, clientX: innerWidth / 2, clientY: innerHeight - 40}));
  }

  async function run(cmd) {
    wake();
    await sleep(60);
    const a = cmd.action;
    if (a === "mute" || a === "video") {
      const t = a === "mute" ? toggle("d", "microphone") : toggle("e", "camera");
      if (!t) return;
      // "want" (push to talk): true = muted. Without it, just flip it.
      const want = a === "mute" && typeof cmd.want === "boolean" ? cmd.want : null;
      if (want === null || t.off !== want) t.el.click();
    } else if (a === "captions") {
      await toggleCaptions();
    } else if (a === "share") {
      (find("unshare") || find("share"))?.click();
    } else if (a === "leave") {
      find("leave")?.click();
      await sleep(400);  // a host gets asked: leave, or end the call for everyone? Leave.
      const just = [...document.querySelectorAll("button, [role='button']")].filter(visible)
        .find((b) => /^just leave|^leave call$/i.test(label(b) || b.textContent.trim()));
      just?.click();
    } else if (CONTROLS[a]) {
      find(a)?.click();
    }
  }

  // For checking these selectors against Meet as it is: what's on its bar
  // (the helper asks: py -3 windows\meetings.py --inspect-meet). Read only.
  function controls() {
    const els = [...document.querySelectorAll("button, [role='button'], [aria-label*='hand' i], [aria-label*='caption' i]")]
      .filter(visible);
    return [...new Set(els)].slice(0, 80).map((el) => ({
      tag: el.tagName.toLowerCase(), label: label(el), pressed: el.getAttribute("aria-pressed"),
      expanded: el.getAttribute("aria-expanded"), muted: el.getAttribute("data-is-muted"),
      jsname: el.getAttribute("jsname"),
      icons: [...el.querySelectorAll("i, span")].filter((i) => !i.children.length)
        .map((i) => i.textContent.trim()).filter((t) => t && t.length < 30).slice(0, 3),
    }));
  }

  chrome.runtime.onMessage.addListener((msg, sender, reply) => {
    if (msg && msg.type === "state?") {
      reply(state());  // (background.js asks every 2 s: see there)
      return;
    }
    if (msg && msg.type === "command" && msg.command && msg.command.action === "inspect") {
      reply(Object.assign(state(), {controls: controls()}));
      return;
    }
    if (msg && msg.type === "command") {
      run(msg.command).then(() => sleep(300)).then(() => reply(state()), () => reply(state()));
      return true;  // (replying later)
    }
  });

  const timer = setInterval(() => {
    try {
      chrome.runtime.sendMessage({type: "state", state: state()}).catch(() => {});
    } catch (e) {
      clearInterval(timer);  // the extension was reloaded: this copy of the script is done
    }
  }, 1500);
})();
