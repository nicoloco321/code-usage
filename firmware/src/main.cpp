// Claude Code usage display for ESP32 + SPI TFT.
//
// Fully self-contained: the ESP32 fetches your real 5-hour / weekly rate-limit
// utilization straight from Anthropic's OAuth usage API over HTTPS, using a
// dedicated login it refreshes itself (see config.h and server/device_login.py).
// No companion server needed.
//
// "Claude is thinking right now" can't be read from the usage API, so the
// device listens for tiny HTTP "beacons" instead: run server/beacon.py on any
// computer you use Claude Code on (Mac, Windows, Linux) and it pings the device
// while Claude is working. The onboard RGB LED blinks green whenever a beacon
// is live, from any machine.
//
// Layout (portrait 172x320, e.g. Waveshare ESP32-C6-LCD-1.47):
//   top:     Clawd mascot + title
//   middle:  animated thinking spinner while a beacon says Claude is working
//   bottom:  5-hour limit bar and weekly limit bar, then a status line
//
// Layout geometry is derived from SCREEN_W / SCREEN_H below, so the same code
// drives the 480x320 landscape boards too (see other envs in platformio.ini).

#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <WebServer.h>
#include <ESPmDNS.h>
#include <ArduinoJson.h>
#include <Preferences.h>
#include <time.h>
#include <ctype.h>

#include "display.h"
#include "config.h"
#include "mascot.h"

DisplayTFT tft;
DisplaySprite spin(&tft);

static const char *USAGE_URL = "https://api.anthropic.com/api/oauth/usage";
static const char *TOKEN_URL = "https://console.anthropic.com/v1/oauth/token";
static const char *OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e";  // Claude Code's public client

// OAuth tokens. The usage endpoint needs a user:profile-scoped token, which the
// device gets from DEVICE_REFRESH_TOKEN (minted by server/device_login.py). The
// access token lasts only ~8h, so the device refreshes it itself and remembers
// the rotated refresh token in flash (NVS) - it's a separate login from your
// Mac's, so this never disturbs Claude Code there.
static Preferences prefs;
static String g_access;
static String g_refresh;
static long   g_expiresAt = 0;  // epoch seconds when g_access expires

// ---- palette (RGB565) ----
static const uint16_t COL_BG     = 0x1082;  // #121212 near-black
static const uint16_t COL_CARD   = 0x2945;  // dark gray track
static const uint16_t COL_ORANGE = 0xDBAA;  // #D97757 Claude orange
static const uint16_t COL_TEXT   = 0xEF7D;  // #ECECEC
static const uint16_t COL_DIM    = 0x7BCF;  // mid gray
static const uint16_t COL_GREEN  = 0x3DCA;
static const uint16_t COL_YELLOW = 0xDD08;
static const uint16_t COL_RED    = 0xE289;

// SCREEN_W / SCREEN_H / SCREEN_ROTATION come from display.h (per board).

// ---- layout (portrait) ----
static const int SPIN_SIZE = 60;
static const int SPIN_X = (SCREEN_W - SPIN_SIZE) / 2;  // centered
static const int SPIN_Y = 78;

static const int BAR_X = 12;
static const int BAR_W = SCREEN_W - 2 * BAR_X;
static const int BAR_H = 24;
static const int PCT_X = SCREEN_W - BAR_X;  // right edge of the % / reset readouts
static const int DIV_Y = 166;               // divider under the header
static const int SEC1_Y = 176;              // 5-hour section
static const int SEC2_Y = 244;              // weekly section
static const int STATUS_Y = 304;

struct Usage {
    bool  valid = false;
    float fivePct = -1;
    float weekPct = -1;
    char  fiveReset[24] = "";
    char  weekReset[24] = "";
};

static Usage cur;
static WebServer beacon(BEACON_PORT);
static volatile unsigned long lastBeacon = 0;  // millis() of the last "thinking" ping

