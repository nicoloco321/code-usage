// Meeting controls for the display's Zoom, Microsoft Teams and Google Meet
// screens: the Mac half of windows/meetings.py, which does the same on Windows.
//
// The screens on the display are buttons; this presses them, on the Mac the
// call is on. For each meeting screen installed on the display it waits on
// GET /meeting/<app>/commands (the display answers the moment a button's
// tapped), carries the tap out, and posts what the call's doing - muted? on
// camera? - to POST /meeting/<app>/state, when it changes and every 10 s.
//
//   Zoom   its Meeting menu in the menu bar ("Mute audio", "Start video"...)
//          and its meeting window's own buttons, through the Accessibility
//          API: pressed where they are, so nothing comes forward, and their
//          names say whether you're muted. Failing that, Zoom's shortcuts
//          (⇧⌘A and friends), with Zoom in front just for that.
//   Teams  Teams' third-party app API (Settings > Privacy > Third-party app
//          API) when it's on: real state, reactions, blur. Otherwise the
//          meeting window's buttons through Accessibility, then its shortcuts.
//   Meet   the browser extension in ../extension/meet, which this serves on
//          http://127.0.0.1:47613: it presses Meet's buttons in its tab.
//
// Zoom and Teams need DeskSwitch allowed in System Settings › Privacy &
// Security › Accessibility (the menu offers it); Meet doesn't.
//
// Push to talk: the display says "on" when the button goes down, again every
// second while it's held, and "off" when it's let go. The mic opens on the
// first, and shuts on the last - or 2.5 s after the display last said it's
// held, so a lost "off" can't leave you talking.
//
// The display can't tell this from the Windows helper (but for "helper":
// "mac"), so change one, change the other. DeskSwitch runs it; on its own,
// for trying it out (from Terminal, which then needs the Accessibility access):
//
//   DeskSwitch.app/Contents/MacOS/DeskSwitch --meetings --host 192.168.1.42 -v
//   DeskSwitch.app/Contents/MacOS/DeskSwitch --dump zoom      (what Accessibility sees)
//   DeskSwitch.app/Contents/MacOS/DeskSwitch --inspect-meet   (what the extension sees)

import Cocoa
import ApplicationServices
import Network

typealias JSON = [String: Any]

let kMeetingApps: Set<String> = ["meet", "zoom", "teams"]
let kMeetExtensionPort: UInt16 = 47613   // the Meet extension's localhost line to this
private let kPollWait = 20                       // s: how long the display may hold a request for taps
private let kHeartbeat: TimeInterval = 10        // the state goes to the display at least this often
private let kLookShowing: TimeInterval = 1       // look at the call this often while its screen is showing
private let kLookIdle: TimeInterval = 4          // ...and otherwise (a meeting starting should still show up)
private let kPttGrace: TimeInterval = 2.5        // push to talk shuts this long after the display last said "held"
private let kExpectSecs: TimeInterval = 2        // what a tap just set outweighs the app's older word this long
private let kScreensEvery: TimeInterval = 30     // re-read which meeting screens the display has
let kNeedsAccess = "allow DeskSwitch in System Settings: Privacy & Security, Accessibility"

private func mono() -> TimeInterval { ProcessInfo.processInfo.systemUptime }

private func same(_ a: JSON?, _ b: JSON?) -> Bool {
    guard let a = a, let b = b else { return a == nil && b == nil }
    return NSDictionary(dictionary: a).isEqual(to: b)
}

private func rx(_ pattern: String) -> NSRegularExpression {
    try! NSRegularExpression(pattern: pattern, options: [.caseInsensitive])
}

extension NSRegularExpression {
    func hit(_ text: String) -> Bool {
        firstMatch(in: text, range: NSRange(text.startIndex..., in: text)) != nil
    }

    /// The groups of the first match (nil for one that took no part), or nil for no match.
    func capture(_ text: String) -> [String?]? {
        guard let m = firstMatch(in: text, range: NSRange(text.startIndex..., in: text)) else { return nil }
        return (1..<max(1, m.numberOfRanges)).map { Range(m.range(at: $0), in: text).map { String(text[$0]) } }
    }
}

/// Where the meeting controls' lines go: the system log in the app, the
/// terminal from the command line.
enum MeetingLog {
    static var console = false
    static var verbose = false
    private static let lock = NSLock()
    private static let stamp: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "HH:mm:ss.SSS"
        return f
    }()

    static func info(_ text: String) { emit(text) }
    static func debug(_ text: String) { if verbose { emit(text) } }

    private static func emit(_ text: String) {
        if console {
            lock.lock()
            print("\(stamp.string(from: Date())) \(Thread.current.name ?? "") \(text)")
            lock.unlock()
        } else {
            NSLog("meetings: %@", text)
        }
    }
}

// ---------------------------------------------------------------- Accessibility

/// A button (or menu item) in another app, and what it's called: its title,
/// description (a web page's aria-label) and help (its tooltip).
struct Control {
    let el: AXUIElement
    let role: String
    let names: [String]
    let enabled: Bool

    var label: String { names.joined(separator: " | ") }
    func matches(_ pattern: NSRegularExpression) -> Bool { names.contains(where: pattern.hit) }
}

/// The Accessibility API: other apps' windows, buttons and menus, by name,
/// and pressing them - the way VoiceOver does, so nothing moves.
enum AX {
    static func trusted(prompt: Bool = false) -> Bool {
        let key = kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String
        return AXIsProcessTrustedWithOptions([key: prompt] as CFDictionary)
    }

    static func get(_ el: AXUIElement, _ attr: String) -> CFTypeRef? {
        var value: CFTypeRef?
        return AXUIElementCopyAttributeValue(el, attr as CFString, &value) == .success ? value : nil
    }

    static func string(_ el: AXUIElement, _ attr: String) -> String { get(el, attr) as? String ?? "" }
    static func bool(_ el: AXUIElement, _ attr: String) -> Bool? { (get(el, attr) as? NSNumber)?.boolValue }
    static func title(_ el: AXUIElement) -> String { string(el, kAXTitleAttribute) }

    static func element(_ el: AXUIElement, _ attr: String) -> AXUIElement? {
        guard let value = get(el, attr), CFGetTypeID(value) == AXUIElementGetTypeID() else { return nil }
        return (value as! AXUIElement)
    }

    static func elements(_ el: AXUIElement, _ attr: String) -> [AXUIElement] {
        get(el, attr) as? [AXUIElement] ?? []
    }

    static func windows(_ app: AXUIElement) -> [AXUIElement] { elements(app, kAXWindowsAttribute) }

    @discardableResult
    static func set(_ el: AXUIElement, _ attr: String, _ value: Bool) -> Bool {
        AXUIElementSetAttributeValue(el, attr as CFString, (value ? kCFBooleanTrue : kCFBooleanFalse) as CFTypeRef)
            == .success
    }

    static func press(_ el: AXUIElement) -> Bool {
        AXUIElementPerformAction(el, kAXPressAction as CFString) == .success
    }

    /// No app may hold a call up for long: a hung one costs a second and a half, not six.
    static func shortTimeouts() {
        AXUIElementSetMessagingTimeout(AXUIElementCreateSystemWide(), 1.5)
    }

    struct Node {
        var role = ""
        var names: [String] = []
        var enabled = true
        var children: [AXUIElement] = []
    }

    private static let fields = [kAXRoleAttribute, kAXTitleAttribute, kAXDescriptionAttribute, kAXHelpAttribute,
                                 kAXEnabledAttribute, kAXChildrenAttribute] as CFArray
    private static let fieldsNoChildren = [kAXRoleAttribute, kAXTitleAttribute, kAXDescriptionAttribute,
                                           kAXHelpAttribute, kAXEnabledAttribute] as CFArray

