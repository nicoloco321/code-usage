# Claude Code Usage Display

An ESP32 + SPI TFT desk display for your Claude Code rate limits. The default
build targets the **Waveshare ESP32-C6-LCD-1.47** (a 172x320 portrait panel);
the 480x320 landscape boards are still supported (see [Hardware](#hardware)).

- **Top** — Clawd, the Claude Code mascot (pixel art) + title
- **Middle** — animated thinking spinner whenever Claude is actively working
- **Bottom** — two live bars: your **5-hour limit** and **weekly limit**,
  with real utilization percentages and reset times (green < 50%, yellow < 80%, red above)
- **Onboard RGB LED** — blinks green whenever Claude is thinking (Waveshare C6 board)

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

Claude Code fires lifecycle [hooks](https://docs.claude.com/en/docs/claude-code/hooks)
you can hang a command on. We use three: `UserPromptSubmit` → **on**, `Stop` →
**off**, and `PreToolUse` → **on** (a keep-alive for long turns). Merge
[`server/claude-hooks.example.json`](server/claude-hooks.example.json) into your
`~/.claude/settings.json` (user-level, so it applies to every project), replacing
`claude-display.local` with the device's IP if mDNS doesn't resolve. Each hook is
just:

```sh
curl -sf -m 1 -X POST http://claude-display.local:8080/thinking/on  >/dev/null 2>&1 || true
curl -sf -m 1 -X POST http://claude-display.local:8080/thinking/off >/dev/null 2>&1 || true
```

The LED turns on the moment you hit enter and off the moment Claude stops — no
trailing delay. (`-m 1` keeps a hook from ever blocking Claude Code; the device's
5-min `BEACON_TTL_MS` is just a backstop if a `Stop` hook is ever missed.)

> **Use the device's IP, not `claude-display.local`, in hooks.** With the 1s
> `curl` timeout, `.local` mDNS names often don't resolve in time (macOS `ping`
> resolves them, but `curl` frequently can't), so the hook silently no-ops. Get
> the IP from the device's status line, or run
> [`python3 server/find_display.py`](server/find_display.py) — it resolves the
> device and prints the IP plus the exact hook commands ready to paste.
>
> Then **pin that IP** so it doesn't change out from under the hooks: add a
> **DHCP reservation** for the device in your router (map the display's MAC
> address to a fixed IP). Otherwise a new lease can reassign the address and the
> hooks quietly stop working. Hooks also load at **session start**, so restart
> Claude Code after editing `settings.json`.

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

On **Windows**, drop a shortcut to `pythonw beacon.py` in
`shell:startup`, or register it with Task Scheduler at logon.

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