static unsigned long lastPoll = 0;
static unsigned long pollBackoff = 0;   // >0 => wait this long before next poll (429 cooldown)
static unsigned long lastFrame = 0;
static unsigned long lastOkFetch = 0;
static int  frame = 0;
static bool idleSpinnerDrawn = false;
static int  lastStatusWord = -1;   // 0=idle 1=working
static uint32_t lastLed = 0xFFFFFFFFu;  // cache so we only push the LED on change

// ---------------------------------------------------------------- LED

static void ledSet(uint8_t r, uint8_t g, uint8_t b) {
#if RGB_LED_PIN >= 0
    uint32_t packed = ((uint32_t)r << 16) | ((uint32_t)g << 8) | b;
    if (packed == lastLed) return;  // neopixelWrite re-clocks the LED; skip no-ops
    lastLed = packed;
#if RGB_LED_SWAP_RG
    neopixelWrite(RGB_LED_PIN, g, r, b);  // board wires R/G swapped
#else
    neopixelWrite(RGB_LED_PIN, r, g, b);
#endif
#else
    (void)r; (void)g; (void)b;
#endif
}

static const int RGB_LED_MAX   = 70;     // peak green brightness (full is harsh)
static const int LED_BREATHE_MS = 3500;  // one inhale+exhale

// Breathe green while a beacon is live, otherwise off.
static void updateLed(bool active, unsigned long now) {
    if (!active) {
        ledSet(0, 0, 0);
        return;
    }
    // Smooth 0->1->0 over LED_BREATHE_MS; squared for a more natural ease that
    // lingers dim, like breathing rather than a triangle fade.
    float phase = (now % LED_BREATHE_MS) / (float)LED_BREATHE_MS;  // 0..1
    float level = 0.5f - 0.5f * cosf(phase * 2.0f * PI);           // 0..1..0
    level *= level;
    ledSet(0, (uint8_t)(level * RGB_LED_MAX + 0.5f), 0);
}

// ---------------------------------------------------------------- time

// resets_at arrives as ISO-8601 UTC, e.g. "2026-06-24T06:30:00.5+00:00".
// Render it in the configured local timezone like the old server did.
static time_t utc_from_tm(const struct tm *t) {
    static const int mdays[] = {0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334};
    long year = t->tm_year + 1900;
    long days = (year - 1970) * 365 + (year - 1969) / 4
                - (year - 1901) / 100 + (year - 1601) / 400;
    days += mdays[t->tm_mon] + (t->tm_mday - 1);
    bool leap = (year % 4 == 0 && year % 100 != 0) || year % 400 == 0;
    if (t->tm_mon > 1 && leap) days += 1;
    return ((time_t)days * 24 + t->tm_hour) * 3600 + t->tm_min * 60 + t->tm_sec;
}

static void fmtReset(const char *iso, char *out, size_t n) {
    out[0] = '\0';
    if (!iso || !iso[0]) return;
    struct tm t = {};
    if (sscanf(iso, "%d-%d-%dT%d:%d:%d",
               &t.tm_year, &t.tm_mon, &t.tm_mday,
               &t.tm_hour, &t.tm_min, &t.tm_sec) < 5) return;
    t.tm_year -= 1900;
    t.tm_mon  -= 1;
    time_t when = utc_from_tm(&t);

    struct tm local;
    localtime_r(&when, &local);
    time_t nowt = time(nullptr);
    struct tm nowlocal;
    localtime_r(&nowt, &nowlocal);

    // Only say "today" once NTP has actually set the clock (year >= 2024).
    bool synced = nowlocal.tm_year + 1900 >= 2024;
    char buf[32];
    if (synced && local.tm_year == nowlocal.tm_year && local.tm_yday == nowlocal.tm_yday)
        strftime(buf, sizeof(buf), "%I:%M %p", &local);          // "03:00 PM"
    else
        strftime(buf, sizeof(buf), "%a %I:%M %p", &local);       // "Wed 09:00 AM"

    // Trim a leading zero on the hour ("03:00" -> "3:00") to match the old look.
    char *colon = strchr(buf, ':');
    if (colon) {
        char *hour = colon;  // walk back over the hour digits before ':'
        while (hour > buf && isdigit((unsigned char)*(hour - 1))) hour--;
        if (colon - hour == 2 && hour[0] == '0') memmove(hour, hour + 1, strlen(hour));
    }
    strlcpy(out, buf, n);
}

