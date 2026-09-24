#!/usr/bin/env bash
# Set up the Claude Code usage display on a Raspberry Pi so it starts
# fullscreen on boot. Run it from the repo as your normal user (not root):
#
#   bash pi/install.sh              # asks a few questions
#   bash pi/install.sh --yes        # takes the defaults, skips the logins
#
# It installs pygame, creates ~/.config/claude-display/config.ini, offers to
# mint the Anthropic (and optional Spotify) logins, then starts the display:
#   - Raspberry Pi OS Lite:     a systemd service that draws straight to the
#                               screen (KMS/DRM) - no desktop needed
#   - Raspberry Pi OS desktop:  an autostart entry that opens it fullscreen
#                               after the automatic desktop login
# Force either with --service or --desktop. Safe to re-run.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP="$REPO/pi/claude_display.py"
CONF_DIR="$HOME/.config/claude-display"
CONF="$CONF_DIR/config.ini"
SERVICE=/etc/systemd/system/claude-display.service
AUTOSTART="$HOME/.config/autostart/claude-display.desktop"
WANT_HOSTNAME=claude-display   # what the hooks, beacon.py and find_display.py expect
ME="$(id -un)"

YES=0
MODE=""
for arg in "$@"; do
  case "$arg" in
    -y|--yes) YES=1 ;;
    --service) MODE=service ;;
    --desktop) MODE=desktop ;;
    -h|--help) sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg (try --help)" >&2; exit 2 ;;
  esac
done

