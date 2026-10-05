// DeskSwitch's entry point: a menu bar agent, with no Dock icon or windows.
// Everything it does is in DeskSwitch.swift.

import Cocoa

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