// ---------------------------------------------------------------- drawing

static uint16_t barColor(float pct) {
    if (pct < 50) return COL_GREEN;
    if (pct < 80) return COL_YELLOW;
    return COL_RED;
}

static void drawStatusLine(const char *msg, uint16_t color) {
    tft.fillRect(0, STATUS_Y, SCREEN_W, 12, COL_BG);
    tft.setTextDatum(TL_DATUM);
    tft.setTextColor(color, COL_BG);
    tft.drawString(msg, BAR_X, STATUS_Y, 1);  // font 1 to fit the narrow panel
}

static void drawSpinner(bool active) {
    spin.fillSprite(COL_BG);
    uint16_t c = active ? COL_ORANGE : COL_CARD;
    float rot = active ? frame * 7.5f * DEG_TO_RAD : 0.0f;
    const float cx = SPIN_SIZE / 2.0f, cy = SPIN_SIZE / 2.0f;
    for (int i = 0; i < 8; i++) {
        float a = rot + i * (PI / 4.0f);
        float ca = cosf(a), sa = sinf(a);
#if defined(USE_LOVYANGFX)
        // LovyanGFX anti-aliases against existing sprite pixels (pre-filled
        // with COL_BG), so no explicit background-colour argument.
        spin.drawWideLine(cx + ca * 7, cy + sa * 7,
                          cx + ca * 25, cy + sa * 25, 3.0f, c);
#else
        spin.drawWideLine(cx + ca * 7, cy + sa * 7,
                          cx + ca * 25, cy + sa * 25, 3.0f, c, COL_BG);
#endif
    }
#if defined(USE_LOVYANGFX)
    spin.fillCircle((int)cx, (int)cy, 3, c);
#else
    spin.fillSmoothCircle((int)cx, (int)cy, 3, c, COL_BG);
#endif
    spin.pushSprite(SPIN_X, SPIN_Y);
}

static void drawStatusWord(bool active) {
    int word = active ? 1 : 0;
    if (word == lastStatusWord) return;
    lastStatusWord = word;
    tft.fillRect(0, SPIN_Y + SPIN_SIZE + 4, SCREEN_W, 18, COL_BG);
    tft.setTextDatum(TC_DATUM);
    tft.setTextColor(active ? COL_ORANGE : COL_DIM, COL_BG);
    tft.drawString(active ? "working..." : "idle", SCREEN_W / 2,
                   SPIN_Y + SPIN_SIZE + 4, 2);
}

static void drawBar(int y, const char *label, float pct, const char *reset) {
    // label, left-aligned and dim
    tft.setTextDatum(TL_DATUM);
    tft.setTextColor(COL_DIM, COL_BG);
    tft.drawString(label, BAR_X, y, 2);

    // % readout, bright, right-aligned on the same row as the label
    tft.fillRect(BAR_X + 96, y, PCT_X - (BAR_X + 96), 16, COL_BG);
    tft.setTextDatum(TR_DATUM);
    tft.setTextColor(COL_TEXT, COL_BG);
    if (pct >= 0) {
        char buf[8];
        snprintf(buf, sizeof(buf), "%d%%", (int)roundf(pct));
        tft.drawString(buf, PCT_X, y, 2);
    } else {
        tft.drawString("--", PCT_X, y, 2);
    }

    // reset time on its own line below the label
    tft.fillRect(BAR_X, y + 17, BAR_W, 10, COL_BG);
    if (reset[0] != '\0') {
        char buf[40];
        snprintf(buf, sizeof(buf), "resets %s", reset);
        tft.setTextDatum(TL_DATUM);
        tft.setTextColor(COL_DIM, COL_BG);
        tft.drawString(buf, BAR_X, y + 17, 1);
    }

    // the bar itself, full width
    int by = y + 30;
    tft.fillRoundRect(BAR_X, by, BAR_W, BAR_H, 6, COL_CARD);
    if (pct >= 0) {
        int fw = (int)(BAR_W * (pct > 100 ? 100 : pct) / 100.0f);
        if (fw > 8) tft.fillRoundRect(BAR_X, by, fw, BAR_H, 6, barColor(pct));
    }
}

