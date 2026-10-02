#!/bin/bash
# Build DeskSwitch.app - the menu bar switcher for the Claude Code usage
# display. Its menu lists the screens installed on the display.
#
#   ./mac/build.sh          build into mac/DeskSwitch.app
#   ./mac/build.sh --run    build, then (re)launch it
#
# Needs the Xcode command line tools for swiftc; no other dependencies. Same
# bundle id as the desk-screen repo's DeskSwitch, so this one replaces it.

set -euo pipefail
cd "$(dirname "$0")"

APP="DeskSwitch.app"
BIN="$APP/Contents/MacOS/DeskSwitch"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"

# LSUIElement keeps it out of the Dock and the app switcher: this is a menu
# bar agent, it has no windows to switch to.
cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>              <string>DeskSwitch</string>
    <key>CFBundleDisplayName</key>       <string>DeskSwitch</string>
    <key>CFBundleIdentifier</key>        <string>local.deskscreen.deskswitch</string>
    <key>CFBundleVersion</key>           <string>2.0</string>
    <key>CFBundleShortVersionString</key><string>2.0</string>
    <key>CFBundlePackageType</key>       <string>APPL</string>
    <key>CFBundleExecutable</key>        <string>DeskSwitch</string>
    <key>LSMinimumSystemVersion</key>    <string>12.0</string>
    <key>LSUIElement</key>               <true/>
</dict>
</plist>
PLIST

swiftc -O -parse-as-library DeskSwitch.swift -o "$BIN" \
    -framework Cocoa -target "arm64-apple-macosx12.0"

# Ad-hoc signature. Without it macOS kills the app on launch on Apple silicon.
codesign --force --sign - "$APP" >/dev/null 2>&1 || \
    echo "warning: ad-hoc codesign failed; the app may not launch"

echo "built $APP"

if [[ "${1:-}" == "--run" ]]; then
    pkill -x DeskSwitch 2>/dev/null || true
    open "$APP"
    echo "launched - look for the icon in the menu bar"
fi
