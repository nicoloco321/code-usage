#pragma once
#include <Arduino.h>

// Pixel-art Clawd, the Claude Code mascot - the "space invader" look:
// a rounded body with two eyes, horns poking out the sides, four legs under.
// 0 = transparent, 1 = orange body, 2 = eye
#define MASCOT_COLS 13
#define MASCOT_ROWS 10

static const uint8_t MASCOT[MASCOT_ROWS][MASCOT_COLS] = {
    {0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0},  // body (rounded top)
    {0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0},
    {1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1},  // horns out the sides + body
    {1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1},  // horns out the sides + body
    {0, 0, 1, 1, 2, 2, 1, 2, 2, 1, 1, 0, 0},  // eyes
    {0, 0, 1, 1, 2, 2, 1, 2, 2, 1, 1, 0, 0},  // eyes
    {0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0},
    {0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0},  // body (rounded bottom)
    {0, 0, 0, 1, 0, 1, 0, 1, 0, 1, 0, 0, 0},  // legs
    {0, 0, 0, 1, 0, 1, 0, 1, 0, 1, 0, 0, 0},  // legs
};

template <typename Display>
inline void drawMascot(Display &tft, int x, int y, int scale,
                       uint16_t body, uint16_t eye) {
    for (int r = 0; r < MASCOT_ROWS; r++) {
        for (int c = 0; c < MASCOT_COLS; c++) {
            uint8_t v = MASCOT[r][c];
            if (v == 0) continue;
            tft.fillRect(x + c * scale, y + r * scale, scale, scale,
                         v == 1 ? body : eye);
        }
    }
}
