#pragma once

// Display backend selector.
//
// The Waveshare ESP32-C6 board uses LovyanGFX (TFT_eSPI has no working C6
// driver); the 480x320 landscape boards keep using TFT_eSPI. Both expose the
// same drawing API, so main.cpp talks to `DisplayTFT` / `DisplaySprite` and
// stays backend-agnostic.

#if defined(USE_LOVYANGFX)

#include <LovyanGFX.hpp>

// Waveshare ESP32-C6-LCD-1.47 — 172x320 ST7789 panel on SPI2 with PWM backlight.
class LGFX : public lgfx::LGFX_Device {
    lgfx::Panel_ST7789 _panel;
    lgfx::Bus_SPI      _bus;
    lgfx::Light_PWM    _light;

public:
    LGFX() {
        {
            auto cfg = _bus.config();
            cfg.spi_host    = SPI2_HOST;
            cfg.spi_mode    = 0;
            cfg.freq_write  = 40000000;
            cfg.freq_read   = 16000000;
            cfg.spi_3wire   = true;
            cfg.use_lock    = true;
            cfg.dma_channel = SPI_DMA_CH_AUTO;
            cfg.pin_sclk    = 7;
            cfg.pin_mosi    = 6;
            cfg.pin_miso    = -1;
            cfg.pin_dc      = 15;
            _bus.config(cfg);
            _panel.setBus(&_bus);
        }
        {
            auto cfg = _panel.config();
            cfg.pin_cs          = 14;
            cfg.pin_rst         = 21;
            cfg.pin_busy        = -1;
            cfg.memory_width    = 240;   // ST7789 GRAM is 240 wide; panel is 172
            cfg.memory_height   = 320;
            cfg.panel_width     = 172;
            cfg.panel_height    = 320;
            cfg.offset_x        = 34;    // (240 - 172) / 2, centers the 172px window
            cfg.offset_y        = 0;
            cfg.offset_rotation = 0;
            cfg.readable        = false;
            cfg.invert          = true;  // ST7789 needs colour inversion on
            cfg.rgb_order       = false; // Waveshare panel is BGR
            cfg.dlen_16bit      = false;
            cfg.bus_shared      = false;
            _panel.config(cfg);
        }
        {
            auto cfg = _light.config();
            cfg.pin_bl      = 22;
            cfg.invert      = false;
            cfg.freq        = 12000;
            cfg.pwm_channel = 0;
            _light.config(cfg);
            _panel.setLight(&_light);
        }
        setPanel(&_panel);
    }
};

typedef LGFX                DisplayTFT;
typedef lgfx::LGFX_Sprite   DisplaySprite;

// Portrait 172x320 panel.
static const int SCREEN_ROTATION = 0;
static const int SCREEN_W = 172;
static const int SCREEN_H = 320;

#else  // ---- TFT_eSPI: Sunton ESP32-3248S035 and generic ESP32 + ILI9488 ----

#include <SPI.h>
#include <TFT_eSPI.h>

typedef TFT_eSPI    DisplayTFT;
typedef TFT_eSprite DisplaySprite;

// 480x320 panels driven in landscape. The UI is laid out for the 172x320
// portrait panel above, so it renders the same stacked layout here, just wider.
static const int SCREEN_ROTATION = 1;
static const int SCREEN_W = 480;
static const int SCREEN_H = 320;

#endif