static void drawBars() {
    drawBar(SEC1_Y, "5-HOUR", cur.fivePct, cur.fiveReset);
    drawBar(SEC2_Y, "WEEKLY", cur.weekPct, cur.weekReset);
}

static void drawStaticUI() {
    tft.fillScreen(COL_BG);
    drawMascot(tft, BAR_X, 12, 4, COL_ORANGE, TFT_BLACK);  // 13x10 grid -> 52x40
    tft.setTextDatum(TL_DATUM);
    tft.setTextColor(COL_ORANGE, COL_BG);
    tft.drawString("Claude Code", 68, 18, 2);
    tft.setTextColor(COL_DIM, COL_BG);
    tft.drawString("usage monitor", 68, 40, 1);
    tft.drawFastHLine(BAR_X, DIV_Y, BAR_W, COL_CARD);
    drawBars();
}

// ---------------------------------------------------------------- oauth tokens

static void loadTokens() {
    prefs.begin("clt", false);
    g_refresh   = prefs.getString("refresh", "");
    g_access    = prefs.getString("access", "");
    g_expiresAt = prefs.getLong("exp", 0);
    if (g_refresh.length() == 0) g_refresh = DEVICE_REFRESH_TOKEN;  // first boot: seed from config.h
}

static void saveTokens() {
    prefs.putString("refresh", g_refresh);
    prefs.putString("access", g_access);
    prefs.putLong("exp", g_expiresAt);
}

// Exchange a refresh token for a fresh access token (and the rotated refresh
// token), Claude Code-style. Persists the result. Returns true on success.
static bool tryRefresh(const String &refreshTok) {
    if (refreshTok.length() == 0) return false;
    WiFiClientSecure client;
    client.setInsecure();
    HTTPClient http;
    http.setConnectTimeout(5000);
    http.setTimeout(8000);
    if (!http.begin(client, TOKEN_URL)) return false;
    http.addHeader("Content-Type", "application/json");
    http.addHeader("User-Agent", "claude-usage-display/1.0");  // Cloudflare 1010-blocks the default UA
    String body = String("{\"grant_type\":\"refresh_token\",\"refresh_token\":\"") +
                  refreshTok + "\",\"client_id\":\"" + OAUTH_CLIENT_ID + "\"}";
    int code = http.POST(body);
    if (code != 200) { http.end(); return false; }
    String resp = http.getString();
    http.end();

    JsonDocument doc;
    if (deserializeJson(doc, resp)) return false;
    const char *at = doc["access_token"] | "";
    if (!at[0]) return false;
    g_access = at;
    const char *rt = doc["refresh_token"] | "";
    if (rt[0]) g_refresh = rt;  // refresh tokens rotate; keep the new one
    long expires_in = doc["expires_in"] | 28800;       // ~8h default
    g_expiresAt = (long)time(nullptr) + expires_in;
    saveTokens();
    return true;
}

// Make sure g_access is valid, refreshing if it's missing or about to expire.
// Falls back to the config.h token if the stored (rotated) one is rejected -
// e.g. after a full flash erase wiped NVS but left a fresh token in config.h.
static bool ensureAccessToken() {
    time_t now = time(nullptr);
    bool synced = now > 1700000000;  // NTP set the clock (after ~2023)
    if (g_access.length() && synced && now < g_expiresAt - 300) return true;
    if (tryRefresh(g_refresh)) return true;
    String cfg = DEVICE_REFRESH_TOKEN;
    if (g_refresh != cfg && tryRefresh(cfg)) return true;
    return false;
}

