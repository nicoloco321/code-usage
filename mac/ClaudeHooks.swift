// The Claude Code hooks, from the Mac menu bar: the Swift half of
// server/display_hook.py, doing what the Windows tray helper does with it.
//
// The hooks themselves stay Python. Claude Code runs display_hook.py on every
// event, and it keeps each session's state in ~/.local/state/claude-display.
// This file adds the rest:
//
//   HookSettings  installs and removes the hooks in ~/.claude/settings.json,
//                 like display_hook.py --install / --uninstall
//   HookWatcher   display_hook.py's watch_once(). Hooks can't see an Esc
//                 interrupt, or a tool that outruns the display's 5-minute
//                 backstop, so this looks every couple of seconds.
//
// Running display_hook.py --watch as a child process would do the same, but
// it outlives the app whenever the app is killed rather than quit, and the
// script lives in ~/Documents, which macOS guards behind a permission prompt.
// Nothing here reads the repo.
//
// HookWatcher reads and writes the hooks' own state file, under their lock,
// so it has to agree with display_hook.py about that file and about the
// session summary it sends the display. Change one, change the other.

import Foundation

struct HookError: LocalizedError {
    let errorDescription: String?
    init(_ message: String) { errorDescription = message }
}

/// A JSON number as a Double (JSONSerialization hands back NSNumbers).
private func num(_ value: Any?) -> Double? { (value as? NSNumber)?.doubleValue }

// ---------------------------------------------------------------- settings.json

enum HookSettings {
    /// How we recognise our own hooks in settings.json (display_hook.MARKER).
    static let marker = "display_hook.py"
    /// display_hook.HOOK_EVENTS
    static let events = ["UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure",
                         "Notification", "Stop", "StopFailure", "SubagentStop", "PreCompact",
                         "SessionEnd"]
    static let defaultPath = NSHomeDirectory() + "/.claude/settings.json"

    /// The command Claude Code runs on each event. Always /usr/bin/python3: a
    /// python3 that comes first on your PATH (PlatformIO's, Homebrew's) can
    /// move or vanish on an update, and the hooks would quietly stop.
    static func command(script: String, host: String, port: Int) -> String {
        "\"/usr/bin/python3\" \"\(script)\" --host \(host):\(port)"
    }

    /// The address our installed hooks report to ("host:port"), "" if they
    /// carry none, nil when they aren't installed - display_hook.installed().
    static func installed(path: String = defaultPath) -> String? {
        guard let text = try? String(contentsOfFile: path, encoding: .utf8), text.contains(marker),
              let settings = try? read(path) else { return nil }
        for case let groups as [Any] in (settings["hooks"] as? [String: Any] ?? [:]).values {
            for case let group as [String: Any] in groups {
                for case let hook as [String: Any] in group["hooks"] as? [Any] ?? [] {
                    let command = hook["command"] as? String ?? ""
                    if command.contains(marker), let flag = command.range(of: "--host ") {
                        return command[flag.upperBound...].split(separator: " ").first.map(String.init) ?? ""
                    }
                }
            }
        }
        return ""
    }

    static func install(script: String, host: String, port: Int, path: String = defaultPath) throws {
        var settings = try read(path)
        var hooks = try stripOurs(settings["hooks"])
        let ours: [String: Any] = ["hooks": [["type": "command",
                                              "command": command(script: script, host: host, port: port),
                                              "async": true]]]
        for event in events {
            hooks[event] = (hooks[event] as? [Any] ?? []) + [ours]
        }
        settings["hooks"] = hooks
        try write(settings, to: path)
    }

    static func uninstall(path: String = defaultPath) throws {
        var settings = try read(path)
        let hooks = try stripOurs(settings["hooks"])
        if hooks.isEmpty {
            settings.removeValue(forKey: "hooks")
        } else {
            settings["hooks"] = hooks
        }
        try write(settings, to: path)
    }

    /// "host:port" or "host" -> (host, port) - display_hook.split_host().
    static func splitHost(_ text: String, port: Int? = nil) -> (String, Int) {
        var host = text.trimmingCharacters(in: .whitespaces), port = port
        if host.filter({ $0 == ":" }).count == 1 {
            let parts = host.split(separator: ":", omittingEmptySubsequences: false)
            host = String(parts[0])
            port = port ?? Int(parts[1])
        }
        return (host, port ?? 8080)
    }

