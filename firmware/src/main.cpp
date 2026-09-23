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
// The same little HTTP server also switches what's on screen: the usage view,
// or a Spotify "now playing" view (track / artist / progress bar) fetched with
// a dedicated Spotify login (see server/spotify_login.py). POST /mode/usage,
// /mode/spotify or /mode/toggle - e.g. from the /switch Claude Code command -
// and the choice persists across power cycles.
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

// Spotify (only used when SPOTIFY_* are set in config.h).
static const char *SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token";
static const char *SPOTIFY_NOW_URL =
    "https://api.spotify.com/v1/me/player/currently-playing?additional_types=episode";

// OAuth tokens. The usage endpoint needs a user:profile-scoped token, which the
// device gets from DEVICE_REFRESH_TOKEN (minted by server/device_login.py). The
// access token lasts only ~8h, so the device refreshes it itself and remembers
// the rotated refresh token in flash (NVS) - it's a separate login from your
// Mac's, so this never disturbs Claude Code there.
static Preferences prefs;
static String g_access;
static String g_refresh;
static long   g_expiresAt = 0;  // epoch seconds when g_access expires

// Spotify tokens, refreshed the same way. The login is a PKCE app, so only the
// client id is needed - no secret ever touches the device.
static String g_spAccess;
static String g_spRefresh;
static long   g_spExpiresAt = 0;

// ---- palette (RGB565) ----
static const uint16_t COL_BG     = 0x1082;  // #121212 near-black
static const uint16_t COL_CARD   = 0x2945;  // dark gray track
static const uint16_t COL_ORANGE = 0xDBAA;  // #D97757 Claude orange
static const uint16_t COL_TEXT   = 0xEF7D;  // #ECECEC
static const uint16_t COL_DIM    = 0x7BCF;  // mid gray
static const uint16_t COL_GREEN  = 0x3DCA;
static const uint16_t COL_YELLOW = 0xDD08;
static const uint16_t COL_RED    = 0xE289;
static const uint16_t COL_SPOTIFY = 0x1DCA;  // #1DB954 Spotify green

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

// ---- layout (Spotify mode) ----
static const int SP_DIV_Y    = 60;    // divider under the header
static const int SP_TRACK_Y  = 72;    // track name, up to two lines
static const int SP_ARTIST_Y = 114;   // artists, tucked under the track
static const int SP_ALBUM_Y  = 138;   // album title
static const int SP_ART_Y    = 156;   // album art, centered (LovyanGFX only)
static const int SP_ART_SIZE = 64;    // Spotify's smallest native variant
static const int SP_TIME_Y   = 238;   // elapsed / total readouts
static const int SP_BAR_Y    = 252;   // progress bar
static const int SP_BAR_H    = 10;
static const int SP_STATE_Y  = 272;   // playing / paused

struct Usage {
    bool  valid = false;
    float fivePct = -1;
    float weekPct = -1;
    char  fiveReset[24] = "";
    char  weekReset[24] = "";
    char  fiveIso[40] = "";   // raw resets_at, for GET /usage
    char  weekIso[40] = "";
};

static Usage cur;

// Which screen is showing. Persisted in NVS so the display comes back up in
// the same mode after a power cycle.
enum DisplayMode : uint8_t { MODE_USAGE = 0, MODE_SPOTIFY = 1 };
static DisplayMode g_mode = MODE_USAGE;

struct NowPlaying {
    bool valid = false;      // at least one successful fetch
    bool hasTrack = false;
    bool playing = false;
    long progressMs = 0;
    long durationMs = 0;
    char track[80] = "";
    char artist[64] = "";
    char album[64] = "";
    char artUrl[120] = "";   // smallest suitable album-art jpeg
    long artW = 0;           // its native width, for scaling
};

static NowPlaying np;
static unsigned long npFetchedAt = 0;   // millis() when np.progressMs was current
static unsigned long spLastPoll = 0;
static unsigned long spBackoff = 0;     // 429 cooldown, like pollBackoff
static unsigned long spLastOk = 0;
static char spSig[192] = "\x01";        // what the track area currently shows
static char spShownArt[120] = "";       // art url currently on screen (or tried)
static long spShownSec = -1;            // progress second on screen
static int  spShownState = -1;          // 0=paused 1=playing

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
    drawMascot(tft, BAR_X, 16, 4, COL_ORANGE, TFT_BLACK);  // 12x8 grid -> 48x32
    tft.setTextDatum(TL_DATUM);
    tft.setTextColor(COL_ORANGE, COL_BG);
    tft.drawString("Claude Code", 68, 18, 2);
    tft.setTextColor(COL_DIM, COL_BG);
    tft.drawString("usage monitor", 68, 40, 1);
    tft.drawFastHLine(BAR_X, DIV_Y, BAR_W, COL_CARD);
    drawBars();
}

