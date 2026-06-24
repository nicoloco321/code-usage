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
   from Anthropic's OAuth usage API over HTTPS, using a **dedicated long-lived
   token** you mint once with `claude setup-token`. That token is separate from
   your everyday Claude Code login, so the device refreshing it never logs you
   out anywhere. The device also syncs time over NTP to render reset times
   locally.
2. **"Claude is thinking"** — the usage API has no real-time activity signal, so
   the device listens for tiny HTTP *beacons* instead. **`server/beacon.py`** is
   a small, dependency-free script you run on any computer you use Claude Code on
   (macOS, Windows, Linux). It watches that machine's `~/.claude/projects`
   session logs and pings the display while Claude is working — and the green LED
   blinks. Run it on every machine you use; the display blinks whenever *any* of
   them is active. (It's optional — skip it and you just lose the LED/spinner.)

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

### 1. Mint a token for the device

On any machine logged into Claude Code:

```sh
claude setup-token
```

Copy the `sk-ant-oat01-...` value it prints. This is a long-lived token just for
the display; it won't disturb your normal Claude Code login.

Optionally verify it can read usage before flashing — this should print JSON
with a `five_hour` block:

```sh
curl -s https://api.anthropic.com/api/oauth/usage \
  -H "Authorization: Bearer sk-ant-oat01-..." \
  -H "anthropic-beta: oauth-2025-04-20" | head -c 200
```

### 2. Configure and flash the ESP32

Edit [config.h](firmware/src/config.h):

- `WIFI_SSID` / `WIFI_PASS` — your 2.4 GHz network (ESP32 has no 5 GHz)
- `ANTHROPIC_TOKEN` — the `sk-ant-oat01-...` value from step 1
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

### 3. (Optional) Blink the LED while Claude is thinking

Run the beacon on each computer you use Claude Code on:

```sh
python3 server/beacon.py            # auto-finds the display at claude-display.local
python3 server/beacon.py --host 192.168.1.42   # or point at its IP
```

No dependencies — Python 3 stdlib only, and it works on macOS, Windows and Linux.
While Claude is working on that machine it pings the display and the green LED
blinks; run it on several machines and the display reacts to whichever is busy.

> On **Windows**, the `claude-display.local` name needs Apple Bonjour installed.
> If it can't resolve, pass `--host <the IP shown on the display>`.

To keep it running across reboots on macOS, add a LaunchAgent:

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
  `-1` to disable). Colour/blink rate live in `updateLed()` in main.cpp.

## Troubleshooting

| Symptom | Fix |
|---|---|
| White / blank screen | Wrong driver for your panel — try the other env, check `TFT_BL` pin |
| Bars show `--` | No successful fetch yet; check the status line and Wi-Fi |
| "token rejected" | Re-run `claude setup-token` and update `ANTHROPIC_TOKEN` in config.h |
| "usage fetch failed" | Wi-Fi/DNS issue, or Anthropic unreachable; the device keeps retrying |
| Reset times look wrong | Set the correct `TIMEZONE` in config.h; they're blank until NTP syncs (~few s) |
| LED/spinner never moves | Run `server/beacon.py` on the busy machine; check it prints `blinking`, not `could not reach …` |
| Beacon can't find device | Use `--host <IP shown on the display>` (Windows needs Bonjour for `.local`) |
