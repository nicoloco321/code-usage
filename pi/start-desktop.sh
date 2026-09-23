#!/bin/sh
# Desktop autostart launcher (install.sh points ~/.config/autostart at it).
# Restarts the display if it crashes; stays closed if you quit it (Ctrl+Q).
cd "$(dirname "$0")" || exit 1
sleep 2  # let the desktop settle so the fullscreen window lands on top
until python3 claude_display.py; do
  sleep 5
done