// ---------------------------------------------------------------- spotify screen

// The built-in fonts are ASCII-only, so drop other UTF-8 bytes instead of
// rendering them as garbage glyphs ("Beyoncé" -> "Beyonc").
static void asciiCopy(char *dst, size_t n, const char *src) {
    size_t j = 0;
    for (const unsigned char *p = (const unsigned char *)src; *p && j + 1 < n; p++)
        if (*p >= 0x20 && *p < 0x7F) dst[j++] = (char)*p;
    dst[j] = '\0';
}

// Shorten s in place (appending "..") until it fits in maxW pixels.
static void ellipsize(char *s, size_t cap, int maxW, int font) {
    tft.setTextFont(font);
    if ((int)tft.textWidth(s) <= maxW) return;
    char buf[96];
    size_t len = strlen(s);
    do {
        len--;
        snprintf(buf, sizeof(buf), "%.*s..", (int)len, s);
    } while (len > 0 && (int)tft.textWidth(buf) > maxW);
    strlcpy(s, buf, cap);
}

// Break s at a word boundary so the first line fits in maxW; the remainder is
// ellipsized into l2. A single overlong word gets hard-broken instead.
static void wrapTwoLines(const char *s, char *l1, size_t n1, char *l2, size_t n2,
                         int maxW, int font) {
    tft.setTextFont(font);
    strlcpy(l1, s, n1);
    l2[0] = '\0';
    if ((int)tft.textWidth(l1) <= maxW) return;

    size_t fit = strlen(l1);
    while (fit > 1) {  // longest prefix that fits
        l1[--fit] = '\0';
        if ((int)tft.textWidth(l1) <= maxW) break;
    }
    size_t brk = fit;
    while (brk > 0 && l1[brk - 1] != ' ') brk--;  // back up to a space
    size_t split = brk > 0 ? brk : fit;
    strlcpy(l2, s + split, n2);
    l1[split] = '\0';
    while (split > 0 && l1[split - 1] == ' ') l1[--split] = '\0';
    ellipsize(l2, n2, maxW, font);
}

static void fmtMs(long ms, char *out, size_t n) {
    if (ms < 0) ms = 0;
    long s = ms / 1000;
    snprintf(out, n, "%ld:%02ld", s / 60, s % 60);
}

static void drawSpotifyLogo(int cx, int cy, int r) {
    tft.fillCircle(cx, cy, r, COL_SPOTIFY);
    // flat take on the three "sound wave" bars of the Spotify mark
    tft.fillRoundRect(cx - 13, cy - 9, 27, 4, 2, COL_BG);
    tft.fillRoundRect(cx - 11, cy - 1, 22, 4, 2, COL_BG);
    tft.fillRoundRect(cx - 8,  cy + 7, 17, 4, 2, COL_BG);
}

static void drawSpotifyProgress(long ms) {
    char el[12], tot[12];
    fmtMs(ms, el, sizeof(el));
    fmtMs(np.durationMs, tot, sizeof(tot));

    tft.fillRect(BAR_X, SP_TIME_Y, BAR_W, 12, COL_BG);
    tft.setTextColor(COL_DIM, COL_BG);
    tft.setTextDatum(TL_DATUM);
    tft.drawString(el, BAR_X, SP_TIME_Y, 1);
    tft.setTextDatum(TR_DATUM);
    tft.drawString(tot, PCT_X, SP_TIME_Y, 1);

    tft.fillRoundRect(BAR_X, SP_BAR_Y, BAR_W, SP_BAR_H, 4, COL_CARD);
    if (np.durationMs > 0) {
        int fw = (int)((long long)BAR_W * ms / np.durationMs);
        if (fw > 6) tft.fillRoundRect(BAR_X, SP_BAR_Y, fw, SP_BAR_H, 4, COL_SPOTIFY);
    }
}

