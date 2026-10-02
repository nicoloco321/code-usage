// DeskSwitch - a macOS menu bar switcher for the Claude Code usage display.
//
// The menu lists the screens installed on the display, so a screen you add
// from the Screen Market shows up here by itself: the list is read again every
// time the menu opens, and every 30 s for the menu bar glyph. It speaks the
// display's HTTP API, the one the /switch Claude Code command uses:
//
//   GET  /screens                        the installed screens and the one showing (Pi app)
//   POST /mode/<screen> | /mode/toggle   switch
//   GET  /mode                           the screen showing (any display)
//
// An ESP32 display has no GET /screens: then the menu falls back to the
// firmware's own screens.
//
// Build with mac/build.sh, which produces DeskSwitch.app. It runs as an agent
// (LSUIElement), so there is no Dock icon or main window - just the menu bar
// item, whose glyph tracks whatever the display is currently showing.

import Cocoa

private let kHost = "claude-display.local"
private let kPort = 8080
private let kTimeout: TimeInterval = 5   // headroom for a cold mDNS resolve
private let kRefreshSeconds: TimeInterval = 30

// ---------------------------------------------------------------- screens

/// One screen on the display, as GET /screens lists it.
struct Screen: Equatable {
    let id: String
    let name: String
    let icon: String   // the Screen Market's emoji, "" if none

    var title: String { icon.isEmpty ? name : "\(icon)  \(name)" }

    /// Menu bar glyph, so the icon alone says what the panel is showing.
    var symbol: String { Screen.symbols[id] ?? "display" }

    static let symbols: [String: String] = [
        "usage": "chart.bar.fill", "spotify": "music.note", "split": "rectangle.split.2x1.fill",
        "bambu": "printer.fill", "planes": "airplane", "f1": "flag.checkered",
        "metro": "tram.fill", "weather": "cloud.sun.fill", "world-clock": "clock.fill",
        "countdown": "timer", "crypto": "bitcoinsign.circle.fill", "hacker-news": "newspaper.fill",
    ]

    /// What an ESP32 display has, since it can't list its screens.
    static let firmware = [
        Screen(id: "usage", name: "Claude Code usage", icon: ""),
        Screen(id: "spotify", name: "Spotify now playing", icon: ""),
    ]
}

// ---------------------------------------------------------------- network

/// Talks to the display, addressed by its mDNS hostname.
///
/// The shell commands have to pass `curl -4`, because a raw dual-stack
/// getaddrinfo on a .local name stalls ~5s on the AAAA query that nothing
/// answers - and on a cold mDNS cache it fails outright with EAI_NONAME.
/// URLSession does not share that problem: CFNetwork resolves .local on its
/// own path, measured at 369ms cold and ~90ms warm with no stalls. So hand the
/// hostname straight to URLSession.
///
/// Do not "optimise" this by pre-resolving to an IPv4 literal - that was the
/// first cut and it was both slower and flaky on a cold cache, on top of going
/// stale whenever DHCP moves the device.
final class Device {
    /// `done(status, body)` on the main queue; status 0 when unreachable.
    func send(path: String, method: String, done: @escaping (Int, Data?) -> Void) {
        guard let url = URL(string: "http://\(kHost):\(kPort)\(path)") else {
            DispatchQueue.main.async { done(0, nil) }
            return
        }
        var req = URLRequest(url: url, timeoutInterval: kTimeout)
        req.httpMethod = method
        req.cachePolicy = .reloadIgnoringLocalCacheData   // never cached: it changes under us

        URLSession.shared.dataTask(with: req) { data, response, _ in
            let code = (response as? HTTPURLResponse)?.statusCode ?? 0
            DispatchQueue.main.async { done(code, data) }
        }.resume()
    }

    static func text(_ data: Data?) -> String {
        guard let data = data else { return "" }
        return String(decoding: data, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
    }
}

// ---------------------------------------------------------------- menu

final class Controller: NSObject, NSMenuDelegate {
    private let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    private let device = Device()
    private let menu = NSMenu()
    private let statusLine = NSMenuItem(title: "Checking…", action: nil, keyEquivalent: "")
    private let firstFixed = NSMenuItem.separator()   // the screens go between statusLine and this
    private var screens: [Screen] = []
    private var screenItems: [NSMenuItem] = []
    private var current: String?
    private var online = false
    private var timer: Timer?