    /// An element's role, names and children, in one round trip to its app.
    static func node(_ el: AXUIElement, children: Bool = true) -> Node {
        var node = Node()
        var out: CFArray?
        guard AXUIElementCopyMultipleAttributeValues(el, children ? fields : fieldsNoChildren, [], &out) == .success,
              let values = out as? [AnyObject], values.count >= 5 else { return node }
        func value(_ i: Int) -> AnyObject? {
            let v = values[i]   // (a missing one comes back as an error value)
            if CFGetTypeID(v) == AXValueGetTypeID(), AXValueGetType(v as! AXValue) == .axError { return nil }
            return v
        }
        node.role = value(0) as? String ?? ""
        var names: [String] = []
        for i in 1...3 {
            if let s = (value(i) as? String)?.trimmingCharacters(in: .whitespacesAndNewlines), !s.isEmpty,
               !names.contains(s) {
                names.append(s)
            }
        }
        node.names = names
        node.enabled = (value(4) as? NSNumber)?.boolValue ?? true
        if children, values.count >= 6 { node.children = value(5) as? [AXUIElement] ?? [] }
        return node
    }

    static let pressable: Set<String> = ["AXButton", "AXCheckBox", "AXMenuButton", "AXPopUpButton", "AXRadioButton",
                                         "AXMenuItem"]

    /// The buttons in a window, with their names. Web content (Teams is a web
    /// page inside) can hold thousands of elements, so a web area is asked to
    /// search itself, the way VoiceOver does; elsewhere it's a walk, within limits.
    static func controls(in root: AXUIElement, limit: Int = 3000) -> [Control] {
        var out: [Control] = []
        var queue = [root], i = 0
        while i < queue.count && i < limit {
            let el = queue[i]
            i += 1
            let n = node(el)
            if pressable.contains(n.role) {
                if !n.names.isEmpty { out.append(Control(el: el, role: n.role, names: n.names, enabled: n.enabled)) }
                continue
            }
            if n.role == "AXWebArea", let found = search(el) {
                for b in found.prefix(limit) {
                    let m = node(b, children: false)
                    if !m.names.isEmpty { out.append(Control(el: b, role: m.role, names: m.names, enabled: m.enabled)) }
                }
                continue
            }
            queue.append(contentsOf: n.children)
        }
        return out
    }

    /// A web area's buttons and checkboxes, found by the web engine itself.
    private static func search(_ webArea: AXUIElement) -> [AXUIElement]? {
        let query: [String: Any] = ["AXSearchKey": ["AXButtonSearchKey", "AXCheckBoxSearchKey"],
                                    "AXResultsLimit": 2000, "AXDirection": "AXDirectionNext",
                                    "AXImmediateDescendantsOnly": false, "AXVisibleOnly": false]
        var value: CFTypeRef?
        guard AXUIElementCopyParameterizedAttributeValue(webArea, "AXUIElementsForSearchPredicate" as CFString,
                                                         query as CFDictionary, &value) == .success
        else { return nil }
        return value as? [AXUIElement]
    }

    /// An app's menu items in the menu bar: those of the menus with a title
    /// like `titled`, or of all its own menus if there's none. Read without
    /// opening anything, and pressed the same way.
    static func menuItems(_ app: AXUIElement, titled: NSRegularExpression) -> [Control] {
        guard let bar = element(app, kAXMenuBarAttribute) else { return [] }
        let tops = elements(bar, kAXChildrenAttribute)
        var menus = tops.filter { titled.hit(title($0)) }
        if menus.isEmpty { menus = Array(tops.dropFirst(2)) }   // (not the Apple menu, or the app's own)
        var out: [Control] = []
        func collect(_ holder: AXUIElement, depth: Int) {
            for menu in elements(holder, kAXChildrenAttribute) {   // the AXMenu under a menu title, or a submenu's
                for item in elements(menu, kAXChildrenAttribute) {
                    let n = node(item)
                    if n.role == "AXMenuItem", !n.names.isEmpty {
                        out.append(Control(el: item, role: n.role, names: n.names, enabled: n.enabled))
                    }
                    if depth < 2, !n.children.isEmpty { collect(item, depth: depth + 1) }
                }
            }
        }
        for top in menus { collect(top, depth: 0) }
        return out
    }

    static func frontmostPid() -> pid_t? {
        guard let app = element(AXUIElementCreateSystemWide(), kAXFocusedApplicationAttribute) else { return nil }
        var pid: pid_t = 0
        return AXUIElementGetPid(app, &pid) == .success ? pid : nil
    }
}

// ---------------------------------------------------------------- the keyboard

/// Shortcuts, for when there's no button to press. The app has to be in
/// front to take them, so it comes forward for a moment.
enum Keys {
    private static let codes: [String: CGKeyCode] = [
        "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9, "b": 11, "q": 12, "w": 13,
        "e": 14, "r": 15, "y": 16, "t": 17, "o": 31, "u": 32, "i": 34, "p": 35, "l": 37, "j": 38, "k": 40, "n": 45,
        "m": 46, "return": 36, "space": 49, "esc": 53,
    ]
    private static let modifiers: [String: CGEventFlags] = [
        "cmd": .maskCommand, "shift": .maskShift, "alt": .maskAlternate, "ctrl": .maskControl,
    ]

    /// Press keys together and let them go, e.g. "cmd+shift+a", "alt+y".
    static func press(_ combo: String) {
        var flags: CGEventFlags = []
        var key: CGKeyCode?
        for part in combo.lowercased().split(separator: "+").map(String.init) {
            if let m = modifiers[part] { flags.insert(m) } else { key = codes[part] }
        }
        guard let key = key, let source = CGEventSource(stateID: .hidSystemState) else { return }
        for down in [true, false] {
            let event = CGEvent(keyboardEventSource: source, virtualKey: key, keyDown: down)
            event?.flags = flags
            event?.post(tap: .cghidEventTap)
            usleep(10_000)
        }
    }

    /// A shortcut to an app that only takes it in front: in front (its
    /// meeting window raised) just for that, then back to what you were in.
    /// Nothing is sent unless it really came forward: Zoom's ⌘W in the wrong
    /// app would close your window.
    static func send(_ combo: String, pid: pid_t, window: AXUIElement?) -> Bool {
        let app = AXUIElementCreateApplication(pid)
        let before = AX.frontmostPid()
        AX.set(app, kAXFrontmostAttribute, true)
        if let window = window {
            if AX.bool(window, kAXMinimizedAttribute) == true { AX.set(window, kAXMinimizedAttribute, false) }
            AX.set(window, kAXMainAttribute, true)
            _ = AXUIElementPerformAction(window, kAXRaiseAction as CFString)
        }
        var tries = 0
        while AX.frontmostPid() != pid && tries < 25 {
            usleep(20_000)
            tries += 1
        }
        guard AX.frontmostPid() == pid else {
            MeetingLog.info("couldn't bring the meeting forward for \(combo)")
            return false
        }
        usleep(50_000)
        press(combo)
        usleep(50_000)
        if let before = before, before != pid {
            AX.set(AXUIElementCreateApplication(before), kAXFrontmostAttribute, true)
        }
        return true
    }
}

// ---------------------------------------------------------------- the apps

/// One meeting app: what it's doing (look()) and doing what's tapped (act()).
/// Both run on its relay's own thread.
class MeetingApp {
    var name: String { "" }
    var via = ""
    var onChange: (() -> Void)?   // the relay's: the app says it's changed, so look now

    func setup() {}

    /// The call's state: ["available", "in_meeting", "muted", ...] (missing: not known).
    func look() -> JSON { ["available": false, "in_meeting": false] }

    /// Carry out a tap. Returns a note when it can't.
    func act(_ action: String, _ cmd: JSON, _ state: JSON) -> String? { nil }

    func setMuted(_ want: Bool, _ state: JSON) {
        if (state["muted"] as? Bool) != want { _ = act("mute", [:], state) }
    }

    func close() {}
}

/// An app whose meeting window has named buttons (Zoom, Teams without its
/// API): Accessibility reads and presses them, its shortcuts are the fallback.
class ButtonApp: MeetingApp {
    let bundleIDs: [String]
    let patterns: [String: [NSRegularExpression]]   // action -> the button that does it, best first
    let states: [String: (on: NSRegularExpression, off: NSRegularExpression)]
    let shortcuts: [String: String]                  // action -> keys, for when there's no button to press
    var leaveConfirm: NSRegularExpression? { nil }   // the "Leave meeting" button a leave asks about
    var carry: [String] { [] }   // state keys whose buttons are in a menu, so only seen while it's open
    let sticky: TimeInterval = 20   // a window just seen to be the call still is, while a menu hides its buttons