// The built-in bitmap fonts are ASCII-only, so there is no ▶ / ⏸ glyph to
// print - drawString("▶") just emits the raw UTF-8 bytes as junk. Draw the
// icons as shapes instead: a triangle while playing, two bars while paused.
static void drawSpotifyStateWord(bool playing) {
    tft.fillRect(0, SP_STATE_Y, SCREEN_W, 18, COL_BG);
    const int cx = SCREEN_W / 2, y = SP_STATE_Y + 1, h = 14;
    if (playing) {
        tft.fillTriangle(cx - 5, y, cx - 5, y + h, cx + 7, y + h / 2, COL_SPOTIFY);
    } else {
        tft.fillRoundRect(cx - 8, y, 5, h, 1, COL_DIM);
        tft.fillRoundRect(cx + 3, y, 5, h, 1, COL_DIM);
    }
}

static void spSignature(char *out, size_t n) {
    snprintf(out, n, "%d|%d|%ld|%s|%s|%s", (int)np.valid, (int)np.hasTrack,
             np.durationMs, np.track, np.artist, np.album);
}

// Repaint the whole track area (everything between the header divider and the
// status line) from np. Progress/state redraw right after via their caches.
static void drawSpotifyTrack() {
    spSignature(spSig, sizeof(spSig));
    spShownSec = -1;
    spShownState = -1;
    tft.fillRect(0, SP_DIV_Y + 2, SCREEN_W, STATUS_Y - SP_DIV_Y - 6, COL_BG);
    if (!np.hasTrack) {
        tft.setTextDatum(TC_DATUM);
        tft.setTextColor(COL_DIM, COL_BG);
        tft.drawString(np.valid ? "nothing playing" : "loading...",
                       SCREEN_W / 2, (SP_DIV_Y + STATUS_Y) / 2 - 8, 2);
        return;
    }

    char l1[80], l2[80];
    wrapTwoLines(np.track, l1, sizeof(l1), l2, sizeof(l2), BAR_W, 2);
    tft.setTextDatum(TL_DATUM);
    tft.setTextColor(COL_TEXT, COL_BG);
    tft.drawString(l1, BAR_X, SP_TRACK_Y, 2);
    if (l2[0]) tft.drawString(l2, BAR_X, SP_TRACK_Y + 20, 2);

    char line[64];
    strlcpy(line, np.artist, sizeof(line));
    ellipsize(line, sizeof(line), BAR_W, 2);
    tft.setTextColor(COL_DIM, COL_BG);
    tft.drawString(line, BAR_X, SP_ARTIST_Y, 2);

    strlcpy(line, np.album, sizeof(line));
    ellipsize(line, sizeof(line), BAR_W, 1);
    tft.drawString(line, BAR_X, SP_ALBUM_Y, 1);

#if defined(USE_LOVYANGFX)
    // Album art placeholder; drawAlbumArt() paints over it once fetched.
    spShownArt[0] = '\0';
    if (np.artUrl[0])
        tft.fillRoundRect((SCREEN_W - SP_ART_SIZE) / 2, SP_ART_Y,
                          SP_ART_SIZE, SP_ART_SIZE, 4, COL_CARD);
#endif
}

static void drawSpotifyStaticUI() {
    tft.fillScreen(COL_BG);
    drawSpotifyLogo(BAR_X + 20, 32, 20);
    tft.setTextDatum(TL_DATUM);
    tft.setTextColor(COL_SPOTIFY, COL_BG);
    tft.drawString("Spotify", 68, 18, 2);
    tft.setTextColor(COL_DIM, COL_BG);
    tft.drawString("now playing", 68, 40, 1);
    tft.drawFastHLine(BAR_X, SP_DIV_Y, BAR_W, COL_CARD);
    drawSpotifyTrack();
}

// Flip between the usage screen and the Spotify screen (persisted in NVS).
static void applyMode(DisplayMode m) {
    if (m == g_mode) return;
    g_mode = m;
    prefs.putUChar("mode", (uint8_t)m);
    if (m == MODE_SPOTIFY) {
        spLastPoll = 0;  // fetch as soon as loop() comes back around
        spBackoff = 0;
        drawSpotifyStaticUI();
        drawStatusLine("fetching spotify...", COL_DIM);
    } else {
        idleSpinnerDrawn = false;  // loop() repaints the spinner + status word
        lastStatusWord = -1;
        drawStaticUI();            // bars redraw from the cached usage numbers
        char msg[48];
        snprintf(msg, sizeof(msg), "%s.local  %s", MDNS_NAME,
                 WiFi.localIP().toString().c_str());
        drawStatusLine(msg, COL_DIM);
    }
}

