#pragma once

// ---- Fill these in before flashing ----

#define WIFI_SSID   "SSID"
#define WIFI_PASS   "PASSWORD"

// Dedicated long-lived OAuth token for THIS device.
//   1. On any machine logged into Claude Code, run:  claude setup-token
//   2. Paste the sk-ant-oat01-... value it prints here.
// This token is separate from your everyday Claude Code login, so the device
// using (and occasionally refreshing) it never logs you out anywhere else.
// Treat it like a password - anyone with it can read your usage.
#define ANTHROPIC_TOKEN "sk-ant-oat01-REPLACE_ME"

// Your local timezone, as a POSIX TZ string, so the device can render reset
// times in local time. A few examples:
//   US Pacific   "PST8PDT,M3.2.0,M11.1.0"
//   US Eastern   "EST5EDT,M3.2.0,M11.1.0"
//   UK           "GMT0BST,M3.5.0/1,M10.5.0"
//   Central Eu   "CET-1CEST,M3.5.0,M10.5.0/3"
// Full list: https://github.com/nayarsystems/posix_tz_db/blob/master/zones.csv
#define TIMEZONE    "PST8PDT,M3.2.0,M11.1.0"

// How often to refetch usage from Anthropic (ms). The API is cached server-side;
// keep this gentle - the LED reacts instantly via beacons regardless.
#define USAGE_POLL_MS 30000

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