    var pid: pid_t = 0
    var window: AXUIElement?
    var inMeeting = false
    var controls: [Control] = []
    var last: (window: AXUIElement, st: JSON, at: TimeInterval)?

    init(bundleIDs: [String], patterns: [String: [String]], states: [String: (String, String)],
         shortcuts: [String: String]) {
        self.bundleIDs = bundleIDs
        self.patterns = patterns.mapValues { $0.map(rx) }
        self.states = states.mapValues { (on: rx($0.0), off: rx($0.1)) }
        self.shortcuts = shortcuts
    }

    override func setup() { AX.shortTimeouts() }

    func runningApp() -> NSRunningApplication? {
        for id in bundleIDs {
            if let app = NSRunningApplication.runningApplications(withBundleIdentifier: id).first(where: { !$0.isTerminated }) {
                return app
            }
        }
        return nil
    }

    /// Anything to do to the app before reading it (Teams: see there).
    func prepare(_ app: AXUIElement) {}

    /// Controls that aren't in a window: Zoom's Meeting menu.
    func appControls(_ app: AXUIElement) -> [Control] { [] }

    /// Is this window worth looking through for the call?
    func wantsWindow(_ window: AXUIElement, title: String) -> Bool { true }

    /// Is this window the call? By default: it has a mute button.
    func isMeeting(_ window: AXUIElement, title: String, _ own: [Control]) -> Bool { find("mute", in: own) != nil }

    /// Fill in what the buttons didn't say.
    func moreState(_ st: inout JSON) {}

    func title(_ window: AXUIElement) -> String { AX.title(window) }

    func find(_ action: String, in list: [Control]? = nil) -> Control? {
        let list = list ?? controls
        for pattern in patterns[action] ?? [] {
            let hits = list.filter { $0.matches(pattern) }
            if let hit = hits.first(where: \.enabled) ?? hits.first { return hit }
        }
        return nil
    }

    override func look() -> JSON {
        window = nil
        inMeeting = false
        controls = []
        guard let running = runningApp() else { return ["available": false, "in_meeting": false] }
        pid = running.processIdentifier
        guard AX.trusted() else {
            via = ""
            return ["available": true, "in_meeting": false, "note": kNeedsAccess]
        }
        let app = AXUIElementCreateApplication(pid)
        prepare(app)
        let shared = appControls(app)
        var wins = AX.windows(app)
        if let lw = last?.window, let i = wins.firstIndex(where: { CFEqual($0, lw) }) {
            wins.insert(wins.remove(at: i), at: 0)   // (the call's window, first)
        }
        for w in wins {
            let t = AX.title(w)
            guard wantsWindow(w, title: t) else { continue }
            let own = AX.controls(in: w)
            if isMeeting(w, title: t, own) {
                (window, controls, inMeeting) = (w, own + shared, true)
                break
            }
        }
        if !inMeeting, find("mute", in: shared.filter(\.enabled)) != nil {
            (controls, inMeeting) = (shared, true)   // (Zoom's menu says it's a call, wherever its window is)
        }

        var st: JSON
        var fresh = true
        if !inMeeting, let l = last, mono() - l.at < sticky, wins.contains(where: { CFEqual($0, l.window) }) {
            // (Teams hides the rest of its bar from Accessibility while a
            // menu of it is open: the call hasn't gone anywhere)
            (window, inMeeting, fresh) = (l.window, true, false)
            controls = AX.controls(in: l.window) + shared
            st = l.st
        } else if !inMeeting {
            via = ""
            return ["available": true, "in_meeting": false]
        } else {
            st = ["available": true, "in_meeting": true, "title": window.map(title) ?? ""]
        }
        var seen = Set<String>()
        for (key, s) in states {
            if controls.contains(where: { $0.matches(s.on) }) {
                st[key] = true
            } else if controls.contains(where: { $0.matches(s.off) }) {
                st[key] = false
            } else {
                continue
            }
            seen.insert(key)
        }
        if let l = last, let w = window, CFEqual(l.window, w) {
            for key in carry where !seen.contains(key) { st[key] = l.st[key] }   // (what it last said)
        }
        moreState(&st)
        if fresh, let w = window { last = (w, st, mono()) }
        via = controls.isEmpty ? "keyboard" : "Accessibility"
        return st
    }

    override func act(_ action: String, _ cmd: JSON, _ state: JSON) -> String? {
        guard inMeeting else { return "not in a \(name) meeting" }
        if let control = find(action), AX.press(control.el) {
            if action == "leave" { confirmLeave() }
            return nil
        }
        if let keys = shortcuts[action] {
            guard Keys.send(keys, pid: pid, window: window) else { return "couldn't bring \(name) forward" }
            if action == "leave" { confirmLeave() }
            return nil
        }
        return "\(name) has no \(action) button showing"
    }

    /// Leaving asks "leave, or end it for everyone?": leave, never end.
    func confirmLeave() {
        guard let confirm = leaveConfirm else { return }
        let app = AXUIElementCreateApplication(pid)
        for _ in 0..<15 {
            usleep(100_000)
            for w in AX.windows(app) {
                if let button = AX.controls(in: w, limit: 800).first(where: { $0.matches(confirm) }) {
                    _ = AX.press(button.el)
                    return
                }
            }
        }
    }
}

/// A name may go on after a comma or in brackets, ", ⇧⌘A" or "(⇧⌘A)".
private let tail = #"(\s*[,(].*)?$"#