// ---------------------------------------------------------------- oauth tokens

static void loadTokens() {
    prefs.begin("clt", false);
    g_refresh   = prefs.getString("refresh", "");
    g_access    = prefs.getString("access", "");
    g_expiresAt = prefs.getLong("exp", 0);
    if (g_refresh.length() == 0) g_refresh = DEVICE_REFRESH_TOKEN;  // first boot: seed from config.h

    g_spRefresh   = prefs.getString("sp_refresh", "");
    g_spAccess    = prefs.getString("sp_access", "");
    g_spExpiresAt = prefs.getLong("sp_exp", 0);
    if (g_spRefresh.length() == 0) g_spRefresh = SPOTIFY_REFRESH_TOKEN;

    g_mode = (DisplayMode)prefs.getUChar("mode", MODE_USAGE);
    if (g_mode == MODE_SPOTIFY && g_spRefresh.length() == 0) g_mode = MODE_USAGE;
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

// Spotify's equivalent of tryRefresh(): swap the refresh token for an access
// token. PKCE app, so the body is form-encoded and carries no client secret.
// Spotify sometimes rotates the refresh token too; keep whatever comes back.
static bool trySpotifyRefresh(const String &refreshTok) {
    if (refreshTok.length() == 0 || strlen(SPOTIFY_CLIENT_ID) == 0) return false;
    WiFiClientSecure client;
    client.setInsecure();
    HTTPClient http;
    http.setConnectTimeout(5000);
    http.setTimeout(8000);
    if (!http.begin(client, SPOTIFY_TOKEN_URL)) return false;
    http.addHeader("Content-Type", "application/x-www-form-urlencoded");
    String body = String("grant_type=refresh_token&refresh_token=") + refreshTok +
                  "&client_id=" + SPOTIFY_CLIENT_ID;
    int code = http.POST(body);
    if (code != 200) { http.end(); return false; }
    String resp = http.getString();
    http.end();

    JsonDocument doc;
    if (deserializeJson(doc, resp)) return false;
    const char *at = doc["access_token"] | "";
    if (!at[0]) return false;
    g_spAccess = at;
    const char *rt = doc["refresh_token"] | "";
    if (rt[0]) g_spRefresh = rt;
    long expires_in = doc["expires_in"] | 3600;
    g_spExpiresAt = (long)time(nullptr) + expires_in;
    prefs.putString("sp_refresh", g_spRefresh);
    prefs.putString("sp_access", g_spAccess);
    prefs.putLong("sp_exp", g_spExpiresAt);
    return true;
}

static bool ensureSpotifyToken() {
    time_t now = time(nullptr);
    bool synced = now > 1700000000;
    if (g_spAccess.length() && synced && now < g_spExpiresAt - 300) return true;
    if (trySpotifyRefresh(g_spRefresh)) return true;
    String cfg = SPOTIFY_REFRESH_TOKEN;
    if (cfg.length() && g_spRefresh != cfg && trySpotifyRefresh(cfg)) return true;
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
    strlcpy(u.fiveIso, doc["five_hour"]["resets_at"] | "", sizeof(u.fiveIso));
    strlcpy(u.weekIso, doc["seven_day"]["resets_at"] | "", sizeof(u.weekIso));
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

// Pick the art variant to fetch: the smallest one that's still >= SP_ART_SIZE
// (Spotify lists images largest first; albums ship 640/300/64). Falls back to
// the largest available if nothing reaches SP_ART_SIZE.
static void pickArt(JsonArray imgs, NowPlaying &out) {
    long best = 0;
    for (JsonObject im : imgs) {
        const char *u = im["url"] | "";
        long w = im["width"] | 0L;
        if (!u[0]) continue;
        bool haveGood = best >= SP_ART_SIZE;
        bool thisGood = w >= SP_ART_SIZE;
        bool better = !out.artUrl[0] ||
                      (thisGood && !haveGood) ||
                      (thisGood && haveGood && w < best) ||
                      (!thisGood && !haveGood && w > best);
        if (better) {
            best = w;
            out.artW = w;
            strlcpy(out.artUrl, u, sizeof(out.artUrl));
        }
    }
}

// One GET to Spotify's currently-playing endpoint. 204 means nothing is
// playing - that's a success, just an empty one.
static int spotifyRequest(NowPlaying &out, int &retryAfter) {
    retryAfter = 0;
    WiFiClientSecure client;
    client.setInsecure();
    HTTPClient http;
    http.setConnectTimeout(5000);
    http.setTimeout(5000);
    if (!http.begin(client, SPOTIFY_NOW_URL)) return -1;
    http.addHeader("Authorization", String("Bearer ") + g_spAccess);
    const char *collect[] = {"Retry-After"};
    http.collectHeaders(collect, 1);

    int code = http.GET();
    if (code == 204) {
        http.end();
        out = NowPlaying();
        out.valid = true;
        return 200;
    }
    if (code != 200) {
        if (code == 429) retryAfter = http.header("Retry-After").toInt();
        http.end();
        return code;
    }
    String body = http.getString();
    http.end();

    // The full response is several KB of album art URLs etc.; filter it down
    // to the handful of fields we render.
    JsonDocument filter;
    filter["is_playing"] = true;
    filter["progress_ms"] = true;
    filter["item"]["name"] = true;
    filter["item"]["duration_ms"] = true;
    filter["item"]["artists"][0]["name"] = true;
    filter["item"]["album"]["name"] = true;
    filter["item"]["album"]["images"][0]["url"] = true;
    filter["item"]["album"]["images"][0]["width"] = true;
    filter["item"]["show"]["name"] = true;  // podcast episodes have a show, not artists
    filter["item"]["images"][0]["url"] = true;   // ...and their art hangs off the item
    filter["item"]["images"][0]["width"] = true;
    JsonDocument doc;
    if (deserializeJson(doc, body, DeserializationOption::Filter(filter))) return -2;

    out = NowPlaying();
    out.valid = true;
    out.hasTrack = !doc["item"].isNull();
    if (!out.hasTrack) return 200;
    out.playing = doc["is_playing"] | false;
    out.progressMs = doc["progress_ms"] | 0L;
    out.durationMs = doc["item"]["duration_ms"] | 0L;
    asciiCopy(out.track, sizeof(out.track), doc["item"]["name"] | "");
    asciiCopy(out.album, sizeof(out.album), doc["item"]["album"]["name"] | "");
    for (JsonObject a : doc["item"]["artists"].as<JsonArray>()) {
        char nm[48];
        asciiCopy(nm, sizeof(nm), a["name"] | "");
        if (!nm[0]) continue;
        if (out.artist[0]) strlcat(out.artist, ", ", sizeof(out.artist));
        strlcat(out.artist, nm, sizeof(out.artist));
    }
    if (!out.artist[0])
        asciiCopy(out.artist, sizeof(out.artist), doc["item"]["show"]["name"] | "");
    pickArt(doc["item"]["album"]["images"].as<JsonArray>(), out);
    if (!out.artUrl[0]) pickArt(doc["item"]["images"].as<JsonArray>(), out);
    return 200;
}

static int fetchNowPlaying(NowPlaying &out, int &retryAfter) {
    retryAfter = 0;
    if (!ensureSpotifyToken()) return -3;
    int code = spotifyRequest(out, retryAfter);
    if (code == 401) {
        g_spExpiresAt = 0;  // force a refresh
        if (ensureSpotifyToken()) code = spotifyRequest(out, retryAfter);
    }
    return code;
}

#if defined(USE_LOVYANGFX)
// Fetch np.artUrl and draw it centered in the art slot. Blocking for ~1s on a
// track change; on any failure the placeholder square just stays. LovyanGFX
// bundles a jpeg decoder, so only the compressed image (a few KB at 64px) ever
// sits in RAM, and it's freed on every path. TFT_eSPI builds skip album art -
// they'd need the TJpg_Decoder library.
static void drawAlbumArt() {
    WiFiClientSecure client;
    client.setInsecure();
    HTTPClient http;
    http.setConnectTimeout(4000);
    http.setTimeout(5000);
    if (!http.begin(client, np.artUrl)) return;
    if (http.GET() != 200) { http.end(); return; }
    int len = http.getSize();
    if (len <= 0 || len > 60000) { http.end(); return; }

    uint8_t *buf = (uint8_t *)malloc(len);
    if (!buf) { http.end(); return; }
    WiFiClient *stream = http.getStreamPtr();
    int got = 0;
    unsigned long deadline = millis() + 6000;
    while (got < len && (long)(deadline - millis()) > 0) {
        int avail = stream->available();
        if (avail > 0) {
            int want = len - got;
            if (want > avail) want = avail;
            int n = stream->read(buf + got, want);
            if (n > 0) got += n;
        } else if (!client.connected()) {
            break;
        } else {
            delay(1);
        }
    }
    http.end();

    if (got == len) {
        float scale = np.artW > 0 ? (float)SP_ART_SIZE / np.artW : 1.0f;
        tft.drawJpg(buf, len, (SCREEN_W - SP_ART_SIZE) / 2, SP_ART_Y,
                    SP_ART_SIZE, SP_ART_SIZE, 0, 0, scale, scale);
    }
    free(buf);
}
#endif

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
                "/thinking/off when done. POST /mode/usage, /mode/spotify or "
                "/mode/toggle to switch screens; GET /mode to ask; GET /usage "
                "for JSON.\n");
}

