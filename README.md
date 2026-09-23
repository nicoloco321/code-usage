# Claude Code Usage Display

An ESP32 + SPI TFT desk display for your Claude Code rate limits. The default
build targets the **Waveshare ESP32-C6-LCD-1.47** (a 172x320 portrait panel);
the 480x320 landscape boards are still supported (see [Hardware](#hardware)).

- **Top** — Clawd, the Claude Code mascot (pixel art) + title
- **Middle** — animated thinking spinner whenever Claude is actively working
- **Bottom** — two live bars: your **5-hour limit** and **weekly limit**,
  with real utilization percentages and reset times (green < 50%, yellow < 80%, red above)
- **Onboard RGB LED** — blinks green whenever Claude is thinking (Waveshare C6 board)
- **Spotify mode** — flip the same screen to a Spotify **now playing** view
  (track, artist, album, live progress bar). Switch from inside Claude Code
  with the **`/switch`** slash command, or with a one-line `curl`; the choice
  survives power cycles

No ESP32? The same display also runs as a **fullscreen Raspberry Pi app** that
starts on boot ([Raspberry Pi edition](#raspberry-pi-edition)), and a
**Windows tray helper** puts your usage next to the clock
([Windows tray helper](#windows-tray-helper)).

```
+------------------+   172x320 portrait
|  [Clawd] Claude  |
|         Code     |
|         usage..  |
|                  |
|    ( spinner )   |
|    working...    |
|------------------|
|  5-HOUR     33%  |
|  resets 4:19 AM  |
|  [#######      ] |
|                  |
|  WEEKLY     14%  |
|  resets Mon 1 PM |
|  [###          ] |
|  usage ok  ...   |
+------------------+
```

## How it works

The ESP32 talks to Anthropic directly — **no companion server required**:

1. **Usage** — the device fetches your real 5-hour / weekly utilization straight
   from Anthropic's OAuth usage API over HTTPS, using a **dedicated login** you
   mint once with `server/device_login.py`. It's a separate authorization from
   your everyday Claude Code session, so the device refreshing its token never
   logs you out anywhere. The access token is short-lived, so the device
   refreshes it itself and remembers the rotated token in flash (NVS). It also
   syncs time over NTP to render reset times locally.
   (Note: `claude setup-token` does **not** work here — those tokens lack the
   `user:profile` scope the usage endpoint requires.)
2. **"Claude is thinking"** — the usage API has no real-time activity signal, so
   the device listens for tiny HTTP *beacons* on `/thinking/on` and
   `/thinking/off`. There are two ways to send them (both optional — skip them
   and you just lose the LED/spinner):
   - **Claude Code hooks (precise, recommended)** — fire `on` the instant you
     submit a prompt and `off` the instant Claude finishes, straight from Claude
     Code's lifecycle events. No timeout, no guessing. See
     [Precise thinking via hooks](#3-optional-light-up-while-claude-is-thinking).
   - **`server/beacon.py` (no config)** — a dependency-free watcher that infers
     activity from `~/.claude/projects` session-log writes. Less precise (a small
     trailing delay) but needs zero setup. Run it on any machine; the display
     reacts whenever *any* of them is active.

3. **Screen modes** — the same tiny HTTP server switches what's on screen:
   `POST /mode/usage`, `/mode/spotify` or `/mode/toggle` (and `GET /mode` to
   ask). In Spotify mode the device polls Spotify's currently-playing endpoint
   with its own login (minted once with `server/spotify_login.py`, PKCE — no
   client secret on the device) and ticks the progress bar locally between
   polls. The repo ships a [`/switch` Claude Code command](.claude/commands/switch.md)
   that drives this. See [Spotify now-playing mode](#4-optional-spotify-now-playing-mode).
   `GET /usage` returns the numbers on screen as JSON (the Windows tray helper
   reads it).

> The old `server/claude_usage_server.py` (a Mac-side usage proxy) is no longer
> needed and is kept only as a fallback. The display is self-contained now.

## Hardware

Three build environments are defined in
[platformio.ini](firmware/platformio.ini); pick the one for your board.

| Env | Board | Panel | Notes |
|---|---|---|---|
| `waveshare-c6-lcd-147` | Waveshare ESP32-C6-LCD-1.47 | 172x320 ST7789 | **default**, portrait |
| `esp32-3248s035` | Sunton ESP32-3248S035 ("Cheap Yellow Display") | 480x320 ST7796 | landscape |
| `ili9488` | bare ESP32 + separate ILI9488 module | 480x320 ILI9488 | landscape, wire it yourself |

The **Waveshare ESP32-C6-LCD-1.47** has the 172x320 ST7789 panel wired to the
ESP32-C6 on-board: MOSI 6, SCLK 7, CS 14, DC 15, RST 21, backlight 22 (no
MISO). Those pins (plus colour order / inversion / the 34px column offset) live
in [display.h](firmware/src/display.h). If you get a blank or wrong-coloured
screen, cross-check them against the
[board wiki](https://www.waveshare.com/wiki/ESP32-C6-LCD-1.47) — wrong pins are
the usual cause.

> **Why the C6 is special:** it's RISC-V, so it needs two things the older
> boards don't.
> - **Toolchain:** Arduino-ESP32 core 3.x, which the stock PlatformIO
>   `espressif32` platform doesn't ship — the env uses the
>   [pioarduino](https://github.com/pioarduino/platform-espressif32) fork
>   instead (the first build downloads it, a few hundred MB). If the pinned
>   release URL 404s, bump it to the newest tag from the pioarduino releases.
> - **Graphics library:** TFT_eSPI has no working C6 driver (it miscompiles the
>   RISC-V SPI/GPIO as a classic Xtensa ESP32), so the C6 env uses **LovyanGFX**.
>   The 480x320 envs stay on TFT_eSPI. [display.h](firmware/src/display.h)
>   typedef-switches between the two, so the rest of the firmware is shared.

The 480x320 boards' pins and driver live in
[platformio.ini](firmware/platformio.ini) (per-env `build_flags`). Screen size
and rotation for each board are in [display.h](firmware/src/display.h), and the
layout scales from those.

## Setup

### 1. Mint a login for the device

```sh
python3 server/device_login.py
```

Open the URL it prints, approve access, and paste the code back. It runs the
same OAuth flow Claude Code uses (requesting the `user:profile` scope the usage
endpoint needs), checks the new token against the usage API, and prints a
`DEVICE_REFRESH_TOKEN` line to paste into config.h. This is a separate login
from your everyday Claude Code session, so it won't disturb it.

> Don't use `claude setup-token` — those tokens are inference-only and lack
> `user:profile`, so the usage endpoint returns `403`.

### 2. Configure and flash the ESP32

Edit [config.h](firmware/src/config.h):

- `WIFI_SSID` / `WIFI_PASS` — your 2.4 GHz network (ESP32 has no 5 GHz)
- `DEVICE_REFRESH_TOKEN` — the value printed by step 1
- `TIMEZONE` — your POSIX TZ string (examples are in the file) for reset times

Then plug in the board and:

```sh
cd firmware
pio run -t upload                          # Waveshare ESP32-C6-LCD-1.47 (default)
pio run -e esp32-3248s035 -t upload        # Sunton Cheap Yellow Display
pio run -e ili9488 -t upload               # generic ESP32 + ILI9488 module
```

All three environments are verified to compile. The first `waveshare-c6-lcd-147`
build is slow because it downloads the pioarduino toolchain. On boot the display
shows its address (e.g. `claude-display.local  192.168.1.42`) on the status line.

The bars should fill in within ~30 s. If they show `--`, see Troubleshooting.

### 3. (Optional) Light up while Claude is thinking

Pick **one** of these per machine. Hooks are precise (exact start/stop); the
beacon needs zero config but lags a little.

#### Option A — Claude Code hooks (recommended)

Claude Code fires lifecycle [hooks](https://docs.claude.com/en/docs/claude-code/hooks),
and [`server/display_hook.py`](server/display_hook.py) turns them into beacons.
One command installs it into `~/.claude/settings.json` (user-level, so every
project gets it), and Claude Code picks it up immediately:

```sh
python3 server/display_hook.py --install --host 192.168.1.42    # macOS / Linux
py -3 server\display_hook.py --install --host 192.168.1.42      # Windows
```

It tracks each Claude Code session on the machine as **working**, **waiting**
or **idle**:
- **Working:** you submit a prompt, or a tool runs.
- **Waiting:** a permission prompt or question is up. The display goes idle,
  since Claude is waiting on you.
- **Idle:** Claude finishes, errors out, or the session ends.

The display shows "working" while *any* session is. The hooks run async, so
they never slow Claude down. Because async hooks can land out of order, a
beacon arriving within a moment of a stop counts as a straggler, not new work.
`--status` shows what it sees; `--uninstall` removes it. It also replaces the
older curl hooks in
[`claude-hooks.example.json`](server/claude-hooks.example.json).

Hooks can't see two things:
- **Esc interrupts:** Claude Code fires no hook for them.
- **Tools running longer than the display's 5-minute backstop.**

A watcher covers both. The [Windows tray helper](#windows-tray-helper) runs it
automatically; elsewhere, run `display_hook.py --watch` alongside Claude Code.
Without it, an interrupted turn stays "working" until the 5-minute backstop
(`BEACON_TTL_MS`) clears it.

> **Name or IP?** `--host claude-display.local` is the most robust choice when
> it resolves quickly on your machine: it keeps working if the display's IP
> changes, for example when you move it from Ethernet to Wi-Fi. Windows 10/11
> resolves it natively. Check with
> [`python3 server/find_display.py`](server/find_display.py), which also
> prints the IP and the install command ready to paste. If the name doesn't
> resolve, use the IP, and **pin it** with a **DHCP reservation** in your router
> (map the display's MAC address to a fixed IP), so a new lease can't move it
> out from under the hooks. (The old curl hooks need the IP either way: with a
> 1s timeout, curl often can't resolve `.local` names in time.)

#### Option B — the beacon watcher (no config)

```sh
python3 server/beacon.py            # auto-finds the display at claude-display.local
python3 server/beacon.py --host 192.168.1.42   # or point at its IP
```

No dependencies — Python 3 stdlib only, macOS/Windows/Linux. It infers activity
from session-log writes, sends `/thinking/on` while busy and `/thinking/off` when
idle. Simpler, but a few seconds less precise than hooks.

> On **Windows**, the `claude-display.local` name needs Apple Bonjour installed.
> If it can't resolve, use the device's IP (in the hook URLs, or `--host`).

To keep the beacon running across reboots on macOS, add a LaunchAgent:

```sh
cat > ~/Library/LaunchAgents/com.nicoloco.claude-beacon.plist <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.nicoloco.claude-beacon</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/python3</string>
    <string>$HOME/Documents/code-usage/server/beacon.py</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict></plist>
EOF
launchctl load ~/Library/LaunchAgents/com.nicoloco.claude-beacon.plist
```

On **Windows**, the [tray helper](#windows-tray-helper) has this watcher built
in. Or drop a shortcut to `pythonw beacon.py` in `shell:startup`, or register
it with Task Scheduler at logon.

### 4. (Optional) Spotify now-playing mode

The display can flip to a "now playing" screen. It needs its own Spotify
authorization (any free account works):

1. Create an app at <https://developer.spotify.com/dashboard> (any
   name/description). In the app's settings add this **exact Redirect URI**:
   `http://127.0.0.1:8898/callback`, and tick **Web API**. You never need the
   client secret — the login uses PKCE.
2. Mint the token (paste the app's **Client ID** when prompted):

   ```sh
   python3 server/spotify_login.py
   ```

3. Paste the two printed `#define` lines (`SPOTIFY_CLIENT_ID`,
   `SPOTIFY_REFRESH_TOKEN`) into `firmware/src/config.h` and reflash.

Switch screens any time (the device remembers the mode across power cycles,
and the thinking LED keeps working in both modes):

```sh
curl -X POST http://claude-display.local:8080/mode/spotify   # now playing
curl -X POST http://claude-display.local:8080/mode/usage     # back to usage
curl -X POST http://claude-display.local:8080/mode/toggle    # flip
curl      http://claude-display.local:8080/mode              # ask
```

#### The `/switch` command in Claude Code

The repo ships a slash command at
[.claude/commands/switch.md](.claude/commands/switch.md) — inside this repo,
just type `/switch`. To use it from **any** project, copy it to your user
commands folder:

```sh
mkdir -p ~/.claude/commands && cp .claude/commands/switch.md ~/.claude/commands/
```

Then `/switch spotify`, `/switch usage`, `/switch toggle` — or plain
`/switch` to be asked which one you want. (Claude Code discovers new commands
at session start, so restart it once after copying.)

## Raspberry Pi edition

[pi/claude_display.py](pi/claude_display.py) is the same display as a
fullscreen app for a Raspberry Pi (built for a **Pi 2**, fine on anything
newer) on an HDMI monitor or TV, or the official touchscreen. It has the same
screens (usage with the thinking spinner; Spotify now playing, with album art)
and speaks the same HTTP API on port 8080, so the hooks, `beacon.py`,
`find_display.py` and `/switch` work unchanged. The layout follows the
screen's shape: landscape screens get a wide layout with a clock, portrait
ones the ESP32's stacked layout, and long bar panels their own (below).

**Long bar screens** like the Waveshare 11.9" (320×1480) get two dedicated
layouts. On its side, a **bar** shows two rows of long meters. Standing up, a
**strip** stacks everything with a big clock at the bottom. The panel is
portrait out of the box. To lay it on its side, either rotate it in
**Screen Configuration → HDMI-A-1 → Orientation** on the desktop edition
(this turns touch too), or set `rotate = 90` in config.ini (works on Lite too;
taps anywhere switch screens, so touch doesn't need turning). Preview it on
any PC with `--demo --windowed 1480x320`.

```
+--------------------------------------------------------------+
| [Clawd] Claude Code                                10:28 PM  |
|         usage monitor                            Tue Sep 22  |
|--------------------------------------------------------------|
|                    5-HOUR                              33%   |
|    ( spinner )     resets 12:41 AM  ·  in 2h 13m             |
|                    [##########                          ]    |
|    working...      WEEKLY                              86%   |
|                    resets Sat 2:28 AM  ·  in 3d 4h           |
|                    [##################################  ]    |
| usage ok                  claude-display.local  192.168.1.42 |
+--------------------------------------------------------------+
```

**You need:** a Pi 2 or newer, a screen, and a network connection. The Pi 2
has no Wi-Fi, so use Ethernet or a USB Wi-Fi dongle. Use **Raspberry Pi OS
Bullseye or newer, 32-bit**. Bullseye only packages pygame 1.9, so on
Bullseye the installer gets pygame 2 from pip. **Lite** is the better fit for
a Pi 2: the app draws straight to the screen with no desktop, so it boots
faster and leaves more RAM free. The desktop edition works too; tested on a
Pi 2 with the Bullseye desktop and the Waveshare 11.9" bar.

**Power:** a Pi 2 plus a USB-powered touchscreen and Wi-Fi dongle easily
exceeds a phone charger. If the desktop shows "Low voltage warning" (or
`vcgencmd get_throttled` isn't `0x0`), use a proper 5V 2.5A supply and power
the screen from its own Power port.

1. Flash the OS with Raspberry Pi Imager. In its settings, set the hostname
   to `claude-display`, your user, Wi-Fi (for a dongle), SSH, and **your time
   zone** (reset times use the Pi's clock).
2. On the Pi, or over SSH:

   ```sh
   sudo apt install -y git
   git clone https://github.com/nicoloco321/code-usage.git
   cd code-usage
   bash pi/install.sh
   ```

   The installer:
   - installs pygame and creates `~/.config/claude-display/config.ini`
   - walks you through `device_login.py` (paste the code as usual) and,
     optionally, the Spotify login
   - offers to rename the Pi to `claude-display`, so `claude-display.local`
     resolves
   - turns off screen blanking
   - makes the display start fullscreen on boot: a `claude-display` systemd
     service on Lite, or an autostart entry plus desktop auto-login on the
     desktop edition

   It's safe to re-run.
3. Reboot if it asks you to. From then on the display comes up by itself.

**Using it:**

- Tap the screen (or click, or press Space) to switch screens. Ctrl+Q quits.
- **Session panel.** While Claude works, or waits on you, the usage
  screen's bars slide to the right end of the screen and shrink, opening the
  middle for what's going on. It shows each session's project and how long
  it's been working, what Claude is doing right now ("Running: Compile the
  firmware", "Editing main.cpp"), any subagents and what they're doing, and a
  yellow "needs you" with the reason when a permission prompt or question is
  waiting. When Claude is done the bars slide back. The details come from the
  [Claude Code hooks](#option-a--claude-code-hooks-recommended), which send
  them along with each beacon (the ESP32 ignores them). Beacons from
  `beacon.py` or the old curl hooks just show "Claude is working". This is on
  the bar and landscape layouts.
- The Pi has no status LED, so while Claude works the Spotify and printer
  screens' status line shows a small spinner and "Claude is working...".
- **3D printer screen (Bambu Lab).** A third screen follows a print on a
  Bambu Lab printer on your network: print name, a progress bar with the
  percentage, layer count, time left and when it'll finish, and
  nozzle/bed temperatures. Paused prints turn the bar yellow, failed ones red.
  Set it up on the Pi with:

  ```sh
  python3 pi/claude_display.py --setup-bambu
  ```

  It finds the printer on the network (Bambu printers announce themselves),
  asks for its **access code** (on the printer's screen under Settings →
  WLAN, or Network on an X1), checks it can log in, and saves it to
  config.ini. Restart the display, then tap through to the new screen, or use
  `POST /mode/bambu`. The display reads the printer's local status feed
  (MQTT over TLS on port 8883) directly: no Bambu cloud login, and only
  while the printer screen is showing. If the printer refuses a correct code,
  newer firmware may need **LAN Only Mode** with **Developer Mode** turned on.
- Settings live in `~/.config/claude-display/config.ini`: poll rates, port,
  `size = 1280x720` to push fewer pixels on a big TV (easier on a Pi 2), and
  `rotate` for a monitor mounted on its side. Apply changes with
  `sudo systemctl restart claude-display`. Logs:
  `journalctl -u claude-display -f`.
- Re-mint a login any time with
  `python3 server/device_login.py --config ~/.config/claude-display/config.ini`
  (`spotify_login.py` takes `--config` too).
- Rotated tokens and the chosen screen persist in
  `~/.local/state/claude-display/state.json`, the Pi's equivalent of the
  ESP32's NVS.
- **Spotify login over SSH:** Spotify redirects to `127.0.0.1:8898`, so
  connect with `ssh -L 8898:127.0.0.1:8898 you@claude-display.local`, run the
  login in that session, and open its link on your computer.
- **Try it anywhere, no logins needed:**
  `python3 pi/claude_display.py --demo --windowed 800x480` (on a PC,
  `pip install pygame` first).

## Windows tray helper

[windows/claude_tray.py](windows/claude_tray.py) puts the display in the
Windows notification area, next to the clock:

- **Clawd with a 5-hour meter** under him (green / yellow / red), or the
  5-hour % itself if you prefer. Hover for both numbers; right-click for reset
  times.
- Clawd **walks** while Claude is working, on any of your machines.
- **Left-click** cycles the display through its screens (usage, Spotify,
  3D printer); the menu lists them all.
- **Track Claude with hooks (exact)** installs or removes the
  [Claude Code hooks](#option-a--claude-code-hooks-recommended). While they're
  installed, the tray runs their watcher: it catches Esc interrupts and keeps
  the display awake through long tool runs. Without hooks, **Guess activity
  from transcripts** falls back to the `beacon.py` approach.
- **Start with Windows.**

It reads everything from the display's `GET /usage`, so the PC needs no
Anthropic login and adds no load on the rate-limited usage API. It works with
the Pi app, and with the ESP32 once it's flashed with this firmware. Older
firmware still gets screen switching and beacons, and the menu tells you to
re-flash for the numbers.

```powershell
py -3 -m pip install -r windows\requirements.txt
pyw -3 windows\claude_tray.py --host 192.168.1.42
```

`--host` is the display's IP or name, shown on its status line or by
`find_display.py`. You only need it the first time: it's saved to
`%APPDATA%\claude-display\tray.json`, and **Display address…** in the menu
changes it later (and re-points the hooks). Windows often resolves
`claude-display.local` on its own, but the IP always works. Then tick
**Track Claude with hooks** and **Start with Windows**.

> Use `py -3` / `pyw -3` rather than `python` / `pythonw` if you have more than
> one Python: the bare names can start a different install than the one you
> gave the packages to. With the **Microsoft Store** Python, Windows keeps
> `tray.json` (and the hooks' state file) in the app's private folder,
> `%LOCALAPPDATA%\Packages\PythonSoftwareFoundation.Python.3.x_…\LocalCache\Roaming\claude-display`,
> rather than `%APPDATA%`. The hooks pin the same Python as the tray, so they
> share it.

## Customizing

- **Mascot** — pixel grid in [mascot.h](firmware/src/mascot.h); edit the
  array, any size works (adjust the scale passed in `drawStaticUI`).
- **Thinking animation** — a frame-drawn rotating starburst in `drawSpinner`
  ([main.cpp](firmware/src/main.cpp)). Frame-based drawing looks crisper than
  decoding an actual GIF on-device, but if you want a real GIF, the
  `bitbank2/AnimatedGIF` library works well with TFT_eSPI.
- **Screen upside down?** Change `SCREEN_ROTATION` in
  [display.h](firmware/src/display.h) (C6: `0`↔`2`; landscape boards: `1`↔`3`).
- **Colours wrong?** On the C6, toggle `cfg.rgb_order` (red/blue swapped) or
  `cfg.invert` (photo-negative) in [display.h](firmware/src/display.h); on the
  480x320 boards, toggle `-DTFT_RGB_ORDER=TFT_BGR` in `build_flags`.
- **Poll rate / thresholds** — `USAGE_POLL_MS` in config.h; bar colors in
  `barColor()`; "working" detection window is `ACTIVE_WINDOW_SECS` in
  `beacon.py`, and how long the LED keeps blinking after the last beacon is
  `BEACON_TTL_MS` in config.h.
- **Spotify poll rate** — `SPOTIFY_POLL_MS` in config.h paces how fast track
  changes/seeks show up; the progress bar animates locally between polls
  either way. The Spotify screen layout lives in the `SP_*` constants in
  [main.cpp](firmware/src/main.cpp).
- **RGB LED** — pin is `RGB_LED_PIN` in config.h (GPIO8 on the Waveshare C6,
  `-1` to disable); `RGB_LED_SWAP_RG` fixes boards that show the wrong colour.
  The green "breathing" effect (brightness, speed) lives in `updateLed()` /
  `RGB_LED_MAX` / `LED_BREATHE_MS` in main.cpp.

## Troubleshooting

| Symptom | Fix |
|---|---|
| White / blank screen | Wrong driver for your panel — try the other env, check `TFT_BL` pin |
| Bars show `--` | No successful fetch yet; check the status line and Wi-Fi |
| "auth failed - run device_login.py" | The refresh token was rejected (revoked, or NVS was wiped and config.h's token is stale). Re-run `python3 server/device_login.py` and update `DEVICE_REFRESH_TOKEN` in config.h |
| "rate limited, retry in …s" | Throttled on the usage endpoint. The device backs off automatically (honors `Retry-After`) and clears itself. Don't lower `USAGE_POLL_MS` much, and avoid polling the same token from elsewhere |
| "usage fetch failed" | Wi-Fi/DNS issue, or Anthropic unreachable; the device keeps retrying |
| Reset times look wrong | Set the correct `TIMEZONE` in config.h; they're blank until NTP syncs (~few s) |
| LED/spinner never moves | Run `server/beacon.py` on the busy machine; check it prints `blinking`, not `could not reach …` |
| Beacon can't find device | Use `--host <IP shown on the display>` (Windows needs Bonjour for `.local`) |
| `/mode/spotify` answers 409 / "spotify not set up" | Spotify isn't configured: run `python3 server/spotify_login.py`, paste both `#define`s into config.h, reflash |
| "spotify auth failed" | Refresh token revoked or wrong client id — re-run `spotify_login.py`. A persistent 403 usually means your account isn't added to the Spotify app (Dashboard → your app → User Management) |
| "nothing playing" but music is on | Spotify only reports an *active* device; start playback from any Spotify app and it appears within one poll (~5 s) |
| Pi: blank screen, service keeps restarting | `journalctl -u claude-display -e`. "could not open the screen" on Lite means no KMS driver: `/boot/firmware/config.txt` needs `dtoverlay=vc4-kms-v3d` (the default). Re-run `bash pi/install.sh` to fix group access |
| Pi: picture has black borders or is cut off | Turn off overscan (`sudo raspi-config` → Display Options), or force a mode with `size = WxH` in config.ini |
| Tray icon is grey / "Display not reachable" | Set the right address with **Display address…** in the tray menu (the IP from the display's status line always works) |
| Tray says "Re-flash the display firmware" | The ESP32 predates `GET /usage`; flash this version. Switching screens and beacons work either way |