// ---------------------------------------------------------------- network

// One GET to the usage endpoint with the current access token. Returns the HTTP
// status (200 ok), or a negative HTTPClient error. On 429/403, retryAfter is
// set to the server's requested cooldown in seconds.
static int usageRequest(Usage &u, int &retryAfter) {
    retryAfter = 0;
    WiFiClientSecure client;
    client.setInsecure();  // skip CA validation; fine on a home LAN to a known host

    HTTPClient http;
    http.setConnectTimeout(5000);
    http.setTimeout(5000);
    if (!http.begin(client, USAGE_URL)) return -1;
    http.addHeader("Authorization", String("Bearer ") + g_access);
    http.addHeader("anthropic-beta", "oauth-2025-04-20");
    http.addHeader("Content-Type", "application/json");
    http.addHeader("User-Agent", "claude-usage-display/1.0");
    const char *collect[] = {"Retry-After"};
    http.collectHeaders(collect, 1);

    int code = http.GET();
    if (code != 200) {
        if (code == 429 || code == 403) retryAfter = http.header("Retry-After").toInt();
        http.end();
        return code;
    }
    String body = http.getString();
    http.end();

    // Only pull the fields we render, so a big response stays cheap to parse.
    JsonDocument filter;
    for (const char *k : {"five_hour", "seven_day"}) {
        filter[k]["utilization"] = true;
        filter[k]["resets_at"] = true;
    }
    JsonDocument doc;
    if (deserializeJson(doc, body, DeserializationOption::Filter(filter))) return -2;

    u.fivePct = doc["five_hour"]["utilization"] | -1.0f;
    u.weekPct = doc["seven_day"]["utilization"] | -1.0f;
    fmtReset(doc["five_hour"]["resets_at"] | "", u.fiveReset, sizeof(u.fiveReset));
    fmtReset(doc["seven_day"]["resets_at"] | "", u.weekReset, sizeof(u.weekReset));
    u.valid = true;
    return 200;
}

// Fetch usage, getting/refreshing the access token as needed. Returns the HTTP
// status (200 ok), -3 if no valid token could be obtained, or a negative
// HTTPClient error. A 401 means the token went stale mid-flight: refresh once
// and retry.
static int fetchUsage(Usage &u, int &retryAfter) {
    retryAfter = 0;
    if (!ensureAccessToken()) return -3;
    int code = usageRequest(u, retryAfter);
    if (code == 401) {
        g_expiresAt = 0;  // force a refresh
        if (ensureAccessToken()) code = usageRequest(u, retryAfter);
    }
    return code;
}

// ---------------------------------------------------------------- beacons

// /thinking and /thinking/on -> Claude is working (refresh the keep-alive).
static void handleThinkingOn() {
    lastBeacon = millis();
    beacon.send(200, "text/plain", "on\n");
}

// /thinking/off -> Claude stopped: go idle immediately, no trailing timeout.
static void handleThinkingOff() {
    lastBeacon = 0;
    beacon.send(200, "text/plain", "off\n");
}

static void handleRoot() {
    beacon.send(200, "text/plain",
                "Claude Code usage display. POST /thinking/on while working, "
                "/thinking/off when done.\n");
}

// "Thinking" is sticky between an explicit on and off. BEACON_TTL_MS is only a
// backstop: if a sender dies mid-turn and never sends /thinking/off, fall idle.
static bool beaconActive(unsigned long now) {
    return lastBeacon != 0 && (now - lastBeacon) < BEACON_TTL_MS;
}

// ---------------------------------------------------------------- arduino