// ---- screen mode switching (used by the /switch Claude Code command) ----

static const char *modeName(DisplayMode m) {
    return m == MODE_SPOTIFY ? "spotify" : "usage";
}

static void handleModeGet() {
    beacon.send(200, "text/plain", String(modeName(g_mode)) + "\n");
}

static void handleModeUsage() {
    applyMode(MODE_USAGE);
    beacon.send(200, "text/plain", "usage\n");
}

static void handleModeSpotify() {
    if (g_spRefresh.length() == 0) {
        beacon.send(409, "text/plain",
                    "spotify not configured - run server/spotify_login.py and "
                    "set SPOTIFY_CLIENT_ID / SPOTIFY_REFRESH_TOKEN in config.h\n");
        return;
    }
    applyMode(MODE_SPOTIFY);
    beacon.send(200, "text/plain", "spotify\n");
}

static void handleModeToggle() {
    if (g_mode == MODE_USAGE) handleModeSpotify();
    else handleModeUsage();
}

// "Thinking" is sticky between an explicit on and off. BEACON_TTL_MS is only a
// backstop: if a sender dies mid-turn and never sends /thinking/off, fall idle.
static bool beaconActive(unsigned long now) {
    return lastBeacon != 0 && (now - lastBeacon) < BEACON_TTL_MS;
}