    /// Our hooks (and the old curl /thinking/ ones) taken out of a hooks
    /// object - display_hook._strip_ours(). Everyone else's stay as they are.
    private static func stripOurs(_ value: Any?) throws -> [String: Any] {
        guard let hooks = value as? [String: Any] ?? (value == nil ? [:] : nil) else {
            throw HookError("\"hooks\" in settings.json isn't an object")
        }
        var out: [String: Any] = [:]
        for (event, value) in hooks {
            guard let groups = value as? [Any] else {
                out[event] = value
                continue
            }
            var kept: [Any] = []
            for group in groups {
                guard var group = group as? [String: Any], let entries = group["hooks"] as? [Any] else {
                    kept.append(group)
                    continue
                }
                let others = entries.filter { entry in
                    let command = (entry as? [String: Any])?["command"] as? String ?? ""
                    return !command.contains(marker) && !command.contains("/thinking/")
                }
                if !others.isEmpty {
                    group["hooks"] = others
                    kept.append(group)
                }
            }
            if !kept.isEmpty { out[event] = kept }
        }
        return out
    }

    private static func read(_ path: String) throws -> [String: Any] {
        guard FileManager.default.fileExists(atPath: path) else { return [:] }
        let data = try Data(contentsOf: URL(fileURLWithPath: path))
        guard let settings = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw HookError("settings.json isn't a JSON object")
        }
        return settings
    }

    /// Writes in place (so a symlinked settings.json stays a symlink) after
    /// keeping the previous version as settings.json.bak, like display_hook.py.
    /// JSONSerialization can't keep key order, so the keys come out sorted.
    private static func write(_ settings: [String: Any], to path: String) throws {
        let url = URL(fileURLWithPath: path)
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(),
                                                withIntermediateDirectories: true)
        if FileManager.default.fileExists(atPath: path) {
            try Data(contentsOf: url).write(to: URL(fileURLWithPath: path + ".bak"))
        }
        var data = try JSONSerialization.data(withJSONObject: settings,
                                              options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes])
        data.append(0x0A)
        try data.write(to: url)
    }
}

// ---------------------------------------------------------------- watcher

/// display_hook.py's watch_once(), on the hooks' own state file.
final class HookWatcher {
    // display_hook.py's constants - the ones the watcher needs
    static let idleAfter: TimeInterval = 300   // IDLE_AFTER_SECS
    static let toolMax: TimeInterval = 1800    // TOOL_MAX_SECS
    static let keepAlive: TimeInterval = 60    // KEEPALIVE_SECS
    static let sendTimeout: TimeInterval = 1.5 // SEND_TIMEOUT
    static let agentStale: TimeInterval = 300  // AGENT_STALE_SECS
    static let historyLength = 60              // HISTORY
    static let interruptedMark = "[Request interrupted by user"  // INTERRUPTED
    static let working = "working", waiting = "waiting", idle = "idle"

    let stateDir: String
    private var checkedMtime: [String: Date] = [:]   // interrupted() skips a transcript that hasn't changed
    private let session: URLSession

    init(stateDir: String = NSHomeDirectory() + "/.local/state/claude-display") {
        self.stateDir = stateDir
        let config = URLSessionConfiguration.ephemeral
        config.connectionProxyDictionary = [:]   // the display is on the LAN: no proxy
        config.timeoutIntervalForRequest = Self.sendTimeout
        config.timeoutIntervalForResource = Self.sendTimeout
        session = URLSession(configuration: config)
    }

