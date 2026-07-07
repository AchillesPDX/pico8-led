#!/bin/sh
# run_led.sh - builds and continuously runs xserver-screen, restarting
# it automatically whenever it exits - including when switch_mode.sh
# deliberately kills it to apply new --led-pwm-bits/--led-pwm-dither-bits
# for a mode switch, since those settings require a full reinit and
# can't be changed on a running instance.
#
# This loop is what keeps the overall service alive across those
# restarts. If this script itself exited when xserver-screen did,
# systemd would consider the whole service finished and tear down
# Xvfb/PICO-8/feh along with it, not just the renderer.

make -C rpi-rgb-led-matrix/lib

BRIGHTNESS_FILE=/var/lib/pico8-led/brightness.state

# Hardware topology - fixed for this panel.
PANEL_FLAGS="--led-rows=64 --led-cols=64 --led-chain=2 --led-parallel=2 --led-slowdown-gpio=4"

# Per-mode PWM tuning. Game mode favors refresh rate for smoother PICO-8
# animation; Spotify mode favors color depth/quality for album art.
# Tune these to taste with --led-show-refresh.
GAME_PWM_FLAGS="--led-pwm-bits=7 --led-pwm-dither-bits=1"
SPOTIFY_PWM_FLAGS="--led-pwm-bits=11 --led-pwm-dither-bits=0"

# Current mode is derived directly from X11 (which window is currently
# visible) rather than read from a file. A file-based approach here
# previously broke because it gets written from two different user
# contexts (the controller combo, running as 'daemon' after the matrix
# library's privilege drop, vs. anything run by hand) - not worth
# fighting that permission mismatch again.
detect_mode() {
  local pico_win
  pico_win=$(xdotool search --name "PICO-8" 2>/dev/null | head -n1)
  if [ -n "$pico_win" ] && xwininfo -id "$pico_win" 2>/dev/null | grep -q "IsViewable"; then
    echo "game"
  else
    echo "spotify"
  fi
}

while true; do
  MODE=$(detect_mode)
  if [ "$MODE" = "spotify" ]; then
    PWM_FLAGS="$SPOTIFY_PWM_FLAGS"
  else
    PWM_FLAGS="$GAME_PWM_FLAGS"
  fi

  BRIGHTNESS=$(cat "$BRIGHTNESS_FILE" 2>/dev/null || echo 50)

  echo "Starting xserver-screen (mode=$MODE, brightness=${BRIGHTNESS}%)..."
  sudo ./xserver-screen --update-interval 2778 --led-scan-mode=0 $PANEL_FLAGS $PWM_FLAGS --led-brightness "$BRIGHTNESS"

  echo "xserver-screen exited; respawning in 1s..."
  sleep 1
done
