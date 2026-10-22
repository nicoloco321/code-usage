#!/bin/sh
# Desktop autostart launcher (install.sh points ~/.config/autostart at it).
# Restarts the display if it crashes or is killed, so this restarts it:
#   pkill -KILL -fx "python3 claude_display.py"
# It stays closed if you quit it (Ctrl+Q). Its output goes to display.log in
# ~/.local/state/claude-display, a fresh one each time this starts (the one
# before is kept as display.log.old).
cd "$(dirname "$0")" || exit 1
LOG="$HOME/.local/state/claude-display/display.log"
mkdir -p "$(dirname "$LOG")"
[ -f "$LOG" ] && mv -f "$LOG" "$LOG.old"
exec >>"$LOG" 2>&1
echo "$(date '+%F %T') starting the display"
sleep 2  # let the desktop settle so the fullscreen window lands on top
until python3 claude_display.py; do
  echo "$(date '+%F %T') the display stopped (exit $?) - starting it again in 5s"
  sleep 5
done