    override init() {
        super.init()

        menu.delegate = self
        statusLine.isEnabled = false
        menu.addItem(statusLine)
        menu.addItem(.separator())
        menu.addItem(firstFixed)

        let toggle = NSMenuItem(title: "Cycle to next", action: #selector(cycle), keyEquivalent: "t")
        toggle.target = self
        menu.addItem(toggle)

        let market = NSMenuItem(title: "Add screens from the Screen Market…",
                                action: #selector(openMarketHelp), keyEquivalent: "")
        market.target = self
        menu.addItem(market)

        menu.addItem(.separator())
        menu.addItem(NSMenuItem(title: "Quit DeskSwitch",
                                action: #selector(NSApplication.terminate(_:)),
                                keyEquivalent: "q"))

        statusItem.menu = menu
        setIcon()
        refresh()

        // Keeps the glyph and the list honest when they change elsewhere - a
        // screen installed from the Screen Market, a tap on the display, the
        // /switch command, another machine.
        timer = Timer.scheduledTimer(withTimeInterval: kRefreshSeconds, repeats: true) { [weak self] _ in
            self?.refresh()
        }
    }

    // -- what's on the display

    /// Rebuild the screen items when the list changed; tick the one showing.
    private func show(_ list: [Screen], current mode: String?) {
        online = true
        current = mode
        if list != screens {
            screens = list
            for item in screenItems { menu.removeItem(item) }
            screenItems = []
            var at = menu.index(of: firstFixed)
            if list.isEmpty {
                let none = NSMenuItem(title: "No screens yet - add some from the Screen Market",
                                      action: nil, keyEquivalent: "")
                none.isEnabled = false
                menu.insertItem(none, at: at)
                screenItems.append(none)
            }
            for screen in list {
                let item = NSMenuItem(title: screen.title, action: #selector(pick(_:)), keyEquivalent: "")
                item.target = self
                item.representedObject = screen.id
                menu.insertItem(item, at: at)
                screenItems.append(item)
                at += 1
            }
        }
        for item in screenItems {
            item.state = (item.representedObject as? String) == mode ? .on : .off
        }
        let showing = list.first { $0.id == mode }
        statusLine.title = showing.map { "Showing: \($0.name)" } ?? (list.isEmpty ? "No screens installed" : "Showing: \(mode ?? "?")")
        setIcon()
    }

    private func unreachable() {
        online = false
        current = nil
        statusLine.title = "Display unreachable"
        setIcon()
    }

    /// Unreachable gets a neutral glyph rather than a stale screen's.
    private func setIcon() {
        let screen = screens.first { $0.id == current }
        let name = online ? (screen?.symbol ?? "display") : "display.trianglebadge.exclamationmark"
        let label = online ? "Claude display: \(screen?.name ?? current ?? "on")" : "Claude display unreachable"
        let image = NSImage(systemSymbolName: name, accessibilityDescription: label)
            ?? NSImage(systemSymbolName: "display", accessibilityDescription: label)
        image?.isTemplate = true          // let the menu bar tint it for light/dark
        statusItem.button?.image = image
        statusItem.button?.toolTip = label
    }

    private func refresh() {
        device.send(path: "/screens", method: "GET") { [weak self] code, data in
            guard let self = self else { return }
            if code == 200, let data = data,
               let info = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] {
                let installed = info["installed"] as? [[String: Any]] ?? []
                let list = installed.compactMap { s -> Screen? in
                    guard let id = s["id"] as? String, (s["ready"] as? Bool) != false else { return nil }
                    return Screen(id: id, name: s["name"] as? String ?? id, icon: s["icon"] as? String ?? "")
                }
                self.show(list, current: info["mode"] as? String)
            } else if code != 0 {
                // An ESP32: it can't list its screens, but it says which one is up.
                self.device.send(path: "/mode", method: "GET") { [weak self] code, data in
                    if code == 200 {
                        self?.show(Screen.firmware, current: Device.text(data))
                    } else {
                        self?.unreachable()
                    }
                }
            } else {
                self.unreachable()
            }
        }
    }

    // Opening the menu is the moment the list matters, so read it again then -
    // that's what makes a newly installed screen appear without waiting.
    func menuWillOpen(_ menu: NSMenu) { refresh() }

    // -- actions

    private func switchTo(_ path: String) {
        statusLine.title = "Switching…"
        device.send(path: path, method: "POST") { [weak self] code, data in
            guard let self = self else { return }
            if code == 409 {
                // Not set up yet: the display says what it needs.
                self.statusLine.title = Device.text(data)
                return
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

// ---------------------------------------------------------------- main

final class AppDelegate: NSObject, NSApplicationDelegate {
    // Held here so the status item outlives launch; nothing else retains it.
    private var controller: Controller?

    func applicationDidFinishLaunching(_ notification: Notification) {
        controller = Controller()
    }
}

@main
struct DeskSwitchApp {
    static func main() {
        let app = NSApplication.shared
        // NSApplication.delegate is unowned, so this local has to outlive the
        // call - it does, because run() only returns when the app quits.
        let delegate = AppDelegate()
        app.delegate = delegate
        app.setActivationPolicy(.accessory)   // menu bar only, no Dock icon
        app.run()
    }
}
