#!/bin/sh
# pico8_supervisor.sh - owns the pico8_64 process lifecycle so it isn't
# just sitting there rendering in the background (and heating up the Pi)
# while its window is unmapped.
#
# Why this exists as a separate loop, run as jared, rather than having
# switch_mode.sh launch/kill pico8_64 directly: switch_mode.sh runs
# inside xserver-screen via system(), and xserver-screen runs as
# whatever user the matrix library dropped root to (daemon) after GPIO
# init - not jared. Launching a GUI process as the wrong user is exactly
# the kind of privilege-drop landmine that's bitten this project before
# (state files under /home/jared/, joystick device permissions, etc.).
#
# Instead of adding a new privileged path, this reuses the bridge that
# already exists: switch_mode.sh writes the desired mode ("game" or
# "spotify") to RENDER_MODE_FILE on every switch, purely so run_led.sh
# (which does run as jared) can pick PWM flags. This loop, also running
# as jared, just watches the same file and starts/stops pico8_64 to
# match. switch_mode.sh and toggle_display.sh need no changes - their
# window map/unmap calls are already idempotent whether or not a PICO-8
# window happens to exist yet.
#
# Run from ~/pico8-led (inherits WorkingDirectory from pico8-led.service,
# or run_splore.sh's own cwd when run by hand).

export DISPLAY=:3.0

RENDER_MODE_FILE=/tmp/pico8-led-render-mode
PICO_PATTERN='pico-8/pico8_64'

launch_pico8() {
  echo "pico8_supervisor: launching pico8_64 (splore)"
  ./pico-8/pico8_64 -windowed 1 -window_x 128 -window_y 128 -width 128 -height 128 -frameless 1 -splore &

  # Same pattern run_splore.sh already uses at boot: poll for the
  # window rather than guessing a fixed sleep, since splore's startup
  # time isn't perfectly consistent.
  for i in $(seq 1 50); do
    pico_win=$(xdotool search --name "PICO-8" 2>/dev/null | head -n1)
    [ -n "$pico_win" ] && break
    sleep 0.2
  done

  # Map + focus it now that it exists. switch_mode.sh already called
  # toggle_display.sh game once (when the switch happened) but at that
  # point there was no pico8 window yet for it to act on, so this call
  # is what actually brings it on screen.
  ./toggle_display.sh game
}

kill_pico8() {
  echo "pico8_supervisor: stopping pico8_64"
  pkill -f "$PICO_PATTERN" 2>/dev/null

  for i in $(seq 1 25); do
    pgrep -f "$PICO_PATTERN" >/dev/null 2>&1 || return 0
    sleep 0.2
  done

  echo "pico8_supervisor: pico8_64 didn't exit cleanly, forcing"
  pkill -9 -f "$PICO_PATTERN" 2>/dev/null
}

# Boots into spotify mode with no pico8_64 running (matches the default
# ./toggle_display.sh spotify call at the end of run_splore.sh).
current="spotify"

while true; do
  desired=$(cat "$RENDER_MODE_FILE" 2>/dev/null || echo "spotify")

  if [ "$desired" != "$current" ]; then
    if [ "$desired" = "game" ]; then
      launch_pico8
    else
      kill_pico8
    fi
    current="$desired"
  fi

  sleep 0.3
done
