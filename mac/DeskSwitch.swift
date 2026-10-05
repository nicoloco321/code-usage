// DeskSwitch - the Claude Code usage display, from the Mac menu bar: the Mac
// version of the Windows tray helper (windows/claude_tray.py).
//
// The menu bar item is Clawd with a meter of your 5-hour usage under him; he
// walks while Claude is working, on this Mac or any other. The menu has:
//
//   - your 5-hour and weekly usage and when they reset, read from the display
//     (GET /usage) - or why it can't show them
//   - the screens installed on the display, to switch between. A screen you
//     add from the Screen Market shows up by itself; an ESP32 display, which
//     can't list them, gets its own usage and Spotify screens.
//   - Track Claude with hooks: installs the Claude Code hooks
//     (server/display_hook.py) and runs their watcher - see ClaudeHooks.swift
//   - Start at login
//
// It speaks the display's HTTP API, the one the /switch Claude Code command uses:
//
//   GET  /usage                          usage, the screen showing, whether Claude is working
//   GET  /screens                        the installed screens (Pi app)
//   POST /mode/<screen> | /mode/toggle   switch
//   GET  /mode                           the screen showing (firmware older than /usage)
//
// Build with mac/build.sh, which produces DeskSwitch.app. It runs as an agent
// (LSUIElement), so there is no Dock icon or main window - just the menu bar
// item.

import Cocoa
import ServiceManagement

private let kPollSeconds: TimeInterval = 5     // re-read the display this often (it's on the LAN)
private let kWatchSeconds: TimeInterval = 2    // the hooks' watcher, like the Windows tray's
private let kFrameSeconds: TimeInterval = 0.5  // Clawd's walk
private let kOfflineAfter = 2                  // failed polls in a row before the display counts as offline

private func infoItem() -> NSMenuItem {
    let item = NSMenuItem(title: "", action: nil, keyEquivalent: "")
    item.isEnabled = false
    return item
}

