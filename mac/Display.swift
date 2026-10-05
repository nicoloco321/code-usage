// What the display says, and how the menu bar shows it: the HTTP client, its
// screens and usage, the menu's wording, and Clawd. The menu itself is in
// DeskSwitch.swift.

import Cocoa

let kHost = "claude-display.local"
let kPort = 8080
let kTimeout: TimeInterval = 5   // headroom for a cold mDNS resolve

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

    static func json(_ data: Data?) -> [String: Any]? {
        data.flatMap { (try? JSONSerialization.jsonObject(with: $0)) as? [String: Any] }
    }
}

// ---------------------------------------------------------------- screens

/// One screen on the display, as GET /screens lists it.
struct Screen: Equatable {
    let id: String
    let name: String
    let icon: String   // the Screen Market's emoji, "" if none

    var title: String { icon.isEmpty ? name : "\(icon)  \(name)" }

    /// What an ESP32 display has, since it can't list its screens.
    static let firmware = [
        Screen(id: "usage", name: "Claude Code usage", icon: ""),
        Screen(id: "spotify", name: "Spotify now playing", icon: ""),
    ]

    /// The installed screens in a GET /screens answer, minus any that aren't
    /// set up yet.
    static func installed(_ info: [String: Any]) -> [Screen] {
        (info["installed"] as? [[String: Any]] ?? []).compactMap { s in
            guard let id = s["id"] as? String, (s["ready"] as? Bool) != false else { return nil }
            return Screen(id: id, name: s["name"] as? String ?? id, icon: s["icon"] as? String ?? "")
        }
    }
}

// ---------------------------------------------------------------- usage

/// GET /usage: what the display's Claude Usage screen shows. The ESP32 and the
/// Pi app answer it alike, so the Mac needs no Anthropic login of its own and
/// adds no load on the rate-limited usage API.
struct Usage {
    struct Window {
        let pct: Double?
        let resetsAt: Date?
        let resets: String   // the display's own wording, for when resets_at won't parse

        init(_ json: Any?) {
            let window = json as? [String: Any] ?? [:]
            pct = (window["pct"] as? NSNumber)?.doubleValue
            resetsAt = parseISO(window["resets_at"] as? String)
            resets = window["resets"] as? String ?? ""
        }
    }

    let valid: Bool        // false until the display's first good fetch
    let fiveHour: Window
    let sevenDay: Window
    let thinking: Bool     // Claude is working on one of your machines
    let mode: String?      // the screen showing

    init?(json data: Data?) {
        guard let usage = Device.json(data) else { return nil }
        valid = usage["valid"] as? Bool ?? false
        fiveHour = Window(usage["five_hour"])
        sevenDay = Window(usage["seven_day"])
        thinking = usage["thinking"] as? Bool ?? false
        mode = usage["mode"] as? String
    }
}

