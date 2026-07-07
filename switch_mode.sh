#!/bin/bash
# switch_mode.sh - full mode switch between PICO-8 and Spotify album art.
#
# Swaps the visible X11 window (via toggle_display.sh) AND arranges for
# xserver-screen to be restarted with mode-appropriate
# --led-pwm-bits/--led-pwm-dither-bits, since those settings can only be
# changed by fully reinitializing the LED matrix hardware driver - there
# is no live API for them the way there is for brightness.
#
# This script does NOT launch xserver-screen itself. It just records the
# desired mode and kills the current renderer; run_led.sh's own respawn
# loop notices the exit, re-reads the mode, and relaunches with the
# right flags. Keeping run_led.sh as the sole thing that ever launches
# xserver-screen avoids both a launch race and the need for this script
# (which runs unprivileged, after the matrix library's own privilege
# drop) to have any sudo rights of its own.
#
# Deliberately stateless about *tracking* mode across calls: the target
# mode is derived directly from whether PICO-8's window is currently
# visible, not read back from a file. An earlier version of this
# feature used a state file for this and ran into permission issues
# across the root -> daemon privilege drop - not worth repeating.
#
# Usage: ./switch_mode.sh {game|spotify|toggle}

cd "$(dirname "$0")" || exit 1
export DISPLAY=:3.0

RENDER_MODE_FILE=/tmp/pico8-led-render-mode

resolve_target() {
  local pico_win
  pico_win=$(xdotool search --name "PICO-8" 2>/dev/null | head -n1)
  if [ -n "$pico_win" ] && xwininfo -id "$pico_win" 2>/dev/null | grep -q "IsViewable"; then
    echo "spotify"
  else
    echo "game"
  fi
}

TARGET="$1"
if [ "$TARGET" != "game" ] && [ "$TARGET" != "spotify" ]; then
  TARGET=$(resolve_target)
fi

# 1. Swap which window is visible.
./toggle_display.sh "$TARGET"

# 2. Record the desired render flags for run_led.sh's loop to pick up
# on its next respawn. Only ever written by this script (which always
# runs as the same unprivileged user), never by anything running as
# root, so there's no cross-user ownership conflict to worry about.
echo "$TARGET" > "$RENDER_MODE_FILE" 2>/dev/null
chmod 666 "$RENDER_MODE_FILE" 2>/dev/null

# 3. Kill the current renderer. It's running as the same unprivileged
# user this script is (the matrix library drops root after hardware
# init), so no special permission is needed to signal it. run_led.sh's
# loop will notice it exited and relaunch it with the new mode's flags.
pkill -f '\./xserver-screen' 2>/dev/null

echo "Switched to $TARGET mode"