final class Controller: NSObject, NSMenuDelegate {
    let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    private let device = Device()
    private let menu = NSMenu()
    private let statusLine = infoItem()
    private let weeklyLine = infoItem()
    private let workingLine = infoItem()
    private let noteLine = infoItem()                 // what the last thing you did came to
    private let firstFixed = NSMenuItem.separator()   // the screens go between the info lines and this
    private let cycleItem = NSMenuItem(title: "Cycle to next", action: #selector(cycle), keyEquivalent: "t")
    private let hooksItem = NSMenuItem(title: "Track Claude with hooks (exact)",
                                       action: #selector(toggleHooks), keyEquivalent: "")
    private let loginItem = NSMenuItem(title: "Start at login", action: #selector(toggleLogin), keyEquivalent: "")
    private var screenItems: [NSMenuItem] = []
    private var shownScreens: [Screen]?

    private var display = DisplayState()
    private var failures = kOfflineAfter
    private var polling = false, pollAgain = false
    private var frame = 0
    private var iconKey = ""
    private var note = ""
    private var noteSeen = false

    // The hooks. display_hook.py's path is baked in by build.sh.
    private let hookScript = Bundle.main.object(forInfoDictionaryKey: "DisplayHookScript") as? String
    private let watcher = HookWatcher()
    private let hooksQueue = DispatchQueue(label: "DeskSwitch.hooks")   // settings.json and the watcher
    private var hooksHost: String?   // where the installed hooks report, nil when they aren't installed
    private var watching = false

    override init() {
        super.init()

        menu.delegate = self
        menu.autoenablesItems = false   // offline, the display's items are greyed out by hand
        for item in [statusLine, weeklyLine, workingLine, noteLine] { menu.addItem(item) }
        menu.addItem(.separator())
        menu.addItem(firstFixed)

        let market = NSMenuItem(title: "Add screens from the Screen Market…",
                                action: #selector(openMarketHelp), keyEquivalent: "")
        for item in [cycleItem, market] {
            item.target = self
            menu.addItem(item)
        }
        menu.addItem(.separator())
        for item in [hooksItem, loginItem] {
            item.target = self
            menu.addItem(item)
        }
        menu.addItem(.separator())
        menu.addItem(NSMenuItem(title: "Quit DeskSwitch",
                                action: #selector(NSApplication.terminate(_:)),
                                keyEquivalent: "q"))

        statusItem.menu = menu
        update()
        refresh()
        watch()

        // Keeps everything honest when it changes elsewhere - a screen
        // installed from the Screen Market, a tap on the display, the /switch
        // command, Claude starting work on another machine.
        repeatEvery(kPollSeconds) { $0.refresh() }
        repeatEvery(kWatchSeconds) { $0.watch() }
        repeatEvery(kFrameSeconds) { $0.step() }
    }

    /// In the common modes, so the icon and the menu keep updating while the
    /// menu is open.
    private func repeatEvery(_ seconds: TimeInterval, _ action: @escaping (Controller) -> Void) {
        let timer = Timer(timeInterval: seconds, repeats: true) { [weak self] _ in
            if let self = self { action(self) }
        }
        RunLoop.main.add(timer, forMode: .common)
    }

    // -- what's on the display

    /// Read the display again: /usage, then /screens - the ESP32 serves one
    /// request at a time.
    private func refresh() {
        if polling {
            pollAgain = true
            return
        }
        polling = true
        device.send(path: "/usage", method: "GET") { [weak self] code, body in
            self?.device.send(path: "/screens", method: "GET") { [weak self] scode, sbody in
                self?.polled(usage: (code, body), screens: (scode, sbody))
            }
        }
    }

    private func polled(usage: (code: Int, body: Data?), screens: (code: Int, body: Data?)) {
        var list = Screen.firmware
        let info = screens.code == 200 ? Device.json(screens.body) : nil
        if let info = info {   // the Pi app: its screens come from the Screen Market
            list = Screen.installed(info)
        }
        if usage.code == 200, let data = Usage(json: usage.body) {
            polled(list, usage: data, mode: data.mode)
        } else if usage.code == 404, let info = info {   // no Claude Usage screen installed
            polled(list, noUsage: true, mode: info["mode"] as? String)
        } else if usage.code == 404 {   // older ESP32 firmware: no /usage, but /mode still works
            device.send(path: "/mode", method: "GET") { [weak self] code, data in
                if code == 0 {
                    self?.pollFailed()
                } else {
                    self?.polled(list, legacy: true, mode: code == 200 ? Device.text(data) : nil)
                }
            }
        } else {
            pollFailed()
        }
    }

    private func polled(_ screens: [Screen], usage: Usage? = nil, legacy: Bool = false,
                        noUsage: Bool = false, mode: String?) {
        display.checked = true
        display.online = true
        display.screens = screens
        display.usage = usage
        display.legacy = legacy
        display.noUsage = noUsage
        display.mode = mode
        failures = 0
        pollDone()
    }

    private func pollFailed() {
        display.checked = true
        failures += 1
        // The ESP32 can't answer while it's mid-fetch, so one miss isn't "offline".
        if failures >= kOfflineAfter { display.online = false }
        pollDone()
    }

    private func pollDone() {
        polling = false
        update()
        if pollAgain {
            pollAgain = false
            refresh()
        }
    }

    /// Bring the menu and the icon up to date with what we know.
    private func update() {
        // Offline, the last list stays up, greyed out.
        if display.online && display.screens != shownScreens { rebuildScreens() }
        for item in screenItems {
            item.state = (item.representedObject as? String) == display.mode ? .on : .off
            item.isEnabled = display.online && item.representedObject != nil
        }
        cycleItem.isEnabled = display.online

        statusLine.title = display.statusLine
        weeklyLine.title = display.usageLine(\.sevenDay)
        weeklyLine.isHidden = !display.usageKnown
        workingLine.title = display.thinking ? "Claude is working…" : "Claude is idle"
        workingLine.isHidden = !display.online
        noteLine.title = note
        noteLine.isHidden = note.isEmpty

        hooksItem.state = hooksHost != nil ? .on : .off
        hooksItem.isEnabled = hooksHost != nil || hookScript != nil   // removing them needs no script
        setIcon()
    }

    private func rebuildScreens() {
        shownScreens = display.screens
        for item in screenItems { menu.removeItem(item) }
        screenItems = []
        var at = menu.index(of: firstFixed)
        if display.screens.isEmpty {
            let none = NSMenuItem(title: "No screens yet - add some from the Screen Market",
                                  action: nil, keyEquivalent: "")
            menu.insertItem(none, at: at)
            screenItems.append(none)
        }
        for screen in display.screens {
            let item = NSMenuItem(title: screen.title, action: #selector(pick(_:)), keyEquivalent: "")
            item.target = self
            item.representedObject = screen.id
            menu.insertItem(item, at: at)
            screenItems.append(item)
            at += 1
        }
    }

    private func setIcon() {
        let walkFrame = display.thinking ? 1 + frame % 2 : 0
        let pct = display.fivePct
        let key = "\(pct.map { Int($0.rounded(.toNearestOrEven)) } ?? -1) \(display.online) \(walkFrame)"
        if key != iconKey {
            iconKey = key
            let image = Clawd.image(pct: pct, online: display.online, frame: walkFrame)
            image.accessibilityDescription = display.tooltip
            statusItem.button?.image = image
        }
        statusItem.button?.toolTip = display.tooltip
    }

    /// Clawd's next step, while Claude is working.
    private func step() {
        frame += 1
        if display.thinking { setIcon() }
    }

    // Opening the menu is the moment the list matters, so read it again then -
    // that's what makes a newly installed screen appear without waiting.
    func menuWillOpen(_ menu: NSMenu) {
        refresh()
        loginItem.state = SMAppService.mainApp.status == .enabled ? .on : .off
        noteSeen = !note.isEmpty
    }

    // A note stays until you've had the chance to read it.
    func menuDidClose(_ menu: NSMenu) {
        if noteSeen {
            noteSeen = false
            show(note: "")
        }
    }

    private func show(note text: String) {
        note = text
        update()
    }

    // -- this Mac's Claude Code hooks

    /// The hooks' watcher, every couple of seconds while they're installed:
    /// it catches Esc interrupts and keeps the display awake through long
    /// tool runs, and says whether Claude is working here.
    private func watch() {
        if watching { return }
        watching = true
        hooksQueue.async { [watcher] in
            let ours = HookSettings.installed()
            var working: Bool? = false
            if let ours = ours {
                let (host, port) = ours.isEmpty ? (kHost, kPort) : HookSettings.splitHost(ours)
                working = watcher.watchOnce(host: host, port: port)
            }
            DispatchQueue.main.async { [weak self] in
                guard let self = self else { return }
                self.watching = false
                self.hooksHost = ours
                if let working = working { self.display.localWorking = working }
                self.update()
            }
        }
    }

    @objc private func toggleHooks() {
        let script = hookScript
        hooksQueue.async {
            var problem = ""
            do {
                if HookSettings.installed() != nil {
                    try HookSettings.uninstall()
                } else if let script = script {
                    try HookSettings.install(script: script, host: kHost, port: kPort)
                }
            } catch {
                problem = "Couldn't update ~/.claude/settings.json: \(error.localizedDescription)"
            }
            let ours = HookSettings.installed()
            DispatchQueue.main.async { [weak self] in
                self?.hooksHost = ours
                self?.show(note: problem)
            }
        }
    }

    // -- actions

    @objc private func toggleLogin() {
        let service = SMAppService.mainApp
        var problem = ""
        do {
            if service.status == .enabled {
                try service.unregister()
            } else {
                try service.register()
            }
        } catch {
            problem = "Couldn't change Start at login: \(error.localizedDescription)"
        }
        if service.status == .requiresApproval {
            problem = "Allow DeskSwitch in System Settings › General › Login Items"
            SMAppService.openSystemSettingsLoginItems()
        }
        loginItem.state = service.status == .enabled ? .on : .off
        show(note: problem)
    }

    private func switchTo(_ path: String) {
        show(note: "Switching…")
        device.send(path: path, method: "POST") { [weak self] code, data in
            guard let self = self else { return }
            switch code {
            case 200..<300:
                self.show(note: "")
            case 409:   // not set up yet: the display says what it needs
                let text = Device.text(data)
                self.show(note: text.isEmpty ? "That screen isn't set up on the display yet." : text)
            case 404:
                self.show(note: "This display doesn't have that screen - add it from the Screen Market.")
            case 0:
                self.show(note: "Couldn't reach the display at \(kHost).")
            default:
                self.show(note: "The display answered HTTP \(code).")
            }
            self.refresh()
        }
    }

    @objc private func pick(_ sender: NSMenuItem) {
        guard let id = sender.representedObject as? String else { return }
        switchTo("/mode/\(id)")
    }

    @objc private func cycle() { switchTo("/mode/toggle") }

    @objc private func openMarketHelp() {
        if let url = URL(string: "https://github.com/nicoloco321/screen-market#run-it-on-casaos") {
            NSWorkspace.shared.open(url)
        }
    }
}