say() { printf '\n\033[1;33m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }
interactive() { [ "$YES" = 0 ] && [ -t 0 ]; }

# ask "question" y|n - succeeds on yes; non-interactive runs take the default
ask() {
  local ans hint="[y/N]"
  [ "$2" = y ] && hint="[Y/n]"
  if ! interactive; then [ "$2" = y ]; return; fi
  read -r -p "$1 $hint " ans || ans=""
  [[ "${ans:-$2}" =~ ^[Yy] ]]
}

# conf_get SECTION KEY - print a value from config.ini (empty if unset)
conf_get() {
  awk -v sec="$1" -v key="$2" '
    { line = $0; gsub(/^[ \t]+|[ \t]+$/, "", line) }
    line ~ /^\[/ { insec = (line == "[" sec "]"); next }
    insec && line ~ ("^" key "[ \t]*=") { sub(/^[^=]*=[ \t]*/, "", line); val = line }
    END { print val }' "$CONF"
}

[ "$(id -u)" -ne 0 ] || die "run this as your normal user, not root - it uses sudo where it needs to"
[ -f "$APP" ] || die "can't find $APP"

pygame2() { python3 -c 'import sys, pygame; sys.exit(pygame.version.vernum[0] < 2)' 2>/dev/null; }

say "Installing pygame and fonts"
sudo apt-get update
# libsdl2-ttf is pygame's text renderer; pip builds of pygame don't bundle it
sudo apt-get install -y fonts-dejavu-core avahi-daemon libsdl2-ttf-2.0-0
pygame2 || sudo apt-get install -y python3-pygame
if ! pygame2; then
  # Bullseye packages pygame 1.9; piwheels has pygame 2 built against its SDL2
  say "This OS only packages pygame 1.x - installing pygame 2 with pip"
  sudo apt-get install -y python3-pip libsdl2-2.0-0 libsdl2-image-2.0-0
  python3 -m pip install --user "pygame>=2.1,<3"
fi
pygame2 || die "couldn't install pygame 2 - try: python3 -m pip install --user pygame"
python3 -c 'import pygame; pygame.font.init()' 2>/dev/null \
  || die "pygame can't draw text - check that libsdl2-ttf-2.0-0 is installed"

say "Config: $CONF"
mkdir -p "$CONF_DIR"
if [ -f "$CONF" ]; then
  echo "already there - leaving it as it is"
else
  cp "$REPO/pi/config.example.ini" "$CONF"
  echo "created from pi/config.example.ini"
fi
chmod 600 "$CONF"

if [ -z "$(conf_get anthropic refresh_token)" ] && interactive \
   && ask "Log the display in to your Claude account now?" y; then
  python3 "$REPO/server/device_login.py" --config "$CONF" \
    || echo "Login didn't finish - run it again later: python3 server/device_login.py --config $CONF"
fi

if [ -z "$(conf_get spotify refresh_token)" ] && interactive \
   && ask "Set up the optional Spotify now-playing screen now?" n; then
  cat <<EOF

Spotify sends your browser back to http://127.0.0.1:8898/callback, so the
browser has to reach port 8898 on this Pi. Over SSH, reconnect with a tunnel
first and open the printed link on your computer:

    ssh -L 8898:127.0.0.1:8898 $ME@$(hostname).local

EOF
  python3 "$REPO/server/spotify_login.py" --config "$CONF" \
    || echo "Spotify setup didn't finish - run it again later: python3 server/spotify_login.py --config $CONF"
fi

if [ -z "$(conf_get bambu access_code)" ] && interactive \
   && ask "Set up the optional Bambu Lab 3D printer screen now?" n; then
  python3 "$APP" --setup-bambu --config "$CONF" \
    || echo "Printer setup didn't finish - run it again later: python3 pi/claude_display.py --setup-bambu"
fi

if [ -z "$(conf_get planes lat)" ] && interactive \
   && ask "Set up the optional planes-overhead screen now (it asks for your location)?" n; then
  python3 "$APP" --setup-planes --config "$CONF" \
    || echo "Planes setup didn't finish - run it again later: python3 pi/claude_display.py --setup-planes"
fi

NEED_REBOOT=0
if [ "$(hostname)" != "$WANT_HOSTNAME" ] \
   && ask "Rename this Pi to '$WANT_HOSTNAME' so it answers at $WANT_HOSTNAME.local?" y; then
  if command -v raspi-config >/dev/null; then
    sudo raspi-config nonint do_hostname "$WANT_HOSTNAME"
  else
    sudo hostnamectl set-hostname "$WANT_HOSTNAME"
    sudo sed -i "s/^127\.0\.1\.1.*/127.0.1.1\t$WANT_HOSTNAME/" /etc/hosts
  fi
  NEED_REBOOT=1
fi

if command -v raspi-config >/dev/null; then
  say "Turning off screen blanking"
  sudo raspi-config nonint do_blanking 1 || echo "(couldn't - turn it off in raspi-config > Display)"
fi

# KMS/DRM needs the screen (video, render) and, for taps and keys, input.
DEV_GROUPS=""
for g in video render input; do
  getent group "$g" >/dev/null && DEV_GROUPS="$DEV_GROUPS $g"
done
for g in $DEV_GROUPS; do sudo usermod -aG "$g" "$ME"; done

if [ -z "$MODE" ]; then
  if [ "$(systemctl get-default)" = graphical.target ]; then MODE=desktop; else MODE=service; fi
fi

if [ "$MODE" = service ]; then
  say "Installing the claude-display service (fullscreen on boot, no desktop needed)"
  rm -f "$AUTOSTART"
  sudo tee "$SERVICE" >/dev/null <<EOF
[Unit]
Description=Claude Code usage display
After=network-online.target systemd-user-sessions.service
Wants=network-online.target

[Service]
User=$ME
SupplementaryGroups=$DEV_GROUPS
Environment=SDL_VIDEODRIVER=kmsdrm
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/python3 "$APP"
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
  sudo systemctl daemon-reload
  sudo systemctl enable claude-display.service
  sudo systemctl restart claude-display.service
else
  say "Adding a desktop autostart entry (fullscreen after the desktop logs in)"
  if [ -f "$SERVICE" ]; then
    sudo systemctl disable --now claude-display.service || true
    sudo rm -f "$SERVICE"
    sudo systemctl daemon-reload
  fi
  mkdir -p "$(dirname "$AUTOSTART")"
  cat >"$AUTOSTART" <<EOF
[Desktop Entry]
Type=Application
Name=Claude Code usage display
Exec=sh "$REPO/pi/start-desktop.sh"
X-GNOME-Autostart-enabled=true
EOF
  if command -v raspi-config >/dev/null; then
    sudo raspi-config nonint do_boot_behaviour B4  # boot to desktop, logged in
  fi
  NEED_REBOOT=1
fi

PORT="$(conf_get server port)"
say "Done"
echo "The display answers at  http://$(hostname).local:${PORT:-8080}  ($(hostname -I 2>/dev/null | awk '{print $1}'))"
echo "Point your Claude Code hooks or beacon.py at it - see the README."
[ "$MODE" = service ] && echo "Logs: journalctl -u claude-display -f"
if [ "$NEED_REBOOT" = 1 ]; then
  if interactive && ask "Reboot now to finish?" y; then
    sudo reboot
  else
    echo "Reboot to finish: sudo reboot"
  fi
fi