    /// One pass: catch Esc interrupts, keep the display awake through long
    /// tool runs, and send "off" once working sessions time out. Returns
    /// whether a Claude Code session on this Mac is working, nil if the state
    /// can't be read right now. Blocks while it talks to the display, so call
    /// it off the main thread.
    func watchOnce(host: String, port: Int) -> Bool? {
        let now = Date().timeIntervalSince1970
        return locked { () -> Bool? in
            guard var st = load() else { return nil }
            var changed = false
            var sessions = st["sessions"] as? [String: Any] ?? [:]
            for (sid, value) in sessions {
                guard var s = value as? [String: Any], s["state"] as? String != Self.idle,
                      let transcript = s["transcript"] as? String, !transcript.isEmpty,
                      interrupted(transcript) else { continue }
                setState(&s, Self.idle, now)
                s["tools"] = [String: Any]()
                sessions[sid] = s
                st["last"] = ["event": "interrupted (Esc)", "session": String(sid.prefix(8)), "at": now]
                record(&st, now, "watcher: Esc interrupt in \(sid.prefix(8)) -> idle")
                changed = true
            }
            st["sessions"] = sessions
            let on = anyWorking(st, now)
            if on && (st["sent"] as? String != "on" || now - (num(st["sent_at"]) ?? 0) > Self.keepAlive) {
                let sent = send(&st, host: host, port: port, on: true, now: now)
                record(&st, now, "watcher: keep-alive, sent \(sent)")
                changed = true
            } else if !on && st["sent"] as? String != "off" {
                let sent = send(&st, host: host, port: port, on: false, now: now)
                record(&st, now, "watcher: nothing working, sent \(sent)")
                changed = true
            }
            if changed { save(st) }
            return on
        }
    }

    /// What the display's session panel shows: this Mac's sessions that are
    /// working or waiting on you - display_hook.summary().
    func summary(_ st: [String: Any], _ now: Double) -> [String: Any] {
        let sessions = (st["sessions"] as? [String: Any] ?? [:])
            .compactMap { sid, value in (value as? [String: Any]).map { (sid, $0) } }
            .sorted { (num($0.1["seen"]) ?? 0) > (num($1.1["seen"]) ?? 0) }
        var out: [[String: Any]] = []
        for (sid, s) in sessions {
            let state = s["state"] as? String ?? ""
            if !sessionWorking(s, now) && state != Self.waiting { continue }
            // display_hook.py lists agents in the order they started, which
            // JSONSerialization can't see; by name at least stays put.
            let agents = (s["agents"] as? [String: Any] ?? [:]).values
                .compactMap { $0 as? [String: Any] }
                .filter { now - (num($0["seen"]) ?? now) < Self.agentStale }
                .map { ["label": $0["label"] as? String ?? "agent", "activity": $0["activity"] as? String ?? ""] }
                .sorted { $0["label"]! < $1["label"]! }
            let start = num(s["turn"]) ?? num(s["changed"]) ?? now
            out.append(["id": String(sid.prefix(8)), "project": s["project"] as? String ?? "",
                        "state": state, "elapsed": Int((now - start).rounded(.toNearestOrEven)),
                        "activity": s["activity"] as? String ?? "", "waiting": s["waiting"] as? String ?? "",
                        "agents": Array(agents.prefix(4))])
        }
        return ["host": Self.hostname, "sessions": out]
    }

    /// socket.gethostname(), exactly: the display files each machine's
    /// sessions under this name, so it has to match what the hooks send.
    /// (ProcessInfo.hostName lowercases it, and the Mac would show up twice.)
    static var hostname: String {
        var name = [CChar](repeating: 0, count: 256)
        gethostname(&name, name.count)
        return String(cString: name)
    }

    // -- the state file, as display_hook.py keeps it

    private var statePath: String { stateDir + "/hook-state.json" }

    /// Under the hooks' own lock: they can run concurrently with us.
    private func locked<T>(_ body: () -> T?) -> T? {
        try? FileManager.default.createDirectory(atPath: stateDir, withIntermediateDirectories: true)
        let fd = open(stateDir + "/hook-state.lock", O_RDWR | O_CREAT, 0o600)
        guard fd >= 0 else { return nil }
        defer { close(fd) }   // releases the lock
        guard flock(fd, LOCK_EX) == 0 else { return nil }
        return body()
    }