// GET /usage -> what's on screen, as JSON. The Windows tray helper
// (windows/claude_tray.py) reads this, so it needs no Anthropic login of its
// own and adds no load on the rate-limited usage endpoint.
static void handleUsageJson() {
    JsonDocument doc;
    const struct { const char *key; float pct; const char *iso; const char *fmt; } wins[] = {
        {"five_hour", cur.fivePct, cur.fiveIso, cur.fiveReset},
        {"seven_day", cur.weekPct, cur.weekIso, cur.weekReset},
    };
    for (const auto &w : wins) {
        JsonObject o = doc[w.key].to<JsonObject>();
        if (cur.valid && w.pct >= 0) o["pct"] = roundf(w.pct * 10) / 10;
        else o["pct"] = nullptr;
        o["resets_at"] = w.iso;
        o["resets"] = w.fmt;
    }
    doc["valid"] = cur.valid;
    doc["thinking"] = beaconActive(millis());
    doc["mode"] = modeName(g_mode);
    if (lastOkFetch) doc["age_s"] = (millis() - lastOkFetch) / 1000;
    else doc["age_s"] = nullptr;
    String out;
    serializeJson(doc, out);
    beacon.send(200, "application/json", out);
}

// ---------------------------------------------------------------- spotify tick

// Everything Spotify mode does per loop(): poll on its own schedule, repaint
// the track area when the song changes, and tick the progress bar locally
// between polls (1 Hz) so it moves smoothly without hammering the API.
static void spotifyTick(unsigned long now) {
    unsigned long interval = spBackoff ? spBackoff : SPOTIFY_POLL_MS;
    if (spLastPoll == 0 || now - spLastPoll >= interval) {
        spLastPoll = now;
        if (WiFi.status() != WL_CONNECTED) {
            drawStatusLine("WiFi reconnecting...", COL_RED);
        } else if (g_spRefresh.length() == 0) {
            drawStatusLine("spotify not set up - spotify_login.py", COL_YELLOW);
        } else {
            NowPlaying u;
            int retryAfter = 0;
            int code = fetchNowPlaying(u, retryAfter);
            if (code == 200) {
                spBackoff = 0;
                np = u;
                npFetchedAt = now;
                spLastOk = now;
                char msg[48];
                snprintf(msg, sizeof(msg), "spotify ok  %s.local", MDNS_NAME);
                drawStatusLine(msg, COL_GREEN);
            } else if (code == 401 || code == 403 || code == -3) {
                // 403 usually means the Spotify app doesn't include this
                // account (Dashboard -> app -> User Management).
                drawStatusLine("spotify auth failed - spotify_login.py", COL_RED);
            } else if (code == 429) {
                unsigned long secs = (retryAfter > 0 ? (unsigned long)retryAfter : 30) + 2;
                spBackoff = secs * 1000UL;
                char msg[48];
                snprintf(msg, sizeof(msg), "spotify rate limited, %lus", secs);
                drawStatusLine(msg, COL_YELLOW);
            } else if (now - spLastOk > 30000) {
                char msg[40];
                snprintf(msg, sizeof(msg), "spotify fetch failed (%d)", code);
                drawStatusLine(msg, COL_RED);
            }
        }
    }

    char sig[192];
    spSignature(sig, sizeof(sig));
    if (strcmp(sig, spSig) != 0) drawSpotifyTrack();

#if defined(USE_LOVYANGFX)
    if (np.hasTrack && np.artUrl[0] && strcmp(np.artUrl, spShownArt) != 0) {
        strlcpy(spShownArt, np.artUrl, sizeof(spShownArt));  // one attempt per track
        drawAlbumArt();
    }
#endif

    if (!np.hasTrack) return;
    long est = np.progressMs + (np.playing ? (long)(now - npFetchedAt) : 0);
    if (np.durationMs > 0 && est > np.durationMs) {
        est = np.durationMs;
        // Track should have ended - poll right away to catch the next one.
        if (np.playing && now - spLastPoll > 3000) spLastPoll = 0;
    }
    long sec = est / 1000;
    if (sec != spShownSec) {
        spShownSec = sec;
        drawSpotifyProgress(est);
    }
    int st = np.playing ? 1 : 0;
    if (st != spShownState) {
        spShownState = st;
        drawSpotifyStateWord(np.playing);
    }
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

    if (g_mode == MODE_SPOTIFY) {
        drawSpotifyStaticUI();
    } else {
        drawStaticUI();
        drawSpinner(false);
        drawStatusWord(false);
    }
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
        beacon.on("/mode", handleModeGet);
        beacon.on("/mode/usage", handleModeUsage);
        beacon.on("/mode/spotify", handleModeSpotify);
        beacon.on("/mode/toggle", handleModeToggle);
        beacon.on("/usage", HTTP_GET, handleUsageJson);
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

    // Usage keeps polling in both modes (so the bars are current the moment
    // you switch back), but only paints the screen in usage mode.
    bool showUsage = g_mode == MODE_USAGE;
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
                if (showUsage && barsChanged) drawBars();
                if (showUsage) {
                    char msg[48];
                    snprintf(msg, sizeof(msg), "usage ok  %s.local", MDNS_NAME);
                    drawStatusLine(msg, COL_GREEN);
                }
                lastOkFetch = now;
            } else if (code == 401 || code == -3) {
                if (showUsage) drawStatusLine("auth failed - run device_login.py", COL_RED);
            } else if (code == 429 || code == 403) {
                // A 403 here is the edge rate-limiter, not a real auth failure: a
                // valid token still gets it when hammered. Back off (plus a small
                // margin) so the cooldown actually expires instead of being
                // re-armed by the next poll. Default 10 min if no Retry-After.
                unsigned long secs = (retryAfter > 0 ? (unsigned long)retryAfter : 600) + 30;
                pollBackoff = secs * 1000UL;
                if (showUsage) {
                    char msg[48];
                    snprintf(msg, sizeof(msg), "rate limited, retry in %lus", secs);
                    drawStatusLine(msg, COL_YELLOW);
                }
            } else if (now - lastOkFetch > 90000) {
                if (showUsage) {
                    char msg[40];
                    snprintf(msg, sizeof(msg), "usage fetch failed (%d)", code);
                    drawStatusLine(msg, COL_RED);
                }
            }
        } else if (showUsage) {
            drawStatusLine("WiFi reconnecting...", COL_RED);
        }
    }

    if (g_mode == MODE_SPOTIFY) spotifyTick(now);

    // The LED breathes while Claude is thinking in either mode; the spinner
    // and working/idle word only exist on the usage screen.
    bool active = beaconActive(now);

    if (g_mode == MODE_USAGE) {
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
    }
    updateLed(active, now);

    delay(10);
}