void setup() {
    Serial.begin(115200);
    ledSet(0, 0, 0);
    loadTokens();

    tft.init();
    tft.setRotation(SCREEN_ROTATION);  // C6 portrait=0 (use 2 if upside down); landscape boards=1
    spin.setColorDepth(16);
    spin.createSprite(SPIN_SIZE, SPIN_SIZE);

    drawStaticUI();
    drawSpinner(false);
    drawStatusWord(false);
    drawStatusLine("connecting to WiFi...", COL_DIM);

    WiFi.mode(WIFI_STA);
    WiFi.setAutoReconnect(true);
    WiFi.begin(WIFI_SSID, WIFI_PASS);
    unsigned long start = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - start < 20000) {
        delay(250);
    }
    if (WiFi.status() == WL_CONNECTED) {
        // Local time for reset formatting, plus mDNS + beacon listener.
        configTzTime(TIMEZONE, "pool.ntp.org", "time.google.com", "time.nist.gov");
        if (MDNS.begin(MDNS_NAME)) MDNS.addService("http", "tcp", BEACON_PORT);
        beacon.on("/thinking", HTTP_POST, handleThinkingOn);
        beacon.on("/thinking", HTTP_GET, handleThinkingOn);
        beacon.on("/thinking/on", HTTP_POST, handleThinkingOn);
        beacon.on("/thinking/on", HTTP_GET, handleThinkingOn);
        beacon.on("/thinking/off", HTTP_POST, handleThinkingOff);
        beacon.on("/thinking/off", HTTP_GET, handleThinkingOff);
        beacon.on("/", handleRoot);
        beacon.begin();

        char msg[48];
        snprintf(msg, sizeof(msg), "%s.local  %s", MDNS_NAME,
                 WiFi.localIP().toString().c_str());
        drawStatusLine(msg, COL_DIM);
    } else {
        drawStatusLine("WiFi failed - check config.h", COL_RED);
    }
}

void loop() {
    unsigned long now = millis();

    beacon.handleClient();

    unsigned long interval = pollBackoff ? pollBackoff : USAGE_POLL_MS;
    if (lastPoll == 0 || now - lastPoll >= interval) {
        lastPoll = now;
        if (WiFi.status() == WL_CONNECTED) {
            Usage u;
            int retryAfter = 0;
            int code = fetchUsage(u, retryAfter);
            if (code == 200) {
                pollBackoff = 0;
                bool barsChanged = !cur.valid ||
                                   u.fivePct != cur.fivePct ||
                                   u.weekPct != cur.weekPct ||
                                   strcmp(u.fiveReset, cur.fiveReset) != 0 ||
                                   strcmp(u.weekReset, cur.weekReset) != 0;
                cur = u;
                if (barsChanged) drawBars();
                char msg[48];
                snprintf(msg, sizeof(msg), "usage ok  %s.local", MDNS_NAME);
                drawStatusLine(msg, COL_GREEN);
                lastOkFetch = now;
            } else if (code == 401 || code == -3) {
                drawStatusLine("auth failed - run device_login.py", COL_RED);
            } else if (code == 429 || code == 403) {
                // A 403 here is the edge rate-limiter, not a real auth failure: a
                // valid token still gets it when hammered. Back off (plus a small
                // margin) so the cooldown actually expires instead of being
                // re-armed by the next poll. Default 10 min if no Retry-After.
                unsigned long secs = (retryAfter > 0 ? (unsigned long)retryAfter : 600) + 30;
                pollBackoff = secs * 1000UL;
                char msg[48];
                snprintf(msg, sizeof(msg), "rate limited, retry in %lus", secs);
                drawStatusLine(msg, COL_YELLOW);
            } else if (now - lastOkFetch > 90000) {
                char msg[40];
                snprintf(msg, sizeof(msg), "usage fetch failed (%d)", code);
                drawStatusLine(msg, COL_RED);
            }
        } else {
            drawStatusLine("WiFi reconnecting...", COL_RED);
        }
    }

    bool active = beaconActive(now);

    if (active) {
        idleSpinnerDrawn = false;
        if (now - lastFrame >= 90) {
            lastFrame = now;
            frame = (frame + 1) % 48;
            drawSpinner(true);
        }
    } else if (!idleSpinnerDrawn) {
        idleSpinnerDrawn = true;
        drawSpinner(false);
    }
    drawStatusWord(active);
    updateLed(active, now);

    delay(10);
}