    /// Only a missing or garbled file means a fresh start - one we can't read
    /// right now is nil, so it never gets saved over (display_hook.load()).
    private func load() -> [String: Any]? {
        var st: [String: Any] = [:]
        if FileManager.default.fileExists(atPath: statePath) {
            guard let data = FileManager.default.contents(atPath: statePath) else { return nil }
            st = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:]
        }
        if !(st["sessions"] is [String: Any]) { st["sessions"] = [String: Any]() }
        return st
    }

    private func save(_ st: [String: Any]) {
        guard let data = try? JSONSerialization.data(withJSONObject: st, options: [.prettyPrinted, .sortedKeys])
        else { return }
        try? data.write(to: URL(fileURLWithPath: statePath))
    }

    private func record(_ st: inout [String: Any], _ now: Double, _ text: String) {
        let history = (st["history"] as? [Any] ?? []) + [[now, text]]
        st["history"] = Array(history.suffix(Self.historyLength))
    }

    // -- sessions

    private func setState(_ s: inout [String: Any], _ state: String, _ now: Double) {
        if s["state"] as? String != state {
            s["state"] = state
            s["changed"] = now
        }
    }

    private func sessionWorking(_ s: [String: Any], _ now: Double) -> Bool {
        guard s["state"] as? String == Self.working else { return false }
        let tools = (s["tools"] as? [String: Any] ?? [:]).values.compactMap(num)
        if let latest = tools.max(), now - latest < Self.toolMax {
            return true  // a tool is running - builds and tests can take a while
        }
        return now - (num(s["seen"]) ?? 0) < Self.idleAfter
    }

    private func anyWorking(_ st: [String: Any], _ now: Double) -> Bool {
        (st["sessions"] as? [String: Any] ?? [:]).values.contains { s in
            (s as? [String: Any]).map { sessionWorking($0, now) } ?? false
        }
    }

    /// True if the transcript's last message is Claude Code's Esc marker.
    private func interrupted(_ path: String) -> Bool {
        guard let mtime = (try? FileManager.default.attributesOfItem(atPath: path))?[.modificationDate] as? Date,
              checkedMtime[path] != mtime else { return false }  // nothing new since we last looked
        checkedMtime[path] = mtime
        guard let file = FileHandle(forReadingAtPath: path) else { return false }
        defer { try? file.close() }
        let end = (try? file.seekToEnd()) ?? 0
        try? file.seek(toOffset: end > 65536 ? end - 65536 : 0)
        let tail = String(decoding: (try? file.readToEnd()) ?? Data(), as: UTF8.self)
        for line in tail.split(whereSeparator: \.isNewline).reversed() {
            guard let entry = (try? JSONSerialization.jsonObject(with: Data(line.utf8))) as? [String: Any],
                  ["user", "assistant"].contains(entry["type"] as? String ?? "")
            else { continue }  // bookkeeping lines
            var content = (entry["message"] as? [String: Any])?["content"]
            if let parts = content as? [Any] {
                content = parts.compactMap { $0 as? [String: Any] }
                    .filter { $0["type"] as? String == "text" }
                    .map { $0["text"] as? String ?? "" }
                    .joined(separator: " ")
            }
            return (content as? String)?.hasPrefix(Self.interruptedMark) ?? false
        }
        return false
    }

    // -- the display

    /// POST /thinking/on or /off with the session summary (the Pi app shows
    /// it; the ESP32 ignores the body). Records the result in the state and
    /// returns a word for the history - display_hook.send().
    private func send(_ st: inout [String: Any], host: String, port: Int, on: Bool, now: Double) -> String {
        let word = on ? "on" : "off"
        final class Reply: @unchecked Sendable { var code = 0, failure = "bad address" }
        let reply = Reply()
        if let url = URL(string: "http://\(host):\(port)/thinking/\(word)") {
            var request = URLRequest(url: url, timeoutInterval: Self.sendTimeout)
            request.httpMethod = "POST"
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try? JSONSerialization.data(withJSONObject: summary(st, now))
            let done = DispatchSemaphore(value: 0)
            session.dataTask(with: request) { _, response, error in
                reply.code = (response as? HTTPURLResponse)?.statusCode ?? 0
                reply.failure = error?.localizedDescription ?? "HTTP \(reply.code)"
                done.signal()
            }.resume()
            done.wait()
        }
        if (200..<300).contains(reply.code) {
            st["sent"] = word
            st["sent_at"] = now
            st["fail_at"] = 0
            st["error"] = nil
            return word
        }
        st["fail_at"] = now
        st["error"] = String(reply.failure.prefix(200))
        return "\(word) FAILED (\(reply.failure.prefix(60)))"
    }
}