/// Zoom (checked against Zoom Workplace 7.1.9's own words for them). Its
/// Meeting menu has "Mute audio" / "Unmute audio", "Start video" / "Stop
/// video", "Start share" / "Stop share", "Raise hand" / "Lower hand",
/// "Start recording", "Show chat", "Participants" and "Leave meeting", there
/// in the menu bar whether or not its toolbar's hiding; the toolbar's buttons
/// say the same. Leaving picks "Leave meeting", never "End meeting for all".
final class ZoomApp: ButtonApp {
    override var name: String { "Zoom" }
    private let meetingTitle = rx(#"^zoom (meeting|webinar)\b"#)
    private let mainTitle = rx(#"^zoom( workplace)?( - .*)?$"#)
    private let meetingMenu = rx(#"^meetings?$"#)
    override var leaveConfirm: NSRegularExpression? { rx(#"^leave (meeting|webinar)$"#) }

    init() {
        let record = #"^(record|start recording)( to the cloud| on this computer| to computer)?"# + tail
        super.init(
            bundleIDs: ["us.zoom.xos"],
            patterns: [
                "mute": [#"^(un)?mute( my)?( audio)?"# + tail],
                "video": [#"^(start|stop)( my)? video"# + tail],
                "share": [#"^stop shar(e|ing)"# + tail, #"^(start )?share( screen)?"# + tail],
                "hand": [#"^(raise|lower) hand(?!, submenu)"# + tail],
                "chat": [#"^((show|hide|open|close) )?chat"# + tail],
                "people": [#"^((show|hide|manage|open|close) )?participants"# + tail],
                "record": [#"^stop recording"# + tail, record],
                "leave": [#"^leave( meeting| webinar)?"# + tail, #"^end"# + tail],
            ],
            states: [
                "muted": (#"^unmute( my)?( audio)?"# + tail + #"|currently muted"#,
                          #"^mute( my)?( audio)?"# + tail + #"|currently unmuted"#),
                "video": (#"^stop( my)? video"# + tail, #"^start( my)? video"# + tail),
                "sharing": (#"^stop shar(e|ing)"# + tail, #"^(start )?share( screen)?"# + tail),
                "hand": (#"^lower hand"# + tail, #"^raise hand(?!, submenu)"# + tail),
                "recording": (#"^((stop|pause|resume) recording|recording)"# + tail, record),
            ],
            shortcuts: ["mute": "cmd+shift+a", "video": "cmd+shift+v", "share": "cmd+shift+s", "hand": "alt+y",
                        "chat": "cmd+shift+h", "people": "cmd+u", "record": "cmd+shift+r", "leave": "cmd+w"])
    }

    override func appControls(_ app: AXUIElement) -> [Control] { AX.menuItems(app, titled: meetingMenu) }

    // (its main window, with Team Chat and the rest, is never the call)
    override func wantsWindow(_ window: AXUIElement, title: String) -> Bool { !mainTitle.hit(title) }

    override func isMeeting(_ window: AXUIElement, title: String, _ own: [Control]) -> Bool {
        meetingTitle.hit(title) || super.isMeeting(window, title: title, own)
    }

    override func title(_ window: AXUIElement) -> String { "" }   // (its window just says "Zoom Meeting")
}

/// Teams without its API: the meeting window's buttons (it's a web page
/// inside, which Accessibility can see into), then its shortcuts. The bar has
/// "Mute mic" / "Unmute mic", "Turn camera on" / "Turn camera off", "Share",
/// "React", "Chat", "People", "Record", "More" and "Leave"; React opens
/// "Like", "Love", "Applause", "Laugh", "Surprised" and "Raise". While a menu's
/// open, the rest of the bar may be hidden (see ButtonApp.sticky).
final class TeamsButtons: ButtonApp {
    override var name: String { "Teams" }
    override var carry: [String] { ["hand"] }
    static let reactions = ["like": "Like", "love": "Love", "applause": "Applause", "laugh": "Laugh", "wow": "Surprised"]
    private let menuOpen = rx(#"^(like|love|applause|laugh|surprised|raise|lower)$"#)   // React's menu, open
    private let handTip = rx(#"your hand is (not )?raised"#)   // React's tooltip
    private var opened = Set<pid_t>()

    init() {
        super.init(
            bundleIDs: ["com.microsoft.teams2", "com.microsoft.teams"],
            patterns: [
                "mute": [#"^(mute|unmute)( mic(rophone)?)?\b"#],
                "video": [#"^turn camera (on|off)\b|^(start|stop) video\b"#],
                "share": [#"^stop sharing"#, #"^share\b"#],
                "chat": [#"^chat\b"#], "people": [#"^people\b"#], "react": [#"^react\b"#],
                "leave": [#"^leave\b|^hang up"#],
            ],
            states: [
                "muted": (#"^unmute\b"#, #"^mute\b"#),
                "video": (#"^turn camera off|^stop video"#, #"^turn camera on|^start video"#),
                "hand": (#"^lower( your| my)?( hand)?$"#, #"^raise( your| my)?( hand)?$"#),
                "sharing": (#"^stop sharing"#, #"^share\b"#),
            ],
            shortcuts: ["mute": "cmd+shift+m", "video": "cmd+shift+o", "share": "cmd+shift+e", "hand": "cmd+shift+k",
                        "blur": "cmd+shift+p", "leave": "cmd+shift+h"])
    }

    // A Chromium-based Teams only shows its page to Accessibility once asked.
    override func prepare(_ app: AXUIElement) {
        if opened.insert(pid).inserted { AX.set(app, "AXManualAccessibility", true) }
    }

    override func wantsWindow(_ window: AXUIElement, title: String) -> Bool { !title.isEmpty }

    // (with React's menu open, that menu is all there may be of the bar)
    override func isMeeting(_ window: AXUIElement, title: String, _ own: [Control]) -> Bool {
        super.isMeeting(window, title: title, own) || own.contains { $0.matches(menuOpen) }
    }

    override func moreState(_ st: inout JSON) {
        for c in controls {
            for n in c.names {
                if let said = handTip.capture(n) {
                    st["hand"] = (said.first ?? nil) == nil   // ("not raised": down)
                    return
                }
            }
        }
    }

    override func title(_ window: AXUIElement) -> String {
        let t = AX.title(window)
        return t.replacingOccurrences(of: #"\s*\|\s*Microsoft Teams.*$"#, with: "", options: .regularExpression)
            .trimmingCharacters(in: .whitespaces)
    }

    // Raise hand and the reactions are in React's menu: open it, press one.
    override func act(_ action: String, _ cmd: JSON, _ state: JSON) -> String? {
        if action == "hand" || action == "react", inMeeting, window != nil {
            let target = action == "hand" ? rx(#"^(raise|lower)( your| my)?( hand)?$"#) :
                rx("^" + NSRegularExpression.escapedPattern(
                    for: Self.reactions[cmd["type"] as? String ?? ""] ?? "Like") + "$")
            if fromMenu("react", target) {
                if action == "hand" { last?.st["hand"] = !(state["hand"] as? Bool ?? false) }   // (only in the menu: keep it)
                return nil
            }
        }
        return super.act(action, cmd, state)
    }

    /// Open the menu `opener` opens, press `target` in it, and see it shut.
    private func fromMenu(_ opener: String, _ target: NSRegularExpression) -> Bool {
        guard let w = window, let button = find(opener), AX.press(button.el) else { return false }
        for _ in 0..<15 {
            usleep(100_000)
            if let hit = AX.controls(in: w).first(where: { $0.matches(target) }) {
                _ = AX.press(hit.el)
                usleep(300_000)
                shut()
                return true
            }
        }
        shut()
        return false
    }

    /// Close React's menu if it's still open. It hides the button that opened
    /// it, so it's Escape (Teams comes forward for a moment).
    private func shut() {
        guard let w = window, AX.controls(in: w).contains(where: { $0.matches(menuOpen) }) else { return }
        _ = Keys.send("esc", pid: pid, window: w)
    }
}

/// Teams' third-party app API: a WebSocket on 127.0.0.1:8124 that Teams opens
/// when Settings > Privacy > Third-party app API is on. The first command
/// after connecting makes Teams ask you to allow this app; it then sends a
/// token, kept in DeskSwitch's preferences, for next time.
final class TeamsAPI: NSObject, URLSessionWebSocketDelegate, @unchecked Sendable {
    static let port = 8124
    private static let tokenKey = "TeamsAPIToken"
    private static let actions: [String: (String, String?)] = [
        "mute": ("toggle-mute", nil), "video": ("toggle-video", nil), "hand": ("toggle-hand", nil),
        "blur": ("toggle-background-blur", nil), "leave": ("leave-call", nil), "chat": ("toggle-ui", "chat"),
        "share": ("toggle-ui", "share-tray"), "react": ("send-reaction", nil),
    ]

    private let lock = NSLock()
    private var task: URLSessionWebSocketTask?
    private var meeting: JSON?   // the latest meetingUpdate's meetingState
    private var can: JSON = [:]
    private var request = 0
    private var tried: TimeInterval = -100
    private var _problem: String?   // why it isn't working, in words
    private lazy var session = URLSession(configuration: .ephemeral, delegate: self, delegateQueue: nil)

    var problem: String? {
        lock.lock()
        defer { lock.unlock() }
        return _problem
    }

    /// Open the socket if Teams is listening (tried every 10 s at most).
    func connect() {
        lock.lock()
        guard task == nil, mono() - tried >= 10 else {
            lock.unlock()
            return
        }
        tried = mono()
        var url = URLComponents(string: "ws://127.0.0.1:\(Self.port)/")!
        url.queryItems = [
            URLQueryItem(name: "token", value: UserDefaults.standard.string(forKey: Self.tokenKey) ?? ""),
            URLQueryItem(name: "protocol-version", value: "2.0.0"),
            URLQueryItem(name: "manufacturer", value: "Claude display"),
            URLQueryItem(name: "device", value: "Screen Market screen"),
            URLQueryItem(name: "app", value: "claude-display"),
            URLQueryItem(name: "app-version", value: "1.0"),
        ]
        let t = session.webSocketTask(with: url.url!)
        task = t
        lock.unlock()
        t.resume()
        listen(t)
        _ = send("query-state")
    }

    func urlSession(_ session: URLSession, webSocketTask: URLSessionWebSocketTask,
                    didOpenWithProtocol proto: String?) {
        lock.lock()
        _problem = nil
        lock.unlock()
        MeetingLog.debug("Teams API: connected")
    }

    private func listen(_ t: URLSessionWebSocketTask) {
        t.receive { [weak self] result in
            guard let self = self else { return }
            switch result {
            case .success(let message):
                switch message {
                case .string(let text): self.got(text)
                case .data(let data): self.got(String(decoding: data, as: UTF8.self))
                @unknown default: break
                }
                self.listen(t)
            case .failure(let error):
                self.closed(t, error)
            }
        }
    }

    private func got(_ text: String) {
        guard let msg = (try? JSONSerialization.jsonObject(with: Data(text.utf8))) as? JSON else { return }
        if let token = msg["tokenRefresh"] as? String, !token.isEmpty {
            UserDefaults.standard.set(token, forKey: Self.tokenKey)
            MeetingLog.info("Teams API: paired")
        }
        if let update = msg["meetingUpdate"] as? JSON {
            lock.lock()
            meeting = update["meetingState"] as? JSON ?? [:]
            can = update["meetingPermissions"] as? JSON ?? [:]
            lock.unlock()
        }
        if let error = msg["errorMsg"] {
            MeetingLog.info("Teams API: \(error)")
        }
    }

    private func closed(_ t: URLSessionWebSocketTask, _ error: Error) {
        let refused = (error as? URLError)?.code == .cannotConnectToHost
        lock.lock()
        if task === t {
            task = nil
            meeting = nil
        }
        _problem = refused ? "turn on Teams' third-party app API (Settings, Privacy) for its full controls"
            : "Teams API: \(error.localizedDescription)"
        lock.unlock()
        t.cancel(with: .goingAway, reason: nil)
        if !refused { MeetingLog.info("Teams API closed: \(error.localizedDescription)") }
    }

    @discardableResult
    private func send(_ action: String, _ kind: String? = nil) -> Bool {
        lock.lock()
        let t = task
        request += 1
        let msg: JSON = ["action": action, "parameters": kind.map { ["type": $0] } ?? [:], "requestId": request]
        lock.unlock()
        guard let t = t, let data = try? JSONSerialization.data(withJSONObject: msg) else { return false }
        t.send(.string(String(decoding: data, as: UTF8.self))) { error in
            if let error = error { MeetingLog.debug("Teams API send: \(error.localizedDescription)") }
        }
        return true
    }

    func look() -> JSON? {
        lock.lock()
        let m = meeting, can = self.can
        lock.unlock()
        guard let m = m else { return nil }
        var st: JSON = ["available": true, "in_meeting": m["isInMeeting"] as? Bool ?? false]
        if st["in_meeting"] as? Bool == true {
            let keys = ["muted": "isMuted", "video": "isVideoOn", "hand": "isHandRaised", "sharing": "isSharing",
                        "recording": "isRecordingOn", "blur": "isBackgroundBlurred"]
            for (ours, theirs) in keys { st[ours] = m[theirs] }
            let perms = ["mute": "canToggleMute", "video": "canToggleVideo", "hand": "canToggleHand",
                         "blur": "canToggleBlur", "leave": "canLeave", "react": "canReact",
                         "chat": "canToggleChat", "share": "canToggleShareTray"]
            st["can"] = perms.mapValues { can[$0] as? Bool ?? true }
        }
        return st
    }

    func act(_ action: String, _ cmd: JSON) -> Bool {
        guard let entry = Self.actions[action] else { return false }
        let (what, kind) = entry
        lock.lock()
        let sharing = meeting?["isSharing"] as? Bool == true
        lock.unlock()
        if action == "share" && sharing { return send("stop-sharing") }
        if action == "react" { return send("send-reaction", cmd["type"] as? String ?? "like") }
        return send(what, kind)
    }

    func close() {
        lock.lock()
        let t = task
        task = nil
        lock.unlock()
        t?.cancel(with: .goingAway, reason: nil)
        session.invalidateAndCancel()
    }
}

/// Teams: its API when it's on, the meeting window's buttons when not.
final class TeamsApp: MeetingApp {
    override var name: String { "Teams" }
    private let api = TeamsAPI()
    private let buttons = TeamsButtons()

    override func setup() { buttons.setup() }

    override func look() -> JSON {
        api.connect()
        if let st = api.look() {
            via = "Teams API"
            return st
        }
        var st = buttons.look()
        via = buttons.via
        if st["in_meeting"] as? Bool == true, via == "keyboard", let problem = api.problem {
            st["note"] = problem   // (with its buttons to press, it's fine without the API)
        }
        return st
    }

    override func act(_ action: String, _ cmd: JSON, _ state: JSON) -> String? {
        if api.look() != nil && api.act(action, cmd) { return nil }
        return buttons.act(action, cmd, state)
    }

    override func close() { api.close() }
}

/// Google Meet: the browser extension does the pressing. This keeps its taps
/// for it, and its word on the call, on 127.0.0.1:47613.
final class MeetApp: MeetingApp {
    override var name: String { "Google Meet" }
    private static let fresh: TimeInterval = 15   // the extension's word is this old: it's gone (it says every 3 s)
    private let cond = NSCondition()
    private var pending: [JSON] = []
    private var state: JSON?
    private var stateAt: TimeInterval = 0
    private var inspected: Any?
    private var server: MeetServer?

    override init() {
        super.init()
        via = "browser extension"
    }

    override func setup() {
        server = MeetServer(meet: self)
        server?.start()
    }

    func gotState(_ data: JSON) {
        var data = data
        cond.lock()
        if let controls = data.removeValue(forKey: "controls") { inspected = controls }   // (an inspect's answer)
        let changed = !same(data, state)
        state = data
        stateAt = mono()
        cond.broadcast()
        cond.unlock()
        if changed { onChange?() }
    }

    /// What's on Meet's bar, as the extension sees it: for checking its
    /// selectors against Meet as it is now.
    func inspect() -> JSON {
        cond.lock()
        defer { cond.unlock() }
        inspected = nil
        pending.append(["action": "inspect"])
        cond.broadcast()
        let until = Date(timeIntervalSinceNow: 8)
        while inspected == nil && cond.wait(until: until) {}
        return ["state": state ?? NSNull(), "controls": inspected ?? NSNull()]
    }

    /// The extension asking for taps: whatever's waiting, or [] after `wait`.
    func take(_ wait: TimeInterval) -> [JSON] {
        cond.lock()
        defer { cond.unlock() }
        let until = Date(timeIntervalSinceNow: wait)
        while pending.isEmpty && cond.wait(until: until) {}
        let out = pending
        pending = []
        if !out.isEmpty { cond.broadcast() }   // (close may be waiting for them to go)
        return out
    }

    override func look() -> JSON {
        cond.lock()
        let st = state, fresh = mono() - stateAt < Self.fresh
        cond.unlock()
        guard fresh, let st = st else {
            return ["available": false, "in_meeting": false,
                    "note": "open Meet in a browser with the Claude display extension"]
        }
        var out: JSON = ["available": true]
        for key in ["in_meeting", "muted", "video", "hand", "sharing", "captions", "title"] { out[key] = st[key] }
        return out
    }

    override func act(_ action: String, _ cmd: JSON, _ state: JSON) -> String? {
        cond.lock()
        pending.append(["action": action, "on": cmd["on"] ?? NSNull(), "type": cmd["type"] ?? NSNull(),
                        "want": cmd["want"] ?? NSNull()])
        cond.broadcast()
        cond.unlock()
        return nil
    }

    override func setMuted(_ want: Bool, _ state: JSON) {
        _ = act("mute", ["want": want], state)   // (the extension knows the state best: it says which way)
    }

    /// Quitting: a tap still waiting for the extension (push to talk's mute,
    /// most likely) gets a second to be collected first.
    override func close() {
        cond.lock()
        let until = Date(timeIntervalSinceNow: 1)
        while !pending.isEmpty && cond.wait(until: until) {}
        let collected = pending.isEmpty
        cond.unlock()
        if collected { usleep(100_000) }   // (and for the reply carrying it to go out)
        server?.stop()
    }
}

/// The Meet extension's line to this: a little HTTP server on 127.0.0.1 only.
/// The extension long-polls GET /meet/commands for taps and posts the call to
/// POST /meet/state; GET /meet says who's here. Only the extension (or a
/// program on this Mac) may use it, not a web page.
private final class MeetServer {
    private let meet: MeetApp
    private let queue = DispatchQueue(label: "DeskSwitch.meet")
    private var listener: NWListener?

    init(meet: MeetApp) { self.meet = meet }

    func start() {
        let params = NWParameters.tcp
        params.requiredLocalEndpoint = .hostPort(host: "127.0.0.1", port: NWEndpoint.Port(rawValue: kMeetExtensionPort)!)
        params.allowLocalEndpointReuse = true
        do {
            let l = try NWListener(using: params)
            l.newConnectionHandler = { [weak self] c in self?.accept(c) }
            l.stateUpdateHandler = { state in
                if case .failed(let error) = state {
                    MeetingLog.info("Meet: can't listen on 127.0.0.1:\(kMeetExtensionPort) for the extension: \(error)")
                }
            }
            l.start(queue: queue)
            listener = l
        } catch {
            MeetingLog.info("Meet: can't listen on 127.0.0.1:\(kMeetExtensionPort) for the extension: \(error)")
        }
    }

    func stop() { listener?.cancel() }

    private func accept(_ c: NWConnection) {
        c.start(queue: queue)
        read(c, Data())
    }

    private func read(_ c: NWConnection, _ sofar: Data) {
        c.receive(minimumIncompleteLength: 1, maximumLength: 65536) { [weak self] data, _, done, error in
            guard let self = self else { return }
            var buf = sofar
            if let data = data { buf.append(data) }
            if let request = Request(buf) {
                self.handle(c, request)
            } else if error != nil || done || buf.count > 1 << 20 {
                c.cancel()
            } else {
                self.read(c, buf)
            }
        }
    }

    private struct Request {
        let method: String
        let path: String
        let query: [String: String]
        let headers: [String: String]   // lower-case names
        let body: Data

        /// A whole request, or nil while there's more to come.
        init?(_ buf: Data) {
            guard let split = buf.range(of: Data("\r\n\r\n".utf8)) else { return nil }
            let lines = String(decoding: buf[..<split.lowerBound], as: UTF8.self).components(separatedBy: "\r\n")
            let first = lines[0].split(separator: " ")
            guard first.count >= 2 else { return nil }
            var headers: [String: String] = [:]
            for line in lines.dropFirst() {
                guard let colon = line.firstIndex(of: ":") else { continue }
                headers[line[..<colon].lowercased()] = line[line.index(after: colon)...].trimmingCharacters(in: .whitespaces)
            }
            let length = Int(headers["content-length"] ?? "") ?? 0
            guard buf.count - split.upperBound >= length else { return nil }
            let url = URLComponents(string: String(first[1]))
            method = String(first[0])
            path = url?.path ?? String(first[1])
            query = Dictionary((url?.queryItems ?? []).map { ($0.name, $0.value ?? "") }, uniquingKeysWith: { a, _ in a })
            self.headers = headers
            body = buf.subdata(in: split.upperBound..<(split.upperBound + length))
        }
    }

    private func handle(_ c: NWConnection, _ r: Request) {
        let origin = r.headers["origin"] ?? ""
        // the extension, or a program on this Mac - not a web page
        let allowed = origin.isEmpty || origin.hasPrefix("chrome-extension://") || origin.hasPrefix("moz-extension://")
        if r.method == "OPTIONS" {
            let cors = ["Access-Control-Allow-Origin": origin.isEmpty ? "*" : origin,
                        "Access-Control-Allow-Methods": "GET, POST", "Access-Control-Allow-Headers": "Content-Type",
                        "Access-Control-Allow-Private-Network": "true"]
            return reply(c, allowed ? 204 : 403, nil, origin: "", extra: allowed ? cors : [:])
        }
        guard allowed else { return reply(c, 403, ["error": "not from the extension"], origin: origin) }
        switch (r.method, r.path) {
        case ("GET", "/meet/commands"):
            let wait = min(25, Double(r.query["wait"] ?? "") ?? 20)
            let t = Thread { [meet] in self.reply(c, 200, ["commands": meet.take(wait)], origin: origin) }
            t.name = "meet-commands"
            t.start()
        case ("GET", "/meet"):
            reply(c, 200, ["helper": "mac", "extension_port": Int(kMeetExtensionPort)], origin: origin)
        case ("POST", "/meet/inspect") where origin.isEmpty:   // (--inspect-meet, from this Mac)
            let t = Thread { [meet] in self.reply(c, 200, meet.inspect(), origin: origin) }
            t.name = "meet-inspect"
            t.start()
        case ("POST", "/meet/state"):
            let parsed: Any? = r.body.isEmpty ? JSON() : (try? JSONSerialization.jsonObject(with: r.body))
            guard let data = parsed else {
                return reply(c, 400, ["error": "not JSON"], origin: origin)
            }
            meet.gotState(data as? JSON ?? [:])
            reply(c, 200, ["ok": true], origin: origin)
        default:
            reply(c, 404, ["error": "not found"], origin: origin)
        }
    }

    private func reply(_ c: NWConnection, _ code: Int, _ data: Any?, origin: String, extra: [String: String] = [:]) {
        let body = data.flatMap { try? JSONSerialization.data(withJSONObject: $0) } ?? Data()
        let reason = [200: "OK", 204: "No Content", 400: "Bad Request", 403: "Forbidden", 404: "Not Found"][code] ?? ""
        var head = "HTTP/1.1 \(code) \(reason)\r\nContent-Length: \(body.count)\r\nConnection: close\r\n"
        if data != nil { head += "Content-Type: application/json\r\n" }
        if !origin.isEmpty { head += "Access-Control-Allow-Origin: \(origin)\r\n" }
        for (k, v) in extra { head += "\(k): \(v)\r\n" }
        c.send(content: Data((head + "\r\n").utf8) + body, completion: .contentProcessed { _ in c.cancel() })
    }
}

// ---------------------------------------------------------------- the display

/// The display's HTTP API, called from the meeting controls' own threads.
final class MeetingDisplay: @unchecked Sendable {
    private let address: () -> (String, Int)
    private let session: URLSession = {
        let config = URLSessionConfiguration.ephemeral
        config.connectionProxyDictionary = [:]   // (it's on the LAN)
        config.httpMaximumConnectionsPerHost = 10   // three screens' long polls, and their states
        config.requestCachePolicy = .reloadIgnoringLocalCacheData
        return URLSession(configuration: config)
    }()

    init(address: @escaping () -> (String, Int)) { self.address = address }

    private final class Box: @unchecked Sendable { var result: (Int, Any?) = (0, nil) }

    /// (status, JSON); status 0 when it couldn't be reached. Waits for it.
    func call(_ path: String, _ body: JSON? = nil, timeout: TimeInterval = kTimeout) -> (Int, Any?) {
        let (host, port) = address()
        guard let url = URL(string: "http://\(host):\(port)\(path)") else { return (0, nil) }
        var req = URLRequest(url: url, timeoutInterval: timeout)
        if let body = body {
            req.httpMethod = "POST"
            req.httpBody = try? JSONSerialization.data(withJSONObject: body)
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        let box = Box(), done = DispatchSemaphore(value: 0)
        session.dataTask(with: req) { data, response, _ in
            let code = (response as? HTTPURLResponse)?.statusCode ?? 0
            box.result = (code, data.flatMap { try? JSONSerialization.jsonObject(with: $0, options: .fragmentsAllowed) })
            done.signal()
        }.resume()
        done.wait()
        return box.result
    }
}

/// A flag a thread can sleep on, and be woken by.
private final class Stopper {
    private let cond = NSCondition()
    private var stopped = false

    var isStopped: Bool {
        cond.lock()
        defer { cond.unlock() }
        return stopped
    }

    func stop() {
        cond.lock()
        stopped = true
        cond.broadcast()
        cond.unlock()
    }

    /// Sleep `secs`, less if it's stopped meanwhile: whether it's stopped.
    @discardableResult
    func sleep(_ secs: TimeInterval) -> Bool {
        cond.lock()
        defer { cond.unlock() }
        let until = Date(timeIntervalSinceNow: secs)
        while !stopped && cond.wait(until: until) {}
        return stopped
    }
}

private final class Inbox {
    enum Item { case command(JSON), stop, idle }
    private let cond = NSCondition()
    private var items: [Item] = []

    func put(_ item: Item) {
        cond.lock()
        items.append(item)
        cond.signal()
        cond.unlock()
    }

    /// The next item, or .idle if none comes within `timeout`.
    func get(timeout: TimeInterval) -> Item {
        cond.lock()
        defer { cond.unlock() }
        let until = Date(timeIntervalSinceNow: timeout)
        while items.isEmpty {
            if !cond.wait(until: until) { return .idle }
        }
        return items.removeFirst()
    }
}

/// One meeting screen: the taps from it, done in its app, and the app's
/// state back to it. Two threads: one waits on the display for taps, the
/// other owns the app and does everything to it.
final class Relay {
    let appID: String
    let app: MeetingApp
    private let display: MeetingDisplay
    private let inbox = Inbox()
    private let stopper = Stopper()
    private let finished = DispatchSemaphore(value: 0)

    // read by the menu: under the lock
    private let lock = NSLock()
    private var _showing = false
    private var _connected = false
    private var _state: JSON = [:]
    private var _via = ""

    // the app thread's own
    private var state: JSON = [:]
    private var posted: JSON?
    private var postedAt: TimeInterval = 0
    private var since: Double?            // when this call started (seconds since 1970)
    private var pttUntil: TimeInterval = 0   // push to talk is held till then (mono), or 0
    private var pttOpened = false          // ...and it was push to talk that unmuted you
    private var expected: (key: String, value: Bool, until: TimeInterval)?   // see expect
    private var note: String?              // why the last tap didn't work, for the display

    init(_ appID: String, display: MeetingDisplay) {
        self.appID = appID
        self.display = display
        switch appID {
        case "zoom": app = ZoomApp()
        case "teams": app = TeamsApp()
        default: app = MeetApp()
        }
    }

    private var showing: Bool {
        get { lock.lock(); defer { lock.unlock() }; return _showing }
        set { lock.lock(); _showing = newValue; lock.unlock() }
    }

    func start() {
        app.onChange = { [weak self] in self?.inbox.put(.command(["action": "look"])) }
        let poller = Thread { [weak self] in self?.poll() }
        poller.name = "\(appID)-taps"
        poller.start()
        let worker = Thread { [weak self] in self?.work() }
        worker.name = "\(appID)-app"
        worker.start()
    }

    func stop() {
        stopper.stop()
        inbox.put(.stop)
    }

    /// Wait (up to `secs`) for the app thread to finish: it shuts the mic first.
    func join(_ secs: TimeInterval) { _ = finished.wait(timeout: .now() + secs) }

    /// Wait on the display for taps; each goes to the app thread.
    private func poll() {
        var boot = "", after = 0, backoff: TimeInterval = 1
        while !stopper.isStopped {
            var q = URLComponents()
            q.queryItems = [URLQueryItem(name: "boot", value: boot), URLQueryItem(name: "after", value: String(after)),
                            URLQueryItem(name: "wait", value: String(kPollWait))]
            let (code, body) = display.call("/meeting/\(appID)/commands?\(q.percentEncodedQuery ?? "")",
                                            timeout: TimeInterval(kPollWait + 10))
            guard code == 200, let r = body as? JSON else {
                lock.lock()
                _connected = false
                lock.unlock()
                MeetingLog.debug("\(appID): display: \(code == 0 ? "unreachable" : "HTTP \(code)")")
                if stopper.sleep(backoff) { break }
                backoff = min(30, backoff * 2)
                continue
            }
            backoff = 1
            lock.lock()
            _connected = true
            _showing = r["showing"] as? Bool ?? false
            lock.unlock()
            boot = r["boot"] as? String ?? ""
            after = (r["seq"] as? NSNumber)?.intValue ?? after
            for cmd in r["commands"] as? [JSON] ?? [] { inbox.put(.command(cmd)) }
        }
    }

    /// The app's own thread: taps as they come, and a look at the call every
    /// second or few, posting what it sees.
    private func work() {
        app.setup()
        var nextLook: TimeInterval = 0
        loop: while true {
            let now = mono()
            let wait = max(0, min(nextLook - now, pttUntil > 0 ? pttUntil - now : 99))
            switch inbox.get(timeout: wait) {
            case .stop:
                break loop
            case .command(let cmd):
                handle(cmd)
                nextLook = 0   // look again straight away: the tap changed something
            case .idle:
                break
            }
            if pttUntil > 0 && mono() > pttUntil {
                MeetingLog.info("\(appID): push to talk wasn't let go - shutting the mic")
                pttOff()
            }
            if mono() >= nextLook {
                look()
                nextLook = mono() + (showing || pttUntil > 0 ? kLookShowing : kLookIdle)
            }
        }
        pttOff()   // (quitting mid-sentence: shut the mic first)
        app.close()
        finished.signal()
    }

    private func handle(_ cmd: JSON) {
        let action = cmd["action"] as? String ?? ""
        if action == "look" { return }   // (just a look, which follows every command)
        if action == "ptt" {
            if cmd["on"] as? Bool == true { pttOn() } else { pttOff() }
            return
        }
        note = app.act(action, cmd, state)
        if let note = note { MeetingLog.info("\(appID) \(action): \(note)") }
    }

    private func pttOn() {
        let first = pttUntil == 0
        pttUntil = mono() + kPttGrace
        if first {
            pttOpened = state["muted"] as? Bool != false   // (live already: leave it so)
            if pttOpened {
                app.setMuted(false, state)
                expect("muted", false)
            }
        }
    }

    private func pttOff() {
        guard pttUntil > 0 else { return }
        pttUntil = 0
        if pttOpened {
            var st = state
            st["muted"] = false
            app.setMuted(true, st)
            expect("muted", true)
        }
    }

    /// What a tap just set, believed over the app's word for a moment: it
    /// takes a little while to say so, and another press straight after this
    /// one (or the display) should go by what it is now.
    private func expect(_ key: String, _ value: Bool) {
        state[key] = value
        expected = (key, value, mono() + kExpectSecs)
    }

    private func look() {
        var st = app.look()
        let inMeeting = st["in_meeting"] as? Bool == true
        if let e = expected {
            if st[e.key] as? Bool == e.value || mono() > e.until || !inMeeting {
                expected = nil   // (it says so too now, or it never will)
            } else {
                st[e.key] = e.value
            }
        }
        since = inMeeting ? since ?? Date().timeIntervalSince1970 : nil
        st["helper"] = "mac"
        st["via"] = inMeeting ? app.via : ""
        st["since"] = since ?? NSNull()
        if let note = note, inMeeting, st["note"] == nil { st["note"] = note }
        state = st
        lock.lock()
        _state = st
        _via = app.via
        lock.unlock()
        if !same(st, posted) || mono() - postedAt > kHeartbeat {
            let (code, r) = display.call("/meeting/\(appID)/state", st)
            if code == 200 {
                posted = st
                postedAt = mono()
                lock.lock()
                _showing = (r as? JSON)?["showing"] as? Bool ?? false
                _connected = true   // (before the first wait for taps comes back)
                lock.unlock()
            } else {
                MeetingLog.debug("\(appID): posting state: \(code == 0 ? "unreachable" : "HTTP \(code)")")
            }
        }
    }

    /// A line for the menu.
    func summary() -> String {
        lock.lock()
        let connected = _connected, st = _state, via = _via
        lock.unlock()
        let name = app.name
        if !connected { return "\(name): can't reach the display" }
        guard st["in_meeting"] as? Bool == true else {
            return "\(name): \(st["note"] as? String ?? "not in a meeting")"
        }
        let muted = st["muted"] as? Bool
        var bits = [muted == true ? "muted" : muted == false ? "live" : "in a meeting"]
        if !via.isEmpty { bits.append("via \(via)") }
        return "\(name): " + bits.joined(separator: ", ")
    }
}

/// The relays for the meeting screens the display has, started and stopped
/// as they're installed and removed.
final class MeetingHelper {
    private let display: MeetingDisplay
    private let lock = NSLock()
    private var relays: [String: Relay] = [:]
    private let stopper = Stopper()

    init(address: @escaping () -> (String, Int)) {
        display = MeetingDisplay(address: address)
    }

    func start() {
        let t = Thread { [weak self] in self?.watch() }
        t.name = "meetings"
        t.start()
    }

    private func watch() {
        while !stopper.isStopped {
            let (code, info) = display.call("/screens")
            if code == 200, let info = info as? JSON { follow(screens: info) }
            stopper.sleep(kScreensEvery)
        }
    }

    /// A GET /screens answer: follow the meeting screens in it that are on.
    func follow(screens info: JSON) {
        var wanted = Set<String>()
        for s in info["installed"] as? [JSON] ?? [] {
            if let id = s["id"] as? String, kMeetingApps.contains(id), s["enabled"] as? Bool != false { wanted.insert(id) }
        }
        follow(wanted)
    }

    private func follow(_ wanted: Set<String>) {
        guard !stopper.isStopped else { return }
        lock.lock()
        for id in wanted.subtracting(relays.keys).sorted() {
            MeetingLog.info("\(id) screen found - following its taps")
            let relay = Relay(id, display: display)
            relays[id] = relay
            relay.start()
        }
        for id in Set(relays.keys).subtracting(wanted) { relays.removeValue(forKey: id)?.stop() }
        lock.unlock()
        if !wanted.isDisjoint(with: ["zoom", "teams"]) { askForAccess() }
    }

    /// Zoom and Teams need Accessibility: macOS asks once, then it's the menu's
    /// "Allow Accessibility access…" (and the screen says so).
    private func askForAccess() {
        let asked = "MeetingsAskedForAccessibility"
        guard !MeetingLog.console, !UserDefaults.standard.bool(forKey: asked), !AX.trusted() else { return }
        UserDefaults.standard.set(true, forKey: asked)
        DispatchQueue.main.async { _ = AX.trusted(prompt: true) }
    }

    /// The meeting screens being followed.
    var following: Set<String> {
        lock.lock()
        defer { lock.unlock() }
        return Set(relays.keys)
    }

    /// A line each, for the menu.
    func summary() -> [String] {
        lock.lock()
        let list = relays.sorted { $0.key < $1.key }.map(\.value)
        lock.unlock()
        return list.map { $0.summary() }
    }

    /// Stop following, giving each app thread up to `wait` s to shut the mic.
    func stop(wait: TimeInterval = 0) {
        stopper.stop()
        lock.lock()
        let list = Array(relays.values)
        relays.removeAll()
        lock.unlock()
        for relay in list { relay.stop() }
        if wait > 0 {
            let until = mono() + wait
            for relay in list { relay.join(max(0, until - mono())) }
        }
    }
}

// ---------------------------------------------------------------- from the command line

/// DeskSwitch run from Terminal with one of these runs the meeting controls
/// on their own, or shows what they see, instead of the menu bar app.
enum MeetingsCLI {
    static let usage = """
        DeskSwitch --meetings [--host HOST[:PORT]] [-v]   the meeting controls, without the menu bar app
        DeskSwitch --dump zoom|teams                      the buttons Accessibility sees in that app
        DeskSwitch --inspect-meet                         what's on Meet's bar, as the extension sees it
                                                          (DeskSwitch, or --meetings, must be running)
        """

    /// nil when there's nothing here for it: start the app.
    static func run(_ args: [String]) -> Int32? {
        var meetings = false, inspect = false, verbose = false, help = false
        var dump: String?
        var host = kHost, port = kPort
        var i = 1
        while i < args.count {
            switch args[i] {
            case "--meetings": meetings = true
            case "--inspect-meet": inspect = true
            case "-v", "--verbose": verbose = true
            case "-h", "--help": help = true
            case "--dump":
                i += 1
                dump = i < args.count ? args[i] : ""
            case "--host":
                i += 1
                if i < args.count {
                    let parts = args[i].split(separator: ":", maxSplits: 1)
                    host = parts.first.map(String.init) ?? host
                    if parts.count > 1 { port = Int(parts[1]) ?? port }
                }
            default: break   // (macOS may add its own)
            }
            i += 1
        }
        if help {
            print(usage)
            return 0
        }
        guard meetings || inspect || dump != nil else { return nil }
        setvbuf(stdout, nil, _IOLBF, 0)   // (a line at a time, into a file or a pipe too)
        MeetingLog.console = true
        MeetingLog.verbose = verbose
        if let dump = dump { return self.dump(dump) }
        if inspect { return inspectMeet() }

        if !AX.trusted() {
            print("(Zoom and Teams need Accessibility access for the app this runs in: System Settings, "
                + "Privacy & Security, Accessibility)")
        }
        let helper = MeetingHelper(address: { (host, port) })
        helper.start()
        var sources: [DispatchSourceSignal] = []
        for sig in [SIGINT, SIGTERM] {
            signal(sig, SIG_IGN)
            let source = DispatchSource.makeSignalSource(signal: sig, queue: .main)
            source.setEventHandler {
                helper.stop(wait: 2)   // (shut the mic if push to talk's held)
                exit(0)
            }
            source.resume()
            sources.append(source)
        }
        let timer = Timer(timeInterval: 5, repeats: true) { _ in
            for line in helper.summary() { MeetingLog.info(line) }
        }
        RunLoop.main.add(timer, forMode: .common)
        withExtendedLifetime(sources) { RunLoop.main.run() }
        return 0
    }

    /// The buttons and menu items Accessibility sees in Zoom or Teams: for
    /// checking the names the patterns above look for.
    private static func dump(_ which: String) -> Int32 {
        let app: ButtonApp
        switch which {
        case "zoom": app = ZoomApp()
        case "teams": app = TeamsButtons()
        default:
            print("--dump zoom, or --dump teams")
            return 2
        }
        if !AX.trusted() {
            print("This needs Accessibility access for the app it runs in (Terminal...): System Settings, "
                + "Privacy & Security, Accessibility.")
        }
        app.setup()
        guard let running = app.runningApp() else {
            print("\(app.name) isn't running")
            return 1
        }
        let el = AXUIElementCreateApplication(running.processIdentifier)
        app.pid = running.processIdentifier
        app.prepare(el)
        func show(_ c: Control) {
            let hits = app.patterns.filter { _, list in list.contains(where: c.matches) }.keys.sorted()
            print("  \(c.role) \"\(c.label)\"" + (c.enabled ? "" : " (disabled)")
                + (hits.isEmpty ? "" : "   -> \(hits.joined(separator: ", "))"))
        }
        let menu = app.appControls(el)
        if !menu.isEmpty {
            print("--- menu bar")
            menu.forEach(show)
        }
        let wins = AX.windows(el)
        if wins.isEmpty { print("no \(app.name) windows open") }
        for w in wins {
            print("--- window \"\(AX.title(w))\"")
            AX.controls(in: w).forEach(show)
        }
        print("state:", json(app.look()))
        return 0
    }

    private static func inspectMeet() -> Int32 {
        let display = MeetingDisplay(address: { ("127.0.0.1", Int(kMeetExtensionPort)) })
        let (code, got) = display.call("/meet/inspect", [:], timeout: 15)
        guard code == 200, let got = got as? JSON else {
            print("nothing's listening for the extension on 127.0.0.1:\(kMeetExtensionPort): start DeskSwitch first")
            return 1
        }
        print("state:", json(got["state"] ?? NSNull()))
        for c in got["controls"] as? [Any] ?? [] { print(" ", json(c)) }
        return 0
    }

    private static func json(_ value: Any) -> String {
        guard JSONSerialization.isValidJSONObject(value),
              let data = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys, .withoutEscapingSlashes])
        else { return "\(value)" }
        return String(decoding: data, as: UTF8.self)
    }
}
