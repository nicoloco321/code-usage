#pragma once

// Copy this file to config.h and fill in your values:
//   cp config.h.example config.h
// config.h is gitignored because it holds secrets (Wi-Fi password, OAuth token).

// ---- Fill these in before flashing ----

#define WIFI_SSID   "YourWiFiName"
#define WIFI_PASS   "YourWiFiPassword"

// Dedicated OAuth refresh token for THIS device. Mint one with:
//   python3 server/device_login.py
// (do NOT use `claude setup-token` - those tokens lack the user:profile scope
// the usage endpoint requires, so they 403.) It's a separate login from your
// everyday Claude Code session, so the device refreshing it never logs you out
// elsewhere. The device turns it into short-lived access tokens on its own and
// remembers the rotated token in flash (NVS), so you only paste it once.
// Treat it like a password.
#define DEVICE_REFRESH_TOKEN "REPLACE_ME"

// Your local timezone, as a POSIX TZ string, so the device can render reset
// times in local time. A few examples:
//   US Pacific   "PST8PDT,M3.2.0,M11.1.0"
//   US Eastern   "EST5EDT,M3.2.0,M11.1.0"
//   UK           "GMT0BST,M3.5.0/1,M10.5.0"
//   Central Eu   "CET-1CEST,M3.5.0,M10.5.0/3"
// Full list: https://github.com/nayarsystems/posix_tz_db/blob/master/zones.csv
#define TIMEZONE    "PST8PDT,M3.2.0,M11.1.0"

// How often to refetch usage from Anthropic (ms). The login token tolerates
// this easily - the only limit left is a burst one (~5 requests in a few
// seconds trips a 5-min cooldown), which spaced polling never hits. 30s is also
// fine; going much below ~15s risks the burst limit. The LED reacts instantly
// via beacons regardless, and on a 429/403 the device auto-backs-off.
#define USAGE_POLL_MS 60000   // 60 seconds

// ---- "thinking" beacons ----
// The display listens on this mDNS name + port for pings from your computers.
// Run server/beacon.py on each machine you use Claude Code on; it pings here
// while Claude is working and the green LED blinks. Reach the device at
//   http://<MDNS_NAME>.local:<BEACON_PORT>/
#define MDNS_NAME    "claude-display"
#define BEACON_PORT  8080

// Keep blinking for this long after the last beacon. Senders ping every few
// seconds while active; this is the gap they're allowed before "idle".
#define BEACON_TTL_MS 12000

// Onboard RGB status LED. The Waveshare ESP32-C6-LCD-1.47 has a WS2812 on
// GPIO8. The 480x320 TFT_eSPI boards have no onboard RGB LED, so it's disabled
// there. Set to -1 to disable.
#if defined(USE_LOVYANGFX)
#define RGB_LED_PIN  8
#else
#define RGB_LED_PIN  -1
#endif

// Some WS2812s (including this Waveshare board's) wire red and green opposite to
// what neopixelWrite() assumes, so "green" comes out red. Set to 1 to swap them.
// If the breathing light shows the wrong colour, flip this.
#define RGB_LED_SWAP_RG 1
