// Claude Code usage display for ESP32 + SPI TFT.
//
// Fully self-contained: the ESP32 fetches your real 5-hour / weekly rate-limit
// utilization straight from Anthropic's OAuth usage API over HTTPS, using a
// dedicated long-lived token (see config.h, `claude setup-token`). No companion
// server needed.
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
#include <time.h>
#include <ctype.h>

#include "display.h"
#include "config.h"
#include "mascot.h"

DisplayTFT tft;
DisplaySprite spin(&tft);

static const char *USAGE_URL = "https://api.anthropic.com/api/oauth/usage";

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
    neopixelWrite(RGB_LED_PIN, r, g, b);
#else
    (void)r; (void)g; (void)b;
#endif
}

// Blink green while a beacon is live, otherwise off.
static void updateLed(bool active, unsigned long now) {
    if (active) {
        bool on = (now / 150) % 2 == 0;     // ~3 Hz blink
        ledSet(0, on ? 40 : 0, 0);          // modest green, full brightness is harsh
    } else {
        ledSet(0, 0, 0);
    }
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
    drawMascot(tft, BAR_X, 12, 4, COL_ORANGE, TFT_BLACK);  // 12x12 grid -> 48x48
    tft.setTextDatum(TL_DATUM);
    tft.setTextColor(COL_ORANGE, COL_BG);
    tft.drawString("Claude Code", 68, 18, 2);
    tft.setTextColor(COL_DIM, COL_BG);
    tft.drawString("usage monitor", 68, 40, 1);
    tft.drawFastHLine(BAR_X, DIV_Y, BAR_W, COL_CARD);
    drawBars();
}

// ---------------------------------------------------------------- network

// Fetch usage straight from Anthropic. Returns the HTTP status code (200 on
// success), or a negative HTTPClient error code on a transport failure.
static int fetchUsage(Usage &u) {
    WiFiClientSecure client;
    client.setInsecure();  // skip CA validation; fine on a home LAN to a known host

    HTTPClient http;
    http.setConnectTimeout(5000);
    http.setTimeout(5000);
    if (!http.begin(client, USAGE_URL)) return -1;
    http.addHeader("Authorization", String("Bearer ") + ANTHROPIC_TOKEN);
    http.addHeader("anthropic-beta", "oauth-2025-04-20");
    http.addHeader("Content-Type", "application/json");
    http.addHeader("User-Agent", "claude-usage-display/1.0");

    int code = http.GET();
    if (code != 200) {
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

// ---------------------------------------------------------------- beacons

static void handleBeacon() {
    lastBeacon = millis();
    beacon.send(200, "text/plain", "ok\n");
}

static void handleRoot() {
    beacon.send(200, "text/plain",
                "Claude Code usage display. POST or GET /thinking to blink.\n");
}

static bool beaconActive(unsigned long now) {
    return lastBeacon != 0 && (now - lastBeacon) < BEACON_TTL_MS;
}

// ---------------------------------------------------------------- arduino

void setup() {
    Serial.begin(115200);
    ledSet(0, 0, 0);

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
        beacon.on("/thinking", HTTP_POST, handleBeacon);
        beacon.on("/thinking", HTTP_GET, handleBeacon);
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

    if (lastPoll == 0 || now - lastPoll >= USAGE_POLL_MS) {
        lastPoll = now;
        if (WiFi.status() == WL_CONNECTED) {
            Usage u;
            int code = fetchUsage(u);
            if (code == 200) {
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
            } else if (code == 401 || code == 403) {
                drawStatusLine("token rejected - claude setup-token", COL_RED);
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