private let isoPattern = try! NSRegularExpression(
    pattern: #"^(\d{4})-(\d\d)-(\d\d)[T ](\d\d):(\d\d)(?::(\d\d)(?:\.\d+)?)?\s*(Z|[+-]\d\d:?\d\d)?$"#)

/// "2026-10-05T06:49:59.837534+00:00" -> a Date. ISO8601DateFormatter balks
/// at microseconds, so this takes it apart the way claude_tray.py does.
func parseISO(_ text: String?) -> Date? {
    guard let text = text?.trimmingCharacters(in: .whitespaces),
          let m = isoPattern.firstMatch(in: text, range: NSRange(text.startIndex..., in: text))
    else { return nil }
    func group(_ i: Int) -> String? { Range(m.range(at: i), in: text).map { String(text[$0]) } }
    func int(_ i: Int) -> Int { Int(group(i) ?? "") ?? 0 }
    var offset = 0
    if let tz = group(7), tz != "Z" {
        let digits = tz.dropFirst().replacingOccurrences(of: ":", with: "")
        offset = (tz.hasPrefix("-") ? -1 : 1) * ((Int(digits.prefix(2)) ?? 0) * 3600 + (Int(digits.dropFirst(2)) ?? 0) * 60)
    }
    let parts = DateComponents(timeZone: TimeZone(secondsFromGMT: offset), year: int(1), month: int(2),
                               day: int(3), hour: int(4), minute: int(5), second: int(6))
    return Calendar(identifier: .gregorian).date(from: parts)
}

private func formatter(_ format: String) -> DateFormatter {
    let f = DateFormatter()
    f.locale = Locale(identifier: "en_US_POSIX")   // the display's wording: "Mon 2:49 AM"
    f.dateFormat = format
    return f
}
private let weekday = formatter("EEE"), clock = formatter("h:mm a")

/// "resets 4:19 AM (in 2h 13m)", in this Mac's timezone - the display's rules.
func describeReset(_ window: Usage.Window, now: Date = Date()) -> String {
    guard let when = window.resetsAt else {
        return window.resets.isEmpty ? "" : "resets \(window.resets)"
    }
    let day = Calendar.current.isDate(when, inSameDayAs: now) ? "" : weekday.string(from: when) + " "
    let mins = max(Int((when.timeIntervalSince(now) / 60).rounded(.down)), 0)
    let (days, rest) = mins.quotientAndRemainder(dividingBy: 1440)
    let (hours, minutes) = rest.quotientAndRemainder(dividingBy: 60)
    let until = days > 0 ? "\(days)d \(hours)h" : hours > 0 ? "\(hours)h \(minutes)m" : "\(minutes)m"
    return "resets \(day)\(clock.string(from: when)) (in \(until))"
}

// ---------------------------------------------------------------- what we show

/// Everything the menu bar item shows, and the words for it - the same as the
/// Windows tray helper's.
struct DisplayState {
    var checked = false      // a poll has come back, either way
    var online = false
    var usage: Usage?        // the display's last GET /usage
    var mode: String?        // the screen showing
    var screens: [Screen] = []
    var legacy = false       // it answers, but its firmware predates GET /usage
    var noUsage = false      // a Pi with no Claude Usage screen installed
    var localWorking = false // the hooks say Claude is working on this Mac

    var usageKnown: Bool { online && usage?.valid == true }
    var fivePct: Double? { usageKnown ? usage?.fiveHour.pct : nil }
    var thinking: Bool { localWorking || (online && usage?.thinking == true) }
    var showing: Screen? { screens.first { $0.id == mode } }

    func usageLine(_ window: KeyPath<Usage, Usage.Window>) -> String {
        let label = window == \Usage.fiveHour ? "5-hour" : "Weekly"
        guard let w = usage?[keyPath: window], let pct = w.pct else { return "\(label): --" }
        let reset = describeReset(w)
        return "\(label): \(Int(pct.rounded(.toNearestOrEven)))%" + (reset.isEmpty ? "" : "   \(reset)")
    }

    /// The menu's first line: the 5-hour usage, or why there isn't any.
    var statusLine: String {
        if !checked { return "Checking…" }
        if !online { return "Display not reachable at \(kHost)" }
        if legacy { return "Re-flash the display firmware to see usage here" }
        if noUsage { return "Add the Claude Usage screen from the Screen Market" }
        if !usageKnown { return "Waiting for the display's first usage fetch…" }
        return usageLine(\.fiveHour)
    }

    var tooltip: String {
        var lines: [String]
        if !online {
            lines = ["Claude display - offline"]
        } else if usageKnown, let usage {
            let pct = { (w: Usage.Window) in w.pct.map { "\(Int($0.rounded(.toNearestOrEven)))%" } ?? "--%" }
            lines = ["Claude usage: 5h \(pct(usage.fiveHour)) · week \(pct(usage.sevenDay))"]
        } else {
            lines = ["Claude display"]
        }
        if online, let showing { lines.append("Showing: \(showing.name)") }
        if thinking { lines.append("Claude is working…") }
        return lines.joined(separator: "\n")
    }
}

// ---------------------------------------------------------------- Clawd

/// The menu bar icon: Clawd with a meter of your 5-hour usage under him. He
/// walks while Claude is working, and goes grey when the display can't be
/// reached - the same icon as the Windows tray helper's.
enum Clawd {
    /// His native 12x8 grid, as in firmware/src/mascot.h (1 = body, 2 = eye).
    static let grid: [[UInt8]] = [
        [0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0],
        [0, 0, 1, 2, 1, 1, 1, 1, 2, 1, 0, 0],
        [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
        [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
        [0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0],
        [0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0],
        [0, 0, 1, 0, 1, 0, 0, 1, 0, 1, 0, 0],
        [0, 0, 1, 0, 1, 0, 0, 1, 0, 1, 0, 0],
    ]
    /// Walk frame -> the leg columns off the ground. Frame 0 is standing.
    static let liftedLegs: [[Int]] = [[], [2, 7], [4, 9]]

    static let orange = rgb(217, 119, 87), gray = rgb(140, 140, 140), eye = rgb(18, 18, 18)
    static let green = rgb(57, 186, 82), yellow = rgb(222, 162, 66), red = rgb(230, 81, 74)
    static let track = NSColor(white: 0.5, alpha: 0.45)

    static func rgb(_ r: CGFloat, _ g: CGFloat, _ b: CGFloat) -> NSColor {
        NSColor(srgbRed: r / 255, green: g / 255, blue: b / 255, alpha: 1)
    }

    static func barColor(_ pct: Double) -> NSColor { pct < 50 ? green : pct < 80 ? yellow : red }

    /// 18x18 points. Clawd's cells are 1.5pt, so on a Retina menu bar each
    /// one is exactly 3x3 pixels.
    static func image(pct: Double?, online: Bool, frame: Int) -> NSImage {
        let cell: CGFloat = 1.5, side: CGFloat = 18
        let meter = online ? pct : nil
        let top: CGFloat = meter == nil ? 3 : 1.5   // centred when there's no meter under him
        let image = NSImage(size: NSSize(width: side, height: side), flipped: true) { _ in
            NSGraphicsContext.current?.shouldAntialias = false   // crisp pixel art
            for (r, row) in grid.enumerated() {
                for (c, v) in row.enumerated() where v != 0 {
                    if r == grid.count - 1 && liftedLegs[frame].contains(c) { continue }
                    (v == 1 ? (online ? orange : gray) : eye).setFill()
                    NSRect(x: CGFloat(c) * cell, y: top + CGFloat(r) * cell, width: cell, height: cell).fill()
                }
            }
            if let pct = meter {
                NSGraphicsContext.current?.shouldAntialias = true
                let bar = NSRect(x: 0, y: side - 3, width: side, height: 3)
                track.setFill()
                NSBezierPath(roundedRect: bar, xRadius: 1, yRadius: 1).fill()
                // in whole cells, so the meter lines up with Clawd
                let width = (side * min(pct, 100) / 100 / cell).rounded(.toNearestOrEven) * cell
                if width > 0 {
                    barColor(pct).setFill()
                    NSBezierPath(roundedRect: NSRect(x: 0, y: bar.minY, width: width, height: 3),
                                 xRadius: 1, yRadius: 1).fill()
                }
            }
            return true
        }
        image.isTemplate = false   // he's orange: the menu bar mustn't tint him
        return image
    }
}
